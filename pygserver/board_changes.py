"""Board changes: client tile edits that expire, and what late joiners get.

Reference: GServer-v2 origin/beta4 (25396a18).

A PLI_BOARDMODIFY never rewrites the level's tile array. Level::alterBoard
(level/Level.cpp:1610-1749) appends a LevelBoardChange to the level's list
(:1743) and the level's clients are told about the change; the tile array
("base tiles" here) stays as loaded. The bush/vase drop roll reads it
(PlayerClientPackets.cpp:144-148), so a cut rolls every time, and a level save
writes it (Level::saveLevel, Level.cpp:999-1030, `subLevel->getTiles()`), so a
client edit never reaches the level file. Only level load and server-side
writers (`Level.set_tile`: scripts, combat, NC/RC uploads) change the base.

* Which changes respawn: a change of exactly 2x2 tiles (Level.cpp:1668-1672;
  script updateboard forces it, not modelled here). It remembers the base tiles
  under it (:1709-1722, :1743) and runs a `respawntime` timer
  (Server.h:134, default 15 seconds; 0 starts no timer,
  LevelBoardChange.cpp:29-30). Every other change stays until the level is
  reloaded.
* When the timer runs out the change swaps its new and old tiles and sends
  itself to the level again (LevelBoardChange.cpp:39-50, :107-111), so clients
  see the base tiles come back. The swapped change stays in the list.
* A new change deletes the older ones whose area lies inside its own
  (Level.cpp:1703-1707, rectangleContained at Extents.h:353). Partial overlaps
  coexist and each runs its own timer.
* A player entering the level gets the base board, then every change in the
  list in order (SubLevel::sendBoardChangesToPlayer, Level.cpp:483-530, called
  from PlayerClient.cpp:1523): PLO_BOARDMODIFY2 with the segment's map position
  for clients from 4.0211 on, PLO_BOARDMODIFY before 2.1, and between the two
  PLO_BOARDMODIFY2 on a grid map and PLO_BOARDMODIFY otherwise.
* On a grid map a change, and its respawn, goes to the players of the change's
  segment and of the eight around it (LevelBoardChange.cpp:52-71 and
  PlayerClientPackets.cpp:129-133 -> Server::sendPacketToNearby,
  Server.cpp:2390-2413 -> Level::findPlayersInRangeForData, Level.cpp:3320-3372).
  A neighbouring segment a client preloads (PLI_ADJACENTLEVEL) is sent as its
  base board only (PlayerClientPackets.cpp:994-1014 -> sendStaticLevelData,
  PlayerClient.cpp:1432-1508); its change list follows when the player steps
  onto it (PlayerProps.cpp:905-925 -> sendDynamicLevelData, PlayerClient.cpp:
  1510-1523), which pygserver does through the segment warp.
* Level reload (Level.cpp:653-661) clears the list.

pygserver keeps a live board next to the base tiles (collision and scripts read
it). `Level.alter_board` writes the change to the live board, the expiry writes
the old tiles back, and `Level.set_tile` writes both boards and every entry
over the cell (so a later respawn restores the server's tile).

The live board equals what a joiner rebuilds (base, then the list in order)
everywhere except where an entry respawned under a newer entry that still
covers the cell: there the live board, like the clients that saw the respawn,
shows the respawned tiles, and a joiner's replay shows the newer entry's. The
reference diverges the same way. The next change, server write or eviction
correction over such a cell brings the two back together.

Not in the reference, deliberately: the per-level list is bounded
(MAX_BOARD_CHANGES_PER_LEVEL entries, MAX_BOARD_CHANGE_TILES_PER_LEVEL tiles in
total), because the reference list only shrinks when a later change covers an
entry. A change that would pass the bound evicts the oldest entries until it
fits; it is never refused. Each evicted rectangle is rebuilt as base overlaid
with the entries left (the new change included), written to the live board and
sent like a board change, so present clients and later joiners agree there.
An attacker at the bound therefore causes one relay and one correction per
packet.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

from .protocol.packets import PacketBuilder, build_board_modify, build_board_modify2

if TYPE_CHECKING:
    from .level import Level
    from .server import GameServer

logger = logging.getLogger(__name__)

#: Server.h:134 `respawntime` (SettingCache<uint16_t>, default 15).
DEFAULT_RESPAWN_SECONDS = 15

#: Clients from this version on get board changes as PLO_BOARDMODIFY2
#: (Level.cpp:496-499, CLVER_2_1 / CLVER_4_0211).
MODIFY2_SINCE = 4.0211
MODIFY_ONLY_BEFORE = 2.1

#: Bounds on one level's change list (not in the reference, see above). 1024
#: entries is every 2x2 cell of a level at once; the tile bound keeps the burst
#: a late joiner is sent under two full boards and bounds the per-cell index.
MAX_BOARD_CHANGES_PER_LEVEL = 1024
MAX_BOARD_CHANGE_TILES_PER_LEVEL = 8192

TICK_SECONDS = 0.05

_SIDE = 64


@dataclass(eq=False)
class BoardChange:
    """One LevelBoardChange: the rectangle, what clients see (`tiles`), the
    base tiles it swaps to (`old_tiles`, None when it never respawns) and the
    monotonic time the swap is due (`expires_at`, None when no timer runs).
    `seq` orders the list."""

    x: int
    y: int
    width: int
    height: int
    tiles: List[int]
    old_tiles: Optional[List[int]] = None
    expires_at: Optional[float] = None
    mod_time: float = 0.0
    seq: int = 0

    @property
    def will_respawn(self) -> bool:
        """LevelBoardChange::willRespawn: the timer is running."""
        return self.expires_at is not None

    @property
    def area(self) -> int:
        return self.width * self.height

    def inside(self, x: int, y: int, width: int, height: int) -> bool:
        """rectangleContained(this, rect) (Extents.h:353-362)."""
        return (self.x >= x and self.x + self.width <= x + width
                and self.y >= y and self.y + self.height <= y + height)

    def cells(self) -> Iterable[int]:
        """Cell indexes (y * 64 + x) of the rectangle, row by row."""
        for row in range(self.y, self.y + self.height):
            base = row * _SIDE
            yield from range(base + self.x, base + self.x + self.width)

    def offset(self, cell: int) -> int:
        """Index into `tiles` of a cell inside the rectangle."""
        return (cell // _SIDE - self.y) * self.width + cell % _SIDE - self.x

    def swap(self, now: float) -> None:
        """LevelBoardChange::swapTiles (:107-111) plus the timer stopping."""
        if self.old_tiles is not None:
            self.tiles, self.old_tiles = self.old_tiles, self.tiles
        self.expires_at = None
        self.mod_time = now

    def wire_tiles(self) -> bytes:
        """The tiles as GSHORTs, row by row."""
        builder = PacketBuilder()
        for tile in self.tiles:
            builder.write_gshort(tile)
        return builder.build()


def _tile_at(board: bytearray, cell: int) -> int:
    return board[cell * 2] | (board[cell * 2 + 1] << 8)


class BoardChangeList:
    """One level's change list.

    Entries live in an insertion-ordered dict keyed by sequence number (the
    order a late joiner replays them in). Every cell keeps the entries over it,
    oldest first, so the entries a new rectangle contains, the newest entry
    over a cell and the entries a server write lands under are found from the
    rectangle's own cells. Running timers sit in a heap ordered by expiry.
    Adding costs O(area + entries removed and their area); the tile bound
    keeps the per-cell lists short in total."""

    def __init__(self, max_changes: int = MAX_BOARD_CHANGES_PER_LEVEL,
                 max_tiles: int = MAX_BOARD_CHANGE_TILES_PER_LEVEL):
        self.max_changes = max_changes
        self.max_tiles = max_tiles
        self.clear()

    def clear(self) -> None:
        self._entries: Dict[int, BoardChange] = {}
        self._covering: List[Optional[List[BoardChange]]] = [None] * (_SIDE * _SIDE)
        self._timers: List[Tuple[float, int]] = []
        self._timed = 0
        self._tile_total = 0
        self._next_seq = 0

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(list(self._entries.values()))

    @property
    def tile_total(self) -> int:
        return self._tile_total

    def covering(self, cell: int) -> List[BoardChange]:
        """The entries over a cell, oldest first."""
        return list(self._covering[cell] or ())

    def next_expiry(self) -> Optional[float]:
        """When the earliest running timer is due. The manager asks every
        tick, which is also when the heap sheds the items of deleted entries
        (see _tidy), so it never outgrows the list by more than one tick of
        packets."""
        self._tidy()
        return self._timers[0][0] if self._timers else None

    def _tidy(self) -> None:
        """Deleted entries leave their heap items behind; rebuild the heap once
        they are the majority so it stays proportional to the list."""
        if len(self._timers) > 2 * self._timed + 64:
            self._timers = [item for item in self._timers
                            if (c := self._entries.get(item[1])) is not None
                            and c.expires_at is not None]
            heapq.heapify(self._timers)

    # -- adding ------------------------------------------------------------

    def _contained(self, x: int, y: int, width: int, height: int) -> List[BoardChange]:
        """The entries inside the rectangle (Level.cpp:1703-1707), oldest
        first. Walks the entries over the rectangle's cells, or the list when
        that is shorter."""
        right, bottom = x + width, y + height
        if len(self._entries) <= width * height:
            return [c for c in self._entries.values()
                    if c.x >= x and c.y >= y
                    and c.x + c.width <= right and c.y + c.height <= bottom]
        found = []
        covering = self._covering
        for row in range(y, bottom):
            base = row * _SIDE
            for cell in range(base + x, base + right):
                over = covering[cell]
                if over:
                    for c in over:
                        # Each entry is counted at its top-left cell only.
                        if (c.y * _SIDE + c.x == cell and c.x + c.width <= right
                                and c.y + c.height <= bottom):
                            found.append(c)
        found.sort(key=lambda c: c.seq)
        return found

    def add(self, change: BoardChange, base: bytearray) -> List[BoardChange]:
        """Append `change`, deleting the older entries it contains. When the
        list would pass a bound the oldest entries are evicted until it fits
        (a single change over the tile bound, impossible from a 64x64 level,
        empties the list and is kept anyway). Returns one correction per
        evicted entry: its rectangle rebuilt from the entries left, not in the
        list, for the caller to show and send (see the module notes)."""
        for old in self._contained(change.x, change.y, change.width, change.height):
            self._remove(old)
        evicted = []
        entries = self._entries
        while entries and (len(entries) >= self.max_changes
                           or self._tile_total + change.area > self.max_tiles):
            oldest = next(iter(entries.values()))
            self._remove(oldest)
            evicted.append(oldest)
        self._insert(change)
        if change.expires_at is not None:
            heapq.heappush(self._timers, (change.expires_at, change.seq))
            self._timed += 1
        return [self._rebuild(old, base, change.mod_time) for old in evicted]

    def _rebuild(self, rect: BoardChange, base: bytearray, now: float) -> BoardChange:
        """`rect` as a joiner rebuilds it: per cell the tile of the newest
        entry over it, else the base tile."""
        covering = self._covering
        tiles = []
        for cell in rect.cells():
            over = covering[cell]
            if over:
                top = over[-1]
                tiles.append(top.tiles[top.offset(cell)])
            else:
                tiles.append(_tile_at(base, cell))
        return BoardChange(x=rect.x, y=rect.y, width=rect.width,
                           height=rect.height, tiles=tiles, mod_time=now)

    def _insert(self, change: BoardChange) -> None:
        change.seq = self._next_seq
        self._next_seq += 1
        self._entries[change.seq] = change
        covering = self._covering
        for cell in change.cells():
            over = covering[cell]
            if over is None:
                covering[cell] = [change]
            else:
                over.append(change)      # seq only grows: oldest first
        self._tile_total += change.area

    def _remove(self, change: BoardChange) -> None:
        del self._entries[change.seq]
        covering = self._covering
        for cell in change.cells():
            over = covering[cell]
            over.remove(change)          # identity (eq=False)
            if not over:
                covering[cell] = None
        self._tile_total -= change.area
        if change.expires_at is not None:
            self._timed -= 1     # its heap item is skipped when it comes up

    # -- server writes -----------------------------------------------------

    def patch_cell(self, cell: int, tile: int) -> None:
        """A server write to a cell (Level.set_tile): every entry over it
        shows, and respawns to, the new tile."""
        for change in self._covering[cell] or ():
            i = change.offset(cell)
            change.tiles[i] = tile
            if change.old_tiles is not None:
                change.old_tiles[i] = tile

    # -- expiry ------------------------------------------------------------

    def expire(self, now: float) -> List[BoardChange]:
        """Swap every entry whose timer ran out (LevelBoardChange::update,
        LevelBoardChange.cpp:39-50) and return them, earliest first."""
        done = []
        timers = self._timers
        while timers and timers[0][0] <= now:
            _due, seq = heapq.heappop(timers)
            change = self._entries.get(seq)
            if change is None or change.expires_at is None:
                continue
            change.swap(now)
            self._timed -= 1
            done.append(change)
        self._tidy()
        return done


def _option_uint(config, name: str, default: int) -> int:
    value = getattr(config, name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def respawn_context(server) -> Tuple[float, int]:
    """(now, respawn seconds) for a change made on `server` right now. A server
    without a board-change manager (a stand-in) uses the monotonic clock and the
    reference default."""
    manager = getattr(server, 'board_changes', None)
    clock = manager.clock if manager is not None else time.monotonic
    seconds = _option_uint(getattr(server, 'config', None),
                           'respawntime', DEFAULT_RESPAWN_SECONDS)
    return clock(), seconds


def nearby_level_names(server, level_name: str,
                       gmap_info=False) -> List[str]:
    """`level_name` plus, on a grid map, the loaded segments around it: the
    players Level::findPlayersInRangeForData picks on a gmap (the 3x3 block of
    map positions around the data's position, Level.cpp:3338-3371). Segments
    nobody has loaded are skipped (no player can be on them)."""
    names = [level_name]
    world = getattr(server, 'world', None)
    if gmap_info is False:      # not looked up by the caller
        gmap_info = world.get_gmap_for_level(level_name) if world is not None else None
    if not gmap_info:
        return names
    gmap, map_x, map_y = gmap_info
    level_at = getattr(gmap, 'get_level_at', None)
    loaded = getattr(world, 'get_loaded_level', None)
    if level_at is None or loaded is None:
        return names
    for step_y in (-1, 0, 1):
        for step_x in (-1, 0, 1):
            if not step_x and not step_y:
                continue
            gx, gy = map_x + step_x, map_y + step_y
            if gx < 0 or gy < 0:
                continue
            name = level_at(gx, gy)
            if name and name != level_name and loaded(name) is not None:
                names.append(name)
    return names


async def send_near(server, level_name: str, packet: bytes, gmap_info=False,
                    exclude=None) -> None:
    """Send `packet` to the players of `level_name`, and on a grid map to
    those of the segments around it (Server::sendPacketToNearby,
    Server.cpp:2390-2413; off a map Server::sendPacketToOneLevelPart)."""
    for name in nearby_level_names(server, level_name, gmap_info):
        await server.broadcast_to_level(name, packet, exclude=exclude)


class BoardChangeManager:
    """Expires respawning board changes and builds the packets for them."""

    def __init__(self, server: 'GameServer',
                 clock: Callable[[], float] = time.monotonic):
        self.server = server
        self.clock = clock
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _tick_loop(self):
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Board change tick error")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: Optional[float] = None) -> int:
        """Level::doTimedEvents for board changes (Level.cpp:674-679): swap
        every change whose timer ran out and send it. Each level costs O(1)
        unless something is due. A failed send is logged and the rest still
        go out. Returns how many expired."""
        now = self.clock() if now is None else now
        expired = 0
        for level in list(self.server.world.get_all_levels()):
            due = level.next_board_expiry()
            if due is None or due > now:
                continue
            for change in level.expire_board_changes(now):
                expired += 1
                try:
                    await self.send_to_level(level, change)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Board change respawn send failed on %s",
                                     level.name)
        return expired

    # -- packets -----------------------------------------------------------

    def _map_position(self, level: 'Level') -> Optional[Tuple[int, int]]:
        info = self.server.world.get_gmap_for_level(level.name)
        return (info[1], info[2]) if info else None

    def packet_for(self, level: 'Level', change: BoardChange, version: float = 6.037,
                   live: bool = True, position=False) -> bytes:
        """The packet carrying `change`: the live send uses PLO_BOARDMODIFY on
        a standalone level and PLO_BOARDMODIFY2 on a grid map
        (LevelBoardChange.cpp:52-71); the join replay picks by client version
        (Level.cpp:495-499)."""
        if position is False:
            position = self._map_position(level)
        tiles = change.wire_tiles()
        if live:
            modify2 = position is not None
        elif version >= MODIFY2_SINCE:
            modify2 = True
        elif version < MODIFY_ONLY_BEFORE:
            modify2 = False
        else:
            modify2 = position is not None
        if modify2:
            map_x, map_y = position or (0, 0)
            return build_board_modify2(map_x, map_y, change.x, change.y,
                                       change.width, change.height, tiles)
        return build_board_modify(change.x, change.y, change.width, change.height, tiles)

    async def send_to_level(self, level: 'Level', change: BoardChange) -> None:
        """LevelBoardChange::sendToPlayersOnLevel (:52-71), nobody excluded:
        the level, and on a grid map the segments around it."""
        info = self.server.world.get_gmap_for_level(level.name)
        position = (info[1], info[2]) if info else None
        await send_near(self.server, level.name,
                        self.packet_for(level, change, position=position),
                        gmap_info=info)

    def join_packets(self, level: 'Level', version: float) -> List[bytes]:
        """What a player entering `level` is sent after the base board: every
        change in list order, pending ones with their new tiles."""
        position = self._map_position(level)
        return [self.packet_for(level, change, version, live=False, position=position)
                for change in level.board_changes]
