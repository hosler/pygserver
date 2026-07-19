"""
pygserver.baddy - Enemy (baddy) system management

Handles enemy spawning, AI, damage, and death.
Based on GServer-v2 baddy implementation.
"""

import asyncio
import logging
import time
import random
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, Callable
from enum import IntEnum

from .protocol.constants import PLO, BDPROP, BDMODE
from .protocol.packets import PacketBuilder, build_baddy_props, build_baddy_hurt

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)


class BaddyType(IntEnum):
    """Types of baddies."""
    GRAYBALL = 0
    REDBALL = 1
    BLUEOCTOPUS = 2
    REDOCTOPUS = 3
    GOLDOCTOPUS = 4
    SPIDER = 5
    GRAYSNAKE = 6
    REDSNAKE = 7
    LIZARDMAN = 8
    DRAGONLIZARD = 9
    SPIDER2 = 10
    FIREFLY = 11
    WOLF = 12
    OGRE = 13
    SWAMP_MONSTER = 14
    PIMPLETHING = 15


# Baddy stats by type: (health, damage, speed, detection_range).
# Health for types 0-9 (the ones level files can actually spawn — see
# level.BADDY_NAME_TO_TYPE) is GServer-v2's LevelBaddy.cpp baddyPower table
# (2,3,4,3,2,1,1,6,12,8), not the ad-hoc values this previously had; those
# baddyPower values are also broadcast as the POWERIMAGE prop's power byte.
BADDY_STATS = {
    BaddyType.GRAYBALL: (2, 1, 2.0, 5.0),
    BaddyType.REDBALL: (3, 2, 2.5, 6.0),
    BaddyType.BLUEOCTOPUS: (4, 1, 1.5, 7.0),
    BaddyType.REDOCTOPUS: (3, 2, 2.0, 7.0),
    BaddyType.GOLDOCTOPUS: (2, 3, 2.5, 8.0),
    BaddyType.SPIDER: (1, 1, 3.0, 6.0),
    BaddyType.GRAYSNAKE: (1, 1, 2.5, 5.0),
    BaddyType.REDSNAKE: (6, 2, 3.0, 6.0),
    BaddyType.LIZARDMAN: (12, 2, 2.0, 8.0),
    BaddyType.DRAGONLIZARD: (8, 3, 2.5, 10.0),
    BaddyType.SPIDER2: (5, 2, 3.5, 7.0),
    BaddyType.FIREFLY: (2, 1, 4.0, 8.0),
    BaddyType.WOLF: (4, 2, 4.0, 10.0),
    BaddyType.OGRE: (12, 3, 1.5, 6.0),
    BaddyType.SWAMP_MONSTER: (8, 2, 1.0, 5.0),
    BaddyType.PIMPLETHING: (6, 2, 2.0, 6.0),
}

# Default sprite sheet by type, for types 0-9 — GServer-v2's LevelBaddy.cpp
# baddyImages table. Level-file baddies (BADDY_NAME_TO_TYPE in level.py) only
# ever produce types in this range; anything outside it (the extra
# pygserver-only types above) falls back to the classic gray-ball image, same
# as GServer's own out-of-range clamp to BaddyType::GRAYSOLDIER.
BADDY_DEFAULT_IMAGE = {
    0: "baddygray.png", 1: "baddyblue.png", 2: "baddyred.png",
    3: "baddyblue.png", 4: "baddygray.png", 5: "baddyhare.png",
    6: "baddyoctopus.png", 7: "baddygold.png", 8: "baddylizardon.png",
    9: "baddydragon.png",
}

# Drop tables by baddy type
BADDY_DROPS = {
    BaddyType.GRAYBALL: [(0, 0.6), (1, 0.3), (4, 0.1)],  # (item_type, probability)
    BaddyType.REDBALL: [(0, 0.4), (1, 0.4), (4, 0.2)],
    # Add more as needed
}


@dataclass
class Baddy:
    """Represents an enemy (baddy) in the world."""
    id: int
    level_name: str
    baddy_type: BaddyType
    x: float
    y: float
    direction: int = 2  # 0=up, 1=left, 2=down, 3=right
    mode: int = BDMODE.HUNT  # Current AI mode
    health: int = 3
    max_health: int = 3
    damage: int = 1
    speed: float = 2.0
    detection_range: float = 6.0

    # Spawn info (for respawning)
    spawn_x: float = 0.0
    spawn_y: float = 0.0

    # AI state
    target_player_id: Optional[int] = None
    wander_timer: float = 0.0
    attack_cooldown: float = 0.0
    hurt_timer: float = 0.0

    # Animation frame (BDPROP.ANI): toggles 0/1 while walking/hunting so
    # clients can animate the walk cycle (see _toggle_ani in BaddyManager).
    ani: int = 0

    # Baddy image (sent with POWERIMAGE prop)
    image: str = ""

    # Verse strings parsed from the level file's BADDY block (up to 3 lines:
    # sight/hurt/attack — see level._parse_features). Sent once, in the
    # initial props broadcast (spawn / a joining player's first sighting).
    verses: List[str] = field(default_factory=list)

    # Respawn settings
    respawn_time: float = 60.0
    dead: bool = False
    death_time: float = 0.0

    def __post_init__(self):
        self.spawn_x = self.x
        self.spawn_y = self.y
        if self.baddy_type in BADDY_STATS:
            stats = BADDY_STATS[self.baddy_type]
            self.max_health = stats[0]
            self.health = self.max_health
            self.damage = stats[1]
            self.speed = stats[2]
            self.detection_range = stats[3]
        if not self.image:
            self.image = BADDY_DEFAULT_IMAGE.get(int(self.baddy_type), "baddygray.png")

    def _verse(self, index: int) -> str:
        return self.verses[index] if index < len(self.verses) else ""

    def build_props_packet(self, include_verses: bool = False) -> bytes:
        """Build PLO_BADDYPROPS packet for this baddy.

        include_verses sends VERSESIGHT/VERSEHURT too — only needed once,
        the first time a client learns about this baddy (spawn / a joining
        player's initial level baddy list), not on every AI tick broadcast.
        """
        props = {
            BDPROP.ID: self.id,
            BDPROP.X: self.x,
            BDPROP.Y: self.y,
            BDPROP.TYPE: self.baddy_type,
            BDPROP.POWERIMAGE: (self.health, self.image),
            BDPROP.MODE: self.mode,
            BDPROP.ANI: self.ani,
            BDPROP.DIR: self.direction,
        }
        if include_verses:
            props[BDPROP.VERSESIGHT] = self._verse(0)
            props[BDPROP.VERSEHURT] = self._verse(1)
        return build_baddy_props(self.id, props)


class BaddyManager:
    """
    Manages enemies (baddies) in the game world.

    Handles:
    - Baddy spawning and respawning
    - AI behavior (hunt, wander, hurt, dead)
    - Damage and death
    - Collision with players
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Baddies by level
        self._baddies: Dict[str, Dict[int, Baddy]] = {}  # level_name -> {baddy_id: Baddy}

        # ID counter
        self._next_baddy_id = 1

        # Tick task
        self._tick_task: Optional[asyncio.Task] = None
        self._running = False

        # Settings
        self.default_respawn_time = 60.0
        self.baddy_respawn_enabled = True

    async def start(self):
        """Start the baddy tick loop."""
        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("Baddy manager started")

    async def stop(self):
        """Stop the baddy tick loop."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        logger.info("Baddy manager stopped")

    async def _tick_loop(self):
        """Main baddy tick loop (runs every 100ms)."""
        tick_interval = 0.1  # 100ms = 10 ticks per second

        while self._running:
            try:
                await self._tick(tick_interval)
                await asyncio.sleep(tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Baddy tick error: {e}")
                await asyncio.sleep(tick_interval)

    async def _tick(self, delta_time: float):
        """Process one baddy tick."""
        current_time = time.time()

        for level_name, baddies in self._baddies.items():
            level = self.server.world.get_level(level_name)
            if not level:
                continue

            for baddy_id, baddy in list(baddies.items()):
                # Handle dead baddies
                if baddy.dead:
                    if self.baddy_respawn_enabled:
                        if current_time - baddy.death_time >= baddy.respawn_time:
                            await self._respawn_baddy(baddy)
                    continue

                # Update AI
                await self._update_baddy_ai(baddy, level, delta_time)

    async def _update_baddy_ai(self, baddy: Baddy, level: 'Level', delta_time: float):
        """
        Update baddy AI behavior.

        Args:
            baddy: Baddy to update
            level: Level the baddy is on
            delta_time: Time since last tick
        """
        # Update timers
        baddy.wander_timer = max(0, baddy.wander_timer - delta_time)
        baddy.attack_cooldown = max(0, baddy.attack_cooldown - delta_time)
        baddy.hurt_timer = max(0, baddy.hurt_timer - delta_time)

        # In hurt state, don't do anything
        if baddy.hurt_timer > 0:
            return

        # Find nearest player
        nearest_player = None
        nearest_distance = float('inf')

        for player_id in level.get_player_ids():
            player = self.server.get_player(player_id)
            if not player:
                continue

            dx = player.x - baddy.x
            dy = player.y - baddy.y
            distance = math.sqrt(dx * dx + dy * dy)

            if distance < nearest_distance:
                nearest_distance = distance
                nearest_player = player

        # AI state machine
        if baddy.mode == BDMODE.DEAD:
            return

        elif baddy.mode == BDMODE.HURT:
            # Transition back to hunt after hurt animation
            baddy.mode = BDMODE.HUNT
            await self._broadcast_baddy_props(baddy)

        elif baddy.mode == BDMODE.HUNT:
            if nearest_player and nearest_distance < baddy.detection_range:
                # Move towards player
                baddy.target_player_id = nearest_player.id
                await self._move_towards_target(baddy, nearest_player.x, nearest_player.y, delta_time)

                # Check for attack
                if nearest_distance < 1.5 and baddy.attack_cooldown <= 0:
                    await self._attack_player(baddy, nearest_player)
            else:
                # Wander
                baddy.target_player_id = None
                if baddy.wander_timer <= 0:
                    await self._wander(baddy, delta_time)
                    baddy.wander_timer = random.uniform(1.0, 3.0)

        elif baddy.mode == BDMODE.SWARM:
            # Aggressive mode - always chase
            if nearest_player:
                await self._move_towards_target(baddy, nearest_player.x, nearest_player.y, delta_time)
                if nearest_distance < 1.5 and baddy.attack_cooldown <= 0:
                    await self._attack_player(baddy, nearest_player)

    async def _move_towards_target(self, baddy: Baddy, target_x: float, target_y: float,
                                    delta_time: float):
        """Move baddy towards a target position."""
        dx = target_x - baddy.x
        dy = target_y - baddy.y
        distance = math.sqrt(dx * dx + dy * dy)

        if distance < 0.1:
            return

        # Normalize and apply speed
        move_x = (dx / distance) * baddy.speed * delta_time
        move_y = (dy / distance) * baddy.speed * delta_time

        # Update position
        old_x, old_y = baddy.x, baddy.y
        baddy.x += move_x
        baddy.y += move_y

        # Clamp to level bounds
        baddy.x = max(0, min(63, baddy.x))
        baddy.y = max(0, min(63, baddy.y))

        # Update direction
        if abs(dx) > abs(dy):
            baddy.direction = 3 if dx > 0 else 1  # Right or Left
        else:
            baddy.direction = 2 if dy > 0 else 0  # Down or Up

        # Broadcast if moved significantly
        if abs(baddy.x - old_x) > 0.01 or abs(baddy.y - old_y) > 0.01:
            self._toggle_ani(baddy)
            await self._broadcast_baddy_props(baddy)

    @staticmethod
    def _toggle_ani(baddy: Baddy):
        """Flip the walk animation frame (0/1) for the next props broadcast.

        Cheap walk-cycle animation: only advances when a moving-mode AI tick
        actually broadcasts (no extra packets), toggled here rather than
        every tick.
        """
        baddy.ani = 0 if baddy.ani else 1

    async def _wander(self, baddy: Baddy, delta_time: float):
        """Make baddy wander randomly."""
        # Random direction
        direction = random.randint(0, 3)
        dir_vectors = {
            0: (0, -1),   # Up
            1: (-1, 0),   # Left
            2: (0, 1),    # Down
            3: (1, 0)     # Right
        }

        dx, dy = dir_vectors[direction]
        move_distance = baddy.speed * delta_time * 0.5  # Slower when wandering

        baddy.x += dx * move_distance
        baddy.y += dy * move_distance
        baddy.direction = direction

        # Clamp to level bounds
        baddy.x = max(0, min(63, baddy.x))
        baddy.y = max(0, min(63, baddy.y))

        self._toggle_ani(baddy)
        await self._broadcast_baddy_props(baddy)

    async def _attack_player(self, baddy: Baddy, player: 'Player'):
        """Make baddy attack a player."""
        baddy.attack_cooldown = 1.0  # 1 second cooldown

        # Calculate knockback direction
        dx = player.x - baddy.x
        dy = player.y - baddy.y
        distance = max(0.1, math.sqrt(dx * dx + dy * dy))
        knockback_x = (dx / distance) * 2
        knockback_y = (dy / distance) * 2

        # Apply damage
        if hasattr(self.server, 'combat_manager'):
            from .combat import DamageType
            await self.server.combat_manager.apply_damage(
                player, baddy.damage, knockback_x, knockback_y,
                DamageType.HURT_NPC, None
            )

        logger.debug(f"Baddy {baddy.id} attacked player {player.id}")

    async def _broadcast_baddy_props(self, baddy: Baddy, include_verses: bool = False):
        """Broadcast baddy properties to level."""
        packet = baddy.build_props_packet(include_verses=include_verses)
        await self.server.broadcast_to_level(baddy.level_name, packet)

    async def add_baddy(self, level: 'Level', x: float, y: float,
                        baddy_type: BaddyType,
                        verses: Optional[List[str]] = None) -> Baddy:
        """
        Add a baddy to a level.

        Args:
            level: Level to add to
            x: X position
            y: Y position
            baddy_type: Type of baddy
            verses: Optional sight/hurt/attack verse strings parsed from the
                level file's BADDY block (level.get_baddy_defs()['verses'])

        Returns:
            The created baddy
        """
        baddy_id = self._next_baddy_id
        self._next_baddy_id += 1

        baddy = Baddy(
            id=baddy_id,
            level_name=level.name,
            baddy_type=baddy_type,
            x=x,
            y=y,
            respawn_time=self.default_respawn_time,
            verses=list(verses) if verses else [],
        )

        if level.name not in self._baddies:
            self._baddies[level.name] = {}
        self._baddies[level.name][baddy_id] = baddy

        # Broadcast to level (initial sighting: include verses)
        await self._broadcast_baddy_props(baddy, include_verses=True)

        logger.debug(f"Added baddy {baddy_id} ({baddy_type.name}) at ({x}, {y}) on {level.name}")
        return baddy

    async def remove_baddy(self, level_name: str, baddy_id: int) -> bool:
        """
        Remove a baddy from a level.

        Args:
            level_name: Level name
            baddy_id: Baddy ID

        Returns:
            True if baddy was removed
        """
        if level_name not in self._baddies:
            return False

        if baddy_id not in self._baddies[level_name]:
            return False

        del self._baddies[level_name][baddy_id]
        return True

    def get_baddy(self, level_name: str, baddy_id: int) -> Optional[Baddy]:
        """Get a baddy by ID."""
        if level_name not in self._baddies:
            return None
        return self._baddies[level_name].get(baddy_id)

    async def handle_baddy_hurt(self, player: 'Player', baddy_id: int, damage: int):
        """
        Handle player hitting a baddy.

        Args:
            player: Player hitting
            baddy_id: Baddy ID
            damage: Damage dealt
        """
        if not player.level:
            return

        baddy = self.get_baddy(player.level.name, baddy_id)
        if not baddy or baddy.dead:
            return

        # Apply damage
        baddy.health -= damage
        baddy.hurt_timer = 0.5  # Hurt state duration
        baddy.mode = BDMODE.HURT

        # Knockback away from player
        dx = baddy.x - player.x
        dy = baddy.y - player.y
        distance = max(0.1, math.sqrt(dx * dx + dy * dy))
        norm_dx = dx / distance
        norm_dy = dy / distance
        baddy.x += norm_dx * 0.5
        baddy.y += norm_dy * 0.5

        # Broadcast hurt. PLO_BADDYHURT's hurtDX/hurtDY carry the knockback
        # direction (normalized -1.0..1.0 per axis), not a position - reuse
        # the same direction vector just applied to the baddy above.
        packet = build_baddy_hurt(baddy_id, norm_dx, norm_dy, damage)
        await self.server.broadcast_to_level(player.level.name, packet)

        logger.debug(f"Baddy {baddy_id} hurt by player {player.id}, health: {baddy.health}")

        # Check death
        if baddy.health <= 0:
            await self.handle_baddy_death(baddy, player)

    async def handle_baddy_death(self, baddy: Baddy, killer: Optional['Player'] = None,
                                 exclude: Optional[set] = None):
        """
        Handle baddy death.

        Args:
            baddy: Baddy that died
            killer: Player that killed it (if any)
        """
        baddy.dead = True
        baddy.death_time = time.time()
        baddy.mode = BDMODE.DEAD

        # Broadcast death
        packet = baddy.build_props_packet()
        await self.server.broadcast_to_level(
            baddy.level_name, packet, exclude=exclude
        )

        # Spawn drop
        level = self.server.world.get_level(baddy.level_name)
        if level and hasattr(self.server, 'item_manager'):
            drops = BADDY_DROPS.get(baddy.baddy_type, [(0, 0.5)])  # Default: green rupee 50%
            roll = random.random()
            cumulative = 0.0
            for item_type, probability in drops:
                cumulative += probability
                if roll < cumulative:
                    from .protocol.constants import LevelItemType
                    await self.server.item_manager.spawn_item(
                        level, baddy.x, baddy.y,
                        LevelItemType(item_type)
                    )
                    break

        logger.info(f"Baddy {baddy.id} died, killed by player {killer.id if killer else 'unknown'}")

        # `compusdied` (scripting-gs1-events.md: "Triggers when all of the
        # baddies in the level have died") - check after this baddy's own
        # death is recorded above, so a level with exactly one baddy fires
        # it on that baddy's own death.
        if level is not None and not any(not b.dead for b in self.get_baddies_on_level(baddy.level_name)):
            npc_mgr = getattr(self.server, 'npc_manager', None)
            if npc_mgr is not None and hasattr(npc_mgr, 'on_baddies_cleared'):
                await npc_mgr.on_baddies_cleared(level)

    async def _respawn_baddy(self, baddy: Baddy):
        """Respawn a dead baddy."""
        baddy.dead = False
        baddy.health = baddy.max_health
        baddy.x = baddy.spawn_x
        baddy.y = baddy.spawn_y
        baddy.mode = BDMODE.HUNT

        await self._broadcast_baddy_props(baddy)
        logger.debug(f"Baddy {baddy.id} respawned")

    async def handle_explosion(self, level_name: str, x: float, y: float,
                                radius: float, damage: int):
        """
        Handle explosion affecting baddies.

        Args:
            level_name: Level name
            x: Explosion X
            y: Explosion Y
            radius: Explosion radius
            damage: Explosion damage
        """
        if level_name not in self._baddies:
            return

        for baddy_id, baddy in list(self._baddies[level_name].items()):
            if baddy.dead:
                continue

            dx = baddy.x - x
            dy = baddy.y - y
            distance = math.sqrt(dx * dx + dy * dy)

            if distance < radius:
                # Apply damage
                baddy.health -= damage
                if baddy.health <= 0:
                    await self.handle_baddy_death(baddy, None)
                else:
                    # Knockback
                    knockback = 2.0 * (1 - distance / radius)
                    baddy.x += (dx / max(0.1, distance)) * knockback
                    baddy.y += (dy / max(0.1, distance)) * knockback
                    baddy.mode = BDMODE.HURT
                    baddy.hurt_timer = 0.5
                    await self._broadcast_baddy_props(baddy)

    async def check_arrow_hit(self, level_name: str, x: float, y: float,
                               damage: int, player_id: int) -> bool:
        """
        Check if an arrow hits a baddy.

        Args:
            level_name: Level name
            x: Arrow X
            y: Arrow Y
            damage: Arrow damage
            player_id: Shooting player ID

        Returns:
            True if a baddy was hit
        """
        if level_name not in self._baddies:
            return False

        for baddy_id, baddy in self._baddies[level_name].items():
            if baddy.dead:
                continue

            dx = abs(baddy.x - x)
            dy = abs(baddy.y - y)

            if dx < 1.0 and dy < 1.0:
                player = self.server.get_player(player_id)
                await self.handle_baddy_hurt(player, baddy_id, damage) if player else None
                return True

        return False

    async def handle_hit(self, level_name: str, obj_id: int, power: int, player_id: int):
        """
        Handle a hit on an object (might be a baddy).

        Args:
            level_name: Level name
            obj_id: Object ID
            power: Hit power
            player_id: Hitting player ID
        """
        player = self.server.get_player(player_id)
        if player:
            await self.handle_baddy_hurt(player, obj_id, power)

    def get_baddies_on_level(self, level_name: str) -> List[Baddy]:
        """Get all baddies on a level."""
        return list(self._baddies.get(level_name, {}).values())

    def clear_level(self, level_name: str):
        """Clear all baddies from a level."""
        self._baddies.pop(level_name, None)

    async def send_level_baddies(self, player: 'Player', level: 'Level'):
        """
        Send all baddies on a level to a player.

        Args:
            player: Player to send to
            level: Level to send baddies from
        """
        for baddy in self.get_baddies_on_level(level.name):
            if not baddy.dead:
                # First sighting for this player: include verses too.
                await player.send_raw(baddy.build_props_packet(include_verses=True))
