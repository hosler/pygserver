"""Whole-map GS1 coordinates on a gmap.

The reference server loads a gmap as ONE Level that covers the whole map
(Level::sizeInTiles, include/level/Level.h:461-472), so every GS1 position on
it is a whole-map tile: an NPC's or a player's `x`/`y` is
`mapX * 64 + localPixel / 16` (Character::getTilePosition,
include/object/Character.h:65-68), and a command argument names whichever
segment that whole-map tile falls in.

pygserver keeps each gmap segment its own Level with local 0..63 tiles, and
that internal state and the wire stay as they are. The GS1 host converts at
its boundary, and only there:

* a position a script READS gets the origin of the segment the object is in
  added (`to_map`);
* a position a script WRITES, or a coordinate argument of a query (onwall,
  testnpc, distances), is made relative to a known level by subtracting that
  level's origin (`to_level`); the result may lie outside 0..63, which the
  collision and NPC-roll code already treat as the neighbouring segment;
* a coordinate that PLACES something (putnpc, putbomb, lay2, ...) picks the
  segment the whole-map tile falls in (`segment_at`).

Off a gmap the origin is (0, 0) and every function is the identity. Python NPC
scripts never come through here: their `npc.x`/`npc.y` stay segment-local.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

from reborn_protocol.coords import LEVEL_SIZE


def gmap_cell(server, level):
    """(gmap, grid_x, grid_y) of `level`, or None off a gmap."""
    world = getattr(server, "world", None) if server is not None else None
    if level is None or world is None or not hasattr(world, "get_gmap_for_level"):
        return None
    return world.get_gmap_for_level(getattr(level, "name", "") or "")


def origin(server, level) -> Tuple[int, int]:
    """The whole-map tile of `level`'s top-left corner; (0, 0) off a gmap."""
    info = gmap_cell(server, level)
    if info is None:
        return 0, 0
    return info[1] * LEVEL_SIZE, info[2] * LEVEL_SIZE


def to_map(server, level, x, y):
    """Level-local (x, y) of an object in `level` -> whole-map tiles."""
    ox, oy = origin(server, level)
    return x + ox, y + oy


def to_map_axis(server, level, axis: str, value):
    """`to_map` for one axis ('x' or 'y')."""
    ox, oy = origin(server, level)
    return value + (oy if axis == "y" else ox)


def to_level(server, level, x, y):
    """Whole-map (x, y) -> tiles relative to `level` (may leave 0..63)."""
    ox, oy = origin(server, level)
    return x - ox, y - oy


def to_level_axis(server, level, axis: str, value):
    """`to_level` for one axis ('x' or 'y')."""
    ox, oy = origin(server, level)
    return value - (oy if axis == "y" else ox)


def _segment(server, gmap, cell_x: int, cell_y: int):
    if not (0 <= cell_x < int(getattr(gmap, "width", 0) or 0)
            and 0 <= cell_y < int(getattr(gmap, "height", 0) or 0)):
        return None
    name = gmap.get_level_at(cell_x, cell_y)
    world = getattr(server, "world", None)
    return world.get_level(name) if name and world is not None else None


def segment_at(server, level, x: float, y: float):
    """The (segment, local x, local y) a whole-map position names on
    `level`'s gmap, or None when no segment is there (off the map or a hole).
    Off a gmap: (level, x, y) unchanged.

    The cell is the global position / 64 tiles, C division toward zero
    (toMapPosition, include/utilities/Extents.h:559-562), and the local
    position the rest, so a small negative coordinate stays in cell 0 with a
    negative local position, as in the reference."""
    info = gmap_cell(server, level)
    if info is None:
        return level, x, y
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    cell_x, cell_y = math.trunc(x / LEVEL_SIZE), math.trunc(y / LEVEL_SIZE)
    segment = _segment(server, info[0], cell_x, cell_y)
    if segment is None:
        return None
    return segment, x - cell_x * LEVEL_SIZE, y - cell_y * LEVEL_SIZE


def map_size_tiles(server, level) -> Optional[Tuple[int, int]]:
    """(width, height) of `level`'s gmap in tiles, or None off a gmap."""
    info = gmap_cell(server, level)
    if info is None:
        return None
    gmap = info[0]
    return (int(getattr(gmap, "width", 0) or 0) * LEVEL_SIZE,
            int(getattr(gmap, "height", 0) or 0) * LEVEL_SIZE)


def map_tile(server, level, tile_x: int, tile_y: int):
    """tiles[x,y] on `level`'s gmap: the (segment, local x, local y) of the
    whole-map tile, or None for a missing segment (GS1Variables.cpp:389-444:
    wrap by the map size, clamp, Level::getMapTileAtPosition picks the
    segment, src/level/Level.cpp:3121-3153). None too when `level` is not
    on a gmap."""
    size = map_size_tiles(server, level)
    if size is None or size[0] <= 0 or size[1] <= 0:
        return None
    width, height = size
    tile_x = max(0, min(width - 1, int(tile_x) % width))
    tile_y = max(0, min(height - 1, int(tile_y) % height))
    gmap = gmap_cell(server, level)[0]
    segment = _segment(server, gmap, tile_x // LEVEL_SIZE, tile_y // LEVEL_SIZE)
    if segment is None:
        return None
    return segment, tile_x % LEVEL_SIZE, tile_y % LEVEL_SIZE
