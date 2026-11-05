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

from .audience import (
    BOMB_BLAST_NPCS,
    FIRESPY_HIT_PLAYERS,
)
from .ap_system import kill_penalty
from .tiletypes import get_tile_type
from .protocol.constants import PLO, PLPROP
from reborn_protocol import PLSTATUS
from .protocol.packets import (
    PacketBuilder,
    build_bomb_add,
    build_bomb_del,
    build_arrow_add,
    build_explosion,
    build_hurt_player,
    build_fire_spy,
    build_push_away,
    build_throw_carried,
    build_hit_objects,
    build_board_modify,
    build_board_modify2,
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

# FourPlay TInitStatics.cpp:1502: bush/lift blocks, in column order.
_BLAST_BLOCKS = (
    ((0x02, 0x12, 0x03, 0x13), (0x2a5, 0x2b5, 0x2a6, 0x2b6)),
    ((0x1a4, 0x1b4, 0x1a5, 0x1b5), (0x2a7, 0x2b7, 0x2a8, 0x2b8)),
    ((0x200, 0x210, 0x201, 0x211), (0x70a, 0x71a, 0x70b, 0x71b)),
    ((0x2ac, 0x2bc, 0x2ad, 0x2bd), (0x6ea, 0x6fa, 0x6eb, 0x6fb)),
    ((0x22, 0x32, 0x23, 0x33), (0x72a, 0x73a, 0x72b, 0x73b)),
    ((0x3de, 0x3ee, 0x3df, 0x3ef), (0x72a, 0x73a, 0x72b, 0x73b)),
)


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
    in_flight: bool = False
    landing_position: Optional[Tuple[float, float]] = None
    flight_path: Tuple[Tuple[str, float, float], ...] = ()

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
    speed: float = 20.0  # TClient.cpp:3653: DOUBLE_004023d8 = 1 tile/50ms tick
    max_distance: float = 128.0  # GServer Level.cpp:2629
    distance: float = 0.0
    power: int = 1
    rebound_ticks: int = -1

    @property
    def size(self) -> Tuple[float, float]:
        # FourPlay TInitStatics.cpp:1072: sprites 107..110, scaled by 1/16.
        return (0.625, 1.875) if self.direction % 2 == 0 else (1.875, 0.625)

    @property
    def expired(self) -> bool:
        """Check whether flight has reached the travel cap (6.4s at 20 Hz)."""
        return self.distance >= self.max_distance


@dataclass
class Explosion:
    """Represents an explosion effect."""
    x: float
    y: float
    radius: float
    power: int
    player_id: int
    level_name: str


@dataclass
class ThrownObject:
    player: 'Player'
    level: 'Level'
    x: float
    y: float  # Ground position; the rendered sprite follows a separate arc.
    direction: int
    carry_type: Optional[CarryObjectType]
    ticks_left: int = 9  # FourPlay TPlayer.cpp:7831.
    bomb: Optional[Bomb] = None


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
        self._held_bombs: Dict[int, Bomb] = {}
        self._arrows: Dict[str, Dict[int, Arrow]] = {}  # level_name -> {arrow_id: Arrow}
        self._throws: List[ThrownObject] = []

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
        self.arrow_damage = 1  # Half hearts
        self.sword_damage = [0, 1, 2, 3, 4]  # Damage by sword power level
        # Deviation from FourPlay TPlayer.cpp:2166: bots need automatic revival.
        self.respawn_time = 3.0  # Fallback when no revival input arrives.

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
        for bombs in list(self._bombs.values()):
            expired_bombs = [bomb for bomb in list(bombs.values()) if bomb.expired]

            # Expire only server-maintained level objects
            for bomb in expired_bombs:
                await self._expire_bomb(bomb)

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

        for thrown in list(self._throws):
            if await self._update_throw(thrown):
                if thrown in self._throws:
                    self._throws.remove(thrown)
                if (thrown.bomb is not None
                        and self._held_bombs.get(thrown.player.id) is thrown.bomb):
                    height = 2.375 - .2 * (9 - thrown.ticks_left)
                    water_x, water_y = math.floor(thrown.x + 1), math.floor(thrown.y - height + 1)
                    if (0 <= water_x < thrown.level.WIDTH and 0 <= water_y < thrown.level.HEIGHT
                            and get_tile_type(thrown.level.get_tile(water_x, water_y)) in (11, 12)):
                        self._held_bombs.pop(thrown.player.id, None)
                    else:
                        thrown.bomb.x, thrown.bomb.y = thrown.x, thrown.y - height + 1.5

        # Clean up expired invincibility
        expired_invincible = [
            pid for pid, expire in self._invincible.items()
            if current_time > expire
        ]
        for pid in expired_invincible:
            del self._invincible[pid]

    async def handle_bomb_add(self, player: 'Player', x: float, y: float,
                               power: int, time_left: float = 3.0, *,
                               timer_ticks: Optional[int] = None) -> Optional[Bomb]:
        """
        Handle a player placing a bomb.

        Args:
            player: Player placing the bomb
            x: X position in tiles
            y: Y position in tiles
            power: Bomb type and player-owner flag byte
            time_left: Fuse time in seconds for server callers
            timer_ticks: Original client byte, relayed unchanged when supplied

        Returns:
            The created Bomb, or None if failed
        """
        if not player.level:
            return None

        power &= 0x0f
        if timer_ticks is not None:
            time_left = (timer_ticks + 1) * 0.05

        # Check if player has bombs
        held = self._held_bombs.get(player.id)
        points = list(held.flight_path) if held is not None else []
        if held is not None and (held.in_flight or held.landing_position is not None):
            points.append((held.level_name, held.x, held.y))
            if held.landing_position is not None:
                points.append((held.level_name, *held.landing_position))
        matches = [(name, bx, by) for name, bx, by in points
                   if abs(x - bx) <= 1 and abs(y - by) <= 1]
        if matches:
            # TServerCarry.cpp:235: placement belongs to the bomb's level,
            # even when the thrower stayed in the previous segment.
            destination, _, _ = min(matches, key=lambda point: (
                point[0] != held.level_name, (x - point[1]) ** 2 + (y - point[2]) ** 2))
            self._held_bombs.pop(player.id)
            # TServerCarry.cpp:245: the client reports the landing placement.
            self._throws[:] = [obj for obj in self._throws if obj.bomb is not held]
            held.x, held.y = x, y
            held.level_name = destination
            held.in_flight = False
            held.flight_path = ()
            held.created_at = time.time()
            held.power = power & 7
            if timer_ticks is not None:
                held.time_left = time_left
            else:
                timer_ticks = max(0, round(held.time_left / 0.05) - 1)
            self._bombs.setdefault(held.level_name, {})[held.id] = held
            await self.server.broadcast_to_level(
                held.level_name, build_bomb_add(player.id, x, y, power, timer_ticks),
                exclude={player.id})
            return held
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
            power=power & 7,
            time_left=time_left
        )

        # Store bomb
        if player.level.name not in self._bombs:
            self._bombs[player.level.name] = {}
        self._bombs[player.level.name][bomb_id] = bomb

        # Broadcast bomb to level
        if timer_ticks is None:
            timer_ticks = max(0, round(time_left / 0.05) - 1)
        packet = build_bomb_add(player.id, x, y, power, timer_ticks)
        await self.server.broadcast_to_level(
            player.level.name, packet, exclude={player.id}
        )

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
        packet = build_bomb_del(x, y)
        await self.server.broadcast_to_level(
            level_name, packet, exclude={player.id}
        )
        if level_name not in self._bombs:
            return

        # Find bomb at position
        for bomb_id, bomb in list(self._bombs[level_name].items()):
            if abs(bomb.x - x) < 0.5 and abs(bomb.y - y) < 0.5:
                # TPlayer.cpp:1983-2003: any lying bomb can be carried, fuse intact.
                del self._bombs[level_name][bomb_id]
                bomb.in_flight = False
                bomb.landing_position = None
                bomb.time_left = max(.05, bomb.time_left - (time.time() - bomb.created_at))
                self._held_bombs[player.id] = bomb
                break

    def discard_held_bomb(self, player_id):
        """Release retained state when its carrier disconnects or warps."""
        bomb = self._held_bombs.pop(player_id, None)
        if bomb is not None:
            self._throws[:] = [obj for obj in self._throws if obj.bomb is not bomb]
        return bomb

    async def handle_held_bomb_explosion(self, player, x, y, radius, power):
        bomb = self.discard_held_bomb(player.id)
        if bomb is None:
            return False
        bomb.x, bomb.y = x, y
        if not bomb.in_flight:
            bomb.level_name = player.level.name
        player.carrysprite = -1
        await player.send_props({PLPROP.CARRYSPRITE: -1})
        # Other clients removed their copies on pickup. The carrier already
        # exploded locally: relay once, excluding it (GServer-v2
        # PlayerClientPackets.cpp msgPLI_EXPLOSION:829-844).
        power = (power & 7) | 8
        await self.server.broadcast_to_level(
            bomb.level_name, build_explosion(player.id, x, y, radius, power),
            exclude={player.id})
        level = self.server.world.get_level(bomb.level_name)
        if level is not None:
            await self.blast_level_objects(level, x, y, radius, power, player.id)
        return True

    async def _expire_bomb(self, bomb: Bomb):
        """Expire server bomb state and affect only level objects."""
        self._bombs.get(bomb.level_name, {}).pop(bomb.id, None)
        level = self.server.world.get_level(bomb.level_name)
        if not level:
            return

        # FourPlay TServerLevel.cpp:1310-1337, 1691-1725: EVERY client ticks
        # and explodes its own copy; chain detonation is local too (1497-1500).
        # Receivers call putBomb (TClient.cpp:3562-3576), which unconditionally
        # enables ticking (TServerLevel.cpp:1087-1104).
        # TExplosion.cpp:48-96 hurts only the victim's local player, whose
        # CURPOWER prop reports the result. No timed-path sendExplosion call
        # (TClient.cpp:1386). GServer-v2 Level.cpp:682-702 runs NPC effects
        # without sending PLO_BOMBDEL/PLO_EXPLOSION or hurting players.
        crosses = [(bomb.x, bomb.y, 2)]
        if bomb.power == 2:
            crosses = [(bomb.x + dx, bomb.y + dy, 2)
                       for dx in (-2, 2) for dy in (-2, 2)]
            crosses.append((bomb.x, bomb.y, 4))
        await self.blast_level_objects(
            level, bomb.x, bomb.y, crosses[-1][2], bomb.power,
            bomb.player_id, crosses=crosses)

    async def blast_level_objects(self, level: 'Level', x: float, y: float,
                                  radius: int, power: int, player_id: int = 0,
                                  *, crosses=None):
        """Update the board, NPCs and baddies; clients own player damage."""
        power &= 7
        damage = 2 if power == 3 else 1
        if crosses is None:
            crosses = [(x, y, radius)]
        # FourPlay TServerLevel.cpp:1624: cells step two tiles along each arm.
        # putExplosion (1494) has no wall test; even cells beyond walls exist.
        cells = set()
        for cx, cy, arm_radius in crosses:
            cells.add((cx, cy))
            for dx, dy in ((0, -2), (-2, 0), (0, 2), (2, 0)):
                cells.update((cx + dx * n, cy + dy * n)
                             for n in range(1, arm_radius + 1))
        await self._destroy_blast_blocks(level, cells)

        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.handle_explosion(
                level.name, x, y, radius, damage)

        # GServer-v2 Level.cpp addExplosion: NPC hurtAndPush fires EXPLODED;
        # keep both NPC damage and the event when the server timer expires.
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is not None and hasattr(npc_mgr, 'get_npcs_on_level'):
            attacker = None
            if hasattr(self.server, 'get_player'):
                attacker = self.server.get_player(player_id)
            for npc in self.server.audience.npcs_near(
                    level, x, y, radius, BOMB_BLAST_NPCS):
                npc.hearts = max(0.0, npc.hearts - damage / 2.0)
                if hasattr(npc, 'mark_dirty'):
                    npc.mark_dirty()
                if hasattr(npc_mgr, 'on_npc_exploded'):
                    await npc_mgr.on_npc_exploded(npc, attacker)

    async def _destroy_blast_blocks(self, level: 'Level', cells):
        # FourPlay TServerLevel.cpp:1542: replace only complete matching blocks.
        offsets = ((0, 0), (0, 1), (1, 0), (1, 1))
        touched = {(math.floor(x + dx + 0.5), math.floor(y + dy + 0.5))
                   for x, y in cells for dx, dy in offsets}
        for tx, ty in sorted(touched):
            for original, replacement in _BLAST_BLOCKS:
                for index, (ox, oy) in enumerate(offsets):
                    if level.get_tile(tx, ty) != original[index]:
                        continue
                    x, y = tx - ox, ty - oy
                    if not (0 <= x < level.WIDTH - 1 and
                            0 <= y < level.HEIGHT - 1):
                        continue
                    if tuple(level.get_tile(x + dx, y + dy)
                             for dx, dy in offsets) != original:
                        continue
                    for (dx, dy), tile in zip(offsets, replacement):
                        level.set_tile(x + dx, y + dy, tile)
                    builder = PacketBuilder()
                    for tile in (replacement[0], replacement[2],
                                 replacement[1], replacement[3]):
                        builder.write_gshort(tile)
                    tiles = builder.build()
                    gmap = self.server.world.get_gmap_for_level(level.name)
                    if gmap is None:
                        packet = build_board_modify(x, y, 2, 2, tiles)
                    else:
                        packet = build_board_modify2(gmap[1], gmap[2], x, y, 2, 2, tiles)
                    await self.server.broadcast_to_level(level.name, packet)

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

        packet = build_arrow_add(player.id, x, y, flags, sprite, power)
        await self.server.broadcast_to_level(
            player.level.name, packet, exclude={player.id}
        )

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
            direction=direction,
            power=power,
        )

        # Store arrow
        if player.level.name not in self._arrows:
            self._arrows[player.level.name] = {}
        self._arrows[player.level.name][arrow_id] = arrow

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
        move_speed = min(arrow.speed * 0.05, arrow.max_distance - arrow.distance)

        arrow.x += dx * move_speed
        arrow.y += dy * move_speed
        arrow.distance += move_speed

        if arrow.rebound_ticks >= 0:
            # FourPlay TServerFlying.cpp:403: an ordinary shield rebound
            # lasts four further ticks and cannot hurt a player.
            arrow.rebound_ticks -= 1
            if arrow.rebound_ticks < 0:
                self._arrows.get(arrow.level_name, {}).pop(arrow.id, None)
                return

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

        if arrow.rebound_ticks >= 0:
            return

        width, height = arrow.size
        # FourPlay TServerFlying.cpp:135: strict sprite/body rectangle overlap.
        for player in self.server.audience.players_on_level(
                arrow.level_name, exclude={arrow.player_id}):
            if not (arrow.x + width > player.x + 0.5 and
                    player.x + 2.5 > arrow.x and
                    arrow.y + height > player.y + 1.0 and
                    player.y + 3.0 > arrow.y):
                continue
            # FourPlay TServerFlying.cpp:141: idle/walk, opposite flight
            # direction, any positive shield, and projectile power <= 1.
            shield = getattr(player, 'shield_power', 0)
            if (getattr(player, 'gani', 'idle') in ('idle', 'walk') and
                    player.direction == (arrow.direction + 2) % 4 and
                    shield > 0 and arrow.power <= 1):
                arrow.direction = (arrow.direction + 2) % 4
                if shield == 1:
                    arrow.rebound_ticks = 3
                else:
                    # FourPlay TServerFlying.cpp:181: mirror shields reverse
                    # the flight axis and retain a live projectile.
                    arrow.player_id = player.id
                return
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
        # A life ends once, even after the hit invincibility has expired.
        if player.hearts <= 0 or getattr(player, 'client_status', 0) & PLSTATUS.DEAD:
            return

        # Check invincibility
        if player.id in self._invincible:
            if time.time() < self._invincible[player.id]:
                return

        # Apply damage
        old_hearts = player.hearts
        player.hearts = max(0, player.hearts - (damage / 2.0))

        logger.debug(f"Player {player.id} took {damage/2.0} damage: {old_hearts} -> {player.hearts}")

        # Grant invincibility for the classic hurt window: 40 ticks = 2.0 s in
        # the official client (FourPlay TPlayer::hurtPlayer, TPlayer.cpp:6545;
        # pyReborn hurt.py HURT_SECONDS). The server window must not be shorter
        # or a second hit inside the client's immunity still lands.
        self._invincible[player.id] = time.time() + 2.0

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

    async def credit_kill(self, killer: 'Player', victim: 'Player'):
        """Credit one killer for the victim's current death."""
        if killer is victim or getattr(victim, 'kill_credited', False):
            return
        # Reserve credit before any await so claims cannot double-count it.
        victim.kill_credited = True
        if victim.level and victim.level.is_sparring_zone:
            # GServer-v2/server/src/player/packets/PlayerClientPackets.cpp:420-462
            # updates spar ratings here; rating changes are out of scope.
            return
        killer.kills += 1
        await killer.send_props({PLPROP.KILLSCOUNT: killer.kills})
        # GServer-v2/server/src/player/packets/PlayerClientPackets.cpp:466-487:
        # kill_penalty in ap_system.py uses the same integer-division AP
        # formula, zero clamp, victim-AP exemption, and threshold timer.
        if self.server.config.apsystem:
            result = kill_penalty(killer.ap, victim.ap, self.server.config.aptime)
            if result is not None:
                killer.ap, killer.ap_counter = result
                await killer.send_props({PLPROP.ALIGNMENT: killer.ap})
        logger.info(f"  Killed by {killer.nickname}")

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
        if getattr(player, 'pending_revive', None):
            return
        # Identity also prevents an old fallback from reviving a later life.
        revival = object()
        player.pending_revive = revival
        player.kill_credited = False
        logger.info(f"Player {player.id} ({player.nickname}) died")

        # Update stats
        player.deaths += 1
        await player.send_props({PLPROP.DEATHSCOUNT: player.deaths})

        if killer_id is not None:
            killer = self.server.get_player(killer_id)
            if killer:
                await self.credit_kill(killer, player)

        # Trigger NPC death event (optional hook).
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is not None and hasattr(npc_mgr, 'on_player_dies'):
            await npc_mgr.on_player_dies(player, killer_id)

        # Respawn after delay. Keep a strong reference in self._respawn_tasks
        # (see comment on that attribute) so the task can't be garbage
        # collected before it fires.
        task = asyncio.create_task(self._respawn_player(player, revival))
        self._respawn_tasks.add(task)
        task.add_done_callback(self._respawn_tasks.discard)

    async def _respawn_player(self, player: 'Player', revival):
        """Fallback for clients that send no revival props within three seconds."""
        await asyncio.sleep(self.respawn_time)
        if getattr(player, 'pending_revive', None) is revival:
            await self.revive_player(player)

    async def revive_player(self, player: 'Player'):
        """Restore and warp a pending life once, for input and timer revival."""
        if not getattr(player, 'pending_revive', None):
            return
        # Claim revival before any await so input and fallback cannot both run.
        player.pending_revive = None
        player.kill_credited = False
        if not player.connected:
            return

        # TPlayer.cpp:5581: restart HP depends on alignment.
        ap = getattr(player, 'ap', 50)
        player.hearts = (3.0 if ap <= 19 else min(player.max_hearts, 5.0)
                         if ap <= 39 else player.max_hearts)
        player.client_status = getattr(player, 'client_status', 0) & ~PLSTATUS.DEAD
        # Give both revival paths the same three-second protection window.
        self.set_invincible(player.id, 3.0)
        await player.send_props({
            PLPROP.CURPOWER: int(player.hearts * 2),
            PLPROP.STATUS: player.client_status,
        })
        # Revival input calls loadStartLevel(false) (TPlayer.cpp:2166).
        await player.warp(
            self.server.config.start_level,
            self.server.config.start_x,
            self.server.config.start_y
        )
        self.set_invincible(player.id, 3.0)
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

        # Damage players at position (1.5-tile box, sender excluded)
        for other in self.server.audience.players_near(
                player.level.name, x, y, 1.5, FIRESPY_HIT_PLAYERS,
                exclude={player.id}):
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

        npc_mgr = getattr(self.server, 'npc_manager', None)
        # FourPlay TServerPlayer.cpp:1067: ground origin for remote throws.
        thrown = ThrownObject(
            player, player.level, player.x + 0.5, player.y + 1.0,
            int(direction) & 3, _SPRITE_TO_TYPE.get(carrysprite)
        )
        if player.id in self._held_bombs:
            thrown.bomb = self._held_bombs.get(player.id)
            if thrown.bomb is not None:
                thrown.bomb.in_flight = True
                # TServerCarry.cpp:237: bomb placement is sprite y + 1.5.
                path = ThrownObject(player, thrown.level, thrown.x, thrown.y,
                                    thrown.direction, thrown.carry_type)
                points = [(path.level.name, path.x, path.y - 2.375 + 1.5)]
                dx, dy = ((0, -1), (-1, 0), (0, 1), (1, 0))[path.direction]
                for tick in range(1, 10):
                    path.x += dx
                    path.y += dy
                    path.ticks_left -= 1
                    self._handoff_throw(path)
                    points.append((path.level.name, path.x,
                                   path.y - (2.375 - .2 * tick) + 1.5))
                thrown.bomb.flight_path = tuple(points)
        self._throws.append(thrown)

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

    def _is_throw_wall(self, thrown: ThrownObject) -> bool:
        level, x, y = self._throw_probe(thrown)
        tx, ty = math.floor(x + 0.0001), math.floor(y + 0.0001)
        if not (0 <= tx < level.WIDTH and 0 <= ty < level.HEIGHT):
            return True
        # FourPlay TServerLevel.cpp:2655: type 20 bypasses all throw walls.
        tile_type = get_tile_type(level.get_tile(tx, ty))
        if tile_type == 20:
            return False
        for bomb in self._bombs.get(level.name, {}).values():
            if bomb.x + 0.25 <= x < bomb.x + 3.0 and bomb.y <= y < bomb.y + 2.0:
                return True
        for chest in level.get_chest_defs():
            if chest['x'] <= x < chest['x'] + 2 and chest['y'] <= y < chest['y'] + 2:
                return True
        # FourPlay TServerLevel.cpp:741: blocking tile types, including beds.
        return tile_type > 19 or tile_type == 4

    def _throw_probe(self, thrown):
        level, x, y = thrown.level, thrown.x + 1, thrown.y + 1
        adjacent = getattr(self.server.world, 'get_adjacent_levels', None)
        if adjacent is None:
            return level, x, y
        direction = ('w' if x < 0 else 'e' if x >= level.WIDTH
                     else 'n' if y < 0 else 's' if y >= level.HEIGHT else None)
        if direction is None:
            return level, x, y
        neighbors = adjacent(level.name)
        name = neighbors.get(direction) if isinstance(neighbors, dict) else None
        target = self.server.world.get_level(name) if name else None
        if target is None:
            return level, x, y
        dx, dy = {'w': (-1, 0), 'e': (1, 0), 'n': (0, -1), 's': (0, 1)}[direction]
        return target, x - dx * level.WIDTH, y - dy * level.HEIGHT

    async def _update_throw(self, thrown: ThrownObject) -> bool:
        if (thrown.bomb is not None
                and self._held_bombs.get(thrown.player.id) is not thrown.bomb):
            return True
        # FourPlay TServerCarry.cpp:118 and TInitStatics.cpp:1167: one
        # ground tile per tick; the sprite's vertical arc does not affect hits.
        dx, dy = ((0, -1), (-1, 0), (0, 1), (1, 0))[thrown.direction]
        thrown.x += dx
        thrown.y += dy
        thrown.ticks_left -= 1
        handed_off = self._handoff_throw(thrown)
        if thrown.bomb is not None:
            if self._held_bombs.get(thrown.player.id) is not thrown.bomb:
                return True
            height = 2.375 - .2 * (9 - thrown.ticks_left)
            thrown.bomb.x, thrown.bomb.y = thrown.x, thrown.y - height
            thrown.bomb.level_name = thrown.level.name
            thrown.bomb.landing_position = thrown.x, thrown.y - height + 1.5
        if handed_off:
            return False
        if self._is_throw_wall(thrown):
            return True

        # Keep server NPC pelt events (GServer-v2 Level.cpp:2715, 2x2 search).
        hit = False
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is not None:
            for npc in npc_mgr.get_npcs_on_level(thrown.level):
                if (getattr(npc, 'visible', True) and
                        abs((npc.x + 0.5) - (thrown.x + 1.0)) < 1.5 and
                        abs((npc.y + 0.5) - (thrown.y + 1.0)) < 1.5):
                    await npc.hurtAndPush(2, (dx, dy), ScriptEventType.WASPELT,
                                          thrown.player, thrown.carry_type)
                    hit = True
        if hit:
            return True

        # FourPlay TServerCarry.cpp:181: strict ground/player distances;
        # damage is one heart, reduced to half at HP <= 1, for every sprite.
        for player in self.server.audience.players_on_level(
                thrown.level.name, exclude={thrown.player.id}):
            if (getattr(player, 'hearts', 0) <= 0 or
                    self.is_invincible(player.id)):
                continue
            if (abs(thrown.x - (player.x + 0.5)) < 2.0 and
                    abs(thrown.y - (player.y + 1.0)) < 2.0):
                await self.apply_damage(
                    player, 1 if player.hearts <= 1.0 else 2,
                    dx, dy, DamageType.OTHER, thrown.player.id
                )
                return True
        return thrown.ticks_left <= 0

    def _handoff_throw(self, thrown):
        # TServerCarry.cpp:133-164: transfer to a side level before hit probes.
        adjacent = getattr(getattr(self.server, 'world', None), 'get_adjacent_levels', None)
        if adjacent is None:
            return False
        level = thrown.level
        height = 2.375 - .2 * (9 - thrown.ticks_left)
        sprite_y = thrown.y - height
        dx, dy = ((0, -1), (-1, 0), (0, 1), (1, 0))[thrown.direction]
        outside = (dx < 0 and thrown.x <= 0
                   or dx > 0 and thrown.x >= level.WIDTH
                   or dy < 0 and sprite_y <= 0
                   or dy > 0 and sprite_y >= level.HEIGHT)
        if not outside:
            return False
        neighbors = adjacent(level.name)
        name = neighbors.get(('n', 'w', 's', 'e')[thrown.direction]) if isinstance(neighbors, dict) else None
        target = self.server.world.get_level(name) if name else None
        if target is None:
            return False
        thrown.x -= dx * level.WIDTH
        thrown.y -= dy * level.HEIGHT
        thrown.level = target
        return True

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
        self._held_bombs = {pid: bomb for pid, bomb in self._held_bombs.items()
                            if bomb.level_name != level_name}
