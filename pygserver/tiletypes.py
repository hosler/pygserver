"""Tile type lookup backed by tiletypes1.dat / tiletypesnw.dat.

Same authoritative 4096-entry tables the C# client and GServer clients ship
(one byte per base tile id). Type values match GServer-v2 LevelTileTypes.h:
WATER=11, LAVA=12, BLOCKING=22, etc.

Two lookups live here:

* `get_tile_type` - the long-standing lenient helper (ids >= 4096 wrap mod
  512, negative ids read 0, always the classic table). Combat/swimming use it.
* `strict_tile_type` - the reference's Server::getTileTypeForTile
  (Server.cpp:1414-1434), used by the GS1 `tiletype`/`onwall` collision path
  (see collision.py): ids >= 4096 are BLOCKING and the table follows the
  level's tileset (classic vs new-format).
"""

import os

WATER = 11
LAVA = 12
THROW_THROUGH = 20
JUMP_STONE = 21
BLOCKING = 22

#: TilesetType (LevelTileTypes.h:16-21).
TILESET_CLASSIC = 0
TILESET_NEWFORMAT = 1
TILESET_TERRAIN = 5

#: Tile types Level::isOnWall treats as a wall (Level.cpp:2872-2884).
WALL_TYPES = (THROW_THROUGH, JUMP_STONE, BLOCKING)

_DAT_PATH = os.path.join(os.path.dirname(__file__), "tiletypes1.dat")
_NW_DAT_PATH = os.path.join(os.path.dirname(__file__), "tiletypesnw.dat")


def _load_table(path: str) -> bytes:
    """Load a 4096-entry table; all-walkable fallback so imports never fail."""
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) >= 4096:
            return data[:4096]
    except OSError:
        pass
    return bytes(4096)


def _load_tile_types() -> bytes:
    return _load_table(_DAT_PATH)


TILE_TYPES = _load_tile_types()
#: New-format (tileset type 1) table, extracted from the official client
#: (see pyreborn/tiletypes.py for its provenance).
TILE_TYPES_NW = _load_table(_NW_DAT_PATH)


def get_tile_type(tile_id: int) -> int:
    if tile_id < 0:
        return 0
    if tile_id >= 4096:
        tile_id %= 512
    return TILE_TYPES[tile_id]


def strict_tile_type(tile_id: int, tileset: int = TILESET_CLASSIC) -> int:
    """Reference Server::getTileTypeForTile (Server.cpp:1414-1434).

    TERRAIN levels have no collision at all; a tile id outside the 4096-entry
    table (the reference's uint16 `tile >= 4096`) is BLOCKING; classic and
    new-format read their own table; any other tileset value is BLOCKING.
    """
    if tileset == TILESET_TERRAIN:
        return 0
    tile_id &= 0xFFFF
    if tile_id >= 4096:
        return BLOCKING
    if tileset == TILESET_CLASSIC:
        return TILE_TYPES[tile_id]
    if tileset == TILESET_NEWFORMAT:
        return TILE_TYPES_NW[tile_id]
    return BLOCKING
