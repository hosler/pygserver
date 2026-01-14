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

    def _parse_nw_file(self, data: bytes):
        """
        Parse NW level file format.

        NW format sections:
        - BOARD: Tile data (4096 tiles, 2 bytes each little-endian)
        - LINKS: Level warps
        - SIGNS: Sign text
        - NPCS: Level NPCs
        """
        # Find BOARD section
        board_marker = b'BOARD'
        board_idx = data.find(board_marker)

        if board_idx >= 0:
            # Skip marker and size info
            tile_start = board_idx + len(board_marker)

            # Skip any header bytes (format varies)
            # Try to find where the actual tile data starts
            # In most NW files, tiles start right after BOARD marker + some bytes
            while tile_start < len(data) and tile_start < board_idx + 20:
                # Check if this looks like tile data
                if tile_start + self.BOARD_SIZE <= len(data):
                    # Copy tile data
                    self._tiles = bytearray(data[tile_start:tile_start + self.BOARD_SIZE])
                    break
                tile_start += 1

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
