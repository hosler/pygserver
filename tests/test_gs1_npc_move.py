"""NPC move queue, position clamp / gmap roll, serverside washit, warp packets
(2026-11 upstream delta: bdc52c48, cc20c9d0, 98f18f46)."""
import asyncio
import random
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pygserver import collision, npc_motion, tiletypes
from pygserver.config import ServerConfig
from pygserver.gs1_host import compile_gs1, run_npc_event
from pygserver.level import Level
from pygserver.npc import NPC
from pygserver.protocol.constants import NPCPROP, PLO
from pygserver.protocol.packets import (
    build_npc_move, build_npc_move2, build_npc_moved)
from reborn_protocol.props import NPC_PROPS, StreamPolicy, parse_prop_stream
from pygserver.server import GameServer
from pygserver.world import GMap

WALL = tiletypes.TILE_TYPES.index(22)
WALKABLE = tiletypes.TILE_TYPES.index(0)


def run(coro):
    return asyncio.run(coro)


def flat_level(name="a.nw"):
    level = Level(name)
    for y in range(64):
        for x in range(64):
            level.set_tile(x, y, WALKABLE)
    return level


def make_server(tmp_path, *levels, gmap=None):
    server = GameServer(ServerConfig(base_dir=str(tmp_path)))
    for level in levels:
        server.world.add_level(level)
    if gmap:
        grid = GMap("m")
        grid.grid = dict(gmap)
        server.world._gmaps["m"] = grid
    return server


def add_player(server, level, pid=2, version=6.037):
    player = SimpleNamespace(id=pid, level=level, flags={}, logged_in=True,
                             client_version=version, send_raw=AsyncMock(),
                             x=1.0, y=1.0, direction=2, gani="idle",
                             attach_npc=(0, 0))
    level.add_player(player)
    server.players[pid] = player
    return player


def add_npc(server, level, x, y, code="", npc_id=10001, **attrs):
    npc = NPC(npc_id, f"n{npc_id}")
    npc.x, npc.y = x, y
    for key, value in attrs.items():
        setattr(npc, key, value)
    level.add_npc(npc)
    server.npc_manager._npcs[npc_id] = npc
    if code:
        npc.gs1_program = compile_gs1(code)
    return npc


def sent_ids(player):
    return [call.args[0][0] - 32 for call in player.send_raw.await_args_list]


def prop_ids(packet):
    """The prop ids an NPC props packet carries (id byte, gint3 id, props, \\n)."""
    body = packet[4:-1]
    seen = {}
    handlers = {pid: (lambda out, value, pid=pid: out.__setitem__(pid, value))
                for pid in NPC_PROPS}
    _props, clean, end = parse_prop_stream(
        body, 0, StreamPolicy(table=NPC_PROPS, max_prop_id=max(NPC_PROPS)),
        handlers, seen)
    assert clean and end == len(body)
    return set(seen)


POSITION_PROPS = {2, 3, 75, 76}       # NPCPROP X, Y, X2, Y2


# -- queue arithmetic -----------------------------------------------------------

def test_slices_and_lerp_truncate_per_axis(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (16, 0), 1.0, 0)
    npc_motion.process_queue(npc, server, 120)       # slices 50, 50, 20
    assert npc.move_queue[0].elapsed_ms == 120
    assert npc.x == (160 + 1) / 16.0                  # int(16 * 0.12) == 1
    npc_motion.process_queue(npc, server, 10_000)
    assert npc.x == 11.0 and not npc.move_queue


def test_move_deltas_use_sixteenth_tile_precision(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0, "if (created) { move 0.25,-0.25,0,0; }")
    run_npc_event(npc, "created", server, None)
    move = npc.move_queue[0]
    assert (move.destination[0] - move.origin[0],
            move.destination[1] - move.origin[1]) == (4, -4)


def test_move_is_queued_not_instant(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0, "if (created) { move 2,0,1,0; }")
    run_npc_event(npc, "created", server, None)
    assert (npc.x, npc.y) == (10.0, 10.0) and len(npc.move_queue) == 1


def test_x_assignment_clears_the_queue(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0,
                  "if (created) { move 2,0,1,0; } if (timeout) { x = 5; }")
    run_npc_event(npc, "created", server, None)
    assert npc.move_queue
    run_npc_event(npc, "timeout", server, None)
    assert not npc.move_queue and npc.x == 5.0


def test_overshoot_replaces_the_remaining_delta(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (16, 0), 0.05, npc_motion.APPEND)
    second = npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.APPEND)
    npc_motion.process_queue(npc, server, 100)
    # The first move overshoots by exactly 0 ms, which ends the loop although
    # 50 ms of the original delta remain (NPC.cpp:398-404).
    assert second.elapsed_ms == 0 and len(npc.move_queue) == 1


def test_non_append_move_flushes_the_queue(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    done = []
    first = npc_motion.queue_move(npc, server, (16, 0), 1.0, 0)
    first.on_complete = lambda: done.append(1)
    npc_motion.queue_move(npc, server, (0, 16), 1.0, 0)
    assert done == [1] and len(npc.move_queue) == 1
    assert npc.x == 11.0                              # the flushed move ran to its end


def test_cache_nearby_flushes_only_beyond_five_tiles(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.CACHE_NEARBY)
    npc_motion.queue_move(npc, server, (16 * 4, 0), 1.0, npc_motion.CACHE_NEARBY)
    assert len(npc.move_queue) == 2                   # 5 tiles away: kept
    npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.CACHE_NEARBY)
    assert len(npc.move_queue) == 1                   # 6 tiles away: flushed, new move queued


def test_apply_direction_sets_character_facing(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0, image="#c#")
    npc_motion.queue_move(npc, server, (-16, 16), 1.0, npc_motion.APPLY_DIRECTION)
    npc_motion.process_queue(npc, server, 50)
    assert npc.direction == 1                         # left wins over down
    plain = add_npc(server, level, 20.0, 10.0, npc_id=10002)
    plain.direction = 2
    npc_motion.queue_move(plain, server, (-16, 0), 1.0, npc_motion.APPLY_DIRECTION)
    npc_motion.process_queue(plain, server, 50)
    assert plain.direction == 2                       # only characters turn


# -- walls ----------------------------------------------------------------------

def test_character_stops_before_a_wall_and_the_queue_is_cleared(tmp_path):
    level = flat_level()
    for y in range(64):
        level.set_tile(12, y, WALL)                   # column 12: px 192..207
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 8.0, 10.0, image="#c#", direction=3)
    flags = npc_motion.BLOCKCHECK | npc_motion.INFORM_WHEN_DONE
    npc_motion.queue_move(npc, server, (16 * 4, 0), 1.0, flags | npc_motion.APPEND)
    npc_motion.queue_move(npc, server, (16 * 4, 0), 1.0, flags | npc_motion.APPEND)
    blocked = npc_motion.process_queue(npc, server, 2000)
    assert blocked and not npc.move_queue
    # Probe x is pot + 8 + 32: blocked from pot 152 px; 150 px was the last
    # clear slice (128 + int(64 * 350 / 1000)).
    assert npc.x == 150 / 16.0
    assert npc._move_events == 1                      # one finished move, not two


def test_blocked_move_without_the_inform_bit_raises_no_event(tmp_path):
    level = flat_level()
    for y in range(64):
        level.set_tile(12, y, WALL)
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 8.0, 10.0, image="#c#", direction=3)
    npc_motion.queue_move(npc, server, (16 * 4, 0), 1.0, npc_motion.BLOCKCHECK)
    assert npc_motion.process_queue(npc, server, 2000)
    assert npc._move_events == 0


def test_unchecked_move_walks_through_walls(tmp_path):
    level = flat_level()
    for y in range(64):
        level.set_tile(12, y, WALL)
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 8.0, 10.0, image="#c#", direction=3)
    npc_motion.queue_move(npc, server, (16 * 4, 0), 1.0, 0)
    assert not npc_motion.process_queue(npc, server, 2000)
    assert npc.x == 12.0


@pytest.mark.parametrize("facing,expected", [
    (0, (24, 16)), (1, (8, 32)), (2, (24, 48)), (3, (40, 32))])
def test_character_probe_points_follow_the_server_box(tmp_path, facing, expected):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 0.0, 0.0, image="#c#", direction=facing)
    assert npc_motion._probe_point(npc, (0, 0)) == expected


def test_zero_size_shape_probes_its_own_corner(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 0.0, 0.0)
    for facing in range(4):
        npc.direction = facing
        assert npc_motion._probe_point(npc, (37, 41)) == (37, 41)
    npc.shape = (32, 16)
    npc.direction = 3
    assert npc_motion._probe_point(npc, (0, 0)) == (32, 8)


# -- wire -------------------------------------------------------------------------

def test_move_packet_layouts():
    move = [(160, 320, -32, 48, 3, 7)]
    assert build_npc_move2(257, move) == (
        bytes([PLO.MOVE2 + 32]) + b' "!' + bytes([34, 96, 37, 32, 32, 97, 32, 128, 32, 35, 39, 10]))
    assert build_npc_move(257, move) == (
        bytes([PLO.MOVE + 32]) + b' "!' + bytes([52, 72, 128, 138, 32, 35, 39, 10]))
    assert build_npc_moved(257, "b.nw", 7.5, 9.0) == (
        bytes([PLO.NPCMOVED + 32]) + b' "!' + bytes([47, 50]) + b"b.nw\n")


def test_npc_moved_position_bytes_never_hold_a_newline():
    for half_tiles in range(-400, 401):
        x = half_tiles / 2
        packet = build_npc_moved(5, "b.nw", x, -x)
        assert packet.count(b"\n") == 1 and packet.endswith(b"b.nw\n"), x
        assert 32 <= packet[4] <= 255 and 32 <= packet[5] <= 255, x


def test_npc_moved_position_bytes_follow_the_unsigned_char_clamp():
    def lanes(x, y):
        packet = build_npc_moved(5, "b.nw", x, y)
        return packet[4] - 32, packet[5] - 32
    assert lanes(0.0, 0.0) == (0, 0)
    assert lanes(0.5, 31.5) == (1, 63)
    assert lanes(111.5, 111.0) == (223, 222)          # 223 is the lane top
    assert lanes(112.0, 500.0) == (223, 223)          # clamped, not wrapped
    assert lanes(-11.0, -0.5) == (223, 223)           # int8(-22) was a 0x0A byte
    assert lanes(-0.4, 0.0) == (0, 0)                 # int() truncates toward zero


def test_tick_sends_the_move_once_and_holds_back_position_until_done(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    player = add_player(server, level)
    npc = add_npc(server, level, 10.0, 10.0, "if (created) { move 2,0,0.3,8; }"
                  "if (movementfinished) { this.done = 1; }")
    run_npc_event(npc, "created", server, None)
    manager = server.npc_manager
    server.broadcast_to_level = AsyncMock()

    async def go():
        await manager.tick()                          # primes the clock
        await manager._send_new_moves()
        assert sent_ids(player) == [PLO.MOVE2]
        before = len(player.send_raw.await_args_list)
        await manager._send_new_moves()
        assert len(player.send_raw.await_args_list) == before   # sent once
        npc.message = "hi"                            # a dirty prop mid-move
        npc.mark_dirty()
        manager._move_clock_ns -= 100_000_000
        await manager.tick()
        packet = server.broadcast_to_level.await_args.args[1]
        ids = prop_ids(packet)
        assert NPCPROP.MESSAGE in ids                 # the dirty prop did go out
        assert not ids & POSITION_PROPS               # but no X/Y/X2/Y2 mid-move
        assert npc.move_queue
        manager._move_clock_ns -= 1_000_000_000
        await manager.tick()
        assert not npc.move_queue and npc.x == 12.0
        assert npc.gs1_scopes["this"]["done"] == 1.0
        final = server.broadcast_to_level.await_args.args[1]
        assert final == npc.build_props_packet(include_script=False)
        assert prop_ids(final) >= POSITION_PROPS      # the end state IS announced
    run(go())


def test_position_props_are_withheld_only_while_a_move_runs(tmp_path):
    level = flat_level()
    npc = add_npc(make_server(tmp_path, level), level, 10.0, 10.0)
    assert prop_ids(npc.build_props_packet()) >= POSITION_PROPS
    assert not prop_ids(npc.build_props_packet(include_position=False)) & POSITION_PROPS


def test_old_clients_get_the_classic_move_packet(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    old = add_player(server, level, 2, version=2.22)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (32, 0), 1.0, 0)
    run(server.npc_manager._send_new_moves())
    assert sent_ids(old) == [PLO.MOVE]


def test_late_joiner_learns_the_running_queue(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    player = add_player(server, level)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (160, 0), 1.0, 0)
    npc_motion.process_queue(npc, server, 500)
    run(npc_motion.send_unsent_moves(npc, server, []))    # the level already heard it
    run(npc_motion.send_queue_to_player(npc, server, player))
    packet = player.send_raw.await_args.args[0]
    assert packet == build_npc_move2(npc.id, [(160 + 80, 160, 80, 0, 10, 0)])


def test_trigger_move_uses_the_shared_builders(tmp_path):
    from pygserver.trigger_commands import _npc_move
    level = flat_level()
    server = make_server(tmp_path, level)
    player = add_player(server, level)
    add_npc(server, level, 10.0, 20.0, npc_id=257)
    run(_npc_move(server, player, ["gr.npc.move", "257", "-2", "3", "0.15", "7"], ""))
    assert player.send_raw.await_args.args[0] == build_npc_move2(
        257, [(160, 320, -32, 48, 3, 7)])


# -- clamp and gmap roll ----------------------------------------------------------

@pytest.mark.parametrize("x,y,expected", [
    (-1.0, 70.0, (0.0, 61.0)), (61.5, 3.0, (61.0, 3.0)), (30.5, 61.0, (30.5, 61.0))])
def test_position_is_clamped_on_a_plain_level(tmp_path, x, y, expected):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, x, y)
    run(server.npc_manager.confine_position(npc))
    assert (npc.x, npc.y) == expected


def test_a_moving_npc_stops_at_the_level_edge(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 58.0, 10.0)
    npc_motion.queue_move(npc, server, (16 * 10, 0), 0.5, 0)

    async def go():
        server.broadcast_to_level = AsyncMock()
        await server.npc_manager.tick()
        for _ in range(2):                            # one tick advances at most 250 ms
            server.npc_manager._move_clock_ns -= 1_000_000_000
            await server.npc_manager.tick()
    run(go())
    assert npc.x == 61.0 and not npc.move_queue


def test_gmap_npc_rolls_into_the_neighbour_segment(tmp_path):
    west, east = flat_level("w.nw"), flat_level("e.nw")
    server = make_server(tmp_path, west, east, gmap={(0, 0): "w.nw", (1, 0): "e.nw"})
    on_west = add_player(server, west, 2)
    on_east = add_player(server, east, 3)
    server.broadcast_to_level = AsyncMock()
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    assert npc.level is east and (npc.x, npc.y) == (0.5, 10.0)
    assert npc.id in {n.id for n in east.get_npcs()} and not west.get_npcs()
    calls = {call.args[0]: call.args[1] for call in server.broadcast_to_level.await_args_list}
    # PLO_NPCMOVED goes to every player (NPC.cpp:210), the props to the new level.
    moved = build_npc_moved(npc.id, "e.nw", 0.5, 10.0)
    assert on_west.send_raw.await_args_list[0].args[0] == moved
    assert on_east.send_raw.await_args_list[0].args[0] == moved
    assert "w.nw" not in calls
    assert calls["e.nw"] == npc.build_props_packet()


def test_gmap_npc_rolls_left_and_stays_put_off_the_map(tmp_path):
    west, east = flat_level("w.nw"), flat_level("e.nw")
    server = make_server(tmp_path, west, east, gmap={(0, 0): "w.nw", (1, 0): "e.nw"})
    server.broadcast_to_level = AsyncMock()
    npc = add_npc(server, east, -0.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    assert npc.level is west and (npc.x, npc.y) == (63.5, 10.0)
    off = add_npc(server, west, -3.0, 10.0, npc_id=10002)
    run(server.npc_manager.confine_position(off))
    assert off.level is west and off.x == -3.0       # no segment there


def test_move_across_a_segment_keeps_the_global_path(tmp_path):
    west, east = flat_level("w.nw"), flat_level("e.nw")
    server = make_server(tmp_path, west, east, gmap={(0, 0): "w.nw", (1, 0): "e.nw"})
    server.broadcast_to_level = AsyncMock()
    npc = add_npc(server, west, 62.0, 10.0)
    npc_motion.queue_move(npc, server, (16 * 4, 0), 1.0, 0)
    npc_motion.process_queue(npc, server, 10_000)
    run(server.npc_manager.confine_position(npc))
    assert npc.level is east and npc.x == 2.0


# -- warp ---------------------------------------------------------------------------

def test_warp_tells_the_old_level_and_shows_the_new_one(tmp_path):
    old, new = flat_level("o.nw"), flat_level("n.nw")
    server = make_server(tmp_path, old, new)
    server.broadcast_to_level = AsyncMock()
    elsewhere = add_player(server, flat_level("x.nw"), 5)
    npc = add_npc(server, old, 3.0, 4.0)
    npc_motion.queue_move(npc, server, (16, 0), 1.0, 0)
    run(server.npc_manager.warp_npc(npc, "n.nw", 7.5, 9.0))
    assert npc.level is new and not npc.move_queue and not old.get_npcs()
    # PLO_NPCMOVED to every player, wherever it is (NPC.cpp:210).
    assert elsewhere.send_raw.await_args.args[0] == build_npc_moved(npc.id, "n.nw", 7.5, 9.0)
    calls = [(c.args[0], c.args[1]) for c in server.broadcast_to_level.await_args_list]
    assert calls[0] == ("n.nw", npc.build_props_packet())


def test_warp_to_a_missing_level_leaves_the_npc_alone(tmp_path):
    old = flat_level("o.nw")
    server = make_server(tmp_path, old)
    server.broadcast_to_level = AsyncMock()
    npc = add_npc(server, old, 3.0, 4.0)
    run(server.npc_manager.warp_npc(npc, "nowhere.nw", 1.0, 1.0))
    assert npc.level is old and (npc.x, npc.y) == (3.0, 4.0)
    server.broadcast_to_level.assert_not_awaited()


# -- getareanpcs ----------------------------------------------------------------------

def test_getareanpcs_uses_bounding_boxes_and_a_sixteen_pixel_floor(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    prober = add_npc(server, level, 1.0, 1.0, npc_id=10001,
                     code="if (created) { this.a = getareanpcs(10,10,0,0);"
                          " this.b = getareanpcs(11.1,10,1,1);"
                          " this.c = getareanpcs(9,9,0.5,0.5); }")
    add_npc(server, level, 10.0, 10.0, npc_id=10002, shape=(16, 16))
    hidden = add_npc(server, level, 10.0, 12.0, npc_id=10003, shape=(16, 16))
    hidden.visible = False
    add_npc(server, level, 20.0, 20.0, npc_id=10004, image="#c#")
    run_npc_event(prober, "created", server, None)
    scope = prober.gs1_scopes["this"]
    assert scope["a"] == [1.0]                        # zero size still covers one tile
    assert scope["b"] == []                           # just past the 16 px box
    assert scope["c"] == [1.0]                        # edges touch (inclusive)
    prober.gs1_program = compile_gs1(
        "if (created) { this.d = getareanpcs(10,12,1,1); this.e = getareanpcs(22,22,1,1); }")
    run_npc_event(prober, "created", server, None)
    assert prober.gs1_scopes["this"]["d"] == [2.0]    # invisible NPCs count
    assert prober.gs1_scopes["this"]["e"] == [3.0]    # a character box is 3x3 tiles


# -- serverside washit ------------------------------------------------------------------

@pytest.mark.parametrize("direction,rect", [
    (0, (116, 84, 32, 24)), (1, (76, 116, 32, 32)),
    (2, (116, 164, 32, 16)), (3, (148, 116, 24, 32))])
def test_sword_rectangles(direction, rect):
    assert collision.sword_hit_rect(100, 100, direction) == rect


def _swing(tmp_path, npc_x, npc_y, direction=0, gani="sword"):
    level = flat_level()
    server = make_server(tmp_path, level)
    player = add_player(server, level)
    player.x, player.y, player.direction, player.gani = 100 / 16, 100 / 16, direction, gani
    npc = add_npc(server, level, npc_x / 16, npc_y / 16, shape=(16, 16),
                  code="if (washit) { this.hits = this.hits + 1; }")
    return server, player, npc


@pytest.mark.parametrize("npc_x,hit", [(148, True), (149, False)])
def test_sword_gani_hits_with_inclusive_edges(tmp_path, npc_x, hit):
    server, player, npc = _swing(tmp_path, npc_x, 100)
    run(server.npc_manager.on_player_gani(player))
    assert npc.gs1_scopes["this"].get("hits", 0.0) == (1.0 if hit else 0.0)


def test_only_listed_ganis_swing(tmp_path):
    server, player, npc = _swing(tmp_path, 148, 100, gani="walk")
    run(server.npc_manager.on_player_gani(player))
    assert not npc.gs1_scopes["this"].get("hits")
    player.gani = "sword,extra"                       # attributes are ignored
    run(server.npc_manager.on_player_gani(player))
    assert npc.gs1_scopes["this"]["hits"] == 1.0
    server.config.playerswordganis = []
    run(server.npc_manager.on_player_gani(player))
    assert npc.gs1_scopes["this"]["hits"] == 1.0
    server.config.playerswordganis = ["sword"]
    server.config.serverside = False                  # no NPC server
    run(server.npc_manager.on_player_gani(player))
    assert npc.gs1_scopes["this"]["hits"] == 1.0


def test_option_file_lists_swing_ganis(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("playerswordganis = sword, slash\n")
    assert ServerConfig.from_file(str(path)).playerswordganis == ["sword", "slash"]
    assert ServerConfig().playerswordganis == ["sword"]


def test_one_swing_fires_one_washit_even_with_the_client_report(tmp_path):
    server, player, npc = _swing(tmp_path, 148, 100)
    player.server = server
    run(server.npc_manager.on_player_gani(player))
    from pygserver.combat import CombatManager
    manager = CombatManager.__new__(CombatManager)
    manager.server = server
    server.broadcast_to_level = AsyncMock()
    run(manager.handle_hit_objects(player, 9.5, 6.5, 1.0))
    assert npc.gs1_scopes["this"]["hits"] == 1.0
    # A later swing, no gani rectangle hit: the client report fires on its own.
    server.npc_manager._sword_hits.clear()
    run(manager.handle_hit_objects(player, 9.5, 6.5, 1.0))
    assert npc.gs1_scopes["this"]["hits"] == 2.0


# -- bounded work (review of 2026-11-10) ----------------------------------------------

class _Counter:
    """Counts calls of a function so tests assert step counts, not wall time."""

    def __init__(self, monkeypatch, owner, name):
        self.calls = 0
        original = getattr(owner, name)

        def counted(*args, **kwargs):
            self.calls += 1
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, counted)


def _unchecked_reference(queue, delta_ms):
    """The reference's 50 ms slicing loop (NPC.cpp:384-404), verbatim, for moves
    without a wall test. Returns (last position, finished informwhendone moves)."""
    position, events = None, 0
    while delta_ms != 0 and queue:
        move = queue[0]
        step = min(delta_ms, 50)
        delta_ms -= step
        remaining = 0
        move.elapsed_ms += step
        if move.elapsed_ms < move.duration_ms:
            remaining = move.duration_ms - move.elapsed_ms
        else:
            delta_ms = move.elapsed_ms - move.duration_ms
            move.elapsed_ms = move.duration_ms
        position = move.current_position()
        if remaining <= 0:
            events += bool(move.options & npc_motion.INFORM_WHEN_DONE)
            queue.popleft()
    return position, events


def _random_queue(rng, blockcheck=False):
    moves = []
    for _ in range(rng.randint(1, 4)):
        duration = rng.choice([0, 1, 49, 50, 51, 100, rng.randint(0, 2500)])
        move = npc_motion.NPCMove(
            origin=(400, 400), destination=(400 + rng.randint(-200, 200), 400 + rng.randint(-200, 200)),
            duration_ms=duration,
            options=(npc_motion.INFORM_WHEN_DONE if rng.random() < .5 else 0)
            | (npc_motion.BLOCKCHECK if blockcheck else 0))
        moves.append(move)
    moves[0].elapsed_ms = rng.randint(0, max(0, moves[0].duration_ms - 1))
    return moves


@pytest.mark.parametrize("blockcheck", [False, True])
def test_the_jump_equals_the_sliced_result_for_ordinary_values(tmp_path, blockcheck):
    level = flat_level()
    server = make_server(tmp_path, level)
    rng = random.Random(7)
    for _ in range(400):
        moves = _random_queue(rng, blockcheck)
        delta = rng.choice([1, 49, 50, 51, 120, rng.randint(1, 4000)])
        npc = add_npc(server, level, 10.0, 10.0)
        npc.move_queue = deque(moves)
        expected_queue = deque(npc_motion.NPCMove(**{
            key: getattr(m, key) for key in
            ("origin", "destination", "duration_ms", "options", "elapsed_ms")})
            for m in moves)
        position, events = _unchecked_reference(expected_queue, delta)
        npc_motion.process_queue(npc, server, delta)
        assert npc._move_events == events
        assert len(npc.move_queue) == len(expected_queue)
        assert [m.elapsed_ms for m in npc.move_queue] == [
            m.elapsed_ms for m in expected_queue]
        assert (npc.x * 16, npc.y * 16) == position


def test_one_flush_of_a_huge_move_takes_a_bounded_number_of_steps(tmp_path, monkeypatch):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0,
                  "if (created) { move 1,0,100000,0; move 1,0,1,0; }")
    steps = _Counter(monkeypatch, npc_motion.NPCMove, "current_position")
    run_npc_event(npc, "created", server, None)
    assert steps.calls <= 4                           # was 2,000,000 slices
    assert npc.x == 11.0 and len(npc.move_queue) == 1  # flushed to its end state


@pytest.mark.parametrize("duration", [-1.0, -1e9, float("nan"), float("inf")])
def test_negative_and_non_finite_durations_are_rejected(tmp_path, duration):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    assert npc_motion.queue_move(npc, server, (16, 0), duration, 0) is None
    assert not npc.move_queue


def test_a_huge_duration_is_clamped_to_what_the_wire_can_say(tmp_path, monkeypatch):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    move = npc_motion.queue_move(npc, server, (16, 0), 1e30, npc_motion.APPEND)
    assert move.duration_ms <= npc_motion.MAX_DURATION_MS == 28767 * 50
    assert move.duration_ms >= npc_motion.MAX_DURATION_MS - 5     # float32 rounding
    steps = _Counter(monkeypatch, npc_motion.NPCMove, "current_position")
    npc_motion.process_queue(npc, server, 10 ** 15)
    assert steps.calls <= 2 and not npc.move_queue and npc.x == 11.0


def test_a_blockcheck_call_spends_a_bounded_number_of_wall_tests(tmp_path, monkeypatch):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0, image="#c#")
    walls = _Counter(monkeypatch, collision, "is_on_wall_pixel")
    npc_motion.queue_move(npc, server, (16, 0), 100000.0, npc_motion.BLOCKCHECK)
    npc_motion.queue_move(npc, server, (16, 0), 1.0, 0)    # flushes the long one
    assert walls.calls <= npc_motion.WALL_BUDGET
    assert len(npc.move_queue) == 1                    # the flushed move is over
    npc.move_queue.clear()
    npc_motion.queue_move(npc, server, (16, 0), 100000.0, npc_motion.BLOCKCHECK)
    walls.calls = 0
    npc_motion.process_queue(npc, server, 10 ** 12)
    assert walls.calls <= npc_motion.SLICE_BUDGET
    assert npc.move_queue                              # the rest plays out over time


def test_a_script_loop_of_blockcheck_moves_spends_one_probe_budget(tmp_path, monkeypatch):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0, image="#c#", code=(
        "if (created) { for (i=0;i<3000;i++) { move 0,0,100000,4; } }"))
    walls = _Counter(monkeypatch, collision, "is_on_wall_pixel")
    run_npc_event(npc, "created", server, None)
    assert walls.calls <= 50                          # was 400 per call: 1.2 million
    assert len(npc.move_queue) == 1 and npc.move_queue[0].elapsed_ms == 0
    # The tick loop refills the budget, so a later event may probe again.
    run(server.npc_manager._run_move_queues(0))
    assert npc._wall_budget == npc_motion.WALL_BUDGET


def test_a_spent_budget_still_finishes_a_flushed_move_at_its_end(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0, image="#c#")
    npc._wall_budget = 0
    npc_motion.queue_move(npc, server, (16 * 3, 0), 600.0, npc_motion.BLOCKCHECK)
    npc_motion.queue_move(npc, server, (0, 16), 1.0, 0)
    assert npc.x == 13.0 and len(npc.move_queue) == 1


def test_one_tick_advances_a_queue_by_at_most_the_tick_cap(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (16 * 20, 0), 100.0, 0)
    manager = server.npc_manager
    server.broadcast_to_level = AsyncMock()

    async def go():
        await manager.tick()
        manager._move_clock_ns -= 10 ** 12            # a 1000 s stall
        await manager.tick()
    run(go())
    assert npc.move_queue[0].elapsed_ms == manager.MAX_MOVE_TICK_MS


def test_a_full_queue_is_finished_before_the_next_move_joins(tmp_path, monkeypatch):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0)
    steps = _Counter(monkeypatch, npc_motion.NPCMove, "current_position")
    for _ in range(npc_motion.MAX_QUEUE):
        npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.APPEND)
    assert len(npc.move_queue) == npc_motion.MAX_QUEUE and npc.x == 10.0
    npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.APPEND)
    assert len(npc.move_queue) == 1
    assert npc.x == 10.0 + npc_motion.MAX_QUEUE       # the client does the same
    assert steps.calls <= 2 * npc_motion.MAX_QUEUE


def test_full_queue_finish_reports_events_and_stops_at_a_wall(tmp_path):
    level = flat_level()
    for y in range(64):
        level.set_tile(12, y, WALL)
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 8.0, 10.0, image="#c#", direction=3)
    flags = npc_motion.BLOCKCHECK | npc_motion.INFORM_WHEN_DONE | npc_motion.APPEND
    for _ in range(npc_motion.MAX_QUEUE):
        npc_motion.queue_move(npc, server, (16, 0), 0.5, flags)
    npc_motion.queue_move(npc, server, (16, 0), 0.5, flags)
    assert npc.x < 12.0 and len(npc.move_queue) == 1  # walled, then the new move queued


# -- wire clamps ---------------------------------------------------------------------

@pytest.mark.parametrize("ticks", [28767, 28768, 32768, 40000, 65535])
def test_move2_ticks_clamp_like_the_reference_writer(ticks):
    packet = build_npc_move2(5, [(0, 0, 0, 0, ticks, 0)])
    assert packet.count(b"\n") == 1                   # only the terminator
    assert packet[-4:-2] == bytes([255, 255])         # 28767 in two lanes
    assert packet == build_npc_move2(5, [(0, 0, 0, 0, 28767, 0)])


def test_move2_ticks_below_the_clamp_are_exact():
    packet = build_npc_move2(5, [(0, 0, 0, 0, 28766, 0)])
    assert packet[-4:-2] == bytes([255, 254])
    assert build_npc_move2(5, [(0, 0, 0, 0, 0, 0)])[-4:-2] == b"  "


@pytest.mark.parametrize("dx", range(-976, -968))
def test_classic_move_never_emits_a_newline_byte_for_a_long_left_move(dx):
    packet = build_npc_move(5, [(0, 0, dx, 5, 10, 0)])
    assert packet.count(b"\n") == 1
    assert packet[6] == 223 + 32                      # int8(-22) as an unsigned char, clamped


@pytest.mark.parametrize("pixels,lane", [
    (0, 100), (7, 100), (8, 101), (-8, 99), (-799, 1), (216, 127), (224, 128),
    (1016, 223), (-1024, 223)])
def test_classic_move_lanes_follow_the_unsigned_char_clamp(pixels, lane):
    packet = build_npc_move(5, [(0, 0, pixels, 0, 0, 0)])
    assert packet[6] - 32 == lane
    assert 32 <= packet[6] <= 255 and packet.count(b"\n") == 1


def test_classic_move_position_bytes_clamp_at_the_lane_top():
    packet = build_npc_move(5, [(1800, 1792, 0, 0, 0, 0)])
    assert packet[4] - 32 == 223 and packet[5] - 32 == 223
    assert packet.count(b"\n") == 1
    assert build_npc_move(5, [(1016, 8, 0, 0, 0, 0)])[4:6] == bytes([127 + 32, 1 + 32])


def test_classic_and_modern_option_byte_stays_in_the_lane():
    for builder in (build_npc_move, build_npc_move2):
        assert builder(5, [(0, 0, 0, 0, 0, 255)]).count(b"\n") == 1


# -- one announcement per move ----------------------------------------------------------

def test_a_late_joiner_in_the_announcing_tick_gets_each_move_once(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    early = add_player(server, level, 2)
    npc = add_npc(server, level, 10.0, 10.0)
    npc_motion.queue_move(npc, server, (32, 0), 2.0, npc_motion.APPEND)

    async def go():
        late = add_player(server, level, 3)
        await npc_motion.send_queue_to_player(npc, server, late)   # on arrival
        await server.npc_manager._send_new_moves()                 # the same tick
        return late
    late = run(go())
    assert sent_ids(late).count(PLO.MOVE2) == 1
    assert sent_ids(early).count(PLO.MOVE2) == 1


def test_a_gmap_roll_announces_the_queue_to_the_new_level_once(tmp_path):
    west, east = flat_level("w.nw"), flat_level("e.nw")
    server = make_server(tmp_path, west, east, gmap={(0, 0): "w.nw", (1, 0): "e.nw"})
    on_west = add_player(server, west, 2)
    on_east = add_player(server, east, 3)
    npc = add_npc(server, west, 63.5, 10.0)
    npc_motion.queue_move(npc, server, (32, 0), 2.0, npc_motion.APPEND)

    async def go():
        await server.npc_manager._run_move_queues(1000)           # crosses the seam
        await server.npc_manager._send_new_moves()
    run(go())
    assert npc.level is east
    assert sent_ids(on_east).count(PLO.MOVE2) == 1
    assert sent_ids(on_west).count(PLO.MOVE2) == 1                # still watching it


# -- the gmap seam ---------------------------------------------------------------------

def _strip(server, names=("w.nw", "e.nw", "f.nw", "g.nw")):
    levels = [flat_level(name) for name in names]
    return levels, make_server(server, *levels,
                               gmap={(i, 0): name for i, name in enumerate(names)})


def test_a_player_left_behind_on_the_old_segment_keeps_hearing_the_npc(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    resident = add_player(server, east, 3)
    stranger = add_player(server, last, 4)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    assert npc.level is east
    assert PLO.NPCMOVED in sent_ids(watcher)          # told it moved on
    for player in (watcher, resident, stranger):
        player.send_raw.reset_mock()
    npc.message = "hi"
    npc.mark_dirty()
    run(server.npc_manager.tick())
    assert PLO.NPCPROPS in sent_ids(watcher)          # ... and keeps hearing it
    assert PLO.NPCPROPS in sent_ids(resident)
    assert not stranger.send_raw.await_args_list      # a far segment never does
    npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.APPEND)
    run(server.npc_manager._send_new_moves())
    assert PLO.MOVE2 in sent_ids(watcher)


def test_the_old_segment_is_told_to_forget_an_npc_that_moves_out_of_reach(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    assert [home for _, home in npc._seam_players.values()] == ["w.nw"]
    watcher.send_raw.reset_mock()
    npc.x = 64.5                                      # on into the third segment
    npc._last_relocate = 0.0
    run(server.npc_manager.confine_position(npc))
    assert npc.level is far
    # Nothing is dirty and no tick runs: the roll itself tells the holder,
    # after the move every player hears (NPC.cpp:210).
    assert sent_ids(watcher) == [PLO.NPCMOVED, PLO.NPCDEL] and not npc._seam_players
    run(server.npc_manager.tick())
    assert sent_ids(watcher) == [PLO.NPCMOVED, PLO.NPCDEL]   # and only once


def test_a_warp_to_an_unrelated_level_tells_the_old_segment_to_forget_the_npc(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    elsewhere = flat_level("z.nw")
    server.world.add_level(elsewhere)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    watcher.send_raw.reset_mock()
    run(server.npc_manager.warp_npc(npc, "z.nw", 5.0, 5.0))
    for _ in range(3):
        run(server.npc_manager.tick())
    assert sent_ids(watcher) == [PLO.NPCMOVED, PLO.NPCDEL] and not npc._seam_players


def test_an_empty_queue_roll_gives_the_holder_the_exact_position(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    resident = add_player(server, east, 3)
    npc = add_npc(server, west, 64.3125, 10.0)        # 0.3125 tiles: not a half tile
    run(server.npc_manager.confine_position(npc))
    assert npc.x == 0.3125
    assert sent_ids(watcher) == [PLO.NPCMOVED, PLO.NPCPROPS]
    assert watcher.send_raw.await_args.args[0] == npc.build_props_packet()
    assert sent_ids(resident) == [PLO.NPCMOVED, PLO.NPCPROPS]


def test_a_holder_on_the_destroyed_npcs_own_segment_gets_one_delete(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    west.remove_player(watcher)
    east.add_player(watcher)
    watcher.level = east
    watcher.send_raw.reset_mock()
    run(server.npc_manager.destroy_npc(npc))
    assert sent_ids(watcher) == [PLO.NPCDEL]


def test_a_reused_player_id_does_not_inherit_the_old_holders_updates(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    old = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    west.remove_player(old)
    del server.players[2]                             # disconnected, id 2 is free
    newcomer = add_player(server, west, 2)            # the next login reuses it
    npc.message = "x"
    npc.mark_dirty()
    run(server.npc_manager.tick())
    assert not newcomer.send_raw.await_args_list and not npc._seam_players


def test_removing_a_player_purges_it_from_every_holder_list(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    assert npc._seam_players
    west.remove_player(watcher)
    watcher.level = None                              # nothing left to announce
    watcher.account_name = "w"
    watcher.logged_in = False
    run(server._remove_player(watcher))
    assert not npc._seam_players


def test_a_roll_resends_an_already_announced_queue_to_the_new_level(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    on_west = add_player(server, west, 2)
    on_east = add_player(server, east, 3)
    npc = add_npc(server, west, 63.5, 10.0)
    npc_motion.queue_move(npc, server, (32, 0), 2.0, npc_motion.APPEND)
    run(server.npc_manager._send_new_moves())         # the old level hears it
    assert sent_ids(on_west).count(PLO.MOVE2) == 1 and not sent_ids(on_east)
    assert npc.move_queue[0].sent
    npc.x = 64.5                                      # the walk crossed the seam
    run(server.npc_manager.confine_position(npc))
    assert npc.level is east and not npc.move_queue[0].sent
    run(server.npc_manager._send_new_moves())
    assert sent_ids(on_east).count(PLO.MOVE2) == 1


def test_a_holder_is_dropped_when_the_npc_rolls_back_onto_its_segment(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    assert npc._seam_players
    watcher.send_raw.reset_mock()
    npc.x = -0.5
    npc._last_relocate = 0.0
    run(server.npc_manager.confine_position(npc))
    assert npc.level is west and not npc._seam_players
    # The move every player hears, then one arrival and no extra copy.
    assert sent_ids(watcher) == [PLO.NPCMOVED, PLO.NPCPROPS]


def test_a_holder_who_walked_into_the_npc_segment_is_dropped(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    west.remove_player(watcher)
    east.add_player(watcher)
    watcher.level = east
    watcher.send_raw.reset_mock()
    npc.message = "x"
    npc.mark_dirty()
    run(server.npc_manager.tick())
    assert sent_ids(watcher) == [PLO.NPCPROPS] and not npc._seam_players


def test_destroying_the_npc_reaches_players_on_the_old_segment(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    watcher = add_player(server, west, 2)
    npc = add_npc(server, west, 64.5, 10.0)
    run(server.npc_manager.confine_position(npc))
    watcher.send_raw.reset_mock()
    run(server.npc_manager.destroy_npc(npc))
    assert sent_ids(watcher) == [PLO.NPCDEL]


def test_an_npc_oscillating_across_a_seam_rolls_at_most_once_per_cooldown(tmp_path):
    (west, east, far, last), server = _strip(tmp_path)
    add_player(server, west, 2)
    add_player(server, east, 3)
    npc = add_npc(server, west, 64.5, 10.0)
    manager = server.npc_manager
    rolls = 0
    original = manager.relocate_npc

    async def counting(npc_, level_):
        nonlocal rolls
        rolls += 1
        await original(npc_, level_)
    manager.relocate_npc = counting

    async def go():
        for tick in range(40):
            npc.x = 64.5 if npc.level is west else -0.5
            await manager.confine_position(npc)
    run(go())
    assert rolls == 1 and npc._position_check             # the rest waits its turn
    npc._last_relocate -= manager.RELOCATE_COOLDOWN
    run(manager.confine_position(npc))
    assert rolls == 2


# -- script position writes ---------------------------------------------------------------

def test_a_script_write_wraps_through_the_local_pixel_position(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0,
                  "if (created) { x = 70.03; y = 0-3; }")
    run_npc_event(npc, "created", server, None)
    assert npc.x == 6.0                               # 70.03 -> 1120 px, % 1024 = 96 px
    assert npc.y == -3.0                              # C remainder keeps the sign
    run(server.npc_manager.confine_position(npc))
    assert npc.y == 0.0                               # the testForLinks clamp, later


def test_a_script_write_far_out_of_range_lands_on_pixel_zero(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    npc = add_npc(server, level, 10.0, 10.0,
                  "if (created) { x = 1000000*1000000*1000000; }")
    run_npc_event(npc, "created", server, None)
    assert npc.x == 0.0                               # x86 INT_MIN cast, % 1024


def test_a_script_write_past_a_gmap_edge_rolls_into_the_neighbour(tmp_path):
    west, east = flat_level("w.nw"), flat_level("e.nw")
    server = make_server(tmp_path, west, east, gmap={(0, 0): "w.nw", (1, 0): "e.nw"})
    server.broadcast_to_level = AsyncMock()
    npc = add_npc(server, west, 10.0, 10.0, "if (created) { x = 70; y = 20; }")
    run_npc_event(npc, "created", server, None)
    assert npc.x == 70.0 and npc._position_check
    run(server.npc_manager.confine_position(npc))
    assert npc.level is east and (npc.x, npc.y) == (6.0, 20.0)
    edge = add_npc(server, east, 10.0, 10.0, "if (created) { x = 70; }", npc_id=10002)
    run_npc_event(edge, "created", server, None)
    assert edge.x == 6.0                              # no segment there: wrapped in place


def test_python_api_and_client_position_writes_cancel_the_queue(tmp_path):
    level = flat_level()
    server = make_server(tmp_path, level)
    for attr in ("x", "y"):
        npc = add_npc(server, level, 10.0, 10.0, npc_id=10010 + ord(attr))
        npc_motion.queue_move(npc, server, (16, 0), 5.0, 0)
        setattr(npc.get_api(server.npc_manager), attr, 12.0)
        assert not npc.move_queue and getattr(npc, attr) == 12.0
        assert npc._position_check
    client = add_npc(server, level, 10.0, 10.0, npc_id=10003)
    npc_motion.queue_move(client, server, (16, 0), 5.0, 0)
    client.apply_client_prop(NPCPROP.X, 12.5)
    assert not client.move_queue and client.x == 12.5


# -- sword washit across a seam ---------------------------------------------------------

def test_a_sword_swing_hits_an_npc_on_the_next_segment(tmp_path):
    west, east = flat_level("w.nw"), flat_level("e.nw")
    server = make_server(tmp_path, west, east, gmap={(0, 0): "w.nw", (1, 0): "e.nw"})
    player = add_player(server, west)
    player.x, player.y, player.direction, player.gani = 62.0, 10.0, 3, "sword"
    on_seam = add_npc(server, east, 0.0, 10.0, shape=(16, 16),
                      code="if (washit) { this.hits = this.hits + 1; }")
    far_off = add_npc(server, east, 20.0, 10.0, shape=(16, 16), npc_id=10002,
                      code="if (washit) { this.hits = this.hits + 1; }")
    run(server.npc_manager.on_player_gani(player))
    assert on_seam.gs1_scopes["this"]["hits"] == 1.0
    assert not far_off.gs1_scopes["this"].get("hits")
