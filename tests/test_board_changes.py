"""Board change respawn and the bush/vase "old tile" (reference: GServer-v2
origin/beta4 25396a18).

Level::alterBoard (level/Level.cpp:1610-1749), LevelBoardChange
(level/LevelBoardChange.cpp:21-111), SubLevel::sendBoardChangesToPlayer
(Level.cpp:483-530), msgPLI_BOARDMODIFY (player/packets/PlayerClientPackets.cpp
:109-169), Server.h:134 `respawntime` (default 15). The clock is injected, so
no test waits.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from pygserver import board_changes as bc
from pygserver.board_changes import (
    MAX_BOARD_CHANGES_PER_LEVEL, MAX_BOARD_CHANGE_TILES_PER_LEVEL)
from pygserver.config import ServerConfig
from pygserver.level import Level
from pygserver.protocol.constants import PLI, PLO
from tests.test_item_drops import (
    GREEN, ScriptedRng, g, handler_rig, modify_packet, run)

BUSH = 0x002
CUT = 0x1a0


def tile_bytes(tiles):
    """GSHORT per tile, composed by hand."""
    return b"".join(bytes([(t >> 7) + 32, (t & 0x7f) + 32]) for t in tiles)


def modify_wire(x, y, w, h, tiles, packet_id=PLO.BOARDMODIFY):
    return (bytes([packet_id + 32, x + 32, y + 32, w + 32, h + 32])
            + tile_bytes(tiles) + b"\n")


def modify2_wire(map_x, map_y, x, y, w, h, tiles):
    return (bytes([PLO.BOARDMODIFY2 + 32, map_x + 32, map_y + 32, x + 32, y + 32,
                   w + 32, h + 32]) + tile_bytes(tiles) + b"\n")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def board():
    rig, player = handler_rig()
    clock = Clock()
    rig.server.board_changes.clock = clock
    rig.level.set_tile(10, 20, BUSH)
    rig.manager.rng = ScriptedRng(*[1] * 20)
    board.rig, board.player, board.clock = rig, player, clock
    return rig, player, clock


def cut(rig, player, x=10, y=20, tiles=None, w=2, h=2):
    run(player._dispatch_packet(
        PLI.BOARDMODIFY, modify_packet(x, y, w, h, tiles or [CUT] * (w * h))))


def level_packets(rig):
    return [packet for _name, packet, _exclude in rig.sent
            if packet[:1] in (bytes([PLO.BOARDMODIFY + 32]), bytes([PLO.BOARDMODIFY2 + 32]))]


# -- the drop roll reads the base tile ------------------------------------------------

def test_cut_rolls_and_a_second_cut_rolls_again(board):
    rig, player, _clock = board
    cut(rig, player)
    assert rig.item_types() == [GREEN]
    cut(rig, player)                       # no respawn in between: still rolls
    assert rig.item_types() == [GREEN, GREEN]
    assert rig.manager.rng.calls == [(1, 100), (1, 100)]
    assert rig.level.get_tile(10, 20) == CUT and rig.level.get_base_tile(10, 20) == BUSH


def test_set_tile_stays_permanent_for_scripts_and_combat(board):
    rig, player, clock = board
    rig.level.set_tile(30, 30, 0x55)
    assert rig.level.get_tile(30, 30) == 0x55 and rig.level.get_base_tile(30, 30) == 0x55
    cut(rig, player, 30, 30)               # a cut over it respawns to what set_tile left
    clock.now += 15
    run(rig.server.board_changes.tick())
    assert rig.level.get_tile(30, 30) == 0x55


def test_set_tile_under_a_pending_cut_survives_the_respawn(board):
    """A server write (GS1 tiles[], blast blocks) under a pending cut: the
    live board, joiners, the respawn and the save all show the server's tile
    (Level.set_tile patches the entries over the cell)."""
    rig, player, clock = board
    cut(rig, player, 10, 20)
    rig.level.set_tile(10, 20, 0x77)
    assert rig.level.get_tile(10, 20) == 0x77
    assert join_packets(rig, 6.037) == [modify2_wire(0, 0, 10, 20, 2, 2, [0x77] + [CUT] * 3)]
    rig.sent.clear()
    clock.now += 15
    assert run(rig.server.board_changes.tick()) == 1
    assert rig.sent == [("a.nw", modify_wire(10, 20, 2, 2, [0x77, 0, 0, 0]), None)]
    assert rig.level.get_tile(10, 20) == 0x77 and rig.level.get_tile(11, 20) == 0
    assert join_packets(rig, 6.037) == [modify2_wire(0, 0, 10, 20, 2, 2, [0x77, 0, 0, 0])]
    assert saved_copy(rig.level).get_tile(10, 20) == 0x77
    assert replay(rig.level) == rig.level.get_board_packet()


def test_set_tile_under_permanent_and_respawned_changes(board):
    rig, player, clock = board
    cut(rig, player, 10, 20)                                   # 2x2
    cut(rig, player, 9, 21, tiles=[5] * 6, w=3, h=2)           # permanent, overlaps it
    clock.now += 15
    run(rig.server.board_changes.tick())                       # the 2x2 respawned
    rig.level.set_tile(11, 21, 0x33)                           # under both
    rig.level.set_tile(10, 20, 0x44)                           # under the respawned one
    assert [c.tiles for c in rig.level.board_changes] == [
        [0x44, 0, 0, 0x33], [5, 5, 0x33, 5, 5, 5]]
    assert replay(rig.level)[(21 * 64 + 11) * 2] == 0x33
    assert rig.level.get_tile(11, 21) == 0x33 and rig.level.get_tile(10, 20) == 0x44


# -- which changes respawn, and when ---------------------------------------------------

def test_only_a_2x2_change_respawns_after_respawntime(board):
    rig, player, clock = board
    cut(rig, player, 10, 20)                                  # 2x2
    cut(rig, player, 40, 40, tiles=[7], w=1, h=1)             # 1x1: permanent
    cut(rig, player, 50, 10, tiles=[8] * 6, w=3, h=2)         # 3x2: permanent
    by_rect = {(c.x, c.y): c for c in rig.level.board_changes}
    assert by_rect[(10, 20)].will_respawn
    assert not by_rect[(40, 40)].will_respawn and not by_rect[(50, 10)].will_respawn
    clock.now += 14.9
    assert run(rig.server.board_changes.tick()) == 0
    assert rig.level.get_tile(10, 20) == CUT
    clock.now += 0.1
    assert run(rig.server.board_changes.tick()) == 1
    assert rig.level.get_tile(10, 20) == BUSH                 # restored
    assert rig.level.get_tile(40, 40) == 7 and rig.level.get_tile(50, 10) == 8


def test_respawn_sends_the_base_tiles_to_the_whole_level(board):
    rig, player, clock = board
    for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        rig.level.set_tile(10 + dx, 20 + dy, [BUSH, 5, 6, 7][dx + 2 * dy])
    cut(rig, player)
    rig.sent.clear()
    clock.now += 15
    run(rig.server.board_changes.tick())
    assert rig.sent == [("a.nw", modify_wire(10, 20, 2, 2, [BUSH, 5, 6, 7]), None)]
    clock.now += 100                                           # nothing more is sent
    run(rig.server.board_changes.tick())
    assert len(rig.sent) == 1


def test_respawn_uses_map_position_packet_on_a_grid_map(board):
    rig, player, clock = board
    rig.server.world.get_gmap_for_level = lambda name: (object(), 5, 7)
    cut(rig, player)
    rig.sent.clear()
    clock.now += 15
    run(rig.server.board_changes.tick())
    assert rig.sent == [("a.nw", modify2_wire(5, 7, 10, 20, 2, 2, [BUSH, 0, 0, 0]), None)]


def test_respawntime_option_is_read_when_the_change_is_made(board):
    rig, player, clock = board
    rig.config.respawntime = 40
    cut(rig, player)
    clock.now += 39.9
    assert run(rig.server.board_changes.tick()) == 0
    clock.now += 0.1
    assert run(rig.server.board_changes.tick()) == 1


def test_respawntime_zero_starts_no_timer(board):
    rig, player, clock = board
    rig.config.respawntime = 0                                 # LevelBoardChange.cpp:29-30
    cut(rig, player)
    assert not rig.level.board_changes[0].will_respawn
    clock.now += 10_000
    assert run(rig.server.board_changes.tick()) == 0
    assert rig.level.get_tile(10, 20) == CUT


# -- overlapping changes -----------------------------------------------------------------

def test_a_new_change_replaces_older_ones_it_covers(board):
    rig, player, clock = board
    cut(rig, player, 10, 20)
    cut(rig, player, 10, 20)                                   # same rectangle
    assert [(c.x, c.y, c.width) for c in rig.level.board_changes] == [(10, 20, 2)]
    cut(rig, player, 8, 18, tiles=[3] * 25, w=5, h=5)          # covers it
    assert [(c.x, c.y, c.width) for c in rig.level.board_changes] == [(8, 18, 5)]


def test_partial_overlaps_coexist_and_each_runs_its_own_timer(board):
    rig, player, clock = board
    cut(rig, player, 10, 20)
    clock.now += 5
    cut(rig, player, 11, 21)                                   # overlaps, not contained
    assert len(rig.level.board_changes) == 2
    clock.now += 10                                            # first due, second not
    assert run(rig.server.board_changes.tick()) == 1
    assert rig.level.get_tile(10, 20) == BUSH
    clock.now += 5
    assert run(rig.server.board_changes.tick()) == 1


def test_the_old_tiles_come_from_the_base_not_the_edited_board(board):
    rig, player, clock = board
    cut(rig, player, 10, 20, tiles=[1, 2, 3, 4])
    clock.now += 1
    cut(rig, player, 10, 20, tiles=[9, 9, 9, 9])               # replaces the first
    clock.now += 15
    run(rig.server.board_changes.tick())
    assert rig.level.get_tile(10, 20) == BUSH                  # not 1


# -- late joiners ------------------------------------------------------------------------

def join_packets(rig, version):
    return rig.server.board_changes.join_packets(rig.level, version)


def test_a_late_joiner_sees_the_pending_change(board):
    rig, player, clock = board
    cut(rig, player)
    assert join_packets(rig, 6.037) == [modify2_wire(0, 0, 10, 20, 2, 2, [CUT] * 4)]
    assert rig.level.get_base_board_packet()[(20 * 64 + 10) * 2:][:2] == bytes([BUSH, 0])


def test_the_join_packet_follows_the_client_version(board):
    rig, player, clock = board
    cut(rig, player)
    plain = modify_wire(10, 20, 2, 2, [CUT] * 4)
    assert join_packets(rig, 1.99) == [plain]                  # before 2.1
    assert join_packets(rig, 2.22) == [plain]                  # 2.1..4.0211, no grid map
    assert join_packets(rig, 4.0211) == [modify2_wire(0, 0, 10, 20, 2, 2, [CUT] * 4)]
    rig.server.world.get_gmap_for_level = lambda name: (object(), 3, 4)
    assert join_packets(rig, 2.22) == [modify2_wire(3, 4, 10, 20, 2, 2, [CUT] * 4)]


def test_a_joiner_after_the_respawn_gets_the_swapped_change(board):
    rig, player, clock = board
    cut(rig, player)
    clock.now += 15
    run(rig.server.board_changes.tick())
    assert join_packets(rig, 6.037) == [modify2_wire(0, 0, 10, 20, 2, 2, [BUSH, 0, 0, 0])]


def test_send_level_gives_the_base_board_then_the_changes(board):
    rig, player, clock = board
    cut(rig, player)
    player.send_raw.reset_mock()
    player.x, player.y = 30.0, 40.0
    rig.server.baddy_manager = AsyncMock()
    rig.server.horse_manager = AsyncMock()
    run(player._send_level(rig.level))
    sent = b"".join(call.args[0] for call in player.send_raw.await_args_list)
    base = rig.level.get_base_board_packet()
    assert bytes([PLO.BOARDPACKET + 32]) + base in sent        # base, not the cut tile
    assert sent.index(modify2_wire(0, 0, 10, 20, 2, 2, [CUT] * 4)) > sent.index(base)


# -- reload, save, bounds ----------------------------------------------------------------

def test_reload_clears_pending_changes(board, tmp_path):
    rig, player, clock = board
    path = tmp_path / "a.nw"
    path.write_text(Level("a.nw").serialize(), encoding="latin-1")
    rig.level.file_path = str(path)
    rig.server.npc_manager = None
    rig.server.baddy_manager = None
    rig.server._register_level_features = AsyncMock()
    cut(rig, player)
    assert rig.level.board_changes
    run(rig.server.world.reload_level("a.nw"))
    assert rig.level.board_changes == []
    assert join_packets(rig, 6.037) == []
    assert rig.level.get_tile(10, 20) == 0 and rig.level.get_base_tile(10, 20) == 0
    clock.now += 100
    assert run(rig.server.board_changes.tick()) == 0


def test_a_cut_after_a_reload_is_not_respawned_by_the_old_timer(board, tmp_path):
    """Level.cpp:653-661: a reload drops the change list and its timers. The
    reload leaves no stale timer behind that swaps the next cut early."""
    rig, player, clock = board
    path = tmp_path / "a.nw"
    path.write_text(Level("a.nw").serialize(), encoding="latin-1")
    rig.level.file_path = str(path)
    rig.server.npc_manager = None
    rig.server.baddy_manager = None
    rig.server._register_level_features = AsyncMock()
    cut(rig, player)                                          # timer due at +15 s
    clock.now += 10
    run(rig.server.world.reload_level("a.nw"))
    assert rig.level.next_board_expiry() is None
    cut(rig, player)                                          # its own timer: +25 s
    clock.now += 5                                            # the old timer's time
    assert run(rig.server.board_changes.tick()) == 0
    assert rig.level.get_tile(10, 20) == CUT
    clock.now += 9.9
    assert run(rig.server.board_changes.tick()) == 0
    clock.now += 0.1
    assert run(rig.server.board_changes.tick()) == 1
    assert rig.level.get_tile(10, 20) == 0


def saved_copy(level):
    copy = Level("copy.nw")
    copy._parse_nw_file(level.serialize().encode("latin-1"))
    return copy


def test_a_saved_level_holds_the_base_tiles_only(board):
    """Level::saveLevel writes subLevel->getTiles() (Level.cpp:999-1030), the
    array alterBoard never writes (it only appends to boardChanges, :1743):
    no client change reaches the file, respawning or not."""
    rig, player, clock = board
    cut(rig, player, 10, 20)                                   # 2x2, pending
    cut(rig, player, 40, 40, tiles=[7], w=1, h=1)              # 1x1, never respawns
    cut(rig, player, 50, 10, tiles=[8] * 6, w=3, h=2)          # 3x2, never respawns
    copy = saved_copy(rig.level)
    assert copy.get_tile(10, 20) == BUSH
    assert copy.get_tile(40, 40) == 0 and copy.get_tile(50, 10) == 0
    assert rig.level.get_tile(40, 40) == 7                     # still live


def test_a_cut_with_respawntime_zero_is_not_saved_either(board):
    rig, player, clock = board
    rig.config.respawntime = 0
    cut(rig, player, 10, 20)
    assert saved_copy(rig.level).get_tile(10, 20) == BUSH


def test_a_save_after_a_respawn_holds_the_base(board):
    rig, player, clock = board
    cut(rig, player, 10, 20)
    clock.now += 15
    assert run(rig.server.board_changes.tick()) == 1
    assert saved_copy(rig.level).get_tile(10, 20) == BUSH
    assert rig.level.get_tile(10, 20) == BUSH


def test_server_side_tile_writes_are_saved(board):
    """set_tile (GS1 tiles[]/board[] at gs1/host.py, combat block blasts)
    writes the base, so it survives a save, also under a pending cut."""
    rig, player, clock = board
    cut(rig, player, 10, 20)
    rig.level.set_tile(30, 30, 0x55)
    rig.level.set_tile(11, 21, 0x66)
    copy = saved_copy(rig.level)
    assert copy.get_tile(30, 30) == 0x55 and copy.get_tile(11, 21) == 0x66
    assert copy.get_tile(10, 20) == BUSH


# -- the per-level bound ---------------------------------------------------------------

def replay(level):
    """What a late joiner rebuilds: the base board, then every change in order."""
    board = bytearray(level.get_base_board_packet())
    for change in level.board_changes:
        Level._write_rect(board, change.x, change.y, change.width, change.height,
                          change.tiles)
    return bytes(board)


def fill_with_cuts(level, now, respawn_seconds=15):
    """Every 2x2 cell of the level, once: exactly the entry bound."""
    for i in range(MAX_BOARD_CHANGES_PER_LEVEL):
        assert level.alter_board((i % 32) * 2, (i // 32) * 2, 2, 2, [CUT] * 4,
                                 now, respawn_seconds) is not None


def small_bound(level, max_changes=1024, max_tiles=8192):
    level._board_changes = bc.BoardChangeList(max_changes=max_changes,
                                              max_tiles=max_tiles)
    return level._board_changes


def test_the_bound_evicts_the_oldest_entry():
    level = Level("a.nw")
    fill_with_cuts(level, 0.0)
    assert len(level.board_changes) == MAX_BOARD_CHANGES_PER_LEVEL
    level.expire_board_changes(15.0)                           # all respawned
    fixes = []
    assert level.alter_board(5, 5, 1, 1, [9], 16.0, 15, corrections=fixes) is not None
    changes = level.board_changes
    assert len(changes) == MAX_BOARD_CHANGES_PER_LEVEL
    assert (changes[0].x, changes[0].y) == (2, 0)              # the oldest went
    assert (changes[-1].x, changes[-1].y, changes[-1].tiles) == (5, 5, [9])
    assert [(f.x, f.y, f.width, f.height, f.tiles) for f in fixes] == [(0, 0, 2, 2, [0] * 4)]
    assert level.get_tile(5, 5) == 9
    assert replay(level) == level.get_board_packet()


def test_eviction_sends_the_rebuilt_rectangle_to_the_level(board):
    """The evicted rectangle as a joiner rebuilds it (base, then the entries
    left in order) goes to the level, sender included, after the relay."""
    rig, player, clock = board
    level = rig.level
    small_bound(level, max_changes=3)
    level.set_tile(0, 0, BUSH)
    level.set_tile(1, 1, 0x55)                                 # hidden by e3
    level.alter_board(0, 0, 3, 3, [1] * 9, clock.now, 15)      # e1, oldest
    level.alter_board(2, 2, 2, 2, [CUT] * 4, clock.now, 15)    # e2 over e1's corner
    level.alter_board(1, 1, 1, 1, [7], clock.now, 15)          # e3
    rig.sent.clear()
    cut(rig, player, 40, 40, tiles=[3], w=1, h=1)              # evicts e1
    rebuilt = [BUSH, 0, 0, 0, 7, 0, 0, 0, CUT]
    assert rig.sent == [("a.nw", modify_wire(40, 40, 1, 1, [3]), {2}),
                        ("a.nw", modify_wire(0, 0, 3, 3, rebuilt), None)]
    assert [(c.x, c.y) for c in level.board_changes] == [(2, 2), (1, 1), (40, 40)]
    assert [level.get_tile(x, y) for y in range(3) for x in range(3)] == rebuilt
    assert replay(level) == level.get_board_packet()           # a late joiner agrees
    late = bytearray(level.get_base_board_packet())
    for packet in join_packets(rig, 1.0):                      # PLO_BOARDMODIFY replay
        x, y, w, h = (b - 32 for b in packet[1:5])
        Level._write_rect(late, x, y, w, h, [((packet[5 + 2 * i] - 32) << 7)
                                            | (packet[6 + 2 * i] - 32)
                                            for i in range(w * h)])
    assert bytes(late) == level.get_board_packet()


def test_eviction_on_a_map_reaches_the_neighbouring_segments(grid):
    rig, player, clock = grid
    small_bound(rig.level, max_changes=1)
    rig.level.alter_board(5, 5, 1, 1, [9], clock.now, 15)
    rig.sent.clear()
    cut(rig, player, 40, 40, tiles=[3], w=1, h=1)
    fix = modify2_wire(0, 0, 5, 5, 1, 1, [0])
    assert [(n, e) for n, p, e in rig.sent if p == fix] == [
        ("a.nw", None), ("b.nw", None), ("c.nw", None)]


def test_an_evicted_entry_never_respawns_and_a_newer_one_still_does(board):
    rig, player, clock = board
    level = rig.level
    changes = small_bound(level, max_changes=2)
    level.alter_board(10, 20, 2, 2, [CUT] * 4, clock.now, 15)  # bush, pending
    level.alter_board(0, 0, 3, 3, [1] * 9, clock.now, 15)      # permanent
    clock.now += 1
    level.alter_board(2, 2, 2, 2, [CUT] * 4, clock.now, 15)    # evicts the bush cut
    assert level.get_tile(10, 20) == BUSH                      # corrected at once
    assert changes._timed == 1
    level.alter_board(50, 50, 1, 1, [4], clock.now, 15)        # evicts the 3x3
    assert [level.get_tile(x, 2) for x in range(4)] == [0, 0, CUT, CUT]
    assert [level.get_tile(x, 0) for x in range(3)] == [0, 0, 0]
    rig.sent.clear()
    clock.now += 14
    assert run(rig.server.board_changes.tick()) == 0           # the bush cut's timer is gone
    clock.now += 1
    assert run(rig.server.board_changes.tick()) == 1           # the newer cut respawns
    assert rig.sent == [("a.nw", modify_wire(2, 2, 2, 2, [0] * 4), None)]
    assert level.get_tile(2, 2) == 0 and level.get_tile(10, 20) == BUSH
    assert replay(level) == level.get_board_packet()


def test_a_full_list_of_base_tile_changes_no_longer_blocks_a_cut(board):
    """Four client changes carrying the level's own base tiles fill the tile
    bound with three overlapping entries; the next honest cut evicts the
    oldest instead of being refused."""
    rig, player, clock = board
    level = rig.level
    for x, y, w, h in ((0, 42, 1, 2), (0, 0, 64, 43), (0, 21, 64, 43), (0, 22, 64, 42)):
        base = [level.get_base_tile(x + dx, y + dy) for dy in range(h) for dx in range(w)]
        cut(rig, player, x, y, tiles=base, w=w, h=h)
    assert len(level.board_changes) == 3
    assert level._board_changes.tile_total == MAX_BOARD_CHANGE_TILES_PER_LEVEL
    rig.sent.clear()
    cut(rig, player, 10, 20)
    assert level.get_tile(10, 20) == CUT
    assert [(c.x, c.y, c.width, c.height) for c in level.board_changes] == [
        (0, 21, 64, 43), (0, 22, 64, 42), (10, 20, 2, 2)]
    fixes = [p for _n, p, e in rig.sent if e is None and p[:1] == bytes([PLO.BOARDMODIFY + 32])]
    assert len(fixes) == 1 and fixes[0][1:5] == bytes([32, 32, 64 + 32, 43 + 32])
    assert replay(level) == level.get_board_packet()
    clock.now += 15
    run(rig.server.board_changes.tick())
    assert level.get_tile(10, 20) == BUSH


def test_a_full_list_of_permanent_1x1_edits_no_longer_blocks_a_cut(board):
    rig, player, clock = board
    level = rig.level
    for i in range(MAX_BOARD_CHANGES_PER_LEVEL):
        level.alter_board(i % 64, i // 64, 1, 1, [9], clock.now, 15)
    rig.sent.clear()
    cut(rig, player, 10, 20)
    assert level.get_tile(10, 20) == CUT and level.get_tile(0, 0) == 0
    assert len(level.board_changes) == MAX_BOARD_CHANGES_PER_LEVEL
    assert ("a.nw", modify_wire(0, 0, 1, 1, [0]), None) in rig.sent
    assert replay(level) == level.get_board_packet()


def test_a_full_list_still_takes_a_change_that_covers_old_ones():
    level = Level("a.nw")
    fill_with_cuts(level, 0.0)
    fixes = []
    assert level.alter_board(0, 0, 4, 4, [1] * 16, 1.0, 15, corrections=fixes) is not None
    assert len(level.board_changes) == MAX_BOARD_CHANGES_PER_LEVEL - 3
    assert fixes == []                                         # nothing evicted


def test_permanent_distinct_cuts_never_leave_the_live_board_ahead_of_joiners():
    """respawntime 0 and more distinct cuts than the bound: every one is
    taken, the oldest are evicted and their rectangles rebuilt, so the live
    board and a joiner's replay agree everywhere."""
    level = Level("a.nw")
    for i in range(63 * 63):
        assert level.alter_board(i % 63, i // 63, 2, 2, [0x1a0] * 4, 0.0, 0) is not None
    assert len(level.board_changes) == MAX_BOARD_CHANGES_PER_LEVEL
    assert replay(level) == level.get_board_packet()


def test_evicting_an_entry_under_a_respawned_newer_one():
    """The rebuilt rectangle takes, per cell, the newest entry left over it:
    a respawned 2x2 hides the evicted 3x1 under it, the base shows elsewhere."""
    level = Level("a.nw")
    small_bound(level, max_changes=3)
    level.alter_board(0, 1, 3, 1, [4, 4, 4], 0.0, 15)          # 3x1, permanent
    level.alter_board(0, 0, 2, 2, [CUT] * 4, 0.0, 15)          # overlaps it
    level.expire_board_changes(15.0)
    assert level.get_tile(0, 1) == 0 and level.get_tile(2, 1) == 4
    level.alter_board(40, 40, 1, 1, [5], 20.0, 15)
    fixes = []
    level.alter_board(60, 0, 1, 1, [5], 21.0, 15, corrections=fixes)
    assert [(f.x, f.y, f.tiles) for f in fixes] == [(0, 1, [0, 0, 0])]
    assert [(c.x, c.y) for c in level.board_changes] == [(0, 0), (40, 40), (60, 0)]
    assert level.get_tile(2, 1) == 0
    assert replay(level) == level.get_board_packet()


def test_the_tile_bound_evicts_large_changes():
    level = Level("a.nw")
    for x, y in ((0, 0), (32, 0), (0, 32), (32, 32), (16, 0), (0, 16), (16, 32), (32, 16)):
        assert level.alter_board(x, y, 32, 32, [1] * 1024, 0.0, 15) is not None
    assert sum(c.area for c in level.board_changes) == MAX_BOARD_CHANGE_TILES_PER_LEVEL
    fixes = []
    assert level.alter_board(8, 8, 32, 32, [2] * 1024, 0.0, 15, corrections=fixes) is not None
    assert [(f.x, f.y) for f in fixes] == [(0, 0)]             # the oldest only
    assert sum(c.area for c in level.board_changes) == MAX_BOARD_CHANGE_TILES_PER_LEVEL
    assert replay(level) == level.get_board_packet()
    assert level.alter_board(0, 0, 64, 64, [3] * 4096, 0.0, 15) is not None
    assert len(level.board_changes) == 1
    assert replay(level) == level.get_board_packet()


def test_a_change_over_the_tile_bound_empties_the_list_and_is_kept():
    """Cannot happen with the real bound (64x64 = 4096 tiles); a small bound
    shows the defensive path: everything older is evicted, nothing refused."""
    level = Level("a.nw")
    changes = small_bound(level, max_tiles=10)
    level.alter_board(20, 20, 2, 2, [CUT] * 4, 0.0, 15)
    level.alter_board(30, 30, 3, 2, [7] * 6, 0.0, 15)
    fixes = []
    assert level.alter_board(0, 0, 4, 4, [1] * 16, 0.0, 15, corrections=fixes) is not None
    assert [(f.x, f.y) for f in fixes] == [(20, 20), (30, 30)]
    assert [(c.x, c.y) for c in level.board_changes] == [(0, 0)]
    assert changes.tile_total == 16 and changes._timed == 0
    assert replay(level) == level.get_board_packet()
    level.alter_board(10, 10, 1, 1, [2], 0.0, 15)               # evicts it in turn
    assert [(c.x, c.y) for c in level.board_changes] == [(10, 10)]


def test_expiry_does_not_visit_idle_levels(board):
    rig, player, clock = board
    calls = []
    real = rig.level.expire_board_changes
    rig.level.expire_board_changes = lambda now: calls.append(now) or real(now)
    cut(rig, player)
    run(rig.server.board_changes.tick())
    assert calls == []                                         # nothing due yet
    clock.now += 15
    assert run(rig.server.board_changes.tick()) == 1
    assert len(calls) == 1


def test_deleted_timers_do_not_pile_up():
    level = Level("a.nw")
    for i in range(5000):                                      # same cell again and again
        level.alter_board(10, 20, 2, 2, [i % 4000] * 4, i * 0.001, 15)
    assert len(level.board_changes) == 1
    assert level.next_board_expiry() == pytest.approx(4.999 + 15)  # asked every tick
    assert len(level._board_changes._timers) <= 2 * 1 + 64     # heap rebuilt
    assert level.expire_board_changes(30.0) == level.board_changes


# -- the index the list keeps ---------------------------------------------------------------

def test_cells_lists_the_rectangle_row_by_row():
    change = bc.BoardChange(x=3, y=4, width=2, height=3, tiles=[0] * 6)
    assert list(change.cells()) == [4 * 64 + 3, 4 * 64 + 4, 5 * 64 + 3, 5 * 64 + 4,
                                    6 * 64 + 3, 6 * 64 + 4]
    assert [change.offset(cell) for cell in change.cells()] == list(range(6))
    edge = bc.BoardChange(x=62, y=63, width=2, height=1, tiles=[0, 0])
    assert list(edge.cells()) == [4094, 4095]


def test_every_running_timer_is_counted():
    level = Level("a.nw")
    for i in range(5):
        level.alter_board(i * 4, 0, 2, 2, [CUT] * 4, 0.0, 15)
    level.alter_board(40, 0, 1, 1, [1], 0.0, 15)               # no timer
    changes = level._board_changes
    assert changes._timed == 5
    assert sorted(seq for _due, seq in changes._timers) == [0, 1, 2, 3, 4]
    level.alter_board(0, 0, 4, 2, [1] * 8, 0.0, 15)            # deletes one
    assert changes._timed == 4
    assert len(level.expire_board_changes(15.0)) == 4
    assert changes._timed == 0 and level.next_board_expiry() is None


def test_removed_entries_leave_no_trace_in_the_index():
    level = Level("a.nw")
    changes = small_bound(level, max_changes=2)
    first = level.alter_board(0, 0, 2, 2, [CUT] * 4, 0.0, 15)
    level.alter_board(1, 1, 3, 3, [1] * 9, 0.0, 15)
    level.alter_board(10, 10, 1, 1, [1], 0.0, 15)              # evicts `first`
    covered = [cell for cell in range(64 * 64) if changes.covering(cell)]
    assert all(first not in changes.covering(cell) for cell in covered)
    assert sorted(covered) == sorted(list(range(1 * 64 + 1, 1 * 64 + 4))
                                     + list(range(2 * 64 + 1, 2 * 64 + 4))
                                     + list(range(3 * 64 + 1, 3 * 64 + 4)) + [10 * 64 + 10])
    assert changes._timed == 0 and changes.tile_total == 10
    assert level.expire_board_changes(100.0) == []
    level.alter_board(0, 0, 64, 64, [2] * 4096, 1.0, 15)       # deletes the rest
    assert all(changes.covering(cell) == [level.board_changes[0]] for cell in range(4096))


# -- the respawn tick keeps going --------------------------------------------------------

def test_a_failed_respawn_send_does_not_stop_the_others(board, caplog):
    rig, player, clock = board
    other = rig.add_level("b.nw")
    cut(rig, player, 10, 20)
    cut(rig, player, 30, 30)
    other.alter_board(5, 5, 2, 2, [CUT] * 4, clock.now, 15)
    rig.sent.clear()
    real = rig.server.broadcast_to_level
    failures = []

    async def flaky(level_name, packet, exclude=None):
        if not failures:
            failures.append(level_name)
            raise RuntimeError("send failed")
        await real(level_name, packet, exclude)

    rig.server.broadcast_to_level = flaky
    clock.now += 15
    with caplog.at_level("ERROR", logger="pygserver.board_changes"):
        assert run(rig.server.board_changes.tick()) == 3
    assert failures == ["a.nw"]
    assert sorted(name for name, _p, _e in rig.sent) == ["a.nw", "b.nw"]
    assert rig.level.get_tile(30, 30) == 0 and other.get_tile(5, 5) == 0
    assert any("respawn send failed" in r.getMessage() for r in caplog.records)


# -- join packet version boundaries ------------------------------------------------------

@pytest.mark.parametrize("version, on_map, kind", [
    (2.0, False, "plain"), (2.0, True, "plain"),               # < CLVER_2_1
    (2.1, False, "plain"), (2.1, True, "map"),                 # CLVER_2_1 ..
    (4.02, False, "plain"), (4.02, True, "map"),               # .. < CLVER_4_0211
    (4.0211, False, "map"), (4.0211, True, "map"),             # CLVER_4_0211 on
    (6.037, False, "map"),
])
def test_join_packet_type_at_the_version_boundaries(board, version, on_map, kind):
    """Level.cpp:496-499: style 3 below 2.1, style 1 from 4.0211, between them
    style 1 on a map and 3 off it."""
    rig, player, clock = board
    if on_map:
        rig.server.world.get_gmap_for_level = lambda name: (object(), 3, 4)
    cut(rig, player)
    expected = (modify_wire(10, 20, 2, 2, [CUT] * 4) if kind == "plain"
                else modify2_wire(3 if on_map else 0, 4 if on_map else 0,
                                  10, 20, 2, 2, [CUT] * 4))
    assert join_packets(rig, version) == [expected]


# -- grid map neighbours -------------------------------------------------------------------

@pytest.fixture
def grid(board):
    from pygserver.world import GMap
    rig, player, clock = board
    gmap = GMap("m")
    gmap.width, gmap.height = 4, 2
    gmap.grid = {(0, 0): "a.nw", (1, 0): "b.nw", (1, 1): "c.nw", (3, 0): "far.nw"}
    for name in ("b.nw", "c.nw", "far.nw"):
        rig.add_level(name)
    rig.server.world.add_gmap(gmap)
    return rig, player, clock


def test_a_cut_on_a_map_reaches_the_neighbouring_segments(grid):
    """PlayerClientPackets.cpp:129-133 -> Server::sendPacketToNearby
    (Server.cpp:2390-2413): the 3x3 map positions around the sender."""
    rig, player, clock = grid
    rig.sent.clear()
    cut(rig, player)
    wire = modify2_wire(0, 0, 10, 20, 2, 2, [CUT] * 4)
    assert [(n, p, e) for n, p, e in rig.sent if p == wire] == [
        ("a.nw", wire, {2}), ("b.nw", wire, {2}), ("c.nw", wire, {2})]


def test_the_respawn_on_a_map_reaches_the_neighbouring_segments(grid):
    """LevelBoardChange::sendToPlayersOnLevel on a gmap (:58) uses the same
    nearby send, so a viewer next door sees the bush come back."""
    rig, player, clock = grid
    cut(rig, player)
    rig.sent.clear()
    clock.now += 15
    run(rig.server.board_changes.tick())
    wire = modify2_wire(0, 0, 10, 20, 2, 2, [BUSH, 0, 0, 0])
    assert rig.sent == [("a.nw", wire, None), ("b.nw", wire, None), ("c.nw", wire, None)]


def test_an_adjacent_segment_is_sent_as_its_base_board(grid):
    """PLI_ADJACENTLEVEL answers with the static data only
    (PlayerClientPackets.cpp:994-1014, PlayerClient.cpp:1432-1508)."""
    from pygserver.protocol.packets import PacketBuilder
    rig, player, clock = grid
    neighbour = rig.server.world.get_level("b.nw")
    neighbour.set_tile(10, 20, BUSH)
    neighbour.alter_board(10, 20, 2, 2, [CUT] * 4, clock.now, 15)
    player.send_raw.reset_mock()
    run(player._handle_adjacent_level(
        PacketBuilder().write_gint5(0).write_bytes(b"b.nw").build()))
    sent = b"".join(call.args[0] for call in player.send_raw.await_args_list)
    assert bytes([PLO.BOARDPACKET + 32]) + neighbour.get_base_board_packet() in sent
    assert neighbour.get_board_packet() != neighbour.get_base_board_packet()
    assert modify2_wire(1, 0, 10, 20, 2, 2, [CUT] * 4) not in sent


# -- cost at the bound -------------------------------------------------------------------------

def _median_packet_seconds(fill, packets):
    """Best of three medians of one alter_board call after `fill`; every
    packet must be taken and must evict exactly `evicts` entries."""
    import statistics
    import time
    best = float("inf")
    for _ in range(3):
        level = Level("a.nw")
        fill(level)
        times = []
        for args, evicts in packets:
            fixes = []
            start = time.perf_counter()
            change = level.alter_board(*args, corrections=fixes)
            times.append(time.perf_counter() - start)
            assert change is not None and len(fixes) == evicts
        best = min(best, statistics.median(times))
    return best


def _fill_overlapping_cuts(level):
    """A permanent 2x2 at every position: the list ends full of the newest."""
    for i in range(63 * 63):
        level.alter_board(i % 63, i // 63, 2, 2, [0x1a0] * 4, 0.0, 0)


class _NoWalk(dict):
    """A change-list dict that fails the test when something walks past its
    first (oldest) entry."""

    def values(self):
        def oldest_only():
            for n, value in enumerate(dict.values(self)):
                if n:
                    raise AssertionError("walked the whole change list")
                yield value
        return oldest_only()

    def __iter__(self):
        raise AssertionError("walked the whole change list")


def test_a_small_change_never_walks_the_whole_list():
    """Containment goes through the per-cell index (cells inside the new
    rectangle), the bound through counters and the oldest entry: a packet
    costs its own area plus the entries it touches (the old list scan cost
    ~0.3-0.7 ms per packet with 3969 entries)."""
    level = Level("a.nw")
    _fill_overlapping_cuts(level)
    changes = level._board_changes
    newest = level.board_changes[-1]
    changes._entries = _NoWalk(changes._entries)
    assert level.alter_board(newest.x, newest.y, 2, 2, [1] * 4, 1.0, 0) is not None  # replaces one
    for args in ((40, 40, 2, 2, [1] * 4), (0, 0, 1, 1, [2]), (1, 1, 2, 2, [3] * 4)):
        fixes = []
        assert level.alter_board(*args, 1.0, 0, corrections=fixes) is not None
        assert len(fixes) == 1                                   # evicts the oldest
    assert len(level._board_changes) == MAX_BOARD_CHANGES_PER_LEVEL


def test_a_taken_small_packet_at_the_bound_stays_under_100_microseconds():
    """2x2 and 1x1 packets into a full list: each one is taken and evicts the
    oldest entry (measured ~13-15 us)."""
    twos = [((i % 63, i // 63, 2, 2, [7] * 4, 1.0, 0), 1) for i in range(300)]
    assert _median_packet_seconds(_fill_overlapping_cuts, twos) < 100e-6
    ones = [((i % 64, 30 + (i // 64) % 30, 1, 1, [7], 1.0, 0), 1) for i in range(300)]
    assert _median_packet_seconds(_fill_overlapping_cuts, ones) < 100e-6


def test_a_taken_large_packet_at_the_tile_bound_stays_in_milliseconds():
    """Alternating 63x63 changes, none inside another: two fill the tile
    bound, so each one evicts a 63x63 and rebuilds it (measured ~1.5 ms; a
    64x64, which deletes everything instead, ~1 ms)."""
    def fill(level):
        level.alter_board(0, 0, 63, 63, [5] * 3969, 0.0, 0)
        level.alter_board(1, 1, 63, 63, [6] * 3969, 0.0, 0)
    spots = [(0, 1), (1, 0), (0, 0), (1, 1)]
    packets = [((*spots[i % 4], 63, 63, [i] * 3969, 1.0, 0), 1) for i in range(20)]
    assert _median_packet_seconds(fill, packets) < 20e-3
    whole = [((0, 0, 64, 64, [i] * 4096, 1.0, 0), 0) for i in range(20)]
    assert _median_packet_seconds(fill, whole) < 20e-3
