"""
pygserver.level - Level/map management

Handles loading, storing, and managing level data including
tiles, NPCs, items, signs, and links.
"""

import logging
from typing import TYPE_CHECKING, Optional, List, Set, Dict, Tuple
from pathlib import Path

if TYPE_CHECKING:
    from .player import Player
    from .npc import NPC

logger = logging.getLogger(__name__)


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

        # Players on this level
        self._players: Set[int] = set()

        # NPCs on this level
        self._npcs: Dict[int, 'NPC'] = {}

        # Level links (warps)
        self._links: List[Dict] = []

        # Signs
        self._signs: Dict[Tuple[int, int], str] = {}

        # Items on ground
        self._items: List[Dict] = []

        # Chests
        self._chests: List[Dict] = []

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

        # Parse LINKS section
        links_marker = b'LINKS'
        links_idx = data.find(links_marker)
        if links_idx >= 0:
            self._parse_links_section(data, links_idx + len(links_marker))

        # Parse SIGNS section
        signs_marker = b'SIGNS'
        signs_idx = data.find(signs_marker)
        if signs_idx >= 0:
            self._parse_signs_section(data, signs_idx + len(signs_marker))

    def _parse_links_section(self, data: bytes, start: int):
        """Parse LINKS section from NW file."""
        # LINKS format: newline-separated link definitions
        # Each link: "destLevel x y width height destX destY"
        try:
            # Find end of section (next section or EOF)
            end = len(data)
            for marker in [b'SIGNS', b'NPCS', b'BOARD']:
                idx = data.find(marker, start)
                if idx > start:
                    end = min(end, idx)

            section = data[start:end]
            lines = section.decode('latin-1', errors='replace').split('\n')

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 7:
                    self._links.append({
                        'dest_level': parts[0],
                        'x': int(parts[1]),
                        'y': int(parts[2]),
                        'width': int(parts[3]),
                        'height': int(parts[4]),
                        'dest_x': parts[5],
                        'dest_y': parts[6]
                    })
        except Exception as e:
            logger.debug(f"Error parsing links: {e}")

    def _parse_signs_section(self, data: bytes, start: int):
        """Parse SIGNS section from NW file."""
        # SIGNS format: x y text (newline separated)
        try:
            end = len(data)
            for marker in [b'LINKS', b'NPCS', b'BOARD']:
                idx = data.find(marker, start)
                if idx > start:
                    end = min(end, idx)

            section = data[start:end]
            lines = section.decode('latin-1', errors='replace').split('\n')

            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue

                parts = line.split(None, 2)
                if len(parts) >= 3:
                    try:
                        x = int(parts[0])
                        y = int(parts[1])
                        text = parts[2]
                        self._signs[(x, y)] = text
                    except ValueError:
                        pass
                i += 1

        except Exception as e:
            logger.debug(f"Error parsing signs: {e}")

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
        self._players.add(player.id)

    def remove_player(self, player: 'Player'):
        """Remove a player from this level."""
        self._players.discard(player.id)

    def get_player_ids(self) -> Set[int]:
        """Get IDs of players on this level."""
        return self._players.copy()

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
