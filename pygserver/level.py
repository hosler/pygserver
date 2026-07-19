"""
pygserver.level - Level/map management

Handles loading, storing, and managing level data including
tiles, NPCs, items, signs, and links.
"""

import logging
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple
from pathlib import Path

if TYPE_CHECKING:
    from .player import Player
    from .npc import NPC

logger = logging.getLogger(__name__)


# Item name -> id, mirrors GServer-v2 LevelItem::ItemNames (index == LevelItemType).
from .protocol.constants import LevelItemType as _LevelItemType
ITEM_NAME_TO_ID: Dict[str, int] = {
    t.name.lower(): int(t) for t in _LevelItemType if int(t) >= 0
}

# Baddy name -> type id, mirrors GServer-v2 LevelBaddy::BaddyNames. "spider"
# is a name-only alias for octopus (upstream 580c4888: BaddyType has no
# separate SPIDER value, "if using, spider needs to be manually mapped to
# octopus" - LevelBaddy.h), not a new baddy type.
BADDY_NAME_TO_TYPE: Dict[str, int] = {
    "graysoldier": 0, "bluesoldier": 1, "redsoldier": 2, "shootingsoldier": 3,
    "swampsoldier": 4, "frog": 5, "octopus": 6, "goldenwarrior": 7,
    "lizardon": 8, "dragon": 9, "spider": 6,
}


class Level:
    """
    Represents a game level (64x64 tiles).

    Handles tile data, player/NPC tracking, and level objects.
    """

    # Level dimensions
    WIDTH = 64
    HEIGHT = 64
    TILE_COUNT = WIDTH * HEIGHT  # 4096 tiles
    BOARD_SIZE = TILE_COUNT * 2  # 8192 bytes (2 bytes per tile)

    def __init__(self, name: str):
        self.name = name
        self.file_path: Optional[str] = None

        # Tile data (64x64 = 4096 tiles, 2 bytes each)
        self._tiles = bytearray(self.BOARD_SIZE)

        # Players on this level, insertion-ordered (dict[id, None] as an
        # ordered set) so the first key is always the current level leader -
        # GServer-v2's Level::getPlayers() is a std::vector, and
        # isPlayerLeader()/PLO_ISLEADER rely on "first to join, still
        # present" (see add_player/remove_player/is_player_leader below). A
        # plain Set had no join order, so leader assignment could only ever
        # guess at "lowest id currently on the level" (see the now-stale
        # caveat this replaces in gs1_host.leader_player_for_level).
        self._players: Dict[int, None] = {}

        # NPCs on this level
        self._npcs: Dict[int, 'NPC'] = {}

        # Level links (warps)
        self._links: List[Dict] = []

        # Signs
        self._signs: Dict[Tuple[int, int], str] = {}

        # Items on ground
        self._items: List[Dict] = []

        # Chests (parsed from file: {x, y, item, sign})
        self._chests: List[Dict] = []

        # Baddies (parsed from file: {x, y, type, verses})
        self._baddies: List[Dict] = []

        # NPC definitions (parsed from file: {x, y, image, code})
        self._npc_defs: List[Dict] = []

    @classmethod
    def load(cls, file_path: str) -> 'Level':
        """
        Load a level from a .nw file.

        Args:
            file_path: Path to the .nw level file

        Returns:
            Loaded Level instance
        """
        path = Path(file_path)
        name = path.stem + path.suffix  # Keep extension in name

        level = cls(name)
        level.file_path = file_path

        try:
            with open(file_path, 'rb') as f:
                data = f.read()

            level._parse_nw_file(data)
            logger.debug(f"Loaded level {name} from {file_path}")

        except FileNotFoundError:
            logger.warning(f"Level file not found: {file_path}")
        except Exception as e:
            logger.error(f"Error loading level {file_path}: {e}")

        return level

    # Base64-like encoding used in NW files for tile data
    BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

    def _decode_board_string(self, board_str: str) -> List[int]:
        """Decode a row of tile data from base64-like encoding."""
        tiles = []
        i = 0
        while i + 1 < len(board_str):
            char1 = board_str[i]
            char2 = board_str[i + 1]
            idx1 = self.BASE64_CHARS.find(char1)
            idx2 = self.BASE64_CHARS.find(char2)
            if idx1 >= 0 and idx2 >= 0:
                tile_id = idx1 * 64 + idx2
            else:
                tile_id = 0
            tiles.append(tile_id)
            i += 2
        return tiles

    def _parse_nw_file(self, data: bytes):
        """
        Parse NW level file format.

        NW format sections:
        - BOARD: Tile data as base64-encoded text rows
        - LINKS: Level warps
        - SIGNS: Sign text
        - NPCS: Level NPCs
        """
        # Decode to text for parsing BOARD lines
        try:
            text = data.decode('latin-1', errors='replace')
        except:
            text = data.decode('utf-8', errors='replace')

        lines = text.split('\n')

        # Parse BOARD lines - each is: "BOARD x y width height tile_data"
        # y is row number (0-63), tile_data is base64-encoded
        board_rows: Dict[int, List[int]] = {}

        for line in lines:
            line = line.strip()
            if line.startswith('BOARD '):
                parts = line.split(None, 5)  # Split max 6 parts
                if len(parts) >= 6:
                    try:
                        y = int(parts[2])  # Row number
                        tile_str = parts[5]  # Tile data string
                        if 0 <= y < 64:
                            row_tiles = self._decode_board_string(tile_str)
                            board_rows[y] = row_tiles
                    except (ValueError, IndexError):
                        pass

        # Convert parsed rows to binary tile data
        if board_rows:
            self._tiles = bytearray(self.BOARD_SIZE)
            for y in range(64):
                if y in board_rows:
                    row_tiles = board_rows[y]
                    for x, tile_id in enumerate(row_tiles[:64]):
                        idx = (y * 64 + x) * 2
                        self._tiles[idx] = tile_id & 0xFF
                        self._tiles[idx + 1] = (tile_id >> 8) & 0xFF

        # Parse line-based features (GLEVNW01 text format): LINK, SIGN/SIGNEND,
        # CHEST, BADDY/BADDYEND. Mirrors GServer-v2 LevelLoader.
        self._parse_features(lines)

    def _parse_features(self, lines: List[str]):
        """Parse LINK/SIGN/CHEST/BADDY entries from GLEVNW01 lines."""
        i = 0
        n = len(lines)
        while i < n:
            raw = lines[i].rstrip('\r\n')
            i += 1
            stripped = raw.strip()
            if not stripped:
                continue
            parts = stripped.split()
            section = parts[0]

            if section == 'LINK' and len(parts) >= 8:
                # LINK destLevel... x y w h destX destY (level name may contain spaces)
                try:
                    dest_level = ' '.join(parts[1:-6])
                    x, y, w, h = (int(parts[-6]), int(parts[-5]),
                                  int(parts[-4]), int(parts[-3]))
                    self._links.append({
                        'dest_level': dest_level,
                        'x': x, 'y': y, 'width': w, 'height': h,
                        'dest_x': parts[-2], 'dest_y': parts[-1],
                    })
                except ValueError:
                    pass

            elif section == 'SIGN' and len(parts) == 3:
                try:
                    sx, sy = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                text_lines = []
                while i < n and lines[i].strip() != 'SIGNEND':
                    text_lines.append(lines[i].rstrip('\r\n'))
                    i += 1
                i += 1  # skip SIGNEND
                self._signs[(sx, sy)] = '\n'.join(text_lines)

            elif section == 'CHEST' and len(parts) >= 4:
                try:
                    cx, cy = int(parts[1]), int(parts[2])
                    item_id = ITEM_NAME_TO_ID.get(parts[3].lower())
                    if item_id is None:
                        item_id = int(parts[3])
                    sign = int(parts[4]) if len(parts) >= 5 else 0
                    self._chests.append({
                        'x': cx, 'y': cy, 'item': item_id, 'sign': sign,
                    })
                except (ValueError, KeyError):
                    pass

            elif section == 'NPC' and len(parts) >= 3:
                # NPC <image...> <x> <y>, then GS1 code until NPCEND.
                try:
                    nx, ny = float(parts[-2]), float(parts[-1])
                except ValueError:
                    continue
                image = ' '.join(parts[1:-2]).strip()
                if image == '-':
                    image = ''
                code_lines = []
                while i < n and lines[i].strip() != 'NPCEND':
                    code_lines.append(lines[i].rstrip('\r\n'))
                    i += 1
                i += 1  # skip NPCEND
                self._npc_defs.append({
                    'x': nx, 'y': ny, 'image': image,
                    'code': '\n'.join(code_lines),
                })

            elif section == 'BADDY' and len(parts) == 4:
                try:
                    bx, by = float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                btype = BADDY_NAME_TO_TYPE.get(parts[3].lower())
                if btype is None:
                    try:
                        btype = int(parts[3])
                    except ValueError:
                        btype = 0
                verses = []
                while i < n and lines[i].strip() != 'BADDYEND':
                    verses.append(lines[i].rstrip('\r\n'))
                    i += 1
                i += 1  # skip BADDYEND
                self._baddies.append({
                    'x': bx, 'y': by, 'type': btype, 'verses': verses,
                })

    def get_tile(self, x: int, y: int) -> int:
        """Get tile ID at position."""
        if 0 <= x < self.WIDTH and 0 <= y < self.HEIGHT:
            idx = (y * self.WIDTH + x) * 2
            return self._tiles[idx] | (self._tiles[idx + 1] << 8)
        return 0

    def set_tile(self, x: int, y: int, tile_id: int):
        """Set tile ID at position."""
        if 0 <= x < self.WIDTH and 0 <= y < self.HEIGHT:
            idx = (y * self.WIDTH + x) * 2
            self._tiles[idx] = tile_id & 0xFF
            self._tiles[idx + 1] = (tile_id >> 8) & 0xFF

    def get_board_packet(self) -> bytes:
        """Get tile data as board packet (8192 bytes)."""
        return bytes(self._tiles)

    def add_player(self, player: 'Player'):
        """Add a player to this level."""
        self._players[player.id] = None

    def remove_player(self, player: 'Player'):
        """Remove a player from this level."""
        self._players.pop(player.id, None)

    def get_player_ids(self) -> List[int]:
        """Get IDs of players on this level, in join order (first = leader)."""
        return list(self._players.keys())

    def get_leader_id(self) -> Optional[int]:
        """ID of this level's leader (the first player who joined and is
        still present), or None if the level is empty. GServer-v2
        Level::getPlayers().front() / Level::isPlayerLeader."""
        return next(iter(self._players), None)

    def is_player_leader(self, player: 'Player') -> bool:
        """True if `player` is this level's leader. Backs PLO_ISLEADER
        assignment/handoff (Player._send_level, Player.warp, Player._cleanup)
        and gates leader-only packets like PLI_BADDYPROPS."""
        return self.get_leader_id() == player.id

    def add_npc(self, npc: 'NPC'):
        """Add an NPC to this level."""
        self._npcs[npc.id] = npc
        npc.level = self

    def remove_npc(self, npc: 'NPC'):
        """Remove an NPC from this level."""
        self._npcs.pop(npc.id, None)
        if npc.level == self:
            npc.level = None

    def get_npcs(self) -> List['NPC']:
        """Get all NPCs on this level."""
        return list(self._npcs.values())

    def get_npc(self, npc_id: int) -> Optional['NPC']:
        """Get NPC by ID."""
        return self._npcs.get(npc_id)

    def get_links(self) -> List[Dict]:
        """Get all level links."""
        return self._links.copy()

    def get_sign(self, x: int, y: int) -> Optional[str]:
        """Get sign text at position."""
        return self._signs.get((x, y))

    def get_signs(self) -> Dict[Tuple[int, int], str]:
        """Get all signs as {(x, y): text}."""
        return self._signs.copy()

    def get_chest_defs(self) -> List[Dict]:
        """Get chest definitions parsed from the level file."""
        return self._chests.copy()

    def get_baddy_defs(self) -> List[Dict]:
        """Get baddy definitions parsed from the level file."""
        return self._baddies.copy()

    def get_npc_defs(self) -> List[Dict]:
        """Get NPC definitions parsed from the level file."""
        return self._npc_defs.copy()

    def check_warp(self, x: float, y: float) -> Optional[Dict]:
        """
        Check if position triggers a warp link.

        Returns link info if position is inside a link, None otherwise.
        """
        for link in self._links:
            if (link['x'] <= x < link['x'] + link['width'] and
                link['y'] <= y < link['y'] + link['height']):
                return link
        return None


class LevelManager:
    """
    Manages level loading and caching.
    """

    def __init__(self, levels_dir: str = "levels"):
        self.levels_dir = levels_dir
        self._levels: Dict[str, Level] = {}

    def get_level(self, name: str) -> Optional[Level]:
        """
        Get a level by name, loading if necessary.

        Args:
            name: Level filename (e.g., "onlinestartlocal.nw")

        Returns:
            Level instance, or None if not found
        """
        # Check cache
        if name in self._levels:
            return self._levels[name]

        # Try to load
        level_path = Path(self.levels_dir) / name
        if level_path.exists():
            level = Level.load(str(level_path))
            self._levels[name] = level
            return level

        # Try without extension
        for ext in ['.nw', '.graal', '.zelda']:
            level_path = Path(self.levels_dir) / f"{name}{ext}"
            if level_path.exists():
                level = Level.load(str(level_path))
                self._levels[name] = level
                return level

        return None

    def add_level(self, level: Level):
        """Add a level to the cache."""
        self._levels[level.name] = level

    def get_all_levels(self) -> List[Level]:
        """Get all loaded levels."""
        return list(self._levels.values())
