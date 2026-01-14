"""
pygserver.world - World and GMAP management

Handles the game world, including GMAP support for large
multi-level maps.
"""

import logging
from typing import TYPE_CHECKING, Optional, Dict, Tuple, List
from pathlib import Path

from .level import Level, LevelManager

if TYPE_CHECKING:
    from .server import GameServer

logger = logging.getLogger(__name__)


class GMap:
    """
    Represents a GMAP (Grid Map) - a large map composed of multiple levels.

    GMAP files define how individual levels are arranged in a grid.
    """

    def __init__(self, name: str):
        self.name = name
        self.width = 0  # Grid width in levels
        self.height = 0  # Grid height in levels

        # Grid of level names: grid[(gx, gy)] = level_name
        self.grid: Dict[Tuple[int, int], str] = {}

        # Map image (minimap)
        self.image: Optional[str] = None

    @classmethod
    def load(cls, file_path: str) -> 'GMap':
        """
        Load a GMAP file.

        GMAP format:
        WIDTH <n>
        HEIGHT <n>
        MAPIMG <image>
        LEVELNAMES
        level1.nw
        level2.nw
        ...
        LEVELNAMES END
        """
        path = Path(file_path)
        gmap = cls(path.stem)

        try:
            with open(file_path, 'r') as f:
                content = f.read()

            lines = content.split('\n')
            in_levelnames = False
            level_index = 0

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                if line.startswith('WIDTH'):
                    parts = line.split()
                    if len(parts) >= 2:
                        gmap.width = int(parts[1])

                elif line.startswith('HEIGHT'):
                    parts = line.split()
                    if len(parts) >= 2:
                        gmap.height = int(parts[1])

                elif line.startswith('MAPIMG'):
                    parts = line.split(None, 1)
                    if len(parts) >= 2:
                        gmap.image = parts[1]

                elif line == 'LEVELNAMES':
                    in_levelnames = True
                    level_index = 0

                elif line == 'LEVELNAMES END':
                    in_levelnames = False

                elif in_levelnames and gmap.width > 0:
                    # Each line in LEVELNAMES is a level filename
                    gx = level_index % gmap.width
                    gy = level_index // gmap.width
                    gmap.grid[(gx, gy)] = line
                    level_index += 1

            logger.debug(f"Loaded GMAP {gmap.name}: {gmap.width}x{gmap.height}")

        except Exception as e:
            logger.error(f"Error loading GMAP {file_path}: {e}")

        return gmap

    def get_level_at(self, gx: int, gy: int) -> Optional[str]:
        """Get level name at grid position."""
        return self.grid.get((gx, gy))

    def find_level(self, level_name: str) -> Optional[Tuple[int, int]]:
        """Find grid position of a level."""
        for pos, name in self.grid.items():
            if name == level_name:
                return pos
        return None

    def world_to_local(self, world_x: float, world_y: float) -> Tuple[float, float, int, int]:
        """
        Convert world coordinates to local level coordinates.

        Returns:
            (local_x, local_y, grid_x, grid_y)
        """
        import math
        grid_x = math.floor(world_x / 64)
        grid_y = math.floor(world_y / 64)
        local_x = world_x % 64
        local_y = world_y % 64
        return (local_x, local_y, grid_x, grid_y)

    def local_to_world(self, local_x: float, local_y: float,
                       grid_x: int, grid_y: int) -> Tuple[float, float]:
        """
        Convert local level coordinates to world coordinates.

        Returns:
            (world_x, world_y)
        """
        world_x = local_x + grid_x * 64
        world_y = local_y + grid_y * 64
        return (world_x, world_y)


class World:
    """
    Manages the game world - levels and GMaps.

    Provides level lookup, GMAP coordinate translation, and world state.
    """

    def __init__(self, server: 'GameServer'):
        self.server = server
        self._level_manager = LevelManager(server.config.levels_dir)
        self._gmaps: Dict[str, GMap] = {}

    def add_level(self, level: Level):
        """Add a level to the world."""
        self._level_manager.add_level(level)

    def get_level(self, name: str) -> Optional[Level]:
        """
        Get a level by name.

        Args:
            name: Level filename (e.g., "onlinestartlocal.nw")

        Returns:
            Level instance, or None if not found
        """
        return self._level_manager.get_level(name)

    def load_gmap(self, file_path: str) -> Optional[GMap]:
        """Load a GMAP file."""
        gmap = GMap.load(file_path)
        if gmap:
            self._gmaps[gmap.name] = gmap
        return gmap

    def get_gmap(self, name: str) -> Optional[GMap]:
        """Get a GMAP by name."""
        return self._gmaps.get(name)

    def get_gmap_for_level(self, level_name: str) -> Optional[Tuple[GMap, int, int]]:
        """
        Find the GMAP containing a level.

        Returns:
            Tuple of (gmap, grid_x, grid_y) or None
        """
        for gmap in self._gmaps.values():
            pos = gmap.find_level(level_name)
            if pos:
                return (gmap, pos[0], pos[1])
        return None

    def get_adjacent_levels(self, level_name: str) -> Dict[str, str]:
        """
        Get levels adjacent to a given level in its GMAP.

        Returns:
            Dict mapping direction ('n', 's', 'e', 'w') to level name
        """
        result = {}
        gmap_info = self.get_gmap_for_level(level_name)

        if gmap_info:
            gmap, gx, gy = gmap_info

            # North
            if gy > 0:
                north = gmap.get_level_at(gx, gy - 1)
                if north:
                    result['n'] = north

            # South
            if gy < gmap.height - 1:
                south = gmap.get_level_at(gx, gy + 1)
                if south:
                    result['s'] = south

            # East
            if gx < gmap.width - 1:
                east = gmap.get_level_at(gx + 1, gy)
                if east:
                    result['e'] = east

            # West
            if gx > 0:
                west = gmap.get_level_at(gx - 1, gy)
                if west:
                    result['w'] = west

        return result

    def get_all_levels(self) -> List[Level]:
        """Get all loaded levels."""
        return self._level_manager.get_all_levels()
