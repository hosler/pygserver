"""GS1 coordinates on a gmap are whole-map tiles, as on the reference server.

The reference loads a gmap as ONE Level (include/level/Level.h:461-472), so a
script's `x`/`y` (NPC.cpp:1869-1871, Player.cpp:1394-1396), `tiles[x,y]`
(GS1Variables.cpp:389-444), `updateboard` (Level.cpp:1751-1799) and `putnpc`
(Server.cpp:1485-1502) are whole-map tiles. pygserver keeps one Level per
segment; pygserver/gs1/frame.py converts at the GS1 host boundary. These tests
run scripts on a 3x3 gmap, in the origin cell and in other cells, and check
what a script reads, where a write lands and what goes on the wire.
"""
import asyncio

import pytest

from pygserver import tiletypes
from pygserver.gs1_host import compile_gs1, run_npc_event
from pygserver.protocol.constants import PLI, PLO, PLPROP
from reborn_protocol.props import PLAYER_PROPS, StreamPolicy, parse_prop_stream
from tests.test_gmap_npc_range import gmap_world, new_npc, npc_props
from tests.test_level_entry_cache import (
    FakeClock, loaded_player, map_props, run, sent)

WALL = tiletypes.TILE_TYPES.index(22)


@pytest.fixture
def clock(monkeypatch):
    from pygserver import level_cache
    fake = FakeClock()
    monkeypatch.setattr(level_cache, "clock", fake)
    return fake


def cell(x, y):
    return f"c{x}{y}.nw"


def level(server, x, y):
    return server.world.get_level(cell(x, y))


def script(server, npc, code, player=None, event="created"):
    """Run `if (<event>) { code }` on `npc` and let its sends go out."""
    npc.gs1_program = compile_gs1(f"if ({event}) {{ {code} }}")

    async def go():
        run_npc_event(npc, event, server, player)
        for _ in range(5):
            await asyncio.sleep(0)
    run(go())
    return npc.gs1_scopes["this"]


def player_on(server, x, y, px=30.0, py=30.5, player_id=2):
    player = loaded_player(server, player_id)
    run(player.warp(cell(x, y), px, py))
    player.send_raw.reset_mock()
    return player


# -- reads ---------------------------------------------------------------------------

@pytest.mark.parametrize("grid,expected", [
    ((0, 0), (12.0, 14.0)), ((2, 1), (140.0, 78.0)), ((1, 2), (76.0, 142.0))])
def test_npc_reads_its_own_position_in_whole_map_tiles(tmp_path, grid, expected):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(*grid), 12.0, 14.0)
    scope = script(server, npc, "this.a = x; this.b = y; this.c = npcs[0].x;"
                                " this.d = npcs[0].y;")
    assert (scope["a"], scope["b"]) == expected
    assert (scope["c"], scope["d"]) == expected


@pytest.mark.parametrize("grid", [(0, 0), (1, 1)])
def test_player_position_reads_whole_map_tiles(tmp_path, grid):
    server = gmap_world(tmp_path)
    player = player_on(server, *grid)
    npc = new_npc(server, cell(*grid))
    scope = script(server, npc, "this.a = playerx; this.b = playery;"
                                " this.c = players[0].x; this.d = players[0].y;"
                                " with (players[0]) { this.e = x; }", player)
    wx, wy = 30.0 + 64 * grid[0], 30.5 + 64 * grid[1]
    assert (scope["a"], scope["b"], scope["c"], scope["d"], scope["e"]) == (
        wx, wy, wx, wy, wx)
    # The player's own state stays relative to its segment.
    assert (player.x, player.y) == (30.0, 30.5)


def test_a_hop_keeps_the_player_segment_local_and_scripts_whole_map(tmp_path, clock):
    """After a gmap hop by client props the player's state is local to the
    new segment, and a script reads mapX * 64 + local (Character.h:65-68)."""
    server = gmap_world(tmp_path)
    player = player_on(server, 0, 0)
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert player.level.name == cell(1, 0) and player.x == 30.0
    npc = new_npc(server, cell(1, 0))
    scope = script(server, npc, "this.a = playerx; this.b = playery;", player)
    assert (scope["a"], scope["b"]) == (94.0, 30.5)


def test_level_objects_read_whole_map_positions(tmp_path):
    server = gmap_world(tmp_path)
    here = level(server, 1, 1)
    from pygserver.protocol.constants import LevelItemType
    run(server.item_manager.spawn_item(here, 5.0, 6.0, LevelItemType(0)))
    npc = new_npc(server, cell(1, 1))
    scope = script(server, npc, "this.a = items[0].x; this.b = items[0].y;")
    assert (scope["a"], scope["b"]) == (69.0, 70.0)


def test_a_plain_level_keeps_level_tiles(tmp_path):
    server = gmap_world(tmp_path)
    player = player_on(server, 0, 0)
    run(player.warp("a.nw", 20.0, 21.0))
    npc = new_npc(server, "a.nw", 12.0, 14.0)
    scope = script(server, npc, "this.a = x; this.b = playerx; x = 15;", player)
    assert (scope["a"], scope["b"], npc.x) == (12.0, 20.0, 15.0)


# -- position writes -----------------------------------------------------------------

def test_npc_write_inside_its_own_segment(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "x = 70; y = 100;")
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == cell(1, 1) and (npc.x, npc.y) == (6.0, 36.0)


def test_npc_write_across_a_seam_rolls_into_that_segment(tmp_path):
    """x = 10 from cell (1,1) is cell (0,1); the setter re-maps the cell
    (NPC.cpp:1874-1891) and pygserver's roll moves the NPC there."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "x = 10;")
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == cell(0, 1) and npc.x == 10.0


def test_npc_write_just_left_of_a_seam_truncates_in_the_global_frame(tmp_path):
    """63.97 tiles is global pixel 1023 (truncated), so cell 0 at 63.9375,
    not the cell the NPC stands in."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 0))
    script(server, npc, "x = 63.97;")
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == cell(0, 0) and npc.x == 63.9375


def test_player_write_is_whole_map(tmp_path):
    server = gmap_world(tmp_path)
    player = player_on(server, 1, 1)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "playerx = 70; playery = 75.5;", player)
    assert (player.x, player.y) == (6.0, 11.5)
    # The wire keeps the segment-local form (X2/Y2 local to the cell).
    seen = {}
    for packet_id, body in sent(player):
        if packet_id != PLO.PLAYERPROPS:
            continue
        handlers = {pid: (lambda store, value, pid=pid: seen.__setitem__(pid, value))
                    for pid in (PLPROP.X2, PLPROP.Y2)}
        parse_prop_stream(body, 0, StreamPolicy(table=PLAYER_PROPS, max_prop_id=100),
                          handlers, {})
    assert (seen.get(PLPROP.X2), seen.get(PLPROP.Y2)) == (6.0, 11.5)


# -- tiles[] -------------------------------------------------------------------------

def test_tiles_read_names_the_segment_by_whole_map_tile(tmp_path):
    server = gmap_world(tmp_path)
    level(server, 2, 1).set_tile(5, 5, 123)
    level(server, 1, 1).set_tile(5, 5, 7)
    npc = new_npc(server, cell(1, 1))
    scope = script(server, npc, "this.a = tiles[133,69]; this.b = tiles[69,69];"
                                " this.c = tiles[5,5];")
    assert (scope["a"], scope["b"]) == (123.0, 7.0)
    assert scope["c"] == float(level(server, 0, 0).get_tile(5, 5))


def test_tiles_write_lands_in_the_named_segment_and_sends_nothing(tmp_path):
    server = gmap_world(tmp_path)
    watcher = player_on(server, 2, 1)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "tiles[133,70] = 77;")
    assert level(server, 2, 1).script_tiles_rect(5, 6, 1, 1) == [77]
    assert level(server, 1, 1)._script_tiles is None
    assert not [p for p, _ in sent(watcher) if p in (PLO.BOARDMODIFY, PLO.BOARDMODIFY2)]


def test_tiles_index_wraps_by_the_whole_map_and_clamps_negative(tmp_path):
    server = gmap_world(tmp_path)
    level(server, 0, 0).set_tile(8, 5, 31)
    level(server, 0, 0).set_tile(0, 5, 32)
    npc = new_npc(server, cell(2, 2))
    scope = script(server, npc, "this.a = tiles[200,5]; this.b = tiles[-9,5];")
    assert (scope["a"], scope["b"]) == (31.0, 32.0)    # 200 % 192 = 8; max(0, -9)


def test_tiles_in_a_missing_segment_read_zero_and_ignore_writes(tmp_path):
    server = gmap_world(tmp_path)
    gmap = server.world.get_gmap_for_level(cell(0, 0))[0]
    del gmap.grid[(2, 2)]
    npc = new_npc(server, cell(1, 1))
    scope = script(server, npc, "tiles[130,130] = 5; this.a = tiles[130,130];")
    assert scope["a"] == 0.0


# -- updateboard ---------------------------------------------------------------------

def decode_modify2(body):
    gx, gy, x, y, w, h = (body[i] - 32 for i in range(6))
    encoded = body[6:6 + 2 * w * h]
    tiles = [((encoded[i] - 32) << 7) + encoded[i + 1] - 32
             for i in range(0, len(encoded), 2)]
    return gx, gy, x, y, w, h, tiles


def test_updateboard_rectangle_is_whole_map_from_any_segment(tmp_path):
    """From cell (1,1), a rectangle over the seam of cells (0,0) and (1,0)
    gives each of those parts its own change, sent around that part
    (LevelBoardChange.cpp:51-62); the script's own segment gets nothing."""
    server = gmap_world(tmp_path)
    watcher = player_on(server, 0, 0)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "tiles[63,5] = 11; tiles[64,5] = 12; tiles[65,5] = 13;"
                        " updateboard 63,5,3,1;")
    assert [(c.x, c.y, c.width, c.tiles) for c in level(server, 0, 0).board_changes] == [
        (63, 5, 1, [11])]
    assert [(c.x, c.y, c.width, c.tiles) for c in level(server, 1, 0).board_changes] == [
        (0, 5, 2, [12, 13])]
    assert not level(server, 1, 1).board_changes
    got = sorted(decode_modify2(body) for packet_id, body in sent(watcher)
                 if packet_id == PLO.BOARDMODIFY2)
    assert got == [(0, 0, 63, 5, 1, 1, [11]), (1, 0, 0, 5, 2, 1, [12, 13])]


def test_updateboard_spanning_four_segments_and_off_the_map(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, "updateboard 127,127,2,2; updateboard 190,5,10,1;")
    for grid, (x, y) in {(1, 1): (63, 63), (2, 1): (0, 63), (1, 2): (63, 0),
                         (2, 2): (0, 0)}.items():
        assert [(c.x, c.y, c.width, c.height) for c in level(server, *grid).board_changes
                ][:1] == [(x, y, 1, 1)], grid
    # Only the on-map part of the second rectangle exists (Level.cpp:1696-1698).
    assert [(c.x, c.width) for c in level(server, 2, 0).board_changes] == [(62, 2)]


# -- putnpc --------------------------------------------------------------------------

def test_putnpc_places_by_whole_map_tile_and_sends_around_the_new_npc(tmp_path):
    """Server::addNPC (Server.cpp:1485-1502, :1586-1590): the cell is the
    position / 64, the NPC is sent to the 3x3 around it, creator's segment
    or not."""
    server = gmap_world(tmp_path)
    near = player_on(server, 2, 2, player_id=2)
    far = player_on(server, 0, 0, player_id=3)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, "putnpc guard.png,,140,78;")
    made = [n for n in level(server, 2, 1).get_npcs() if n is not npc]
    assert len(made) == 1 and (made[0].x, made[0].y) == (12.0, 14.0)
    assert made[0].id in npc_props(near)
    assert made[0].id not in npc_props(far)


def test_putnpc2_uses_the_same_frame(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "putnpc2 5,6,{};")
    made = level(server, 0, 0).get_npcs()
    assert [(n.x, n.y) for n in made] == [(5.0, 6.0)]


@pytest.mark.parametrize("pos", ["-70,5", "200,5", "5,200"])
def test_putnpc_off_the_map_creates_nothing(tmp_path, pos):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 1))
    before = len(server.npc_manager._npcs)
    script(server, npc, f"putnpc guard.png,,{pos};")
    assert len(server.npc_manager._npcs) == before


# -- queries and placing commands ----------------------------------------------------

def test_onwall_and_tiletype_probe_whole_map_tiles(tmp_path):
    server = gmap_world(tmp_path)
    level(server, 1, 1).set_tile(5, 5, WALL)
    npc = new_npc(server, cell(1, 1), 30.0, 30.0)
    scope = script(server, npc, "this.a = onwall(69.5,69.5); this.b = onwall(5.5,5.5);"
                                " this.c = tiletype(69,69);")
    assert (scope["a"], scope["b"], scope["c"]) == (1.0, 0.0, 22.0)


def test_testplayer_and_nearest_player_use_whole_map_tiles(tmp_path):
    server = gmap_world(tmp_path)
    player = player_on(server, 1, 1, 30.0, 30.0)
    npc = new_npc(server, cell(1, 1), 10.0, 10.0)
    scope = script(server, npc, "this.a = testplayer(95,96); this.b = testplayer(31,32);"
                                " this.c = getnearestplayer(94,94);", player)
    assert (scope["a"], scope["b"]) == (0.0, -2.0)
    assert scope["c"] == float(player.id)


def test_testplayer_index_selects_the_hit_player_in_players_on_a_gmap(tmp_path):
    """The reference indexes the in-range candidate list on a gmap, which is
    not players[] (GS1Functions.cpp:1605); pygserver keeps the players[] index
    (the segment's), so players[testplayer(...)] is the hit player and a
    player in another segment does not shift it."""
    server = gmap_world(tmp_path)
    player_on(server, 0, 0, 30.0, 30.0, player_id=2)
    player_on(server, 1, 1, 30.0, 30.0, player_id=3)
    player_on(server, 1, 1, 10.0, 10.0, player_id=4)
    hit = player_on(server, 1, 1, 50.0, 50.0, player_id=5)
    npc = new_npc(server, cell(1, 1), 5.0, 5.0)
    scope = script(server, npc,
                   "this.i = testplayer(115,116); this.pid = players[this.i].id;",
                   hit)
    assert scope["i"] == 2.0 and scope["pid"] == 5.0


def test_putbomb_lands_in_the_named_segment(tmp_path):
    server = gmap_world(tmp_path)
    watcher = player_on(server, 2, 1)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "putbomb 1,140,78;")
    bombs = [body for packet_id, body in sent(watcher) if packet_id == PLO.BOMBADD]
    assert [(b[2] - 32, b[3] - 32) for b in bombs] == [(24, 28)]   # 12, 14 tiles * 2


def test_showimg_is_sent_relative_to_the_npc_segment(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "showimg 1,a.png,70,71.5;")
    assert (npc.showimgs[1][1], npc.showimgs[1][2]) == (12, 15)


# -- start-up order ------------------------------------------------------------------

def test_a_segment_npc_created_at_start_up_already_sees_the_gmap(tmp_path):
    """The reference loads the gmap as one level, so a segment NPC's
    `created` script reads whole-map tiles. pygserver therefore loads the
    gmap files before the levels whose NPCs run `created`."""
    from tests.test_level_entry_cache import make_server
    from pygserver.level import Level
    names = [[f"s{x}{y}.nw" for x in range(2)] for y in range(2)]
    for row in names:
        for name in row:
            body = Level(name).serialize()
            if name == "s11.nw":
                body += "NPC - 12 14\nif (created) { this.cx = x; this.cy = y; }\nNPCEND\n"
            (tmp_path / name).write_text(body, encoding="latin-1")
    rows = ",\n".join(",".join(f'"{n}"' for n in row) for row in names)
    (tmp_path / "m.gmap").write_text(
        f"GRMAP001\nWIDTH 2\nHEIGHT 2\nLEVELNAMES\n{rows}\nLEVELNAMESEND\n")
    server = make_server(tmp_path, gmaps=["m.gmap"])
    run(server._load_world())
    npc = server.world.get_level("s11.nw").get_npcs()[0]
    assert (npc.gs1_scopes["this"]["cx"], npc.gs1_scopes["this"]["cy"]) == (76.0, 78.0)


# ==== call-site coverage ==============================================================
# Each test below runs in a NON-origin cell, so a call site that forgets the
# whole-map frame (treats its segment as the origin) gives a different answer.

def wire(player, packet_id):
    return [body for pid, body in sent(player) if pid == packet_id]


# -- negative whole-map writes -------------------------------------------------------

@pytest.mark.parametrize("axis,grid,landing", [
    ("x", (1, 0), (0, 0)), ("x", (2, 1), (0, 1)),
    ("y", (0, 1), (0, 0)), ("y", (1, 2), (1, 0))])
def test_a_small_negative_write_lands_in_cell_zero_at_a_negative_local(
        tmp_path, axis, grid, landing):
    """`x = -5` is global pixel -80: the setter truncates (NPC.cpp:1874-1891,
    toMapPosition Extents.h:559-562), so the cell is 0 and the local pixel
    -80, not the cell below it and local 944. The script reads -5 back, as
    from the origin cell."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(*grid))
    scope = script(server, npc, f"{axis} = -5; this.r = {axis};")
    run(server.npc_manager.confine_position(npc))
    assert scope["r"] == -5.0
    assert npc.level.name == cell(*landing)
    assert getattr(npc, axis) == -5.0


def test_a_negative_write_from_the_origin_cell_is_unchanged(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(0, 0))
    scope = script(server, npc, "x = -5; this.r = x;")
    run(server.npc_manager.confine_position(npc))
    assert (scope["r"], npc.level.name, npc.x) == (-5.0, cell(0, 0), -5.0)


def test_npcs_index_write_uses_the_global_pixel(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 0))
    script(server, npc, "npcs[0].x = -5; npcs[0].y = 70;")
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == cell(0, 1) and (npc.x, npc.y) == (-5.0, 6.0)


def test_npcs_index_write_inside_its_own_segment(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(2, 1))
    script(server, npc, "npcs[0].x = 135.5;")
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == cell(2, 1) and npc.x == 7.5


def test_players_index_write_is_whole_map(tmp_path):
    server = gmap_world(tmp_path)
    player = player_on(server, 1, 1)
    npc = new_npc(server, cell(1, 1))
    script(server, npc, "players[0].x = 70; players[0].y = 75.5;", player)
    assert (player.x, player.y) == (6.0, 11.5)


# -- signs and compus stay local -----------------------------------------------------

def test_signs_and_compus_read_segment_local_positions(tmp_path):
    """LevelSign and LevelBaddy keep local positions on a gmap too
    (LevelSign.h:31, LevelBaddy.h:127), so no origin is added."""
    server = gmap_world(tmp_path)
    here = level(server, 1, 1)
    here._signs[(5, 6)] = "hi"
    baddy = run(server.baddy_manager.add_baddy(
        here, 7.0, 8.0, __import__("pygserver.baddy", fromlist=["BaddyType"]).BaddyType.GRAYSNAKE))
    npc = new_npc(server, cell(1, 1))
    scope = script(server, npc, "this.a = signs[0].x; this.b = signs[0].y;"
                                " this.c = compus[0].x; this.d = compus[0].y;")
    assert (scope["a"], scope["b"]) == (5.0, 6.0)
    assert (scope["c"], scope["d"]) == (baddy.x, baddy.y)
    assert (baddy.x, baddy.y) != (0, 0)


def test_horses_bombs_and_items_read_whole_map_positions(tmp_path):
    server = gmap_world(tmp_path)
    here = level(server, 2, 1)
    run(server.horse_manager.add_horse(here, 5.0, 6.0))
    npc = new_npc(server, cell(2, 1))
    scope = script(server, npc, "this.a = horses[0].x; this.b = horses[0].y;")
    assert (scope["a"], scope["b"]) == (133.0, 70.0)


# -- queries and commands: each in a non-origin cell ---------------------------------

def test_hitplayer_measures_the_push_in_the_targets_frame(tmp_path):
    """Whole-map from (90,94) is left of the player standing at whole-map
    (94,94.5): the push goes right (Server::hitPlayer, Server.cpp:1958-1969)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    server = gmap_world(tmp_path)
    player = player_on(server, 1, 1)
    npc = new_npc(server, cell(1, 1))
    server.combat_manager = SimpleNamespace(apply_damage=AsyncMock())
    script(server, npc, "hitplayer 0,2,90,94;", player)
    target, damage, dx, dy = server.combat_manager.apply_damage.await_args.args[:4]
    assert target is player and damage == 2
    assert dx > 64 and abs(dy - 64) < dx - 64      # wire components centre on 64


def test_hitnpc_measures_the_push_in_the_targets_frame(tmp_path):
    server = gmap_world(tmp_path)
    target = new_npc(server, cell(1, 1), 30.0, 30.0)
    hitter = new_npc(server, cell(1, 1), 5.0, 5.0)
    script(server, hitter, "hitnpc 0,2,90,94;")
    assert target.hurt_dxdy[0] > 0
    assert abs(target.hurt_dxdy[1]) < target.hurt_dxdy[0]


def test_hitcompu_hurts_from_a_whole_map_point(tmp_path):
    """pygserver-only on a gmap (the reference no-ops there): the from point
    reaches the baddy hurt path in the baddy's segment frame."""
    from unittest.mock import AsyncMock
    from pygserver.baddy import BaddyType
    server = gmap_world(tmp_path)
    player = player_on(server, 1, 1)
    baddy = run(server.baddy_manager.add_baddy(level(server, 1, 1), 30.0, 30.0,
                                               BaddyType.GRAYSNAKE))
    npc = new_npc(server, cell(1, 1))
    server.baddy_manager.handle_baddy_hurt = AsyncMock()
    script(server, npc, "hitcompu 0,2,90,94;", player)
    args = server.baddy_manager.handle_baddy_hurt.await_args.args
    assert args[1:] == (baddy.id, 2, 26.0, 30.0)


@pytest.mark.parametrize("call", ["putexplosion 2,140,78;", "putexplosion2 3,2,140,78;"])
def test_putexplosion_lands_and_is_sent_in_the_named_segment(tmp_path, call):
    server = gmap_world(tmp_path)
    near = player_on(server, 2, 1, player_id=2)     # the blast goes to that level
    far = player_on(server, 0, 0, player_id=3)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, call)
    got = wire(near, PLO.EXPLOSION)
    assert len(got) == 1
    assert (got[0][3] - 32, got[0][4] - 32) == (24, 28)     # local 12, 14 tiles * 2
    assert not wire(far, PLO.EXPLOSION)


def test_hitobjects_lands_and_is_sent_in_the_named_segment(tmp_path):
    server = gmap_world(tmp_path)
    near = player_on(server, 2, 2, player_id=2)
    far = player_on(server, 0, 0, player_id=3)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, "hitobjects 1,140,78;")
    got = wire(near, PLO.HITOBJECTS)
    assert len(got) == 1 and (got[0][3] - 32, got[0][4] - 32) == (24, 28)
    assert not wire(far, PLO.HITOBJECTS)


def test_lay2_drops_the_item_in_the_named_segment(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, "lay2 greenrupee,140,78;")
    items = server.item_manager
    assert [(i.x, i.y) for i in items.get_items_on_level(cell(2, 1))] == [(12.0, 14.0)]
    assert not items.get_items_on_level(cell(0, 0))


def test_puthorse_places_the_horse_in_the_named_segment(tmp_path):
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, "puthorse horse.png,140,78;")
    run(asyncio.sleep(0))
    horses = server.horse_manager
    assert [(h.x, h.y) for h in horses.get_horses_on_level(cell(2, 1))] == [(12.0, 14.0)]
    assert not horses.get_horses_on_level(cell(0, 0))


def test_triggeraction_goes_to_the_segment_around_the_whole_map_tile(tmp_path):
    """(140,142) is cell (2,2): a player in (2,1) is in its 3x3, one in
    (0,0) is not, whichever cell the script runs in."""
    server = gmap_world(tmp_path)
    near = player_on(server, 2, 1, player_id=2)
    far = player_on(server, 0, 0, player_id=3)
    npc = new_npc(server, cell(0, 0))
    script(server, npc, "triggeraction 140,142,zzzprobe,1;")
    got = wire(near, PLO.TRIGGERACTION)
    assert len(got) == 1
    assert (got[0][5] - 32, got[0][6] - 32) == (24, 28)     # local 12, 14 tiles * 2
    assert not wire(far, PLO.TRIGGERACTION)


def test_getareanpcs_takes_a_whole_map_rectangle(tmp_path):
    server = gmap_world(tmp_path)
    new_npc(server, cell(1, 1), 10.0, 10.0)
    new_npc(server, cell(1, 1), 50.0, 50.0)
    npc = new_npc(server, cell(1, 1), 30.0, 5.0)
    scope = script(server, npc, "this.r = getareanpcs(112,112,4,4);")
    assert scope["r"] == [1.0]


def test_testnpc_takes_whole_map_tiles(tmp_path):
    server = gmap_world(tmp_path)
    target = new_npc(server, cell(1, 1), 10.0, 10.0)
    target.image = "#c#"
    npc = new_npc(server, cell(1, 1), 40.0, 40.0)
    scope = script(server, npc, "this.a = testnpc(75,76); this.b = testnpc(11,12);")
    assert (scope["a"], scope["b"]) == (0.0, -1.0)


def test_onwater_probes_whole_map_tiles(tmp_path):
    water = tiletypes.TILE_TYPES.index(11) if 11 in tiletypes.TILE_TYPES else None
    assert water is not None
    server = gmap_world(tmp_path)
    level(server, 1, 1).set_tile(5, 5, water)
    npc = new_npc(server, cell(1, 1), 30.0, 30.0)
    scope = script(server, npc, "this.a = onwater(69.5,69.5); this.b = onwater(5.5,5.5);")
    assert (scope["a"], scope["b"]) == (1.0, 0.0)


def two_players_in_the_middle(server):
    first = player_on(server, 1, 1, 10.0, 10.0, player_id=2)
    second = player_on(server, 1, 1, 50.0, 50.0, player_id=3)
    return first, second


def test_nearest_player_ranks_by_whole_map_distance(tmp_path):
    """(80,80): player 2 stands at whole-map (74,74), player 3 at (114,114).
    In segment-local terms player 3 (50,50) would look closer."""
    server = gmap_world(tmp_path)
    first, second = two_players_in_the_middle(server)
    npc = new_npc(server, cell(1, 1))
    scope = script(server, npc, "this.a = getnearestplayer(80,80);"
                                " this.b = getnearestplayers(80,80);"
                                " this.c = getnearestplayer(110,110);", first)
    assert scope["a"] == float(first.id)
    assert scope["b"] == [0.0, 1.0]
    assert scope["c"] == float(second.id)


def test_a_huge_updateboard_rectangle_is_bounded_by_the_map(tmp_path):
    """The rectangle is cut to the map before its parts are enumerated; an
    unbounded one used to walk about 1.5e7 squared part indices."""
    import time
    from pygserver.gs1.commands.world import _board_parts
    server = gmap_world(tmp_path)
    npc = new_npc(server, cell(1, 1))
    start = time.monotonic()
    parts = _board_parts(server, level(server, 1, 1), 0, 0, 10 ** 9, 10 ** 9)
    assert time.monotonic() - start < 1.0
    assert sorted(p.name for p, *_ in parts) == sorted(
        cell(x, y) for x in range(3) for y in range(3))
    assert all(tuple(rect) == (0, 0, 64, 64) for _, *rect in parts)
    script(server, npc, "updateboard 190,0,1000000000000,1; updateboard 0,0,100000000000000000000000,100000000000000000000000;"
                        " updateboard 1000000000,1000000000,5,5;")
    assert len(level(server, 2, 2).board_changes) == 1
    assert len(level(server, 0, 0).board_changes) == 1
