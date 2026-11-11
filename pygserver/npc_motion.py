"""NPC move queue: timed, wall-checked movement shared with clients as PLO_MOVE(2).

Ports GServer-v2 NPC::addMoveToQueue / processMoveQueue / getMoveQueuePacketData
(NPC.cpp:307-548 at origin/beta4, bdc52c48). Everything here is synchronous
state; NPCManager (npc.py) drives it from the tick loop and does the
broadcasting.

Units. Move endpoints are GLOBAL integer pixels (level-local pixels plus the
level's gmap offset, 1024 px per segment), as in the reference. NPC.x / NPC.y
stay level-local float tiles; every pixel is an exact multiple of 1/16 tile.

Bounds. The reference steps a move in 50 ms slices, which lets a script freeze
the loop with `move 1,0,100000,0;`. Here a duration is rejected when negative
or not finite and clamped to MAX_DURATION_MS (what the 28767-tick wire field can
announce); a move without the blockcheck bit jumps to the state the slicing
would have reached (`_jump`, proven equal in the tests); a blockcheck move steps
slice by slice but at most SLICE_BUDGET slices per tick step, and a call
advances by at most MAX_DURATION_MS. Flushing a move (a non-append move that
ends the queued ones, a full queue) is script-driven and so draws on one small
wall-probe budget per NPC (WALL_BUDGET, refilled by the tick loop): once it is
spent the flushed moves jump to their ends without wall tests, so a script loop
cannot turn each `move` into hundreds of probes. A queue holds MAX_QUEUE moves; the move that would
overflow it first finishes the whole queue (`finish_queue`), as the client does
when its own copy reaches that size (pyreborn/npc_movement.py).

Deliberate differences from the reference:
  * `move` accepts 2 or 3 script arguments (missing ones read 0); the reference
    throws unless it gets exactly four (GS1Commands.cpp:1681-1694).
  * The 0.6 s guard between a sword-gani rectangle washit and the client's
    PLI_HITOBJECTS washit (NPCManager.sword_hit_recent) can swallow a legitimate
    client-only hit that follows a rectangle hit inside one swing.
  * A script's `x = ...` / `y = ...` and the `x` / `y` getters work on the
    segment-local tile here; the reference uses the global tile on a gmap
    (Character.h:65, NPC.cpp:1870-1935). A non-finite `x =` value is dropped
    (`script_coordinate` returns None) where the reference would store 0.
  * The CACHE_NEARBY flush distance (addMoveToQueue) uses a single frame: the
    NPC's position at the moment of the call, not a position projected along
    the moves already queued.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional, Tuple

from . import collision

# NPCMove option bits (NPC.h:244-248). Only five bits are stored; the client's
# 0x20 / 0x40 are dropped, exactly like the reference's bitset<5>.
CACHE_NEARBY = 1
APPEND = 2
BLOCKCHECK = 4
INFORM_WHEN_DONE = 8
APPLY_DIRECTION = 16
OPTION_MASK = 0x1F

SLICE_MS = 50                   # NPC.cpp:388-391
SEGMENT_PX = 1024
MAX_TICKS = 28767               # the wire's tick field tops out here
MAX_DURATION_MS = MAX_TICKS * SLICE_MS
MAX_QUEUE = 100                 # pyreborn/npc_movement.py:32
SLICE_BUDGET = 400              # wall-checked slices per tick step (20 s of motion)
WALL_BUDGET = 50                # wall probes script-driven flushes may spend per NPC per tick
_DBL_EPSILON = 2.220446049250313e-16

# Where a blockcheck move probes for a wall, as the offset of the probed box's
# anchor edge: facing 0 top-centre, 1 left-centre, 3 right-centre, else
# bottom-centre (NPC.cpp:437-447).
#
# For a character NPC the probed box is its 32x32 event box at the sprite
# origin + (8, 16), which puts the left and right probes half a tile away from
# the decompiled official client (left (0.5, 1.5) / right (3.0, 1.5) there). We
# follow the reference SERVER (owner decision 2026-11-10).
CHARACTER_BOX = (8, 16, 32, 32)


@dataclass
class NPCMove:
    origin: Tuple[int, int]
    destination: Tuple[int, int]
    duration_ms: int
    options: int = 0
    elapsed_ms: int = 0
    on_complete: Optional[Callable[[], None]] = None
    sent: bool = False          # PLO_MOVE(2) already went to the level

    def current_position(self) -> Tuple[int, int]:
        """NPCMove::getCurrentPosition (NPC.h:252-261): the lerp, truncated
        to whole pixels per axis; the destination once the time is up."""
        if self.elapsed_ms >= self.duration_ms:
            return self.destination
        percent = self.elapsed_ms / self.duration_ms
        return (int(_lerp(self.origin[0], self.destination[0], percent)),
                int(_lerp(self.origin[1], self.destination[1], percent)))


def _lerp(a: float, b: float, t: float) -> float:
    """std::lerp (libstdc++)."""
    a, b = float(a), float(b)
    if (a <= 0 and b >= 0) or (a >= 0 and b <= 0):
        return t * b + (1 - t) * a
    if t == 1:
        return b
    x = a + t * (b - a)
    if (t > 1) == (b > a):
        return x if b < x else b
    return x if x < b else b


def new_queue() -> Deque[NPCMove]:
    return deque()


def wall_budget(npc) -> int:
    return getattr(npc, "_wall_budget", WALL_BUDGET)


def refill_wall_budget(npc) -> None:
    npc._wall_budget = WALL_BUDGET


# -- positions ------------------------------------------------------------------

def level_origin_px(server, level) -> Tuple[int, int]:
    """Pixel offset of `level` inside its gmap (0, 0 off a gmap)."""
    return collision.TileProbe(server, level).origin_px


def global_position_px(npc, server) -> Tuple[int, int]:
    ox, oy = level_origin_px(server, npc.level)
    return (collision.tiles_to_pixels(npc.x) + ox,
            collision.tiles_to_pixels(npc.y) + oy)


# -- queueing -------------------------------------------------------------------

def queue_move(npc, server, delta_px: Tuple[int, int], duration: float,
               options: int) -> Optional[NPCMove]:
    """NPC::addMoveToQueue (NPC.cpp:307-357). `duration` is seconds (float32
    in the reference), `options` the raw uint8 from the script.

    A negative or non-finite duration is rejected (None, nothing queued); a
    longer one than MAX_DURATION_MS is clamped. A full queue is finished first."""
    duration = float(duration)
    if not math.isfinite(duration) or duration < 0:
        return None
    queue = npc.move_queue
    if len(queue) >= MAX_QUEUE:
        finish_queue(npc, server)
    options = int(options) & OPTION_MASK
    duration_ms = min(MAX_DURATION_MS,
                      math.trunc(collision._f32(min(duration, MAX_DURATION_MS / 1000.0)) * 1000))
    origin = queue[-1].destination if queue else global_position_px(npc, server)
    move = NPCMove(origin=origin,
                   destination=(origin[0] + int(delta_px[0]),
                                origin[1] + int(delta_px[1])),
                   duration_ms=duration_ms, options=options)

    finish_all = False
    if not options & (CACHE_NEARBY | APPEND) and queue:
        finish_all = True
    elif options & CACHE_NEARBY and queue:
        # A cached move still far (> 5 tiles) from where the NPC stands ends
        # everything queued so far.
        here = global_position_px(npc, server)
        distance = collision._f32(math.hypot(
            (move.destination[0] - here[0]) / 16.0,
            (move.destination[1] - here[1]) / 16.0))
        finish_all = distance > 5.0

    if finish_all:
        # Pop all but the last move, then run the last one to its end so its
        # completion events fire (NPC.cpp:345-353).
        while len(queue) > 1:
            front = queue.popleft()
            if front.on_complete:
                front.on_complete()
        if queue:
            _, npc._wall_budget = _advance(
                npc, server, queue[0].duration_ms, wall_budget(npc),
                jump_when_spent=True)
    queue.append(move)
    return move


def cancel_moves(npc) -> None:
    """A script (or client) writing x or y ends every running move
    (NPC.cpp:1879,1910) and the manager re-checks the position."""
    queue = getattr(npc, "move_queue", None)
    if queue:
        queue.clear()
    if hasattr(npc, "_position_check"):
        npc._position_check = True


# -- script position writes ----------------------------------------------------------

_INT32_MIN = -2 ** 31


def script_coordinate(server, npc, attr: str, tiles: float) -> Optional[float]:
    """What a script `x = ...` / `y = ...` stores (NPC.cpp:1870-1935).

    The reference does not clamp: it truncates tiles * 16 to an int32 pixel and
    wraps it through toLocalPixelPosition (`% 1024`, C remainder, so a small
    negative stays negative), then re-maps the gmap cell from the global pixel.
    An out-of-range cast is undefined in C++; x86-64 yields INT_MIN, which wraps
    to 0, and so do we. None for a non-finite value.

    On a gmap the pixel stays unwrapped when the segment it points into exists:
    NPCManager.confine_position does the re-map (a roll into that level). A
    pixel pointing at no segment wraps inside the current one, where the
    reference would strand the NPC in a segment nobody can load."""
    if not math.isfinite(tiles):
        return None
    pixel = math.trunc(tiles * 16)
    if not _INT32_MIN <= pixel < 2 ** 31:
        pixel = _INT32_MIN
    info = None
    level = getattr(npc, "level", None)
    world = getattr(server, "world", None)
    if level is not None and world is not None and hasattr(world, "get_gmap_for_level"):
        info = world.get_gmap_for_level(level.name)
    if info is not None:
        gmap, grid_x, grid_y = info
        shift = pixel // SEGMENT_PX
        cell = (grid_x + shift, grid_y) if attr == "x" else (grid_x, grid_y + shift)
        if shift == 0 or gmap.get_level_at(*cell):
            return pixel / 16.0
    return int(math.fmod(pixel, SEGMENT_PX)) / 16.0


# -- stepping -------------------------------------------------------------------

def _probe_point(npc, potential: Tuple[int, int]) -> Tuple[int, int]:
    """The pixel a blockcheck move tests for a wall (global frame)."""
    if collision.npc_is_character(npc):
        ox, oy, width, height = CHARACTER_BOX
        x, y = potential[0] + ox, potential[1] + oy
    else:
        shape = getattr(npc, "shape", None) or (0, 0)
        x, y = potential
        width, height = int(shape[0]), int(shape[1])
    facing = int(getattr(npc, "direction", 2) or 0) & 3
    if facing == 0:
        return x + width // 2, y
    if facing == 1:
        return x, y + height // 2
    if facing == 3:
        return x + width, y + height // 2
    return x + width // 2, y + height


def _start_move(npc, move: NPCMove) -> None:
    """The first-slice events of a move (NPC.cpp:373-390): applydirection."""
    if move.elapsed_ms == 0 and move.options & APPLY_DIRECTION and \
            collision.npc_is_character(npc):
        direction = 0
        if move.destination[0] > move.origin[0]:
            direction = 3
        if move.destination[1] > move.origin[1]:
            direction = 2
        if move.destination[0] < move.origin[0]:
            direction = 1
        if npc.direction != direction:
            npc.direction = direction
            npc.mark_dirty()


def _jump(move: NPCMove, delta_ms: int) -> Tuple[bool, int]:
    """Advance a move WITHOUT a wall test by `delta_ms` (> 0) in one step.

    Equal to running the reference's 50 ms slices, which only differ in where
    they stop: with `span` ms left, the slice sequence 50, 50, ... , remainder
    finishes the move on slice ceil(span / 50) (at least one) after consuming
    min(50 * that, delta_ms); what is left over is the overshoot, which REPLACES
    the remaining delta (NPC.cpp:398-404). Returns (finished, new delta)."""
    span = move.duration_ms - move.elapsed_ms
    if delta_ms < span:
        move.elapsed_ms += delta_ms
        return False, 0
    slices = max(1, -(-span // SLICE_MS))
    consumed = min(slices * SLICE_MS, delta_ms)
    move.elapsed_ms = move.duration_ms
    return True, consumed - span


def _advance(npc, server, delta_ms: int, budget: int,
             jump_when_spent: bool = False) -> Tuple[bool, int]:
    """processMoveQueue for up to `delta_ms`, spending at most `budget`
    wall-checked slices. Once it is spent the loop stops (the rest plays out on
    later ticks), or with `jump_when_spent` the remaining moves are taken
    without wall tests. Returns (a wall ended the queue, budget left)."""
    queue = npc.move_queue
    terminate = False
    moved = False
    origin = level_origin_px(server, npc.level)
    level = npc.level
    while delta_ms > 0 and queue:
        move = queue[0]
        _start_move(npc, move)
        finished = False
        checked = move.options & BLOCKCHECK and level is not None
        if checked and budget <= 0:
            if not jump_when_spent:
                break
            checked = False
        if checked:
            budget -= 1
            step = min(delta_ms, SLICE_MS)
            delta_ms -= step
            remaining = 0
            move.elapsed_ms += step
            if move.elapsed_ms < move.duration_ms:
                remaining = move.duration_ms - move.elapsed_ms
            else:
                delta_ms = move.elapsed_ms - move.duration_ms
                move.elapsed_ms = move.duration_ms
            potential = move.current_position()
            px, py = _probe_point(npc, potential)
            if collision.is_on_wall_pixel(server, level, px - origin[0],
                                          py - origin[1]):
                finished = True
                terminate = True
            else:
                npc.x = (potential[0] - origin[0]) / 16.0
                npc.y = (potential[1] - origin[1]) / 16.0
                moved = True
                finished = remaining <= 0
        else:
            finished, delta_ms = _jump(move, delta_ms)
            potential = move.current_position()
            npc.x = (potential[0] - origin[0]) / 16.0
            npc.y = (potential[1] - origin[1]) / 16.0
            moved = True

        if finished:
            if move.options & INFORM_WHEN_DONE:
                npc._move_events += 1
            if move.on_complete:
                move.on_complete()
            if terminate:
                queue.clear()
            else:
                queue.popleft()

    if moved:
        npc._position_check = True
    if not queue:
        # The move is over: let clients hear the final position. While it is
        # still running they must NOT get X/Y/X2/Y2, which would cancel their
        # own animation (NPC.cpp:493-500; see NPC.build_props_packet).
        npc.mark_dirty()
    return terminate, budget


def process_queue(npc, server, delta_ms: int) -> bool:
    """NPC::processMoveQueue (NPC.cpp:359-500). Advances the queue by
    `delta_ms` (at most MAX_DURATION_MS), testing for a wall BEFORE each slice
    moves; a wall clears the whole queue. Returns True when a wall ended it.

    Side effects on the NPC: x/y, direction (applydirection moves), `_move_events`
    (one per finished informwhendone move, fired by the manager as
    `movementfinished`) and `_position_check` (the manager clamps / rolls it).

    Reference quirk kept: when a move completes, the remaining delta is
    REPLACED by that slice's overshoot (0..49 ms), and an overshoot of exactly
    0 ends the loop even with more moves queued (NPC.cpp:398-404)."""
    if not npc.move_queue:
        return False
    delta_ms = min(int(delta_ms), MAX_DURATION_MS)
    return _advance(npc, server, delta_ms, SLICE_BUDGET)[0]


def finish_queue(npc, server) -> None:
    """Run every queued move to its end (a full queue; the client clears its own
    at the same size). A wall still stops the walk. When the NPC's wall-probe
    budget runs out the rest is taken as reached without further checks."""
    queue = npc.move_queue
    for _ in range(len(queue) + 1):
        if not queue:
            return
        span = max(1, queue[0].duration_ms - queue[0].elapsed_ms)
        terminate, npc._wall_budget = _advance(
            npc, server, span, wall_budget(npc), jump_when_spent=True)
        if terminate:
            return
    if queue:
        origin = level_origin_px(server, npc.level)
        end = queue[-1].destination
        npc.x = (end[0] - origin[0]) / 16.0
        npc.y = (end[1] - origin[1]) / 16.0
        for move in queue:
            if move.options & INFORM_WHEN_DONE:
                npc._move_events += 1
            if move.on_complete:
                move.on_complete()
        queue.clear()
        npc._position_check = True
        npc.mark_dirty()


# -- wire -----------------------------------------------------------------------

def _ticks(move: NPCMove) -> int:
    left = (move.duration_ms - move.elapsed_ms) / 1000.0
    return max(0, min(int(left / 0.05 + _DBL_EPSILON), MAX_TICKS))


def packet_moves(npc, server, only_unsent: bool = False,
                 only_sent: bool = False):
    """The queue as `(x, y, dx, dy, ticks, options)` tuples for the packet
    builders: level-local pixel position and remaining delta (NPC.cpp:512-530)."""
    origin = level_origin_px(server, npc.level)
    moves = []
    for move in npc.move_queue:
        if (only_unsent and move.sent) or (only_sent and not move.sent):
            continue
        current = move.current_position()
        moves.append((current[0] - origin[0], current[1] - origin[1],
                      move.destination[0] - current[0],
                      move.destination[1] - current[1],
                      _ticks(move), move.options))
    return moves


async def send_move_packets(npc_id: int, moves, recipients) -> None:
    """PLO_MOVE2 to clients >= 2.3 and PLO_MOVE to older ones (NPC.cpp:569-584)."""
    if not moves:
        return
    from .handlers.versions import client_version
    from .protocol.packets import build_npc_move, build_npc_move2
    modern = build_npc_move2(npc_id, moves)
    classic = build_npc_move(npc_id, moves)
    for player in recipients:
        await player.send_raw(modern if client_version(player) >= 2.3 else classic)


async def send_queue_to_player(npc, server, player) -> None:
    """NPC::sendMoveQueueToPlayer: a client that just met the NPC learns the
    running queue (Level::sendNPCToPlayer, Level.cpp:1484).

    Only moves the level has already been told about go out. An unsent one is
    still waiting for the next `send_unsent_moves`, which reaches the new
    arrival too, so every move arrives exactly once."""
    await send_move_packets(
        npc.id, packet_moves(npc, server, only_sent=True), [player])


async def send_unsent_moves(npc, server, recipients) -> None:
    """NPC::sendMoveQueueUpdatesToLevel: the moves queued since the last call."""
    moves = packet_moves(npc, server, only_unsent=True)
    for move in npc.move_queue:
        move.sent = True
    await send_move_packets(npc.id, moves, recipients)
