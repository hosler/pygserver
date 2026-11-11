"""Server-side collision foundation: tile walls, bounding boxes, NPC/player tests.

This is the one place the GS1 host (and, next, the NPC move queue) look to ask
"is this spot blocked?". Every function is pure over a Level/NPC/Player and an
optional `server` (config + world for gmap neighbours); nothing here mutates.

Units. Tiles are float level-local tiles (what npc.x/npc.y/absolute_position
hold). Pixels are int level-local pixels (tile * 16). Rects are `(x, y, w, h)`
int tuples in pixels, or None for "no box" (the reference's min-int sentinel).
The z axis is ignored: pygserver has no NPC/player z collision. A gmap is
addressed level-relative, like GS1 `tiles[x,y]`: a coordinate outside 0..63
reaches the neighbouring segment of the script level's gmap in any direction,
from any segment (a missing segment or a coordinate off a single level is
BLOCKING). The reference addresses a gmap in whole-map coordinates; pygserver
scripts hold level-local coordinates, so each probe is shifted by the level's
gmap offset, converted exactly as the reference does, and shifted back.

API (reference in brackets):

  Conversions
    fix_epsilon(v) -> float                  Extents.h:43-47, float32
    tiles_to_pixels(v) -> int                toPixelPosition(TilePosition)
    pixel_to_tile(px, origin=0) -> int       toWholeTilePosition(pixel)
    tile_of(v, origin=0) -> int              toWholeTilePosition(TilePosition)
  Tiles
    tileset_type_for_level(config, level) -> int      Server::getTilesetTypeForLevel
    TileProbe(server, level)                 one lookup session (gmap + segments resolved once)
    tile_type_at(server, level, tx, ty, tileset=None) -> int   Level::getTileTypeAt
    tile_type_at_script_tile(server, level, x, y) -> int       GS1 tiletype()
    is_on_wall(server, level, tx, ty) -> bool         Level::isOnWall(tile)
    is_on_wall_pixel(server, level, px, py) -> bool   Level::isOnWall(pixel)
    is_on_wall_rect(server, level, px, py, w, h) -> bool   Level::isOnWall2
    is_on_water_pixel / is_on_water_rect              Level::isOnWater / isOnWater2
  Boxes
    npc_collision_rect(npc) -> rect|None     NPC::getCollisionBoundingBox
    npc_event_rect(npc) -> rect|None         NPC::getEventBoundingBox
    player_collision_rect(player) -> rect    Player::getCollisionBoundingBox
    rect_contains_point(rect, px, py)        positionInRectangle (inclusive)
    rects_intersect(a, b)                    rectanglesIntersect (inclusive)
  Objects
    find_npc_at(level, point=|rect=, initiator=None) -> int      Level::testNPC
    blocked_by_objects(level, owner, players, point=|rect=) -> bool   fn_onwall tail
"""
from __future__ import annotations

import math
import struct
from fnmatch import fnmatchcase
from typing import Iterable, Optional, Tuple

from . import tiletypes
from .attachments import absolute_position
from .protocol.constants import NPCBLOCKFLAG

Rect = Tuple[int, int, int, int]

PIXELS_PER_TILE = 16
FLT_EPSILON = 1.1920928955078125e-07
_INT32_MAX = 2 ** 31 - 1
CHARACTER_IMAGE = "#c#"  # NPC::isCharacter, NPC.h:499-502


# -- conversions ---------------------------------------------------------------

def _f32(value: float) -> float:
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def fix_epsilon(value: float) -> float:
    """Nudge `value` one float32 epsilon away from zero (Extents.h:43-47).

    Only survives the float32 addition for |value| < 2, which is exactly the
    small-delta case the reference added it for (-0.25 tiles -> -4 px)."""
    v = _f32(float(value))
    return _f32(v - FLT_EPSILON) if v < 0 else _f32(v + FLT_EPSILON)


def _trunc32(value: float) -> int:
    if not math.isfinite(value):
        return 0
    return max(-_INT32_MAX - 1, min(_INT32_MAX, math.trunc(value)))


def tiles_to_pixels(value: float) -> int:
    """toPixelPosition(TilePosition): int(fixEpsilon(v) * 16) in float32."""
    return _trunc32(_f32(fix_epsilon(value) * PIXELS_PER_TILE))


def _c_div(value: int, divisor: int) -> int:
    """C integer division: truncates toward zero."""
    quotient = abs(int(value)) // divisor
    return quotient if value >= 0 else -quotient


def pixel_to_tile(pixel: int, origin: int = 0) -> int:
    """toWholeTilePosition(PixelPosition): C division by 16, truncating toward
    zero, applied to the GLOBAL pixel `pixel + origin`. `origin` is the level's
    gmap offset in pixels (0 off a gmap); the result is relative to the level.

    Truncating in the global frame is what makes a probe at -8 px from an
    interior gmap segment land in column 63 of the left neighbour, exactly like
    -16 px, while a probe up to 15 px past the whole map's top/left edge still
    reads tile 0, as the reference does."""
    return _c_div(int(pixel) + int(origin), 16) - int(origin) // 16


def tile_of(value: float, origin: int = 0) -> int:
    """toWholeTilePosition(TilePosition): trunc(fixEpsilon(v)) in the global
    frame (`origin` is the level's gmap offset in tiles; see pixel_to_tile)."""
    return _trunc32(fix_epsilon(_as_number(value) + origin)) - int(origin)


# -- tiles ------------------------------------------------------------------------

def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def tileset_type_for_level(config, level) -> int:
    """Server::getTilesetTypeForLevel (Server.cpp:1384-1405).

    Options `newtilesetlevels` (name patterns or prefixes) then `newtilesets`
    (everything) select the new-format table; otherwise classic. The
    reference's terrain-level case is not modelled (pygserver loads no
    terrain levels)."""
    if level is None:
        return tiletypes.TILESET_CLASSIC
    name = getattr(level, "name", "") or ""
    for pattern in getattr(config, "newtilesetlevels", None) or ():
        if fnmatchcase(name, pattern) or name.startswith(pattern):
            return tiletypes.TILESET_NEWFORMAT
    if getattr(config, "newtilesets", False) is True:
        return tiletypes.TILESET_NEWFORMAT
    return tiletypes.TILESET_CLASSIC


class TileProbe:
    """One lookup session over a level: the gmap and every neighbour segment
    are resolved at most once, however many cells are read.

    Mirrors Level::getTileTypeAt (Level.cpp:3083-3101): a tile off the whole
    map or in a missing segment is BLOCKING, then Server::getTileTypeForTile
    (ids >= 4096 are BLOCKING). Tile coordinates are relative to `level`
    (negative or >= 64 reaches the neighbouring segments of its gmap, in any
    direction); `origin_tiles` / `origin_px` are the level's own offset in the
    gmap, for converting a script coordinate to a whole-map tile."""

    def __init__(self, server, level, tileset: Optional[int] = None):
        self.level = level
        self.tileset = tileset
        self.world = getattr(server, "world", None)
        self.gmap = None
        self.gx = self.gy = 0
        if level is not None:
            if tileset is None:
                self.tileset = tileset_type_for_level(
                    getattr(server, "config", None), level)
            world = self.world
            info = (world.get_gmap_for_level(getattr(level, "name", ""))
                    if world is not None and hasattr(world, "get_gmap_for_level")
                    else None)
            if info is not None:
                self.gmap, self.gx, self.gy = info
        self.origin_tiles = (self.gx * 64, self.gy * 64)
        self.origin_px = (self.gx * 1024, self.gy * 1024)
        self._segments = {}

    def _segment(self, seg_x: int, seg_y: int):
        key = (seg_x, seg_y)
        if key not in self._segments:
            name = self.gmap.get_level_at(self.gx + seg_x, self.gy + seg_y)
            self._segments[key] = self.world.get_level(name) if name else None
        return self._segments[key]

    def tile_type(self, tile_x: int, tile_y: int) -> int:
        if self.level is None:
            return tiletypes.BLOCKING
        tile_x, tile_y = int(tile_x), int(tile_y)
        if 0 <= tile_x < 64 and 0 <= tile_y < 64:
            return self.level.tile_type_at(tile_x, tile_y, self.tileset)
        if self.gmap is None:
            return tiletypes.BLOCKING
        segment = self._segment(tile_x // 64, tile_y // 64)
        if segment is None:
            return tiletypes.BLOCKING
        return segment.tile_type_at(tile_x % 64, tile_y % 64, self.tileset)

    def type_at_pixel(self, px: int, py: int) -> int:
        return self.tile_type(pixel_to_tile(px, self.origin_px[0]),
                              pixel_to_tile(py, self.origin_px[1]))

    def any_in_rect(self, px: int, py: int, width: int, height: int, wanted) -> bool:
        """Level::isOnWall2 / isOnWater2 scan: `max(1, size)` tiles per axis."""
        tx, ty, tiles_w, tiles_h = _tile_area(px, py, width, height, self.origin_px)
        for cy in range(ty, ty + max(1, tiles_h)):
            for cx in range(tx, tx + max(1, tiles_w)):
                if self.tile_type(cx, cy) in wanted:
                    return True
        return False


def tile_type_at(server, level, tile_x: int, tile_y: int,
                 tileset: Optional[int] = None) -> int:
    """Tile type at whole-tile (tile_x, tile_y) relative to `level`, reaching
    gmap neighbours (see TileProbe). No level at all is BLOCKING."""
    return TileProbe(server, level, tileset).tile_type(tile_x, tile_y)


def is_on_wall(server, level, tile_x: int, tile_y: int) -> bool:
    return tile_type_at(server, level, tile_x, tile_y) in tiletypes.WALL_TYPES


def _tile_area(px: int, py: int, width: int, height: int, origin_px=(0, 0)):
    """toWholeTileRectangleArea(PixelRectangleArea) (Extents.h:775-783): the
    whole tiles the rect touches, computed on the global pixel position and
    returned level-relative; sizes are uint8 (wrap at 256)."""
    gx, gy = px + origin_px[0], py + origin_px[1]
    tx, ty = _c_div(gx, 16), _c_div(gy, 16)
    tiles_w = _c_div(16 + (gx - tx * 16) + width - 1, 16) & 0xFF
    tiles_h = _c_div(16 + (gy - ty * 16) + height - 1, 16) & 0xFF
    return tx - origin_px[0] // 16, ty - origin_px[1] // 16, tiles_w, tiles_h


def is_on_wall_pixel(server, level, px: int, py: int) -> bool:
    return TileProbe(server, level).type_at_pixel(px, py) in tiletypes.WALL_TYPES


def is_on_wall_rect(server, level, px: int, py: int, width: int, height: int) -> bool:
    return TileProbe(server, level).any_in_rect(
        px, py, width, height, tiletypes.WALL_TYPES)


def is_on_water_pixel(server, level, px: int, py: int) -> bool:
    # Level::isOnWater is type WATER only, not lava (Level.cpp:2908-2912).
    return TileProbe(server, level).type_at_pixel(px, py) == tiletypes.WATER


def is_on_water_rect(server, level, px: int, py: int, width: int, height: int) -> bool:
    return TileProbe(server, level).any_in_rect(
        px, py, width, height, (tiletypes.WATER,))


def tile_type_at_script_tile(server, level, x: float, y: float) -> int:
    """tiletype(x, y): script tile coordinates through toWholeTilePosition."""
    probe = TileProbe(server, level)
    return probe.tile_type(tile_of(x, probe.origin_tiles[0]),
                           tile_of(y, probe.origin_tiles[1]))


# -- bounding boxes -------------------------------------------------------------

def npc_is_character(npc) -> bool:
    return getattr(npc, "image", "") == CHARACTER_IMAGE


def _npc_box(npc) -> Rect:
    x = tiles_to_pixels(_as_number(getattr(npc, "x", 0)))
    y = tiles_to_pixels(_as_number(getattr(npc, "y", 0)))
    if npc_is_character(npc):
        # A 2x2 square centred on the feet, whatever setshape said
        # (NPC.h:580-581, 56ac0f20).
        return (x + 8, y + 16, 32, 32)
    # npc.shape is exact pixels, as in the reference (see _c_setshape).
    shape = getattr(npc, "shape", None)
    if shape and len(shape) >= 2:
        return (x, y, int(shape[0]), int(shape[1]))
    return (x, y, 0, 0)


def npc_collision_rect(npc) -> Optional[Rect]:
    """NPC::getCollisionBoundingBox (NPC.h:572-584): None when the NPC is
    invisible or has the NOBLOCK flag."""
    if not getattr(npc, "visible", True):
        return None
    if int(getattr(npc, "block_flags", 0) or 0) & NPCBLOCKFLAG.NOBLOCK:
        return None
    return _npc_box(npc)


def npc_event_rect(npc) -> Optional[Rect]:
    """NPC::getEventBoundingBox (NPC.h:586-598): like the collision box but
    NOBLOCK does not remove it; only an invisible NPC gets no events."""
    if not getattr(npc, "visible", True):
        return None
    return _npc_box(npc)


def npc_bounding_rect(npc) -> Rect:
    """NPC::getBoundingBox (NPC.h:567): position + computed shape, a
    character being 3x3 tiles; visible or not, blocking or not."""
    x = tiles_to_pixels(_as_number(getattr(npc, "x", 0)))
    y = tiles_to_pixels(_as_number(getattr(npc, "y", 0)))
    if npc_is_character(npc):
        return (x, y, 48, 48)
    shape = getattr(npc, "shape", None)
    if shape and len(shape) >= 2:
        return (x, y, int(shape[0]), int(shape[1]))
    return (x, y, 0, 0)


# Corner offsets in pixels from a player's top-left, two per facing direction
# (PlayerProps.cpp:383-386, 98f18f46): the rectangle between them reaches about
# 1.5 tiles from the player, with half a tile of tolerance.
_SWORD_HIT_X = (16, 48, -24, 8, 16, 48, 48, 72)
_SWORD_HIT_Y = (-16, 8, 16, 48, 64, 80, 16, 48)


def sword_hit_rect(px: int, py: int, direction: int) -> Rect:
    """The serverside washit rectangle for a player at pixel (px, py)."""
    i = (int(direction) % 4) * 2
    x1, x2 = px + _SWORD_HIT_X[i], px + _SWORD_HIT_X[i + 1]
    y1, y2 = py + _SWORD_HIT_Y[i], py + _SWORD_HIT_Y[i + 1]
    return (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))


def player_collision_rect(player) -> Rect:
    """Player::getCollisionBoundingBox (Player.h:608-616): the 32x32 square at
    the sprite origin + (8, 16). The reference's no-PK-level exemption is not
    modelled (pygserver has no no-PK zones)."""
    px, py = absolute_position(player)
    return (tiles_to_pixels(_as_number(px)) + 8,
            tiles_to_pixels(_as_number(py)) + 16, 32, 32)


def rect_contains_point(rect: Optional[Rect], px: int, py: int) -> bool:
    """positionInRectangle (Extents.h:329-336): every edge inclusive."""
    if rect is None:
        return False
    x, y, w, h = rect
    return x <= px <= x + w and y <= py <= y + h


def rects_intersect(first: Optional[Rect], second: Optional[Rect]) -> bool:
    """rectanglesIntersect (Extents.h:339-350): touching edges intersect."""
    if first is None or second is None:
        return False
    fx, fy, fw, fh = first
    sx, sy, sw, sh = second
    return not (fx + fw < sx or sx + sw < fx or fy + fh < sy or sy + sh < fy)


# -- objects --------------------------------------------------------------------

def _level_npcs(level) -> list:
    if level is None:
        return []
    if hasattr(level, "get_npcs"):
        return list(level.get_npcs())
    direct = getattr(level, "npcs", getattr(level, "_npcs", []))
    return list(direct.values()) if isinstance(direct, dict) else list(direct)


def _hit(rect, point, area) -> bool:
    if point is not None:
        return rect_contains_point(rect, point[0], point[1])
    return rects_intersect(area, rect)


def find_npc_at(level, *, point: Optional[Tuple[int, int]] = None,
             rect: Optional[Rect] = None, initiator=None) -> int:
    """Level::testNPC (Level.cpp:3003-3045): index (in level NPC order) of the
    first NPC whose collision box holds `point` / meets `rect`, -2 for none.

    Deliberate divergence: the reference stops at its first hit and returns -1
    when that NPC is the initiator, so an overlapping other NPC listed after
    the initiator goes unseen. Here the initiator is skipped and the scan
    continues; -1 is returned only when the initiator was the sole hit."""
    initiator_hit = False
    for index, npc in enumerate(_level_npcs(level)):
        if not _hit(npc_collision_rect(npc), point, rect):
            continue
        if initiator is not None and npc is initiator:
            initiator_hit = True
            continue
        return index
    return -1 if initiator_hit else -2


def blocked_by_objects(level, owner, players: Iterable, *,
                       point: Optional[Tuple[int, int]] = None,
                       rect: Optional[Rect] = None) -> bool:
    """The object half of fn_onwall/fn_onwall2 (GS1Functions.cpp:1116-1188):
    a player (unless the owner NPC ran `noplayeronwall`) or another NPC."""
    if not getattr(owner, "no_player_on_wall", False):
        for player in players:
            if _hit(player_collision_rect(player), point, rect):
                return True
    return find_npc_at(level, point=point, rect=rect, initiator=owner) >= 0
