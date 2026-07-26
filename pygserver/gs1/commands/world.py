from __future__ import annotations

import logging

from reborn_protocol.coords import LEVEL_SIZE, level_index
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
    try:
        from ...protocol.constants import LevelItemType
        item_type = LevelItemType(int(to_num(code)))
    except Exception:
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

def _c_updateboard(self, a, npc, player, ctx):
    # updateboard x,y,width,height — re-broadcast a region of the level board
    if len(a) < 4 or self.server is None:
        return
    lvl = self._level_of(ctx)
    if lvl is None or not hasattr(lvl, "_tiles"):
        return
    x = max(0, int(to_num(a[0])))
    y = max(0, int(to_num(a[1])))
    w = max(0, min(LEVEL_SIZE - x, int(to_num(a[2]))))
    h = max(0, min(LEVEL_SIZE - y, int(to_num(a[3]))))
    if w == 0 or h == 0:
        return
    tiles = bytearray()
    for row in range(y, y + h):
        start = level_index(x, row) * 2
        tiles += bytes(lvl._tiles[start:start + w * 2])
    try:
        from ...protocol.packets import build_board_modify, build_board_modify2
        world = getattr(self.server, "world", None)
        gmap_info = (
            world.get_gmap_for_level(lvl.name)
            if world and hasattr(world, "get_gmap_for_level")
            else None
        )
        if gmap_info:
            _, map_x, map_y = gmap_info
            packet = build_board_modify2(
                map_x, map_y, x, y, w, h, bytes(tiles)
            )
        else:
            packet = build_board_modify(x, y, w, h, bytes(tiles))
        _schedule(self.server.broadcast_to_level(
            lvl.name, packet))
    except Exception:
        logger.debug("updateboard failed", exc_info=True)

_COMMANDS = {
    'lay': _c_lay,
    'lay2': _c_lay2,
    'take': _c_take,
    'toweapons': _c_toweapons,
    'updateboard': _c_updateboard,
    'updateboard2': _c_updateboard,
}
