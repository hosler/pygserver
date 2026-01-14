"""
pygserver.combat - Combat system management

Handles bombs, arrows, damage, explosions, and player death mechanics.
Based on GServer-v2 combat implementation.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, Set
from enum import IntEnum

from .protocol.constants import PLO, PLPROP
from .protocol.packets import (
    PacketBuilder,
    build_bomb_add,
    build_arrow_add,
    build_explosion,
    build_hurt_player,
    build_fire_spy,
    build_push_away,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)


class DamageType(IntEnum):
    """Types of damage sources."""
    SWORD = 0
    BOMB = 1
    ARROW = 2
    FIRE = 3
    DROWN = 4
    HURT_NPC = 5
    SHOOT = 6
    OTHER = 7


@dataclass
class Bomb:
    """Represents an active bomb in the world."""
    id: int
    player_id: int
    level_name: str
    x: float
    y: float
    power: int
    time_left: float  # Seconds until explosion
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        """Check if bomb should explode."""
        return time.time() - self.created_at >= self.time_left


@dataclass
class Arrow:
    """Represents an active arrow in flight."""
    id: int
    player_id: int
    level_name: str
    x: float
    y: float
    direction: int  # 0=up, 1=left, 2=down, 3=right
    created_at: float = field(default_factory=time.time)
    speed: float = 8.0  # Tiles per second

    @property
    def expired(self) -> bool:
        """Check if arrow has traveled max distance (about 2 seconds)."""
        return time.time() - self.created_at >= 2.0


@dataclass
class Explosion:
    """Represents an explosion effect."""
    x: float
    y: float
    radius: float
    power: int
    player_id: int
    level_name: str


class CombatManager:
    """
    Manages combat mechanics including bombs, arrows, and damage.

    Handles:
    - Bomb placement and detonation
    - Arrow firing and collision
    - Damage application and knockback
    - Player death and respawn
    - Fire/ice effects
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Active projectiles by level
        self._bombs: Dict[str, Dict[int, Bomb]] = {}  # level_name -> {bomb_id: Bomb}
        self._arrows: Dict[str, Dict[int, Arrow]] = {}  # level_name -> {arrow_id: Arrow}

        # ID counters
        self._next_bomb_id = 1
        self._next_arrow_id = 1

        # Tick task
        self._tick_task: Optional[asyncio.Task] = None
        self._running = False

        # Combat settings
        self.bomb_damage_radius = 2.5  # Tiles
        self.arrow_damage = 1  # Half hearts
        self.bomb_base_damage = 2  # Half hearts per power level
        self.sword_damage = [0, 1, 2, 3, 4]  # Damage by sword power level
        self.respawn_time = 3.0  # Seconds

        # Invincibility tracking (player_id -> expire_time)
        self._invincible: Dict[int, float] = {}

    async def start(self):
        """Start the combat tick loop."""
        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("Combat manager started")

    async def stop(self):
        """Stop the combat tick loop."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        logger.info("Combat manager stopped")

    async def _tick_loop(self):
        """Main combat tick loop (runs every 50ms)."""
        tick_interval = 0.05  # 50ms = 20 ticks per second

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Combat tick error: {e}")
                await asyncio.sleep(tick_interval)

    async def _tick(self):
        """Process one combat tick."""
        current_time = time.time()

        # Process bombs
        for level_name, bombs in list(self._bombs.items()):
            expired_bombs = []
            for bomb_id, bomb in list(bombs.items()):
                if bomb.expired:
                    expired_bombs.append(bomb)
                    del bombs[bomb_id]

            # Detonate expired bombs
            for bomb in expired_bombs:
                await self._detonate_bomb(bomb)

        # Process arrows
        for level_name, arrows in list(self._arrows.items()):
            level = self.server.world.get_level(level_name)
            if not level:
                continue

            expired_arrows = []
            for arrow_id, arrow in list(arrows.items()):
                if arrow.expired:
                    expired_arrows.append(arrow)
                    del arrows[arrow_id]
                else:
                    # Move arrow and check collision
                    await self._update_arrow(arrow, level)

        # Clean up expired invincibility
        expired_invincible = [
            pid for pid, expire in self._invincible.items()
            if current_time > expire
        ]
        for pid in expired_invincible:
            del self._invincible[pid]

    async def handle_bomb_add(self, player: 'Player', x: float, y: float,
                               power: int, time_left: float = 3.0) -> Optional[Bomb]:
        """
        Handle a player placing a bomb.

        Args:
            player: Player placing the bomb
            x: X position in tiles
            y: Y position in tiles
            power: Bomb power (affects damage and radius)
            time_left: Fuse time in seconds

        Returns:
            The created Bomb, or None if failed
        """
        if not player.level:
            return None

        # Check if player has bombs
        if player.bombs <= 0:
            return None

        # Consume bomb
        player.bombs -= 1

        # Create bomb
        bomb_id = self._next_bomb_id
        self._next_bomb_id += 1

        bomb = Bomb(
            id=bomb_id,
            player_id=player.id,
            level_name=player.level.name,
            x=x,
            y=y,
            power=power,
            time_left=time_left
        )

        # Store bomb
        if player.level.name not in self._bombs:
            self._bombs[player.level.name] = {}
        self._bombs[player.level.name][bomb_id] = bomb

        # Broadcast bomb to level
        packet = build_bomb_add(player.id, x, y, power, time_left)
        await self.server.broadcast_to_level(player.level.name, packet)

        logger.debug(f"Player {player.id} placed bomb at ({x}, {y})")
        return bomb

    async def handle_bomb_del(self, player: 'Player', x: float, y: float):
        """
        Handle bomb deletion (picked up before explosion).

        Args:
            player: Player picking up the bomb
            x: X position
            y: Y position
        """
        if not player.level:
            return

        level_name = player.level.name
        if level_name not in self._bombs:
            return

        # Find bomb at position
        for bomb_id, bomb in list(self._bombs[level_name].items()):
            if abs(bomb.x - x) < 0.5 and abs(bomb.y - y) < 0.5:
                # Only owner can pick up
                if bomb.player_id == player.id:
                    del self._bombs[level_name][bomb_id]
                    player.bombs += 1
                    logger.debug(f"Player {player.id} picked up bomb at ({x}, {y})")
                break

    async def _detonate_bomb(self, bomb: Bomb):
        """
        Detonate a bomb, creating explosion and dealing damage.

        Args:
            bomb: The bomb to detonate
        """
        level = self.server.world.get_level(bomb.level_name)
        if not level:
            return

        radius = self.bomb_damage_radius + (bomb.power * 0.5)
        damage = self.bomb_base_damage * bomb.power

        # Broadcast explosion effect
        packet = build_explosion(bomb.x, bomb.y, radius, damage)
        await self.server.broadcast_to_level(bomb.level_name, packet)

        # Damage players in radius
        for player_id in level.get_player_ids():
            player = self.server.get_player(player_id)
            if not player:
                continue

            # Calculate distance
            dx = player.x - bomb.x
            dy = player.y - bomb.y
            distance = (dx * dx + dy * dy) ** 0.5

            if distance < radius:
                # Apply damage with knockback away from explosion
                knockback_x = dx / (distance + 0.1) * 2  # Prevent division by zero
                knockback_y = dy / (distance + 0.1) * 2
                await self.apply_damage(
                    player, damage, knockback_x, knockback_y,
                    DamageType.BOMB, bomb.player_id
                )

        # Damage baddies in radius
        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.handle_explosion(
                bomb.level_name, bomb.x, bomb.y, radius, damage
            )

        logger.debug(f"Bomb detonated at ({bomb.x}, {bomb.y}) with radius {radius}")

    async def handle_arrow_add(self, player: 'Player', x: float, y: float,
                                direction: int) -> Optional[Arrow]:
        """
        Handle a player firing an arrow.

        Args:
            player: Player firing the arrow
            x: Starting X position
            y: Starting Y position
            direction: Direction (0=up, 1=left, 2=down, 3=right)

        Returns:
            The created Arrow, or None if failed
        """
        if not player.level:
            return None

        # Check if player has arrows
        if player.arrows <= 0:
            return None

        # Consume arrow
        player.arrows -= 1

        # Create arrow
        arrow_id = self._next_arrow_id
        self._next_arrow_id += 1

        arrow = Arrow(
            id=arrow_id,
            player_id=player.id,
            level_name=player.level.name,
            x=x,
            y=y,
            direction=direction
        )

        # Store arrow
        if player.level.name not in self._arrows:
            self._arrows[player.level.name] = {}
        self._arrows[player.level.name][arrow_id] = arrow

        # Broadcast arrow to level
        packet = build_arrow_add(player.id, x, y, direction)
        await self.server.broadcast_to_level(player.level.name, packet)

        logger.debug(f"Player {player.id} fired arrow at ({x}, {y}) direction {direction}")
        return arrow

    async def _update_arrow(self, arrow: Arrow, level: 'Level'):
        """
        Update arrow position and check for collisions.

        Args:
            arrow: The arrow to update
            level: The level the arrow is in
        """
        # Direction vectors
        dir_vectors = {
            0: (0, -1),   # Up
            1: (-1, 0),   # Left
            2: (0, 1),    # Down
            3: (1, 0)     # Right
        }

        dx, dy = dir_vectors.get(arrow.direction, (0, 0))
        move_speed = arrow.speed * 0.05  # Per tick (50ms)

        arrow.x += dx * move_speed
        arrow.y += dy * move_speed

        # Check wall collision
        tile_x = int(arrow.x)
        tile_y = int(arrow.y)
        if tile_x < 0 or tile_x >= 64 or tile_y < 0 or tile_y >= 64:
            # Out of bounds - remove arrow
            if arrow.level_name in self._arrows:
                self._arrows[arrow.level_name].pop(arrow.id, None)
            return

        # Check player collision
        for player_id in level.get_player_ids():
            if player_id == arrow.player_id:
                continue  # Don't hit self

            player = self.server.get_player(player_id)
            if not player:
                continue

            # Check if arrow hits player (within ~1 tile)
            dist_x = abs(player.x - arrow.x)
            dist_y = abs(player.y - arrow.y)

            if dist_x < 1.0 and dist_y < 1.0:
                # Hit! Apply damage and remove arrow
                knockback_x = dx * 2
                knockback_y = dy * 2
                await self.apply_damage(
                    player, self.arrow_damage, knockback_x, knockback_y,
                    DamageType.ARROW, arrow.player_id
                )

                # Remove arrow
                if arrow.level_name in self._arrows:
                    self._arrows[arrow.level_name].pop(arrow.id, None)
                return

        # Check baddy collision
        if hasattr(self.server, 'baddy_manager'):
            hit = await self.server.baddy_manager.check_arrow_hit(
                arrow.level_name, arrow.x, arrow.y, self.arrow_damage, arrow.player_id
            )
            if hit:
                if arrow.level_name in self._arrows:
                    self._arrows[arrow.level_name].pop(arrow.id, None)

    async def handle_hurt_player(self, attacker: 'Player', target_id: int,
                                  power: int, from_x: float, from_y: float):
        """
        Handle PLI_HURTPLAYER packet (sword hit, etc).

        Args:
            attacker: Player dealing damage
            target_id: Target player ID
            power: Damage amount (in half-hearts)
            from_x: X knockback direction
            from_y: Y knockback direction
        """
        target = self.server.get_player(target_id)
        if not target or not target.level:
            return

        # Must be on same level
        if attacker.level != target.level:
            return

        await self.apply_damage(
            target, power, from_x, from_y,
            DamageType.SWORD, attacker.id
        )

    async def apply_damage(self, player: 'Player', damage: int,
                           knockback_x: float, knockback_y: float,
                           damage_type: DamageType = DamageType.OTHER,
                           attacker_id: Optional[int] = None):
        """
        Apply damage to a player.

        Args:
            player: Player taking damage
            damage: Damage in half-hearts
            knockback_x: X knockback force
            knockback_y: Y knockback force
            damage_type: Type of damage source
            attacker_id: ID of attacking player (if any)
        """
        # Check invincibility
        if player.id in self._invincible:
            if time.time() < self._invincible[player.id]:
                return

        # Apply damage
        old_hearts = player.hearts
        player.hearts = max(0, player.hearts - (damage / 2.0))

        logger.debug(f"Player {player.id} took {damage/2.0} damage: {old_hearts} -> {player.hearts}")

        # Grant brief invincibility (1 second)
        self._invincible[player.id] = time.time() + 1.0

        # Send hurt packet to player
        packet = build_hurt_player(player.id, damage, knockback_x, knockback_y)
        await player.send_raw(packet)

        # Broadcast to level
        if player.level:
            await self.server.broadcast_to_level(
                player.level.name, packet, exclude={player.id}
            )

        # Check for death
        if player.hearts <= 0:
            await self.handle_player_death(player, attacker_id, damage_type)

    async def handle_player_death(self, player: 'Player',
                                   killer_id: Optional[int] = None,
                                   damage_type: DamageType = DamageType.OTHER):
        """
        Handle player death.

        Args:
            player: The player who died
            killer_id: ID of killing player (if any)
            damage_type: How the player died
        """
        logger.info(f"Player {player.id} ({player.nickname}) died")

        # Update stats
        # player.deaths += 1  # TODO: Add death tracking

        if killer_id is not None:
            killer = self.server.get_player(killer_id)
            if killer:
                # killer.kills += 1  # TODO: Add kill tracking
                logger.info(f"  Killed by {killer.nickname}")

        # Trigger NPC death event
        if hasattr(self.server, 'npc_manager'):
            await self.server.npc_manager.on_player_dies(player, killer_id)

        # Respawn after delay
        asyncio.create_task(self._respawn_player(player))

    async def _respawn_player(self, player: 'Player'):
        """
        Respawn a player after death.

        Args:
            player: Player to respawn
        """
        await asyncio.sleep(self.respawn_time)

        if not player.connected:
            return

        # Restore health
        player.hearts = player.max_hearts

        # Warp to spawn point
        await player.warp(
            self.server.config.start_level,
            self.server.config.start_x,
            self.server.config.start_y
        )

        logger.info(f"Player {player.id} respawned")

    async def handle_fire_spy(self, player: 'Player', x: float, y: float):
        """
        Handle fire spy placement (from fire wand).

        Args:
            player: Player placing fire
            x: X position
            y: Y position
        """
        if not player.level:
            return

        # Broadcast fire effect
        packet = build_fire_spy(x, y)
        await self.server.broadcast_to_level(player.level.name, packet)

        # Damage players at position
        for player_id in player.level.get_player_ids():
            if player_id == player.id:
                continue

            other = self.server.get_player(player_id)
            if not other:
                continue

            dist_x = abs(other.x - x)
            dist_y = abs(other.y - y)

            if dist_x < 1.5 and dist_y < 1.5:
                await self.apply_damage(
                    other, 2, 0, 0,
                    DamageType.FIRE, player.id
                )

    async def handle_throw_carried(self, player: 'Player', x: float, y: float,
                                    carried_type: int):
        """
        Handle throwing a carried object (bush, pot, etc).

        Args:
            player: Player throwing object
            x: Target X position
            y: Target Y position
            carried_type: Type of carried object
        """
        if not player.level:
            return

        # Broadcast throw
        # Damage calculation depends on carried_type
        damage = 1 if carried_type > 0 else 0

        # Check for player hits at target
        for player_id in player.level.get_player_ids():
            if player_id == player.id:
                continue

            other = self.server.get_player(player_id)
            if not other:
                continue

            dist_x = abs(other.x - x)
            dist_y = abs(other.y - y)

            if dist_x < 1.5 and dist_y < 1.5 and damage > 0:
                knockback_x = (other.x - player.x)
                knockback_y = (other.y - player.y)
                await self.apply_damage(
                    other, damage, knockback_x, knockback_y,
                    DamageType.OTHER, player.id
                )

    async def handle_shoot(self, player: 'Player', shoot_data: bytes):
        """
        Handle PLI_SHOOT packet (projectile weapons).

        Args:
            player: Player shooting
            shoot_data: Raw shoot packet data
        """
        # Parse shoot data - format varies by weapon
        # For now, broadcast to level
        if not player.level:
            return

        # Build and broadcast
        builder = PacketBuilder()
        builder.write_gchar(PLO.SHOOT)
        builder.write_gshort(player.id)
        builder.write_bytes(shoot_data)
        builder.write_byte(ord('\n'))

        await self.server.broadcast_to_level(
            player.level.name, builder.build(), exclude={player.id}
        )

    async def handle_shoot2(self, player: 'Player', shoot_data: bytes):
        """
        Handle PLI_SHOOT2 packet (extended projectile data).

        Args:
            player: Player shooting
            shoot_data: Raw shoot packet data
        """
        if not player.level:
            return

        builder = PacketBuilder()
        builder.write_gchar(PLO.SHOOT)
        builder.write_gshort(player.id)
        builder.write_bytes(shoot_data)
        builder.write_byte(ord('\n'))

        await self.server.broadcast_to_level(
            player.level.name, builder.build(), exclude={player.id}
        )

    async def handle_hit_objects(self, player: 'Player', x: float, y: float,
                                  power: int, objects: List[int]):
        """
        Handle PLI_HITOBJECTS packet (hitting multiple objects with sword).

        Args:
            player: Player hitting
            x: Hit X position
            y: Hit Y position
            power: Hit power
            objects: List of object IDs hit
        """
        if not player.level:
            return

        # Process each hit object
        for obj_id in objects:
            # Could be NPCs, baddies, etc.
            # Check if it's a baddy
            if hasattr(self.server, 'baddy_manager'):
                await self.server.baddy_manager.handle_hit(
                    player.level.name, obj_id, power, player.id
                )

    def is_invincible(self, player_id: int) -> bool:
        """Check if a player is currently invincible."""
        if player_id not in self._invincible:
            return False
        return time.time() < self._invincible[player_id]

    def set_invincible(self, player_id: int, duration: float):
        """
        Set player invincibility.

        Args:
            player_id: Player ID
            duration: Duration in seconds
        """
        self._invincible[player_id] = time.time() + duration

    def get_bombs_on_level(self, level_name: str) -> List[Bomb]:
        """Get all active bombs on a level."""
        return list(self._bombs.get(level_name, {}).values())

    def get_arrows_on_level(self, level_name: str) -> List[Arrow]:
        """Get all active arrows on a level."""
        return list(self._arrows.get(level_name, {}).values())

    def clear_level(self, level_name: str):
        """Clear all combat objects from a level."""
        self._bombs.pop(level_name, None)
        self._arrows.pop(level_name, None)
