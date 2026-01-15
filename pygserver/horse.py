"""
pygserver.horse - Horse system management

Handles horse spawning, mounting, dismounting, and bushes.
Based on GServer-v2 horse implementation.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict

from .protocol.constants import PLO, PLPROP
from .protocol.packets import PacketBuilder, build_horse_add, build_horse_del

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)


@dataclass
class Horse:
    """Represents a horse in the world."""
    id: int
    level_name: str
    x: float
    y: float
    direction: int = 2  # 0=up, 1=left, 2=down, 3=right
    bushes: int = 3  # Number of bushes (health)
    image: str = "horse.png"

    # Who's riding
    rider_id: Optional[int] = None

    # Spawn info for respawning
    spawn_x: float = 0.0
    spawn_y: float = 0.0
    spawn_bushes: int = 3

    # State
    dead: bool = False
    death_time: float = 0.0
    respawn_time: float = 60.0

    def __post_init__(self):
        self.spawn_x = self.x
        self.spawn_y = self.y
        self.spawn_bushes = self.bushes

    @property
    def is_alive(self) -> bool:
        """Check if horse is alive (has bushes)."""
        return self.bushes > 0 and not self.dead

    @property
    def is_ridden(self) -> bool:
        """Check if horse is being ridden."""
        return self.rider_id is not None


class HorseManager:
    """
    Manages horses in the game world.

    Handles:
    - Horse spawning and respawning
    - Mounting and dismounting
    - Horse damage (losing bushes)
    - Horse movement while ridden
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Horses by level
        self._horses: Dict[str, Dict[int, Horse]] = {}  # level_name -> {horse_id: Horse}

        # Mounted horses by player
        self._mounted: Dict[int, Horse] = {}  # player_id -> Horse

        # ID counter
        self._next_horse_id = 1

        # Tick task
        self._tick_task: Optional[asyncio.Task] = None
        self._running = False

        # Settings
        self.default_respawn_time = 60.0
        self.horse_respawn_enabled = True

    async def start(self):
        """Start the horse tick loop."""
        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("Horse manager started")

    async def stop(self):
        """Stop the horse tick loop."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        logger.info("Horse manager stopped")

    async def _tick_loop(self):
        """Main horse tick loop (runs every 1 second)."""
        tick_interval = 1.0

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Horse tick error: {e}")
                await asyncio.sleep(tick_interval)

    async def _tick(self):
        """Process one horse tick - handle respawning."""
        current_time = time.time()

        for level_name, horses in self._horses.items():
            for horse_id, horse in list(horses.items()):
                if horse.dead and self.horse_respawn_enabled:
                    if current_time - horse.death_time >= horse.respawn_time:
                        await self._respawn_horse(horse)

    async def add_horse(self, level: 'Level', x: float, y: float,
                        direction: int = 2, bushes: int = 3,
                        image: str = "horse.png") -> Horse:
        """
        Add a horse to a level.

        Args:
            level: Level to add to
            x: X position
            y: Y position
            direction: Facing direction
            bushes: Number of bushes (health)
            image: Horse image file

        Returns:
            The created horse
        """
        horse_id = self._next_horse_id
        self._next_horse_id += 1

        horse = Horse(
            id=horse_id,
            level_name=level.name,
            x=x,
            y=y,
            direction=direction,
            bushes=bushes,
            image=image,
            respawn_time=self.default_respawn_time
        )

        if level.name not in self._horses:
            self._horses[level.name] = {}
        self._horses[level.name][horse_id] = horse

        # Broadcast to level
        await self._broadcast_horse_add(horse)

        logger.debug(f"Added horse {horse_id} at ({x}, {y}) on {level.name}")
        return horse

    async def remove_horse(self, level_name: str, horse_id: int) -> bool:
        """
        Remove a horse from a level.

        Args:
            level_name: Level name
            horse_id: Horse ID

        Returns:
            True if horse was removed
        """
        if level_name not in self._horses:
            return False

        horse = self._horses[level_name].get(horse_id)
        if not horse:
            return False

        # Dismount any rider
        if horse.rider_id is not None:
            await self.handle_dismount(self.server.get_player(horse.rider_id))

        del self._horses[level_name][horse_id]

        # Broadcast removal
        await self._broadcast_horse_del(level_name, horse.x, horse.y)

        return True

    async def remove_horse_at(self, level_name: str, x: float, y: float) -> bool:
        """
        Remove a horse at a specific position.

        Args:
            level_name: Level name
            x: X position
            y: Y position

        Returns:
            True if horse was removed
        """
        if level_name not in self._horses:
            return False

        for horse_id, horse in list(self._horses[level_name].items()):
            if abs(horse.x - x) < 1.0 and abs(horse.y - y) < 1.0:
                return await self.remove_horse(level_name, horse_id)

        return False

    def get_horse(self, level_name: str, horse_id: int) -> Optional[Horse]:
        """Get a horse by ID."""
        if level_name not in self._horses:
            return None
        return self._horses[level_name].get(horse_id)

    def get_horse_at(self, level_name: str, x: float, y: float) -> Optional[Horse]:
        """Get a horse at a position."""
        if level_name not in self._horses:
            return None

        for horse in self._horses[level_name].values():
            if abs(horse.x - x) < 1.0 and abs(horse.y - y) < 1.0:
                return horse

        return None

    async def handle_mount(self, player: 'Player', x: float, y: float) -> bool:
        """
        Handle player mounting a horse.

        Args:
            player: Player mounting
            x: Horse X position
            y: Horse Y position

        Returns:
            True if mount was successful
        """
        if not player.level:
            return False

        # Check if already mounted
        if player.id in self._mounted:
            return False

        # Find horse at position
        horse = self.get_horse_at(player.level.name, x, y)
        if not horse or not horse.is_alive or horse.is_ridden:
            return False

        # Mount the horse
        horse.rider_id = player.id
        self._mounted[player.id] = horse

        # Remove horse from level (it follows player now)
        await self._broadcast_horse_del(player.level.name, horse.x, horse.y)

        # Update player props to show horse
        await player.send_props({
            PLPROP.HORSEGIF: horse.image,
            PLPROP.HORSEBUSHES: horse.bushes,
        })

        logger.info(f"Player {player.id} mounted horse {horse.id}")
        return True

    async def handle_dismount(self, player: Optional['Player']) -> bool:
        """
        Handle player dismounting a horse.

        Args:
            player: Player dismounting

        Returns:
            True if dismount was successful
        """
        if not player or player.id not in self._mounted:
            return False

        horse = self._mounted.pop(player.id)
        horse.rider_id = None

        # Place horse at player position
        if player.level:
            horse.x = player.x
            horse.y = player.y
            horse.level_name = player.level.name
            horse.direction = player.direction

            # Add horse back to level
            if horse.level_name not in self._horses:
                self._horses[horse.level_name] = {}
            self._horses[horse.level_name][horse.id] = horse

            # Broadcast horse placement
            await self._broadcast_horse_add(horse)

        logger.info(f"Player {player.id} dismounted horse {horse.id}")
        return True

    async def handle_horse_damage(self, player: 'Player', damage: int = 1):
        """
        Handle damage to player's mounted horse.

        Args:
            player: Player whose horse is damaged
            damage: Amount of damage (bushes lost)
        """
        if player.id not in self._mounted:
            return

        horse = self._mounted[player.id]
        horse.bushes = max(0, horse.bushes - damage)

        logger.debug(f"Horse {horse.id} took {damage} damage, bushes: {horse.bushes}")

        if horse.bushes <= 0:
            # Horse died - dismount player
            await self._horse_death(horse, player)

    async def _horse_death(self, horse: Horse, player: 'Player'):
        """Handle horse death."""
        horse.dead = True
        horse.death_time = time.time()

        # Dismount player
        if player.id in self._mounted:
            del self._mounted[player.id]
        horse.rider_id = None

        # Place dead horse at position
        if player.level:
            horse.x = player.x
            horse.y = player.y
            horse.level_name = player.level.name

        logger.info(f"Horse {horse.id} died")

    async def _respawn_horse(self, horse: Horse):
        """Respawn a dead horse."""
        horse.dead = False
        horse.bushes = horse.spawn_bushes
        horse.x = horse.spawn_x
        horse.y = horse.spawn_y
        horse.rider_id = None

        # Broadcast horse respawn
        await self._broadcast_horse_add(horse)
        logger.debug(f"Horse {horse.id} respawned")

    async def _broadcast_horse_add(self, horse: Horse):
        """Broadcast horse add to level."""
        packet = build_horse_add(horse.x, horse.y, horse.direction, horse.bushes, horse.image)
        await self.server.broadcast_to_level(horse.level_name, packet)

    async def _broadcast_horse_del(self, level_name: str, x: float, y: float):
        """Broadcast horse removal from level."""
        packet = build_horse_del(x, y)
        await self.server.broadcast_to_level(level_name, packet)

    def is_mounted(self, player_id: int) -> bool:
        """Check if a player is mounted."""
        return player_id in self._mounted

    def get_mounted_horse(self, player_id: int) -> Optional[Horse]:
        """Get the horse a player is riding."""
        return self._mounted.get(player_id)

    def get_horses_on_level(self, level_name: str) -> List[Horse]:
        """Get all horses on a level."""
        return list(self._horses.get(level_name, {}).values())

    def clear_level(self, level_name: str):
        """Clear all horses from a level."""
        # Dismount any riders
        if level_name in self._horses:
            for horse in self._horses[level_name].values():
                if horse.rider_id is not None:
                    self._mounted.pop(horse.rider_id, None)

        self._horses.pop(level_name, None)

    async def send_level_horses(self, player: 'Player', level: 'Level'):
        """
        Send all horses on a level to a player.

        Args:
            player: Player to send to
            level: Level to send horses from
        """
        for horse in self.get_horses_on_level(level.name):
            if horse.is_alive and not horse.is_ridden:
                packet = build_horse_add(
                    horse.x, horse.y, horse.direction, horse.bushes, horse.image
                )
                await player.send_raw(packet)

    async def handle_player_warp(self, player: 'Player', old_level: Optional['Level'],
                                  new_level: 'Level'):
        """
        Handle player warping between levels while mounted.

        Args:
            player: Player warping
            old_level: Previous level (None if first warp)
            new_level: New level
        """
        if player.id not in self._mounted:
            return

        horse = self._mounted[player.id]

        # Update horse level
        if old_level and old_level.name in self._horses:
            self._horses[old_level.name].pop(horse.id, None)

        horse.level_name = new_level.name
        horse.x = player.x
        horse.y = player.y

        if new_level.name not in self._horses:
            self._horses[new_level.name] = {}
        self._horses[new_level.name][horse.id] = horse

    async def handle_horse_add_packet(self, player: 'Player', x: float, y: float,
                                       direction: int, bushes: int, image: str):
        """
        Handle PLI_HORSEADD packet from client (placing a horse).

        Args:
            player: Player placing horse
            x: X position
            y: Y position
            direction: Facing direction
            bushes: Number of bushes
            image: Horse image
        """
        if not player.level:
            return

        # Check if player is dismounting
        if player.id in self._mounted:
            await self.handle_dismount(player)
            return

        # Otherwise, add new horse at position
        await self.add_horse(player.level, x, y, direction, bushes, image)

    async def handle_horse_del_packet(self, player: 'Player', x: float, y: float):
        """
        Handle PLI_HORSEDEL packet from client (removing/mounting horse).

        Args:
            player: Player
            x: X position
            y: Y position
        """
        if not player.level:
            return

        # Try to mount horse at position
        if await self.handle_mount(player, x, y):
            return

        # Otherwise, try to remove horse (admin action)
        await self.remove_horse_at(player.level.name, x, y)
