"""
pygserver.combat - Combat system management

Handles bombs, arrows, damage, explosions, and player death mechanics.
Based on GServer-v2 combat implementation.
"""

import asyncio
import logging
import math
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
    build_throw_carried,
    build_hit_objects,
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


class CarryObjectSprite(IntEnum):
    BUSH = 1
    STONE = 3
    VASE = 5
    SIGN = 7
    BLACKSTONE = 201
    NPC = 251


class CarryObjectType(IntEnum):
    BUSH = 2
    STONE = 3
    VASE = 4
    SIGN = 5
    BLACKSTONE = 10
    NPC = 11
    PLAYER = 12


class ScriptEventType(str):
    WASPELT = 'waspelt'


_SPRITE_TO_TYPE = {
    CarryObjectSprite.BUSH: CarryObjectType.BUSH,
    CarryObjectSprite.STONE: CarryObjectType.STONE,
    CarryObjectSprite.VASE: CarryObjectType.VASE,
    CarryObjectSprite.SIGN: CarryObjectType.SIGN,
    CarryObjectSprite.BLACKSTONE: CarryObjectType.BLACKSTONE,
    CarryObjectSprite.NPC: CarryObjectType.NPC,
}


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

        # Pending respawn tasks, keyed by nothing in particular - just held
        # so the event loop's *weak* reference to a fire-and-forget task
        # (asyncio docs: "Save a reference to the result of this function,
        # to avoid a task disappearing mid-execution") can't get the
        # respawn coroutine garbage-collected between the death and the
        # warp. Losing it silently stranded the player on the old level
        # forever (no player-left broadcast, no new-level roster) until an
        # unrelated later warp happened to run the real leave/arrive flow -
        # this was the "stale ghost player" bug live 2-bot testing found.
        self._respawn_tasks: Set[asyncio.Task] = set()
        self._thrown_npc_tasks: Set[asyncio.Task] = set()

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

        # Consume bomb; push the new count so the client's inventory tracks
        # (the headless client doesn't self-decrement)
        player.bombs -= 1
        await player.send_props({PLPROP.BOMBSCOUNT: player.bombs})

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
                    await player.send_props({PLPROP.BOMBSCOUNT: player.bombs})
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

        # Damage NPCs in radius and fire `exploded` (scripting-gs1-events.md:
        # "Triggered when an NPC is touched by an explosion" - GServer-v2
        # Level.cpp:2137/2142/2192 hurtAndPush(..., ScriptEventType::EXPLODED)).
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is not None and hasattr(npc_mgr, 'get_npcs_on_level'):
            attacker = None
            if hasattr(self.server, 'get_player'):
                attacker = self.server.get_player(bomb.player_id)
            for npc in npc_mgr.get_npcs_on_level(level):
                if not getattr(npc, 'visible', True):
                    continue
                dx = npc.x - bomb.x
                dy = npc.y - bomb.y
                distance = (dx * dx + dy * dy) ** 0.5
                if distance < radius:
                    npc.hearts = max(0.0, npc.hearts - damage / 2.0)
                    if hasattr(npc, 'mark_dirty'):
                        npc.mark_dirty()
                    if hasattr(npc_mgr, 'on_npc_exploded'):
                        await npc_mgr.on_npc_exploded(npc, attacker)

        logger.debug(f"Bomb detonated at ({bomb.x}, {bomb.y}) with radius {radius}")

    async def handle_arrow_add(self, player: 'Player', x: float, y: float,
                                flags: int, sprite: int = 0,
                                power: int = 1) -> Optional[Arrow]:
        """
        Handle a player firing an arrow.

        Args:
            player: Player firing the arrow
            x: Starting X position
            y: Starting Y position
            flags: raw PLI_ARROWADD flags byte (bit0-1 direction, bit2
                reflect, bit3 fromPlayer) - see GServer-v2 msgPLI_ARROWADD.
            sprite: arrow sprite id, passed through to the relay
            power: arrow power, passed through to the relay

        Returns:
            The created Arrow, or None if failed
        """
        if not player.level:
            return None

        # Check if player has arrows
        if player.arrows <= 0:
            return None

        # Consume arrow; push the new count so the client's inventory tracks
        player.arrows -= 1
        await player.send_props({PLPROP.ARROWSCOUNT: player.arrows})

        direction = flags & 0x03

        # Create arrow
        arrow_id = self._next_arrow_id
        self._next_arrow_id += 1

        # Simulate flight from the server's own tracked player.x/y, NOT the
        # wire-reported x/y. player.x/y is kept authoritative and always in
        # the current level's LOCAL 0-63 space by _handle_player_props (every
        # movement update stores it there, gmap or not - see player.warp()'s
        # PLO_PLAYERWARP2 use of local coords for gmap segments). The client-
        # reported x/y, on the other hand, is whatever coordinate frame the
        # client happens to be using for its own rendering - on a GMAP,
        # pyReborn's Client.player.x/y are WORLD coordinates (local + grid*64,
        # unlike Client.move()'s explicit local_x/local_y conversion), so a
        # PLI_ARROWADD sent while standing on a gmap segment carried e.g.
        # x=94 for a level whose Level.WIDTH is 64 - the bounds check below
        # saw that as instantly out-of-map and dropped the arrow before it
        # ever reached the player-hit check, so PvP arrows always dealt zero
        # damage on gmap levels (ammo still decremented since that happens
        # above, unconditionally). Trusting our own authoritative position
        # instead - matching the "server is authoritative here by design"
        # policy in _update_arrow's docstring - fixes flight/collision
        # regardless of what frame the firing client's x/y happens to be in.
        arrow = Arrow(
            id=arrow_id,
            player_id=player.id,
            level_name=player.level.name,
            x=player.x,
            y=player.y,
            direction=direction
        )

        # Store arrow
        if player.level.name not in self._arrows:
            self._arrows[player.level.name] = {}
        self._arrows[player.level.name][arrow_id] = arrow

        # Broadcast arrow to level
        packet = build_arrow_add(player.id, x, y, flags, sprite, power)
        await self.server.broadcast_to_level(player.level.name, packet)

        logger.debug(f"Player {player.id} fired arrow at ({x}, {y}) direction {direction}")
        return arrow

    async def _update_arrow(self, arrow: Arrow, level: 'Level'):
        """
        Update arrow position and check for collisions.

        Note: pygserver deliberately keeps this server-side flight/damage
        simulation rather than trusting client-reported hits like GServer-v2
        does - the server is authoritative here by design, and our QA client
        (game_tester) depends on server-driven arrow damage. Don't remove it
        to "match" upstream.

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

        # Check wall collision. Use the level's own bounds instead of a bare
        # 64 (Level.WIDTH/HEIGHT today, but this is the source of truth) and
        # math.floor instead of int(), which truncates towards zero and would
        # let e.g. x=-0.5 pass through as tile 0 rather than going out of
        # bounds.
        tile_x = math.floor(arrow.x)
        tile_y = math.floor(arrow.y)
        if tile_x < 0 or tile_x >= level.WIDTH or tile_y < 0 or tile_y >= level.HEIGHT:
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
        arrow_consumed = False
        if hasattr(self.server, 'baddy_manager'):
            arrow_consumed = await self.server.baddy_manager.check_arrow_hit(
                arrow.level_name, arrow.x, arrow.y, self.arrow_damage, arrow.player_id
            )

        # Check NPC collision + fire `wasshot` (scripting-gs1-events.md:
        # "Triggers when an NPC was shot with an arrow" - GServer-v2
        # Level.cpp:2653 hurtAndPush(..., ScriptEventType::WASSHOT, arrow->from)).
        # Only player-fired arrows reach this loop today: _c_shootarrow (an
        # NPC firing an arrow via GS1) only ever broadcasts a cosmetic
        # PLO_ARROWADD and never registers a real flight/hit Arrow here, so
        # `source` is always "player" in practice - "baddy"/"npc" are still
        # recognised by GS1Host (shotbybaddy/shotbynpc) for if/when a baddy-
        # or NPC-fired arrow ever gets real flight simulation.
        if not arrow_consumed:
            npc_mgr = getattr(self.server, 'npc_manager', None)
            if npc_mgr is not None and hasattr(npc_mgr, 'get_npcs_on_level'):
                for npc in npc_mgr.get_npcs_on_level(level):
                    if not getattr(npc, 'visible', True):
                        continue
                    if abs(npc.x - arrow.x) < 1.0 and abs(npc.y - arrow.y) < 1.0:
                        npc.hearts = max(0.0, npc.hearts - self.arrow_damage / 2.0)
                        if hasattr(npc, 'mark_dirty'):
                            npc.mark_dirty()
                        if hasattr(npc_mgr, 'on_npc_wasshot'):
                            shooter = None
                            if hasattr(self.server, 'get_player'):
                                shooter = self.server.get_player(arrow.player_id)
                            await npc_mgr.on_npc_wasshot(npc, 'player', shooter)
                        arrow_consumed = True
                        break

        if arrow_consumed and arrow.level_name in self._arrows:
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
        # You can't sword yourself — a playtester hurt itself with
        # attack&pid=<own id>. Real damage always comes from another player.
        if target_id == attacker.id:
            return

        target = self.server.get_player(target_id)
        if not target or not target.level:
            return

        # Must be on same level
        if attacker.level != target.level:
            return

        # Sanity range check. GServer-v2 relays PLI_HURTPLAYER blindly
        # (damage is client-authoritative in classic), but that lets a
        # modified client hurt anyone anywhere on the level; 6 tiles is
        # generous for sword reach + movement latency.
        if abs(attacker.x - target.x) > 6.0 or abs(attacker.y - target.y) > 6.0:
            logger.debug(f"Rejecting hurt from {attacker.id} on {target_id}: "
                         f"out of range")
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

        # Send hurt packet to the victim ONLY (GServer-v2 msgPLI_HURTPLAYER,
        # PlayerClientPackets.cpp:811-829, calls victim->sendPacket(...)
        # directly - there's no level broadcast). PLO_HURTPLAYER carries no
        # victim id, so broadcasting it made every bystander's client think
        # *they* were the one hurt.
        packet = build_hurt_player(attacker_id or 0, int(knockback_x),
                                   int(knockback_y), damage)
        await player.send_raw(packet)

        # Tell the victim their new heart total. PLO_HURTPLAYER only carries
        # knockback, not the resulting hearts, and pygserver is authoritative
        # for hearts here (we decremented player.hearts above) — without this
        # the client's health bar stays frozen at its old value until a
        # respawn/pickup happens to resend CURPOWER, so a hit "does nothing"
        # visibly even though damage landed. (This is what the old "PvP damage
        # not applied" report actually was; a playtester surfaced it again.)
        await player.send_props({PLPROP.CURPOWER: int(player.hearts * 2)})

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
        player.deaths += 1
        await player.send_props({PLPROP.DEATHSCOUNT: player.deaths})

        if killer_id is not None:
            killer = self.server.get_player(killer_id)
            if killer:
                killer.kills += 1
                await killer.send_props({PLPROP.KILLSCOUNT: killer.kills})
                logger.info(f"  Killed by {killer.nickname}")

        # Trigger NPC death event (optional hook).
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is not None and hasattr(npc_mgr, 'on_player_dies'):
            await npc_mgr.on_player_dies(player, killer_id)

        # Respawn after delay. Keep a strong reference in self._respawn_tasks
        # (see comment on that attribute) so the task can't be garbage
        # collected before it fires.
        task = asyncio.create_task(self._respawn_player(player))
        self._respawn_tasks.add(task)
        task.add_done_callback(self._respawn_tasks.discard)

    async def _respawn_player(self, player: 'Player'):
        """
        Respawn a player after death.

        Args:
            player: Player to respawn
        """
        await asyncio.sleep(self.respawn_time)

        if not player.connected:
            return

        # Restore health and tell the client (CURPOWER = hearts * 2), so it
        # leaves the death state instead of staying locked at 0 hearts.
        player.hearts = player.max_hearts
        await player.send_props({PLPROP.CURPOWER: int(player.hearts * 2)})

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

    async def handle_throw_carried(self, player: 'Player', direction: int,
                                    carrysprite: int):
        """Relay and simulate a client throw using server-owned state."""
        if not player.level:
            return

        # Relay the throw to other players on the level (GServer-v2
        # msgPLI_THROWCARRIED, PlayerClientPackets.cpp:332-336:
        # sendPacketToOneLevelPart(PLO_THROWCARRIED >> id, ..., { m_id })).
        # This was never sent before, so other clients never saw the object
        # leave the thrower's hands.
        packet = build_throw_carried(player.id)
        await self.server.broadcast_to_level(
            player.level.name, packet, exclude={player.id}
        )

        velocities = ((0.0, -0.85), (-0.85, 0.0),
                      (0.0, 0.85), (0.85, 0.0))
        velocity = velocities[int(direction) & 3]
        x, y = player.x + 0.5, player.y + 1.0
        carry_type = _SPRITE_TO_TYPE.get(carrysprite)
        fly_duration = 0
        hit = False
        npc_mgr = getattr(self.server, 'npc_manager', None)
        while fly_duration < 10 and not hit:
            x += velocity[0] * 2
            y += velocity[1] * 2
            fly_duration += 2
            if npc_mgr is not None:
                for npc in npc_mgr.get_npcs_on_level(player.level):
                    # A thrown item's search rectangle is 2x2 tiles.
                    if abs((npc.x + 0.5) - (x + 1.0)) < 1.5 and \
                            abs((npc.y + 0.5) - (y + 1.0)) < 1.5:
                        await npc.hurtAndPush(2, velocity, ScriptEventType.WASPELT,
                                              player, carry_type)
                        hit = True

        if int(carrysprite) == CarryObjectSprite.NPC:
            npc_id = getattr(player, 'npc_id', 0)
            carried_npc = npc_mgr.get_npc(npc_id) if npc_mgr and npc_id else None
            if carried_npc is not None:
                carried_npc.x, carried_npc.y = player.x, player.y
                carried_npc.visible = True
                carried_npc.mark_dirty()
                task = asyncio.create_task(
                    self._move_thrown_npc(carried_npc, player, int(direction) & 3)
                )
                self._thrown_npc_tasks.add(task)
                task.add_done_callback(self._thrown_npc_tasks.discard)
            player.npc_id = 0

    async def _move_thrown_npc(self, npc, player: 'Player', direction: int):
        dx, dy = ((0.0, -0.9), (-0.9, 0.0),
                  (0.0, 0.9), (0.9, 0.0))[direction]
        for _ in range(10):
            npc.x += dx
            npc.y += dy
            npc.mark_dirty()
            await asyncio.sleep(0.05)
        npc.direction = direction
        npc.gani = 'idle'
        npc.mark_dirty()
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is not None:
            await npc_mgr.on_npc_wasthrown(npc, player)

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
                                  power: float, npc_id: Optional[int] = None):
        """
        Handle PLI_HITOBJECTS packet — the client reporting its sword swing
        landed at (x, y). This is the real server-side sword-hit-detection
        path: unlike the GS1 `hitobjects` COMMAND (gs1_host._c_hitobjects,
        which upstream defines as a pure client-notification broadcast with
        no server-side hit logic), this handler is the one that actually
        probes for NPCs at the location and applies damage server-side,
        matching GServer-v2's msgPLI_HITOBJECTS (PlayerClientPackets.cpp:
        1017-1044): decrement the NPC's health and fire `washit`.

        Baddy sword hits are a SEPARATE client packet (PLI_BADDYHURT ->
        BaddyManager.handle_baddy_hurt, wired in player._handle_baddy_hurt)
        and are intentionally not duplicated here — upstream's classic
        baddies and this NPC lookup are two unrelated code paths too.

        Args:
            player: Player whose sword swing landed
            x: Hit X position (tiles)
            y: Hit Y position (tiles)
            power: Damage in HEARTS (already /2-scaled off the wire byte by
                the caller, matching msgPLI_HITOBJECTS's own `power/2.0f`)
            npc_id: Optional npc id the client itself reports the hit came
                from (rare - forwarded to the relay only; upstream doesn't
                use it for hit detection either, see the packet handler)
        """
        if not player.level:
            return

        # Relay a notification to nearby OTHER players so their clients can
        # show a hit effect too (GServer-v2 sendPacketToNearby(..., {m_id})
        # excludes the sender, who already knows locally that it swung).
        if hasattr(self.server, 'broadcast_to_level'):
            packet = build_hit_objects(player.id, int(power * 2), x, y, npc_id)
            await self.server.broadcast_to_level(
                player.level.name, packet, exclude={player.id}
            )

        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is None or not hasattr(npc_mgr, 'get_npcs_on_level'):
            return
        for npc in npc_mgr.get_npcs_on_level(player.level):
            if not getattr(npc, 'visible', True):
                continue
            if abs(npc.x - x) < 1.0 and abs(npc.y - y) < 1.0:
                npc.hearts = max(0.0, npc.hearts - power)
                if hasattr(npc, 'mark_dirty'):
                    npc.mark_dirty()
                if hasattr(npc_mgr, 'on_npc_washit'):
                    await npc_mgr.on_npc_washit(npc, player)

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
