"""Gmap NPC framing and range against the reference server (modern generation).

A gmap's NPCs belong to the one gmap level (LevelLoader.cpp:209-217, :384):
PLO_SETACTIVELEVEL, PLO_NPCMOVED and PLO_NPCDEL2 name the gmap, X/Y/X2/Y2 are
local to the NPC's cell and GMAPLEVELX/Y (41/42, GBYTE1, NPC.h:668-669) name
the cell. pygserver keeps one Level per segment internally; these tests pin
what goes on the wire.
"""

from pygserver.protocol.constants import NPCPROP, PLI, PLO
from pygserver.protocol.packets import (
    PacketBuilder, build_npc_moved, build_npc_props)
from reborn_protocol.props import NPC_PROPS, StreamPolicy, parse_prop_stream

import pytest

from pygserver.level import Level
from pygserver.npc import NPC
from tests.test_level_entry_cache import (
    FakeClock, ids, loaded_player, make_server, run, sent, warp_plain,
    write_level)


@pytest.fixture
def clock(monkeypatch):
    from pygserver import level_cache
    fake = FakeClock()
    monkeypatch.setattr(level_cache, "clock", fake)
    return fake

GRID = [["c00.nw", "c10.nw", "c20.nw"],
        ["c01.nw", "c11.nw", "c21.nw"],
        ["c02.nw", "c12.nw", "c22.nw"]]


def cell_name(x, y):
    return GRID[y][x]


def gmap_world(tmp_path, **options):
    """A 3x3 gmap m.gmap (cell names cXY.nw) plus plain a.nw and b.nw."""
    server = make_server(tmp_path, **options)
    for row in GRID:
        for name in row:
            server.world.add_level(Level.load(str(write_level(tmp_path, name))))
    for name in ("a.nw", "b.nw"):
        server.world.add_level(Level.load(str(write_level(tmp_path, name))))
    rows = "\n".join(",".join(f'"{name}"' for name in row) for row in GRID)
    path = tmp_path / "m.gmap"
    path.write_text(f"GRMAP001\nWIDTH 3\nHEIGHT 3\nLEVELNAMES\n{rows}\nLEVELNAMESEND\n")
    server.world.load_gmap(str(path))
    return server


def new_npc(server, level_name, x=12.0, y=14.0, **attrs):
    """An NPC put on a level the way level loading does."""
    manager = server.npc_manager
    npc = NPC(manager._allocate_id(), "")
    npc.x, npc.y = x, y
    npc.image = "guard.png"
    for key, value in attrs.items():
        setattr(npc, key, value)
    server.world.get_level(level_name).add_npc(npc)
    npc.record_initial_state(level_name)
    manager._npcs[npc.id] = npc
    return npc


def npc_props(player):
    """{npc id: [prop dict, ...]} for every PLO_NPCPROPS a player got."""
    out = {}
    for packet_id, body in sent(player):
        if packet_id != PLO.NPCPROPS:
            continue
        npc_id = (body[0] - 32) << 14 | (body[1] - 32) << 7 | (body[2] - 32)
        values = {}
        handlers = {pid: (lambda store, value, pid=pid: values.__setitem__(pid, value))
                    for pid in NPC_PROPS}
        _, clean, end = parse_prop_stream(
            body[3:], 0, StreamPolicy(table=NPC_PROPS, max_prop_id=max(NPC_PROPS)),
            handlers, {})
        assert clean and end == len(body) - 3
        out.setdefault(npc_id, []).append(values)
    return out


def active_levels(player):
    return [body for packet_id, body in sent(player) if packet_id == PLO.SETACTIVELEVEL]


# -- section 2: framing ---------------------------------------------------------------------

def test_gmap_entry_frames_the_npc_block_with_the_gmap_name_once(tmp_path, clock):
    """Level::sendNPCsToPlayer sends PLO_SETACTIVELEVEL only when the owner
    changes (Level.cpp:1455-1459); every gmap NPC's owner is the gmap, so one
    before the block and one closing it (PlayerClient.cpp:1555)."""
    server = gmap_world(tmp_path)
    new_npc(server, "c11.nw")
    new_npc(server, "c11.nw", x=20.0)
    player = loaded_player(server)
    run(player.warp("c11.nw", 30, 30))
    framing = [packet_id for packet_id in ids(player)
               if packet_id in (PLO.SETACTIVELEVEL, PLO.NPCPROPS)]
    assert framing == [PLO.SETACTIVELEVEL, PLO.NPCPROPS, PLO.NPCPROPS,
                       PLO.SETACTIVELEVEL]
    assert active_levels(player) == [b"m.gmap", b"m.gmap"]


def test_plain_level_frames_with_the_level_name(tmp_path, clock):
    server = gmap_world(tmp_path)
    new_npc(server, "a.nw")
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    assert active_levels(player) == [b"a.nw", b"a.nw"]


def test_both_map_axes_go_out_with_a_loaded_gmap_npc(tmp_path, clock):
    """LevelLoader.cpp:386-392: mapX/mapY set and both mod-stamped at load;
    the position props stay local to the cell."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "c21.nw", x=12.0, y=14.0)
    player = loaded_player(server)
    run(player.warp("c21.nw", 30, 30))
    [props] = npc_props(player)[npc.id]
    assert props[NPCPROP.GMAPLEVELX] == 2 and props[NPCPROP.GMAPLEVELY] == 1
    assert (props[NPCPROP.X2], props[NPCPROP.Y2]) == (12.0, 14.0)
    raw = npc.build_props_packet()
    assert bytes([NPCPROP.GMAPLEVELX + 32, 2 + 32,
                  NPCPROP.GMAPLEVELY + 32, 1 + 32]) in raw


def test_a_plain_level_npc_has_no_map_axes(tmp_path, clock):
    server = gmap_world(tmp_path)
    npc = new_npc(server, "a.nw")
    assert NPCPROP.GMAPLEVELX not in npc.wire_props()
    assert NPCPROP.GMAPLEVELY not in npc.wire_props()


def test_a_y_seam_crossing_is_the_y_axis_and_the_local_position(tmp_path, clock):
    """NPC.cpp:458-467: only the axis that changed, then X2/Y2 wrapped into
    the new cell; no PLO_NPCMOVED for a cell change inside the gmap."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "c11.nw", x=12.0, y=63.5)
    watcher = loaded_player(server)
    run(watcher.warp("c11.nw", 30, 30))
    watcher.send_raw.reset_mock()
    npc.y = 64.25
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == "c12.nw" and npc.y == 0.25
    assert [call.args[0] for call in watcher.send_raw.await_args_list] == [
        build_npc_props(npc.id, {NPCPROP.GMAPLEVELY: 2,
                                 NPCPROP.X2: 12.0, NPCPROP.Y2: 0.25})]


def test_a_warp_inside_the_gmap_is_props_only(tmp_path, clock):
    """NPC::warp to the level it is on sends the position props
    (NPC.cpp:188-199, :230-233): no PLO_NPCMOVED."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "c00.nw")
    watcher = loaded_player(server)
    run(watcher.warp("c11.nw", 30, 30))
    watcher.send_raw.reset_mock()
    run(server.npc_manager.warp_npc(npc, "c11.nw", 5.0, 6.0))
    assert PLO.NPCMOVED not in ids(watcher)
    assert [call.args[0] for call in watcher.send_raw.await_args_list] == [
        build_npc_props(npc.id, {NPCPROP.GMAPLEVELX: 1, NPCPROP.GMAPLEVELY: 1,
                                 NPCPROP.X2: 5.0, NPCPROP.Y2: 6.0})]


def test_a_warp_onto_the_gmap_names_the_gmap_in_npcmoved(tmp_path, clock):
    """NPC.cpp:210: PLO_NPCMOVED carries destinationLevel->levelName, the
    gmap, to every player."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "a.nw")
    elsewhere = loaded_player(server, 3)
    run(elsewhere.warp("b.nw", 30, 30))
    elsewhere.send_raw.reset_mock()
    run(server.npc_manager.warp_npc(npc, "c10.nw", 7.5, 9.0))
    assert sent(elsewhere)[0] == (
        PLO.NPCMOVED, build_npc_moved(npc.id, "m.gmap", 7.5, 9.0)[1:-1])
    assert npc.map_cell() == (1, 0)


def test_npcdel2_names_the_gmap(tmp_path, clock):
    """Server::deleteNPC: PLO_NPCDEL2 {len}{npc->getLevelName()}{id}
    (Server.cpp:1623-1638) to players off the gmap; PLO_NPCDEL to the gmap."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "c00.nw")
    away = loaded_player(server, 2)
    near = loaded_player(server, 3)
    run(away.warp("a.nw", 30, 30))
    run(near.warp("c22.nw", 30, 30))
    away.send_raw.reset_mock()
    near.send_raw.reset_mock()
    run(server.npc_manager.destroy_npc(npc))
    assert sent(away) == [(PLO.NPCDEL2, bytes([len("m.gmap") + 32]) + b"m.gmap"
                           + PacketBuilder().write_gint3(npc.id).build())]
    assert ids(near) == [PLO.NPCDEL]


def test_a_crossing_stamps_only_the_changed_axis(tmp_path, clock):
    """The entry delta after a crossing carries the axis that changed and the
    position, not the other axis or the untouched props: a cell change is no
    setLevel, so nothing is refreshed (modTime is per prop,
    NPC.cpp:1777-1820)."""
    server = gmap_world(tmp_path)
    clock.now = 1000.0
    npc = new_npc(server, "c11.nw", x=63.5, message="hello")
    npc.observe_props(1000.0)
    player = loaded_player(server)
    clock.now = 5000.0
    run(player.warp("c21.nw", 30, 30))
    clock.now = 5010.0
    run(player.warp("a.nw", 30, 30))
    clock.now = 5020.0
    npc.x = 64.5
    run(server.npc_manager.confine_position(npc))
    assert npc.level.name == "c21.nw"
    clock.now = 5030.0
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "c21.nw")))
    [props] = npc_props(player)[npc.id]
    assert set(props) == {NPCPROP.X, NPCPROP.X2, NPCPROP.GMAPLEVELX}
    assert props[NPCPROP.GMAPLEVELX] == 2 and props[NPCPROP.X2] == 0.5


def test_a_reset_stamps_the_map_axes_unless_the_cell_is_0_0(tmp_path, clock):
    """NPC::resetToInitialState stamps GMAPLEVELX/Y only when mapX or mapY
    is not 0 (NPC.cpp:146-150)."""
    server = gmap_world(tmp_path)
    clock.now = 1000.0
    corner = new_npc(server, "c00.nw")
    middle = new_npc(server, "c11.nw")
    for npc in (corner, middle):
        npc.observe_props(1000.0)
    player = loaded_player(server)
    for name in ("c00.nw", "c11.nw"):
        clock.now = 5000.0
        run(player.warp(name, 30, 30))
        clock.now = 5010.0
        run(player.warp("a.nw", 30, 30))
    clock.now = 5020.0
    for npc in (corner, middle):
        run(server.npc_manager.reset_to_initial_state(npc))
    seen = {}
    for name, npc in (("c00.nw", corner), ("c11.nw", middle)):
        clock.now = 5030.0
        player.send_raw.reset_mock()
        run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, name)))
        seen[name] = npc_props(player).get(npc.id, [{}])[0]
        run(player.warp("a.nw", 30, 30))
    assert NPCPROP.GMAPLEVELX in seen["c11.nw"] and NPCPROP.GMAPLEVELY in seen["c11.nw"]
    assert NPCPROP.GMAPLEVELX not in seen["c00.nw"]
    assert NPCPROP.GMAPLEVELY not in seen["c00.nw"]


# -- section 3: range -----------------------------------------------------------------------

import asyncio  # noqa: E402

from pygserver import npc_motion  # noqa: E402
from pygserver.config import ServerConfig  # noqa: E402
from pygserver.protocol.constants import PLPROP  # noqa: E402
from pygserver.protocol.packets import (  # noqa: E402
    build_other_player_props, build_player_left, build_player_props)
from tests.test_level_entry_cache import map_props  # noqa: E402


def sent_npc_ids(player):
    return set(npc_props(player))


def test_entry_sends_the_npcs_of_the_3x3_clipped_to_the_map(tmp_path, clock):
    """Level::findNPCsInRangeForData (Level.cpp:3425-3477): the cells within
    one of the player's, clipped at the map edge."""
    server = gmap_world(tmp_path)
    corner, middle, edge, far = (new_npc(server, name) for name in
                                 ("c00.nw", "c11.nw", "c20.nw", "c22.nw"))
    at_corner = loaded_player(server, 2)
    run(at_corner.warp("c00.nw", 30, 30))
    assert sent_npc_ids(at_corner) == {corner.id, middle.id}
    at_middle = loaded_player(server, 3)
    run(at_middle.warp("c11.nw", 30, 30))
    assert sent_npc_ids(at_middle) == {corner.id, middle.id, edge.id, far.id}
    assert active_levels(at_middle) == [b"m.gmap", b"m.gmap"]


def test_a_hop_filters_every_cell_by_the_entered_cells_leave_time(tmp_path, clock):
    """cachedModTime = getLevelLastEnteredTime(subLevel) (PlayerClient.cpp:
    1534) is passed for all NPCs (:1552); the neighbour's own record is never
    read. Mutation: filtering by the NPC's own cell drops the message."""
    server = gmap_world(tmp_path)
    clock.now = 1000.0
    neighbour = new_npc(server, "c10.nw")
    neighbour.observe_props(1000.0)
    player = loaded_player(server)
    clock.now = 5000.0
    run(player.warp("c11.nw", 30, 30))
    clock.now = 5010.0
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(y=0)))     # to c10
    clock.now = 5020.0
    neighbour.message = "news"
    neighbour.observe_props(5020.0)
    clock.now = 5025.0
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(y=1)))     # back to c11
    assert player.visited_levels.left_at("c10.nw") == 5025.0
    assert npc_props(player)[neighbour.id] == [{NPCPROP.MESSAGE: "news"}]


def test_a_hop_to_a_new_cell_resends_the_overlap_whole_and_deletes_nothing(tmp_path, clock):
    """First visit to the entered cell: no time, so all nine cells' NPCs go
    out with every prop, the shared ones again; cells dropping out of the
    3x3 get no PLO_NPCDEL (the reference client hides them itself)."""
    server = gmap_world(tmp_path)
    clock.now = 1000.0
    kept = new_npc(server, "c11.nw")
    dropped = new_npc(server, "c00.nw")
    player = loaded_player(server)
    clock.now = 5000.0
    run(player.warp("c01.nw", 30, 30))
    assert {kept.id, dropped.id} <= sent_npc_ids(player)
    player.send_raw.reset_mock()
    clock.now = 5010.0
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=2)))     # to c21
    assert npc_props(player)[kept.id] == [kept.wire_props(include_script=True)]
    assert dropped.id not in npc_props(player)
    assert PLO.NPCDEL not in ids(player) and PLO.NPCDEL2 not in ids(player)


def test_live_npc_updates_reach_players_whose_cell_is_in_the_npcs_3x3(tmp_path, clock):
    """Props, moves and show-images go through sendPacketToNearby around the
    NPC (NPC.cpp:1671, :584-591, :263; Level.cpp:3320-3372)."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "c00.nw")
    inside = [loaded_player(server, pid) for pid in (2, 3, 4)]
    outside = loaded_player(server, 5)
    for player, name in zip(inside + [outside], ("c00.nw", "c10.nw", "c11.nw", "c20.nw")):
        run(player.warp(name, 30, 30))
        player.send_raw.reset_mock()

    async def go():
        npc.message = "hi"
        npc.mark_dirty()
        await server.npc_manager.tick()
        npc_motion.queue_move(npc, server, (16, 0), 1.0, npc_motion.APPEND)
        await server.npc_manager._send_new_moves()
        from pygserver.gs1.commands.visual import _broadcast_showimgs
        from types import SimpleNamespace
        _broadcast_showimgs(SimpleNamespace(server=server), npc, {0: {0: "a.png"}})
        await asyncio.sleep(0)
    run(go())
    for player in inside:
        got = ids(player)
        assert PLO.NPCPROPS in got and PLO.MOVE2 in got and PLO.SHOWIMGNPC in got
        assert PLO.SETACTIVELEVEL not in got     # same gmap name, Server.cpp:2402-2412
    assert ids(outside) == []


def sync_world(tmp_path, **options):
    server = gmap_world(tmp_path, **options)
    players = {}
    for pid, (name, x, y) in {2: ("c00.nw", 30, 30), 3: ("c20.nw", 30, 30),
                              4: ("c22.nw", 30, 30), 5: ("c00.nw", 29, 30)}.items():
        players[pid] = loaded_player(server, pid)
        run(players[pid].warp(name, x, y))
    for player in players.values():
        player.send_raw.reset_mock()
    return server, players


def props_from(player, server):
    """Ids of the players whose props-exchange packet `player` received
    (not the minimap location every player gets, PlayerClient.cpp:1328-1339)."""
    got = [call.args[0] for call in player.send_raw.await_args_list]
    return {pid for pid, other in server.players.items()
            if other is not player and other.build_props_packet() in got}


def test_entry_exchanges_props_inside_the_sync_box_inclusive(tmp_path, clock):
    """findPlayersInRangeForSync (Level.cpp:3265-3318): abs(dx) <= syncx and
    abs(dy) <= syncy in gmap tiles. A at c10 (94, 30): (30, 30) is 64 away and
    counts, (29, 30) is 65 and does not, (158, 158) is 128 down."""
    server, others = sync_world(tmp_path, syncdistancex=64, syncdistancey=64)
    arriving = loaded_player(server, 6)
    run(arriving.warp("c10.nw", 30, 30))
    assert props_from(arriving, server) == {2, 3}
    assert 6 in props_from(others[2], server) and 6 in props_from(others[3], server)
    assert 6 not in props_from(others[4], server) and 6 not in props_from(others[5], server)


def test_the_default_sync_box_covers_a_3x3_gmap(tmp_path, clock):
    """192 tiles each way (Server.h:127) covers a 192-tile map: everyone
    (Level.cpp:3294-3300)."""
    server, others = sync_world(tmp_path)
    arriving = loaded_player(server, 6)
    run(arriving.warp("c10.nw", 30, 30))
    assert props_from(arriving, server) == {2, 3, 4, 5}


def test_leave_tells_the_3x3_around_the_destination_and_the_leaver_hears_its_box(tmp_path, clock):
    """leaveLevel (PlayerClient.cpp:1388-1397) runs after warp() has stored the
    destination position and map cell (:1246-1272): JOINLEAVELVL=0 goes to
    the players around the DESTINATION cell on the old gmap (a.nw is cell
    0,0, so c00 and c10 here), and the leaver gets it for each player in its
    sync range around that point (default: the whole 3x3 gmap)."""
    server, others = sync_world(tmp_path)
    leaver = loaded_player(server, 6)
    run(leaver.warp("c10.nw", 30, 30))
    for player in others.values():
        player.send_raw.reset_mock()
    leaver.send_raw.reset_mock()
    run(leaver.warp("a.nw", 30, 30))
    left = build_player_left(6)
    def notices(pid):
        return [c.args[0] for c in others[pid].send_raw.await_args_list
                if c.args[0] == left]
    for pid in (2, 5):                            # c00 is in the 3x3 of cell 0,0
        assert notices(pid) == [left]
    assert notices(3) == [] and notices(4) == []  # c20 and c22 are not
    notices = [body for packet_id, body in sent(leaver) if packet_id == PLO.OTHERPLPROPS
               and body[2:] == bytes([PLPROP.JOINLEAVELVL + 32, 32])]
    assert sorted((b[0] - 32) << 7 | (b[1] - 32) for b in notices) == [2, 3, 4, 5]


def test_a_warp_off_the_gmap_announces_around_the_destination_and_relevels_everyone(
        tmp_path, clock):
    """Observers inside the destination's 3x3 hear JOINLEAVELVL=0, outside it
    they do not, but every player gets the location update that re-levels the
    leaver (PlayerClient.cpp:1327-1339), so none keeps a stale remote player.
    The leaver hears the sync box around the destination point (:1392-1396):
    64 tiles each way around (10, 10) reaches c00 only."""
    server, others = sync_world(tmp_path, syncdistancex=64, syncdistancey=64)
    leaver = loaded_player(server, 6)
    run(leaver.warp("c22.nw", 30, 30))
    for player in list(others.values()) + [leaver]:
        player.send_raw.reset_mock()
    run(leaver.warp("a.nw", 10, 10))
    left = build_player_left(6)
    relevel = build_other_player_props(6, {
        PLPROP.CURLEVEL: "a.nw", PLPROP.X: 10, PLPROP.Y: 10})

    def got(pid):
        return [c.args[0] for c in others[pid].send_raw.await_args_list]
    for pid in (2, 5):
        assert left in got(pid)
    for pid in (3, 4):
        assert left not in got(pid)
    for pid in (2, 3, 4, 5):
        assert relevel in got(pid)
    heard = {(b[0] - 32) << 7 | (b[1] - 32) for packet_id, b in sent(leaver)
             if packet_id == PLO.OTHERPLPROPS
             and b[2:] == bytes([PLPROP.JOINLEAVELVL + 32, 32])}
    assert heard == {2, 5}


def test_the_visit_record_stays_on_the_cell_that_was_left(tmp_path, clock):
    """PlayerClient.cpp:1357 would mark the destination's cell (here 0,0) as
    visited; the record is kept on the cell actually left."""
    server, others = sync_world(tmp_path)
    leaver = loaded_player(server, 6)
    clock.now = 5000.0
    run(leaver.warp("c22.nw", 30, 30))
    clock.now = 5010.0
    run(leaver.warp("a.nw", 10, 10))
    assert leaver.visited_levels.left_at("c22.nw") == 5010.0
    assert leaver.visited_levels.left_at("c00.nw") is None


def chat_props(text):
    return build_player_props({PLPROP.CURCHAT: text})[1:-1]


def test_live_player_props_reach_the_3x3_only(tmp_path, clock):
    """Player props are sent with sendNonLevelBoundPacketToNearby around the
    player (PlayerProps.cpp:1244; NPCServer.cpp:250), the same players that
    got its join, not every segment of the gmap."""
    server, others = sync_world(tmp_path)
    run(others[2]._dispatch_packet(PLI.PLAYERPROPS, chat_props("hi")))
    expected = build_other_player_props(2, {PLPROP.CURCHAT: "hi"})

    def got(pid):
        return [c.args[0] for c in others[pid].send_raw.await_args_list]
    assert expected in got(5)                    # same cell
    assert expected not in got(3) and expected not in got(4)   # c20, c22


def test_a_disconnect_tells_the_3x3_only(tmp_path, clock):
    """Server.cpp:1679 runs leaveLevel: JOINLEAVELVL=0 to the 3x3
    (PlayerClient.cpp:1390)."""
    from pygserver.server import GameServer
    server, others = sync_world(tmp_path)
    run(GameServer._remove_player(server, others[2]))
    left = build_player_left(2)

    def got(pid):
        return [c.args[0] for c in others[pid].send_raw.await_args_list]
    assert left in got(5)
    assert left not in got(3) and left not in got(4)


def test_a_hop_sends_no_leave_notice(tmp_path, clock):
    """A gmap hop is no leaveLevel (PlayerProps.cpp:905-973)."""
    server, others = sync_world(tmp_path)
    hopper = others[2]
    run(hopper._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    for pid in (3, 4, 5):
        assert build_player_left(2) not in [
            c.args[0] for c in others[pid].send_raw.await_args_list]


def test_sync_distance_options_are_read_from_the_file(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("syncdistancex = 64\nsyncdistancey = 32 # comment\n")
    config = ServerConfig.from_file(str(path))
    assert (config.syncdistancex, config.syncdistancey) == (64, 32)
    assert (ServerConfig().syncdistancex, ServerConfig().syncdistancey) == (192, 192)


# -- client NPC packets ---------------------------------------------------------------------

from tests.test_game_packet_parity import npc_props_payload  # noqa: E402


def client_npc_world(tmp_path, **options):
    return gmap_world(tmp_path, serverside=False, **options)


def test_npcdel_from_a_client_follows_the_gmap_level(tmp_path, clock):
    """Server::deleteNPC (Server.cpp:1622-1636): the NPC's level is the gmap,
    so a player on any cell of it gets PLO_NPCDEL and one elsewhere
    PLO_NPCDEL2 naming the gmap."""
    server = client_npc_world(tmp_path)
    npc = new_npc(server, "c00.nw")
    sender = loaded_player(server, 2)
    neighbour = loaded_player(server, 3)
    away = loaded_player(server, 4)
    run(sender.warp("c00.nw", 30, 30))
    run(neighbour.warp("c10.nw", 30, 30))
    run(away.warp("a.nw", 30, 30))
    for player in (sender, neighbour, away):
        player.send_raw.reset_mock()
    run(sender._dispatch_packet(
        PLI.NPCDEL, PacketBuilder().write_gint3(npc.id).build()))
    assert ids(sender) == [PLO.NPCDEL] and ids(neighbour) == [PLO.NPCDEL]
    assert sent(away) == [(PLO.NPCDEL2, bytes([len("m.gmap") + 32]) + b"m.gmap"
                           + PacketBuilder().write_gint3(npc.id).build())]


def test_npcprops_from_a_client_on_another_cell_of_the_gmap_are_taken(tmp_path, clock):
    """PlayerClientPackets.cpp:207 compares the NPC's level with the player's:
    both are the gmap. The update goes to the 3x3 around the NPC, not back to
    the sender."""
    server = client_npc_world(tmp_path)
    npc = new_npc(server, "c00.nw")
    sender = loaded_player(server, 2)
    near = loaded_player(server, 3)
    far = loaded_player(server, 4)
    run(sender.warp("c10.nw", 30, 30))
    run(near.warp("c01.nw", 30, 30))
    run(far.warp("c22.nw", 30, 30))
    for player in (sender, near, far):
        player.send_raw.reset_mock()
    run(sender._dispatch_packet(
        PLI.NPCPROPS, npc_props_payload(npc.id, {NPCPROP.MESSAGE: "yo"})))
    assert npc.message == "yo"
    assert ids(near) == [PLO.NPCPROPS] and ids(far) == [] and ids(sender) == []


def test_npcprops_from_a_player_off_the_gmap_are_refused(tmp_path, clock):
    server = client_npc_world(tmp_path)
    npc = new_npc(server, "c00.nw")
    sender = loaded_player(server, 2)
    run(sender.warp("a.nw", 30, 30))
    run(sender._dispatch_packet(
        PLI.NPCPROPS, npc_props_payload(npc.id, {NPCPROP.MESSAGE: "yo"})))
    assert npc.message != "yo"


def test_a_client_set_position_does_not_roll_the_npc_across_the_seam(tmp_path, clock):
    """A client may only set NPC props when there is no NPC server
    (PlayerClientPackets.cpp:192), and NPC::testForTouch, the only caller of
    the seam roll, returns at once then (NPC.cpp:2141-2146): the NPC keeps the
    out-of-range local value and the relay already carries it."""
    server = client_npc_world(tmp_path)
    npc = new_npc(server, "c00.nw", x=12.0, y=14.0)
    sender = loaded_player(server, 2)
    watcher = loaded_player(server, 3)
    run(sender.warp("c00.nw", 30, 30))
    run(watcher.warp("c10.nw", 30, 30))
    watcher.send_raw.reset_mock()
    run(sender._dispatch_packet(
        PLI.NPCPROPS, npc_props_payload(npc.id, {NPCPROP.X2: 70.0})))
    run(server.npc_manager.tick())
    assert npc.level.name == "c00.nw" and npc.x == 70.0
    assert [c.args[0] for c in watcher.send_raw.await_args_list] == [
        build_npc_props(npc.id, {NPCPROP.X2: 70.0, NPCPROP.X: 70.0})]


def test_a_client_set_position_off_a_gmap_is_still_clamped(tmp_path, clock):
    """Unchanged outside a gmap: pygserver clamps to the level on the next
    tick (the reference does not on this path; reported, not changed)."""
    server = client_npc_world(tmp_path)
    npc = new_npc(server, "a.nw", x=12.0, y=14.0)
    sender = loaded_player(server, 2)
    run(sender.warp("a.nw", 30, 30))
    run(sender._dispatch_packet(
        PLI.NPCPROPS, npc_props_payload(npc.id, {NPCPROP.X2: 70.0})))
    run(server.npc_manager.tick())
    assert npc.x == 61.0


# -- sendToAll props reach everyone, the rest the 3x3 ---------------------------------------

def _got(player):
    return [c.args[0] for c in player.send_raw.await_args_list]


def test_a_client_head_change_reaches_every_player_and_body_only_the_3x3(tmp_path, clock):
    """Head is sendToAll (PlayerProps.cpp:419): the far player gets it, and
    only once; the body image in the same packet stays on the 3x3."""
    server, others = sync_world(tmp_path)
    run(others[2]._dispatch_packet(
        PLI.PLAYERPROPS,
        build_player_props({PLPROP.HEADIMAGE: "head9.png",
                            PLPROP.BODYIMAGE: "body9.png"})[1:-1]))
    head = build_other_player_props(2, {PLPROP.HEADIMAGE: "head9.png"})
    body = build_other_player_props(2, {PLPROP.BODYIMAGE: "body9.png"})
    assert _got(others[3]).count(head) == 1 and body not in _got(others[3])
    assert head in _got(others[4]) and body not in _got(others[4])   # c22
    assert body in _got(others[5]) and head in _got(others[5])
    assert head not in _got(others[2])


def test_a_script_head_change_reaches_every_player_and_body_only_the_3x3(tmp_path, clock):
    from pygserver.gs1.execution import _send_player_props
    server, others = sync_world(tmp_path)
    run(_send_player_props(others[2], server, {
        PLPROP.HEADIMAGE: "head9.png", PLPROP.BODYIMAGE: "body9.png"}))
    head = build_other_player_props(2, {PLPROP.HEADIMAGE: "head9.png"})
    body = build_other_player_props(2, {PLPROP.BODYIMAGE: "body9.png"})
    assert head in _got(others[4]) and body not in _got(others[4])
    assert head in _got(others[3]) and body in _got(others[5])
    assert body not in _got(others[3])            # c20 is outside c00's 3x3


def _rc_manager(server):
    from pygserver.rc import RCManager
    return RCManager(server)


def test_an_rc_head_edit_reaches_every_player_and_body_only_the_3x3(tmp_path, clock):
    server, others = sync_world(tmp_path)
    target = others[2]
    target.head_image, target.body_image = "head9.png", "body9.png"
    run(_rc_manager(server)._push_rc_changes(
        target, {"head_image": 1, "body_image": 1}, ({}, {}), [], [], []))
    head = build_other_player_props(2, {PLPROP.HEADIMAGE: "head9.png"})
    body = build_other_player_props(2, {PLPROP.BODYIMAGE: "body9.png"})
    assert head in _got(others[4]) and body not in _got(others[4])
    assert head in _got(others[3]) and body in _got(others[5])
    assert body not in _got(others[3])            # c20 is outside c00's 3x3


def test_an_rc_nickname_edit_reaches_every_player(tmp_path, clock):
    server, others = sync_world(tmp_path)
    others[2].nickname = "Hero"
    run(_rc_manager(server)._push_rc_changes(
        others[2], {"nickname": 1}, ({}, {}), [], [], []))
    nick = build_other_player_props(2, {PLPROP.NICKNAME: "Hero"})
    assert nick in _got(others[4]) and _got(others[4]).count(nick) == 1


def test_non_global_rc_and_script_props_stay_on_the_3x3(tmp_path, clock):
    from pygserver.gs1.execution import _send_player_props
    server, others = sync_world(tmp_path)
    others[2].body_image = "body9.png"
    run(_rc_manager(server)._push_rc_changes(
        others[2], {"body_image": 1}, ({}, {}), [], [], []))
    run(_send_player_props(others[2], server, {PLPROP.GANI: "walk"}))
    body = build_other_player_props(2, {PLPROP.BODYIMAGE: "body9.png"})
    gani = build_other_player_props(2, {PLPROP.GANI: "walk"})
    for packet in (body, gani):
        assert packet in _got(others[5])            # same cell
        assert packet not in _got(others[3]) and packet not in _got(others[4])


def test_a_client_set_position_leaves_a_running_move_alone(tmp_path, clock):
    """NPC::setProp X/Y (NPC.cpp:1085-1110) never touches moveQueue."""
    for level in ("c00.nw", "a.nw"):
        server = client_npc_world(tmp_path)
        npc = new_npc(server, level, x=12.0, y=14.0)
        npc_motion.queue_move(npc, server, (16, 0), 1.0, 0)
        sender = loaded_player(server, 2)
        run(sender.warp(level, 30, 30))
        run(sender._dispatch_packet(
            PLI.NPCPROPS, npc_props_payload(npc.id, {NPCPROP.X2: 20.0})))
        assert len(npc.move_queue) == 1
