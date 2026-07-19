"""Tile type lookup backed by tiletypes1.dat.

Same authoritative 4096-entry table the C# client and GServer clients ship
(one byte per base tile id). Type values match GServer-v2 LevelTileTypes.h:
WATER=11, LAVA=12, BLOCKING=22, etc.
"""

import os

WATER = 11
LAVA = 12

_DAT_PATH = os.path.join(os.path.dirname(__file__), "tiletypes1.dat")


def _load_tile_types() -> bytes:
    """Load the 4096-entry table; all-walkable fallback so imports never fail."""
    try:
        with open(_DAT_PATH, "rb") as f:
            data = f.read()
        if len(data) >= 4096:
            return data[:4096]
    except OSError:
        pass
    return bytes(4096)


TILE_TYPES = _load_tile_types()


def get_tile_type(tile_id: int) -> int:
    if tile_id < 0:
        return 0
    if tile_id >= 4096:
        tile_id %= 512
    return TILE_TYPES[tile_id]
