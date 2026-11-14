"""
pygserver.combat - Combat system management

Handles bombs, arrows, damage, explosions, and player death mechanics.
Based on GServer-v2 combat implementation.
"""

import asyncio
import contextvars
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, Set
from enum import IntEnum

from . import collision
from .board_changes import respawn_context
from .attachments import absolute_position

from .ap_system import kill_penalty
from .spar_rating import glicko_update
from .tiletypes import WALL_TYPES, get_tile_type
from .protocol.constants import PLO, PLPROP
from reborn_protocol import PLSTATUS
from .protocol.packets import (
    PacketBuilder,
    PacketReader,
    build_bomb_add,
    build_bomb_del,
    build_arrow_add,
    build_explosion,
    build_fire_spy,
    build_hurt_player,
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
    # GServer-v2 utilities/CommonTypes.h:485-501. The script-facing value an
    # event carries (`peltwith*`, `explodedwithjolt`), not the wire sprite.
    NONE = 0
    BOMB = 1
    BUSH = 2
    STONE = 3
    VASE = 4
    SIGN = 5
    SUPERBOMB = 6
    JOLTBOMB = 7
    HOTJOLTBOMB = 8
    HOTBOMB = 9
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


# Reference arrow kinds (GServer-v2 LevelArrow.h:41-45). Script arrows that can
# only come from an NPC (shootarrow, shootfireball, shootfireblast, shootnuke).
ARROW_NORMAL, ARROW_FIREBALL, ARROW_FIREBLAST, ARROW_NUKE = 1, 2, 3, 4
# Level::moveArrow (Level.cpp:2750-2819): one 16 px step per 50 ms tick, the
# arrow is dropped after 2 * 1024 px, and its probe box depends on the facing
# (up, left, down, right).
_SCRIPT_ARROW_STEP = 16
_SCRIPT_ARROW_MAX_PIXELS = 2048
_SCRIPT_ARROW_BOX = ((3, 0), (32, 0), (3, 32), (32, 0))
_SCRIPT_ARROW_VECTORS = ((0, -1), (-1, 0), (0, 1), (1, 0))
# Bounds for what a script or a packet can make the server loop over.
MAX_SCRIPT_ARROWS_PER_LEVEL = 64   # live script arrows kept per level
MAX_SCRIPT_ARROWS_TOTAL = 256      # ... and across every level
MAX_EVENT_NPC_PROBES = 1024        # candidate NPC boxes tested per explosion / arrow step
MAX_WIRE_RADIUS = 223              # largest value one GCHAR carries without wrapping
MAX_EXPLOSION_RADIUS = 64          # NPC-side reach: 64 * 2 tiles covers a whole gmap row
MAX_BLAST_DEPTH = 8                # generations of blast -> `exploded` handler -> blast
MAX_BLASTS_PER_LEVEL_TICK = 32     # blasts / spy fires a level takes per 50 ms
BLAST_WINDOW_SECONDS = 0.05
MAX_FIRESPY_PER_SECOND = 10        # PLI_FIRESPY packets honoured per player
MAX_WIRE_TILE = 111.5              # largest tile coordinate x*2 fits in one GCHAR

# Blast generation of the code currently running. Tasks inherit it when they are
# created, so a blast a script schedules from an `exploded` handler is one
# generation deeper than the blast that raised the event.
_blast_depth = contextvars.ContextVar("pygserver_blast_depth", default=0)


def wire_tile_ok(*values) -> bool:
    """True when every tile coordinate survives a one-byte x*2 encoding."""
    return all(isinstance(v, (int, float)) and math.isfinite(v)
               and 0 <= math.floor(v * 2) <= MAX_WIRE_TILE * 2 for v in values)


@dataclass
class ScriptArrow:
    """An arrow an NPC script fired, flown server-side like LevelArrow."""
    id: int
    level_name: str
    npc_id: int
    kind: int
    x: int  # level-local pixels, the arrow's top-left
    y: int
    start_x: int
    start_y: int
    direction: int


def explosion_event_rects(x: float, y: float, radius: int):
    """The two arm tests of Level::addExplosion (Level.cpp:2228-2245) in
    level-local pixels: a 32 px wide column and a 32 px tall row reaching
    `radius * 32` px either side of the explosion's pixel position."""
    px, py = collision.tiles_to_pixels(x), collision.tiles_to_pixels(y)
    reach, span = radius * 32, (1 + 2 * radius) * 32
    return [(px, py - reach, 32, span), (px - reach, py, span, 32)]


def spyfire_event_rect(px: int, py: int, direction: int, length: int):
    """Level::addSpyFire's NPC test (Level.cpp:2294-2312) for a player whose
    top-left is at pixel (px, py)."""
    d = int(direction) & 3
    sx = px + (8 if d in (0, 2) else (-32 if d == 1 else 48))
    sy = py + (3 if d in (1, 3) else (-24 if d == 0 else 35))
    span = (int(length) + 1) * 32
    return (sx - (span if d == 1 else 0), sy - (span if d == 0 else 0),
            32 if d in (0, 2) else span, 32 if d in (1, 3) else span)


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

    _clock = staticmethod(time.monotonic)
    _drop_logged_at = 0.0

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Active projectiles by level
        self._bombs: Dict[str, Dict[int, Bomb]] = {}  # level_name -> {bomb_id: Bomb}
        self._held_bombs: Dict[int, Bomb] = {}
        self._arrows: Dict[str, Dict[int, Arrow]] = {}  # level_name -> {arrow_id: Arrow}
        self._throws: List[ThrownObject] = []
        self._script_arrows: Dict[str, Dict[int, ScriptArrow]] = {}  # NPC-fired
        self._blast_windows: Dict[str, Tuple[float, int]] = {}

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

        # Arrows fired by NPC scripts; a bad one must not stall the tick.
        for level_name, arrows in list(self._script_arrows.items()):
            level = self.server.world.get_level(level_name)
            # One NPC lookup per level per tick, however many arrows fly there.
            grid = self._collision_grid(level) if level is not None and arrows else None
            for arrow in list(arrows.values()):
                try:
                    keep = level is not None and await self._update_script_arrow(
                        arrow, level, grid)
                except Exception:
                    logger.exception("script arrow update failed")
                    keep = False
                if not keep:
                    arrows.pop(arrow.id, None)
            if not arrows:
                self._script_arrows.pop(level_name, None)

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
        # Level.cpp:688-699: NPCs see one radius-1 blast, or for power 2 one
        # radius-4 blast at the bomb and at its four diagonal neighbours.
        event_crosses = [(bomb.x, bomb.y, 1)]
        if bomb.power == 2:
            crosses = [(bomb.x + dx, bomb.y + dy, 2)
                       for dx in (-2, 2) for dy in (-2, 2)]
            crosses.append((bomb.x, bomb.y, 4))
            event_crosses = [(bomb.x + dx, bomb.y + dy, 4)
                             for dx in (-2, 2) for dy in (-2, 2)]
            event_crosses.append((bomb.x, bomb.y, 4))
        await self.blast_level_objects(
            level, bomb.x, bomb.y, crosses[-1][2], bomb.power,
            bomb.player_id, crosses=crosses, event_crosses=event_crosses)

    def reserve_script_blast(self, level_name: str) -> bool:
        """Admit one SCRIPT-originated blast or spy fire (putexplosion, a
        script arrow's impact, spyfire, or any of these chained from an
        `exploded` handler). False when its chain is already MAX_BLAST_DEPTH
        generations deep or the level used up MAX_BLASTS_PER_LEVEL_TICK in the
        current 50 ms window; the caller then drops BOTH the wire packet and
        the server-side effect. Player-originated blasts (bomb and explosion
        packets, timed bombs, client fire-spy) never come through here, so a
        script flood cannot make a player's blast do nothing."""
        depth = _blast_depth.get()
        windows = self.__dict__.setdefault('_blast_windows', {})
        now = self._clock()
        start, count = windows.get(level_name, (now, 0))
        if now - start >= BLAST_WINDOW_SECONDS:
            start, count = now, 0
        if depth >= MAX_BLAST_DEPTH or count >= MAX_BLASTS_PER_LEVEL_TICK:
            windows[level_name] = (start, count)
            if now - self._drop_logged_at >= 1.0:
                self._drop_logged_at = now
                logger.warning("script blast on %s dropped (depth %d, %d this tick)",
                               level_name, depth, count)
            return False
        windows[level_name] = (start, count + 1)
        return True

    @staticmethod
    def _enter_blast():
        """Mark everything run from here as one generation deeper."""
        return _blast_depth.set(_blast_depth.get() + 1)

    @staticmethod
    def _exit_blast(token):
        _blast_depth.reset(token)

    async def blast_level_objects(self, level: 'Level', x: float, y: float,
                                  radius: int, power: int, player_id: int = 0,
                                  *, crosses=None, event_crosses=None,
                                  script: bool = False, admitted: bool = False):
        """Update the board, NPCs and baddies; clients own player damage.

        `crosses` are the (x, y, arm radius) cells that break blocks;
        `event_crosses` are the explosions whose arm rectangles raise
        `exploded` on NPCs (default: the same list). Non-finite positions
        are ignored. A `script` blast is first admitted by
        `reserve_script_blast`, unless the caller already did (`admitted`)
        so it could drop the wire packet with it; a player's blast is never
        dropped but still counts as a generation for chains it starts."""
        if not (isinstance(x, (int, float)) and isinstance(y, (int, float))
                and math.isfinite(x) and math.isfinite(y)):
            return
        if script and not admitted and not self.reserve_script_blast(level.name):
            return
        token = self._enter_blast()
        try:
            await self._blast_level_objects(
                level, x, y, radius, power, player_id, crosses, event_crosses)
        finally:
            self._exit_blast(token)

    async def _blast_level_objects(self, level, x, y, radius, power, player_id,
                                   crosses, event_crosses):
        power &= 7
        # putexplosion's radius is a uint8 (GS1Commands.cpp:1744); this also
        # bounds the cell loop below.
        radius = max(0, min(int(radius), MAX_EXPLOSION_RADIUS))
        damage = 2 if power == 3 else 1
        if crosses is None:
            crosses = [(x, y, radius)]
        if event_crosses is None:
            event_crosses = crosses
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

        # GServer-v2 Level.cpp:2228-2264 addExplosion: every visible NPC whose
        # EVENT box meets the vertical or horizontal arm rectangle gets
        # hurtAndPush(..., EXPLODED, ...) (no player damage, no `washit`);
        # NPC server only (Level.cpp:2230). The reference queues the event once
        # per arm hit and once per explosion, so an NPC in the centre cell, or
        # in two overlapping blasts of one bomb, runs `exploded` twice. Here
        # it runs once per blast call (deliberate deviation).
        if not getattr(getattr(self.server, 'config', None), 'serverside', True):
            return
        attacker = None
        if hasattr(self.server, 'get_player'):
            attacker = self.server.get_player(player_id)
        rects = []
        for cx, cy, arm_radius in event_crosses:
            rects.extend(explosion_event_rects(cx, cy, arm_radius))
        # Level.cpp:2255-2262: a power-3 blast is a joltbomb.
        await self._explode_npcs(
            level, rects, damage,
            CarryObjectType.JOLTBOMB if power == 3 else CarryObjectType.BOMB,
            attacker)

    @staticmethod
    def _coarse_reach(npc):
        """(left, top, right, bottom) pixel extents the NPC's event or
        collision box can occupy, from its position and shape alone. Cheap and
        conservative: a box the exact test accepts is always inside it."""
        try:
            px, py = float(npc.x) * 16, float(npc.y) * 16
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
        shape = getattr(npc, 'shape', None)
        w = h = 48
        if shape and len(shape) >= 2:
            w, h = max(w, int(shape[0])), max(h, int(shape[1]))
        return px - 1, py - 1, px + w + 1, py + h + 1

    def npcs_in_rects(self, level: 'Level', rects, *, collide: bool = False):
        """NPCs whose event box (`collide`: collision box, which an invisible
        or NOBLOCK NPC lacks) meets any of `rects`, level-local pixels,
        inclusive edges. Searches the level and its gmap neighbours like
        Level::findIntersectingNPCsForEvent (Level.cpp:3546-3562). A coarse
        bounding test discards distant NPCs first; only NPCs that pass it count
        towards MAX_EVENT_NPC_PROBES, so crowds elsewhere on the level never
        starve the ones near the blast."""
        npc_mgr = getattr(self.server, 'npc_manager', None)
        if npc_mgr is None or not hasattr(npc_mgr, 'get_npcs_on_level'):
            return []
        nearby = (npc_mgr.nearby_levels(level)
                  if hasattr(npc_mgr, 'nearby_levels') else [(level, (0, 0))])
        box_of = collision.npc_collision_rect if collide else collision.npc_event_rect
        left = min(r[0] for r in rects)
        top = min(r[1] for r in rects)
        right = max(r[0] + r[2] for r in rects)
        bottom = max(r[1] + r[3] for r in rects)
        found, probes = [], 0
        for near, (shift_x, shift_y) in nearby:
            for npc in npc_mgr.get_npcs_on_level(near):
                reach = self._coarse_reach(npc)
                if (reach is None or reach[2] + shift_x < left
                        or reach[0] + shift_x > right
                        or reach[3] + shift_y < top or reach[1] + shift_y > bottom):
                    continue
                probes += 1
                if probes > MAX_EVENT_NPC_PROBES:
                    return found
                box = box_of(npc)
                if box is None:
                    continue
                box = (box[0] + shift_x, box[1] + shift_y, box[2], box[3])
                if any(collision.rects_intersect(rect, box) for rect in rects):
                    found.append(npc)
        return found

    def _collision_grid(self, level: 'Level'):
        """Collision boxes of the NPCs around `level` bucketed into 64 px
        cells, built once per tick for the script arrows flying there. NPCs
        whose box spans more than 16 cells go to a shared overflow list."""
        npc_mgr = getattr(self.server, 'npc_manager', None)
        cells, big = {}, []
        if npc_mgr is None or not hasattr(npc_mgr, 'get_npcs_on_level'):
            return cells, big
        nearby = (npc_mgr.nearby_levels(level)
                  if hasattr(npc_mgr, 'nearby_levels') else [(level, (0, 0))])
        for near, (shift_x, shift_y) in nearby:
            for npc in npc_mgr.get_npcs_on_level(near):
                box = collision.npc_collision_rect(npc)
                if box is None:
                    continue
                box = (box[0] + shift_x, box[1] + shift_y, box[2], box[3])
                entry = (npc, box, (shift_x, shift_y), near)
                x0, x1 = box[0] >> 6, (box[0] + box[2]) >> 6
                y0, y1 = box[1] >> 6, (box[1] + box[3]) >> 6
                if (x1 - x0 + 1) * (y1 - y0 + 1) > 16:
                    big.append(entry)
                    continue
                for cx in range(x0, x1 + 1):
                    for cy in range(y0, y1 + 1):
                        cells.setdefault((cx, cy), []).append(entry)
        return cells, big

    def _grid_hits(self, grid, rect):
        """NPCs in `grid` whose box meets `rect` (at most MAX_EVENT_NPC_PROBES
        candidates examined). The grid is a snapshot from the start of the
        tick, and an event handler may have moved, hidden, removed or warped an
        NPC since, so every candidate is checked again against its LIVE box and
        level before it counts."""
        cells, big = grid
        npc_mgr = getattr(self.server, 'npc_manager', None)
        lookup = getattr(npc_mgr, 'get_npc', None)
        x, y, w, h = rect
        found, seen, probes = [], set(), 0
        candidates = list(big)
        for cx in range(x >> 6, ((x + w) >> 6) + 1):
            for cy in range(y >> 6, ((y + h) >> 6) + 1):
                candidates.extend(cells.get((cx, cy), ()))
        for npc, box, (shift_x, shift_y), near in candidates:
            probes += 1
            if probes > MAX_EVENT_NPC_PROBES:
                break
            if id(npc) in seen or not collision.rects_intersect(rect, box):
                continue
            if getattr(npc, 'level', near) is not near:
                continue
            if lookup is not None and lookup(getattr(npc, 'id', None)) is not npc:
                continue
            live = collision.npc_collision_rect(npc)
            if live is None or not collision.rects_intersect(
                    rect, (live[0] + shift_x, live[1] + shift_y, live[2], live[3])):
                continue
            seen.add(id(npc))
            found.append(npc)
        return found

    async def _explode_npcs(self, level: 'Level', rects, damage: int,
                            carry_type, attacker):
        """NPC::hurtAndPush(..., EXPLODED) for every NPC in `rects`; a damage
        of 0 leaves the NPC's hearts alone and only raises the event."""
        npc_mgr = getattr(self.server, 'npc_manager', None)
        for npc in self.npcs_in_rects(level, rects):
            if damage > 0:
                npc.hearts = max(0.0, npc.hearts - damage / 2.0)
                if hasattr(npc, 'mark_dirty'):
                    npc.mark_dirty()
            if hasattr(npc_mgr, 'on_npc_exploded'):
                await npc_mgr.on_npc_exploded(npc, attacker, carry_type)

    async def add_spy_fire(self, level: 'Level', player: 'Player',
                           length: int, power: int):
        """The NPC half of Level::addSpyFire (Level.cpp:2268-2320): `exploded`
        on every visible NPC whose event box meets the strip in front of the
        player. hurtAndPush only takes hearts from a character NPC that ran
        enabledamagereactions (NPC.cpp:647,661; NPC.h:454 defaults it off, and
        pygserver has no such switch), so hearts are left alone here."""
        if not getattr(getattr(self.server, 'config', None), 'serverside', True):
            return
        token = self._enter_blast()
        try:
            x, y = absolute_position(player)
            rect = spyfire_event_rect(
                collision.tiles_to_pixels(x), collision.tiles_to_pixels(y),
                getattr(player, 'direction', 2), length)
            await self._explode_npcs(
                level, [rect], 0,
                CarryObjectType.JOLTBOMB if power == 3 else CarryObjectType.BOMB,
                player)
        finally:
            self._exit_blast(token)

    # -- arrows fired by NPC scripts ---------------------------------------

    def spawn_script_arrow(self, level: 'Level', npc, direction: int, kind: int,
                           x: float, y: float) -> Optional[ScriptArrow]:
        """Level::addArrow (Level.cpp:1902-1910): register an arrow an NPC
        script fired from tile position (x, y) so it flies and hits server-side.
        NPC server only; at most MAX_SCRIPT_ARROWS_PER_LEVEL per level and
        MAX_SCRIPT_ARROWS_TOTAL overall stay alive. Returns None when the
        arrow is refused (cap, or a position one byte cannot encode)."""
        if not getattr(getattr(self.server, 'config', None), 'serverside', True):
            return None
        if not wire_tile_ok(x, y):
            return None
        arrows = self._script_arrows.setdefault(level.name, {})
        if (len(arrows) >= MAX_SCRIPT_ARROWS_PER_LEVEL
                or sum(map(len, self._script_arrows.values()))
                >= MAX_SCRIPT_ARROWS_TOTAL):
            return None
        px, py = collision.tiles_to_pixels(x), collision.tiles_to_pixels(y)
        arrow = ScriptArrow(
            id=self._next_arrow_id, level_name=level.name,
            npc_id=getattr(npc, 'id', 0), kind=kind, x=px, y=py,
            start_x=px, start_y=py, direction=int(direction) & 3)
        self._next_arrow_id += 1
        arrows[arrow.id] = arrow
        return arrow

    async def _update_script_arrow(self, arrow: ScriptArrow, level: 'Level',
                                   grid=None) -> bool:
        """One Level::moveArrow iteration (Level.cpp:2750-2819); False once
        the arrow is spent. Players are not tested: clients hit themselves
        from the PLO_ARROWADD the script command broadcast."""
        step_x, step_y = _SCRIPT_ARROW_VECTORS[arrow.direction]
        arrow.x += step_x * _SCRIPT_ARROW_STEP
        arrow.y += step_y * _SCRIPT_ARROW_STEP
        if (abs(arrow.x - arrow.start_x) > _SCRIPT_ARROW_MAX_PIXELS
                or abs(arrow.y - arrow.start_y) > _SCRIPT_ARROW_MAX_PIXELS):
            return False

        width, height = _SCRIPT_ARROW_BOX[arrow.direction]
        box = (arrow.x, arrow.y, width, height)
        power = 2 if arrow.kind == ARROW_FIREBALL else 1
        npc_mgr = getattr(self.server, 'npc_manager', None)
        hit = False
        # The firing NPC is skipped (Level.cpp:2796); each other NPC in the box
        # takes the hit and the arrow is spent (hitWall = true, :2800).
        if grid is None:
            grid = self._collision_grid(level)
        for npc in self._grid_hits(grid, box):
            if getattr(npc, 'id', None) == arrow.npc_id:
                continue
            npc.hearts = max(0.0, npc.hearts - power / 2.0)
            if hasattr(npc, 'mark_dirty'):
                npc.mark_dirty()
            if hasattr(npc_mgr, 'on_npc_wasshot'):
                await npc_mgr.on_npc_wasshot(npc, 'npc', None)
            hit = True

        explosive = arrow.kind in (ARROW_FIREBLAST, ARROW_NUKE)
        if not hit and explosive:
            # The wall test is the tile one column right of the arrow's own
            # (Level.cpp:2806-2809, toWholeTilePosition(...).translate(1, 0)).
            probe = collision.TileProbe(self.server, level)
            tile_x = collision.pixel_to_tile(arrow.x, probe.origin_px[0]) + 1
            tile_y = collision.pixel_to_tile(arrow.y, probe.origin_px[1])
            hit = probe.tile_type(tile_x, tile_y) in WALL_TYPES
        if hit and explosive:
            # addExplosion(position, from, radius 1, power 1): Level.cpp:2815-2818.
            await self.blast_level_objects(
                level, arrow.x / collision.PIXELS_PER_TILE,
                arrow.y / collision.PIXELS_PER_TILE, 1, 1, script=True)
        return not hit

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
                    # The reference server does not blast tiles: the client sends
                    # the 2x2 edit as PLI_BOARDMODIFY and Level::alterBoard
                    # records it (PlayerClientPackets.cpp:125, Level.cpp:1610-1749),
                    # so a joiner or returning player is replayed it and it
                    # respawns after `respawntime`.
                    row_major = [replacement[0], replacement[2],
                                 replacement[1], replacement[3]]
                    now, respawn_seconds = respawn_context(self.server)
                    corrections = []
                    change = level.alter_board(x, y, 2, 2, row_major, now,
                                               respawn_seconds,
                                               corrections=corrections)
                    if change is None:
                        continue
                    manager = getattr(self.server, 'board_changes', None)
                    if manager is not None:
                        for sent in [change] + corrections:
                            await manager.send_to_level(level, sent)
                        continue
                    builder = PacketBuilder()
                    for tile in row_major:
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

        # Simulate from the absolute level-local position, including the NPC
        # displacement while attached. ARROWADD axes may be world coordinates
        # on a gmap, so they cannot serve as the flight/collision origin.
        arrow = Arrow(
            id=arrow_id,
            player_id=player.id,
            level_name=player.level.name,
            x=absolute_position(player)[0],
            y=absolute_position(player)[1],
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
            px, py = absolute_position(player)
            if not (arrow.x + width > px + 0.5 and
                    px + 2.5 > arrow.x and
                    arrow.y + height > py + 1.0 and
                    py + 3.0 > arrow.y):
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
        ax, ay = absolute_position(attacker)
        tx, ty = absolute_position(target)
        if abs(ax - tx) > 6.0 or abs(ay - ty) > 6.0:
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
            if self.server.config.dontupdateratingd:
                return
            if killer.remote_ip == victim.remote_ip:
                return
            old_loser = (victim.elo_rating, victim.elo_deviation)
            old_winner = (killer.elo_rating, killer.elo_deviation)
            win_rating, win_dev, lose_rating, lose_dev = glicko_update(
                *old_loser, *old_winner)
            killer.elo_rating, killer.elo_deviation = win_rating, win_dev
            victim.elo_rating, victim.elo_deviation = lose_rating, lose_dev
            if old_loser != (lose_rating, lose_dev):
                await victim.send_props({PLPROP.RATING: (lose_rating, lose_dev)})
            if old_winner != (win_rating, win_dev):
                await killer.send_props({PLPROP.RATING: (win_rating, win_dev)})
            now = int(time.time())
            victim.last_spar_time = now
            killer.last_spar_time = now
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

    async def handle_status_death(self, player: 'Player'):
        """The STATUS prop went from not-DEAD to DEAD (PlayerProps.cpp:573-580).

        Counts the death and drops items, unless the player has no id or no
        level, or stands in a sparring zone. The remaining reference steps
        (leader handoff, lastDeadTime, PLAYERDIES) are handled by
        handle_player_death.
        """
        level = player.level
        if not player.id or level is None or level.is_sparring_zone:
            return
        # Only counted: the reference queues no DEATHS prop here
        # (PlayerProps.cpp:578; the case at :573-598 adds none to the result).
        player.deaths += 1
        item_manager = getattr(self.server, 'item_manager', None)
        if item_manager is not None:
            await item_manager.drop_items_on_death(player)

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

        # The death counter and the item drop belong to the STATUS prop's
        # transition to DEAD (handle_status_death), not to hearts reaching 0.

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

    async def handle_fire_spy(self, player: 'Player', length_power: int):
        """Relay the packed wand effect from the player's current position.

        PlayerClientPackets.cpp:314-327. The reference builds its relay from
        `pPacket.text() + 1`: text() is the buffer start, whose first byte is
        the packet id, so the +1 skips that id and the relay carries the packed
        length/power byte (and anything after it) unchanged. The wand has no
        coordinates on the wire, so no server-side player damage is derived
        from it.
        """
        if not player.level:
            return
        # Throttle: each packet probes NPCs along a strip, so a client may not
        # flood them. Excess packets are dropped without a reply.
        now = self._clock()
        recent = getattr(player, '_firespy_times', None)
        if recent is None:
            recent = player._firespy_times = deque(maxlen=MAX_FIRESPY_PER_SECOND)
        if len(recent) == recent.maxlen and now - recent[0] < 1.0:
            return
        recent.append(now)
        await self.server.broadcast_to_level(
            player.level.name, build_fire_spy(player.id, length_power),
            exclude={player.id})
        # Level::addSpyFire (PlayerClientPackets.cpp:322-326): packed byte is
        # length << 3 | power.
        await self.add_spy_fire(
            player.level, player, (length_power >> 3) & 0x1f, length_power & 7)

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
            player, player.level, absolute_position(player)[0] + 0.5, absolute_position(player)[1] + 1.0,
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
                carried_npc.x, carried_npc.y = absolute_position(player)[0], absolute_position(player)[1]
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
            if (abs(thrown.x - (absolute_position(player)[0] + 0.5)) < 2.0 and
                    abs(thrown.y - (absolute_position(player)[1] + 1.0)) < 2.0):
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
        """Normalize the classic projectile before choosing recipient layouts."""
        await self._relay_shoot(player, shoot_data, modern=False)

    async def handle_shoot2(self, player: 'Player', shoot_data: bytes):
        """Normalize the pixel projectile before choosing recipient layouts."""
        await self._relay_shoot(player, shoot_data, modern=True)

    async def _relay_shoot(self, player, data, *, modern):
        # PlayerClientPackets.cpp:1329-1405; Player.cpp:239-274 serializes both.
        from .handlers.versions import client_version
        if not player.level:
            return
        reader = PacketReader(data)
        if modern:
            x, y, z = (reader.read_gshort() for _ in range(3))
            x, y, z = ((value + 32768) % 65536 - 32768 for value in (x, y, z))
            ox, oy = reader.read_gchar_signed(), reader.read_gchar_signed()
        else:
            reader.read_gint3()  # The incoming source is discarded.
            x, y = reader.read_gchar_signed() * 8, reader.read_gchar_signed() * 8
            z = (reader.read_gchar_signed() - 50) * 16
            info = self.server.world.get_gmap_for_level(player.level.name)
            if info:
                x += info[1] * 1024
                y += info[2] * 1024
            ox = oy = 0
        angle, zangle, power = (reader.read_gchar() for _ in range(3))
        gravity = reader.read_gchar() if modern else 2
        gani_length = reader.read_gshort() if modern else reader.read_gchar()
        gani = reader.read_bytes(gani_length)
        reader.read_gchar()  # The reference consumes all trailing params.
        params = reader.remaining()
        packets = {}
        for use_modern in (False, True):
            builder = (PacketBuilder().write_gchar(PLO.SHOOT2 if use_modern else PLO.SHOOT)
                       .write_gshort(player.id))
            if use_modern:
                builder.write_gshort(x).write_gshort(y).write_gshort(z)
                builder.write_byte(ox + 32).write_byte(oy + 32)
            else:
                builder.write_gint3(0)
                for value in (x, y):
                    remainder = value - int(value / 1024) * 1024
                    builder.write_gchar(int(remainder / 8))
                builder.write_gchar(int(z / 16) + 50)
            builder.write_gchar(angle).write_gchar(zangle).write_gchar(power)
            if use_modern:
                builder.write_gchar(gravity).write_gshort(len(gani))
            else:
                builder.write_gchar(len(gani))
            builder.write_bytes(gani).write_gchar(len(params)).write_bytes(params)
            packets[use_modern] = builder.write_newline().build()
        for other in self.server.audience.players_in_world(
                player.level.name, exclude={player.id}):
            await other.send_raw(packets[client_version(other) >= 5.07])

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
                # A sword gani already fired washit for this swing.
                recent = getattr(npc_mgr, 'sword_hit_recent', None)
                if recent is not None and recent(player, npc):
                    continue
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
        self._script_arrows.pop(level_name, None)
        self.__dict__.get('_blast_windows', {}).pop(level_name, None)
        self._held_bombs = {pid: bomb for pid, bomb in self._held_bombs.items()
                            if bomb.level_name != level_name}
