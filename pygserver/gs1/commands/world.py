from __future__ import annotations

import logging

from reborn_protocol.coords import LEVEL_SIZE
from reborn_protocol.gs1.values import to_num, to_str

from ..execution import _schedule
from ...audience import GS1_EXPLOSION_PLAYERS, contains  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..host import NPC_CHARPROP, PLAYER_CHARPROP, PLAYER_PROP_WIRE, PLPROP, _NPC_GATTRIB_PROPS, _PLAYER_GATTRIB_PROPS, _charprop_target  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import asyncio  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import math  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import time  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

def _spawn_item(self, ctx, code, x, y):
    im = getattr(self.server, "item_manager", None) if self.server is not None else None
    lvl = self._level_of(ctx)
    if im is None or lvl is None or not hasattr(im, "spawn_item"):
        return
    from ...protocol.constants import LevelItemType
    # scripts say `lay bombs` — the arg is the item NAME; numeric form kept
    # for scripts that pass an index
    try:
        item_type = LevelItemType[str(code).strip().upper()]
    except KeyError:
        # strict numeric parse only — the lenient to_num turns every
        # unknown word into 0 = greenrupee, which was the original bug
        try:
            item_type = LevelItemType(int(float(str(code).strip())))
        except (ValueError, TypeError):
            return
    _schedule(im.spawn_item(lvl, to_num(x), to_num(y), item_type))

def _c_lay(self, a, npc, player, ctx):
    # lay itemname — drop an item at the NPC's position
    if npc is None or not a:
        return
    _spawn_item(self, ctx, a[0], getattr(npc, "x", 0), getattr(npc, "y", 0))

def _c_lay2(self, a, npc, player, ctx):
    # lay2 itemname,x,y — drop an item at an exact position
    if len(a) >= 3:
        _spawn_item(self, ctx, a[0], a[1], a[2])

def _c_take(self, a, npc, player, ctx):
    # take itemname — remove matching items within ~10 tiles of the NPC
    im = getattr(self.server, "item_manager", None) if self.server is not None else None
    lvl = self._level_of(ctx)
    if im is None or lvl is None or npc is None or not hasattr(im, "get_items_on_level"):
        return
    try:
        from ...protocol.constants import LevelItemType
        want = LevelItemType(int(to_num(a[0]))) if a else None
    except Exception:
        return
    nx, ny = to_num(getattr(npc, "x", 0)), to_num(getattr(npc, "y", 0))
    for it in im.get_items_on_level(lvl.name):
        if (want is None or it.item_type == want) and abs(it.x - nx) <= 10 and abs(it.y - ny) <= 10:
            _schedule(im.remove_item(lvl.name, it.x, it.y))

def _c_toweapons(self, a, npc, player, ctx):
    # toweapons name — turn this NPC into a weapon and give it to the player
    if player is None or npc is None or not a:
        return
    name = to_str(a[0])
    gs2 = getattr(self.server, "gs2_manager", None)
    if gs2 is None:
        return
    gs2.upsert_classic_weapon(
        name,
        to_str(getattr(npc, "image", "")),
        to_str(getattr(npc, "gs1_source", "")),
    )
    if hasattr(player, "add_weapon"):
        player.add_weapon(name)
    try:
        _schedule(gs2.announce_weapon(player, name))
    except Exception:
        logger.debug("toweapons send failed for %s", name, exc_info=True)

def _updateboard(self, a, ctx, permanent):
    # updateboard x,y,w,h (GS1Commands.cpp:3712-3727): exactly four
    # arguments, each at least 0, then Level::updateBoard, which publishes
    # the script tile layer for the rectangle as a board change that reverts
    # after `respawntime` (Level.cpp:1847-1850, :1751-1799). updateboard2
    # (:3730-3745, Level.cpp:1852-1875) is the same unless `savelevels` is on.
    if len(a) != 4 or self.server is None:
        return
    lvl = self._level_of(ctx)
    if lvl is None or not hasattr(lvl, "_tiles"):
        return
    x = max(0, int(to_num(a[0])))
    y = max(0, int(to_num(a[1])))
    w = max(0, int(to_num(a[2])))
    h = max(0, int(to_num(a[3])))
    config = getattr(self.server, "config", None)
    keep = bool(permanent and getattr(config, "savelevels", False))
    for part, px, py, pw, ph in _board_parts(self.server, lvl, x, y, w, h):
        tiles = part.script_tiles_rect(px, py, pw, ph)
        self._publish_board_area(part, px, py, pw, ph, tiles, allow_respawn=not keep)
        if keep and getattr(config, "levelsautosave", False):
            # saveBoardChangeFromScriptTiles + saveLevel (Level.cpp:1801-1845,
            # :1868-1873): the written tiles become the level's own, then it
            # is saved. A level with no script layer has nothing to take.
            for dy in range(ph):
                for dx in range(pw):
                    if part._script_tiles is not None:
                        part.set_tile(px + dx, py + dy, tiles[dy * pw + dx])
            if part.file_path:
                try:
                    part.save_to_file()
                except OSError:
                    logger.exception("updateboard2 could not save %s", part.name)


def _board_parts(server, lvl, x, y, w, h):
    """The (level, x, y, w, h) pieces an `updateboard` rectangle covers, each
    in its own segment's tile coordinates. The reference walks every sub-level
    the rectangle touches (Level::applyBoardChangeFromScriptTiles,
    Level.cpp:1751-1799, getSubLevelsInRectangle :3170-3182) and alterBoard
    splits the change per part (:1698-1738); a part that does not exist is
    skipped (:1707-1709).

    Frame: the reference's gmap is one Level covering the whole map, so the
    rectangle is in whole-map tile coordinates. pygserver's `tiles[]` (the
    script layer this reads) addresses the script's own segment and hops to
    a neighbour past its edge (GS1Host._resolve_tile), so the rectangle is
    taken the same way, from the script's own segment. The two frames agree
    for the map's top-left segment. A level outside any gmap keeps the
    one-segment clamp."""
    world = getattr(server, "world", None)
    info = (world.get_gmap_for_level(lvl.name)
            if world is not None and hasattr(world, "get_gmap_for_level") else None)
    if info is None:
        w = min(LEVEL_SIZE - x, w)
        h = min(LEVEL_SIZE - y, h)
        return [(lvl, x, y, w, h)] if w > 0 and h > 0 and x < LEVEL_SIZE and y < LEVEL_SIZE else []
    if w == 0 or h == 0:
        return []
    gmap, gx, gy = info
    parts = []
    for sy in range(y // LEVEL_SIZE, (y + h - 1) // LEVEL_SIZE + 1):
        for sx in range(x // LEVEL_SIZE, (x + w - 1) // LEVEL_SIZE + 1):
            name = gmap.get_level_at(gx + sx, gy + sy)
            part = world.get_level(name) if name else None
            if part is None or not hasattr(part, "_tiles"):
                continue
            left, top = max(x, sx * LEVEL_SIZE), max(y, sy * LEVEL_SIZE)
            right = min(x + w, (sx + 1) * LEVEL_SIZE)
            bottom = min(y + h, (sy + 1) * LEVEL_SIZE)
            parts.append((part, left - sx * LEVEL_SIZE, top - sy * LEVEL_SIZE,
                          right - left, bottom - top))
    return parts

def _c_updateboard(self, a, npc, player, ctx):
    _updateboard(self, a, ctx, permanent=False)

def _c_updateboard2(self, a, npc, player, ctx):
    _updateboard(self, a, ctx, permanent=True)

_COMMANDS = {
    'lay': _c_lay,
    'lay2': _c_lay2,
    'take': _c_take,
    'toweapons': _c_toweapons,
    'updateboard': _c_updateboard,
    'updateboard2': _c_updateboard2,
}
