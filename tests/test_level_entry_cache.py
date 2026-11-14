"""Level enter / leave / re-enter against the reference server (modern
generation): the level mod time, the per-player visited record, the static
data skip rule, NPC deltas on re-entry and gmap hops by client props.

Reference: GServer-v2 server/src/player/PlayerClient.cpp (warp :1203-1275,
enterLevel :1277-1350, leaveLevel :1352-1404, leaveSubLevel :1406-1444,
sendStaticLevelData :1446-1522, sendDynamicLevelData :1524-1605),
player/packets/PlayerClientPackets.cpp (msgPLI_LEVELWARP :52-102,
msgPLI_ADJACENTLEVEL :994-1016) and player/PlayerProps.cpp (GMAPLEVELX/Y
:905-973).
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.baddy import BaddyManager
from pygserver.config import ServerConfig
from pygserver.horse import HorseManager
from pygserver.items import ItemManager
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLO, PLPROP
from pygserver.protocol.packets import PacketBuilder, PacketReader
from pygserver.server import GameServer


MOD_TIME = 1_700_000_000


def run(coro):
    return asyncio.run(coro)


def make_server(tmp_path, **options):
    """A GameServer that is not listening, with the managers entry uses."""
    options.setdefault("levels_dir", str(tmp_path))
    server = GameServer(ServerConfig(base_dir=str(tmp_path), **options))
    server.item_manager = ItemManager(server)
    server.baddy_manager = BaddyManager(server)
    server.horse_manager = HorseManager(server)
    return server


def make_player(server, player_id=2):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    player.send_raw = AsyncMock()
    player.logged_in = True
    server.players[player_id] = player
    return player


def write_level(directory, name, mod_time=MOD_TIME):
    path = directory / name
    path.write_text(Level(name).serialize(), encoding="latin-1")
    os.utime(path, (mod_time, mod_time))
    return path


def split_packets(data: bytes):
    """(packet id, body) for every packet in a send stream. A PLO_RAWDATA
    announcement swallows the raw packet that follows it."""
    out = []
    pos = 0
    while pos < len(data):
        packet_id = data[pos] - 32
        if packet_id == PLO.RAWDATA:
            end = data.index(b"\n", pos)
            size = PacketReader(data[pos + 1:end]).read_gint3()
            raw = data[end + 1:end + 1 + size]
            out.append((raw[0] - 32, raw[1:-1]))
            pos = end + 1 + size
            continue
        end = data.index(b"\n", pos)
        out.append((packet_id, data[pos + 1:end]))
        pos = end + 1
    return out


def sent(player):
    return split_packets(b"".join(call.args[0] for call in player.send_raw.await_args_list))


def ids(player):
    return [packet_id for packet_id, _body in sent(player)]


# -- A. level mod time -----------------------------------------------------------------

def test_a_loaded_level_carries_its_file_time_in_whole_seconds(tmp_path):
    """LevelLoader.cpp:270 keeps the file time; PlayerClient.cpp:1452 truncates
    it to seconds. Mutation: reading the time as 0 or as a float fails."""
    path = write_level(tmp_path, "a.nw", MOD_TIME)
    os.utime(path, (MOD_TIME + 0.75, MOD_TIME + 0.75))
    level = Level.load(str(path))
    assert level.mod_time == MOD_TIME and isinstance(level.mod_time, int)
    assert Level("nofile.nw").mod_time == 0


def test_a_save_keeps_the_mod_time_and_a_reload_takes_the_new_one(tmp_path):
    """Only a reload changes the level mod time (Level.cpp:85-100)."""
    server = GameServer(ServerConfig(base_dir=str(tmp_path)))
    path = write_level(tmp_path, "a.nw", MOD_TIME)
    level = Level.load(str(path))
    server.world.add_level(level)
    level.save_to_file()
    os.utime(path, (MOD_TIME + 50, MOD_TIME + 50))
    assert level.mod_time == MOD_TIME
    run(server.world.reload_level("a.nw"))
    assert level.mod_time == MOD_TIME + 50


def test_a_entry_sends_the_level_mod_time(tmp_path):
    """PLO_LEVELMODTIME {GInt5 seconds} follows the static data
    (PlayerClient.cpp:1513). Mutation: not sending it fails."""
    server = make_server(tmp_path)
    level = Level.load(str(write_level(tmp_path, "a.nw", MOD_TIME)))
    server.world.add_level(level)
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    bodies = [body for packet_id, body in sent(player) if packet_id == PLO.LEVELMODTIME]
    assert [PacketReader(body).read_gint5() for body in bodies] == [MOD_TIME]


# -- B. the visited record ---------------------------------------------------------------

class FakeClock:
    def __init__(self, now=5000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    from pygserver import level_cache
    fake = FakeClock()
    monkeypatch.setattr(level_cache, "clock", fake)
    return fake


def two_levels(tmp_path, **options):
    server = make_server(tmp_path, **options)
    for name in ("a.nw", "b.nw"):
        server.world.add_level(Level.load(str(write_level(tmp_path, name))))
    return server


def test_b_entering_writes_nothing_and_leaving_writes_the_leave_time(tmp_path, clock):
    """Fact 1: only leaveSubLevel writes the record (PlayerClient.cpp:1406-1444,
    called from leaveLevel :1362); enterLevel writes nothing. Mutation:
    recording on entry makes `visited("b.nw")` true and fails."""
    server = two_levels(tmp_path)
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    assert not player.visited_levels.visited("a.nw")
    clock.now = 5010.0
    run(player.warp("b.nw", 30, 30))
    assert player.visited_levels.left_at("a.nw") == 5010.0
    assert not player.visited_levels.visited("b.nw")


def test_b_disconnect_clears_the_record(tmp_path, clock):
    """cleanup() clears both containers (PlayerClient.cpp:184-185)."""
    server = two_levels(tmp_path)
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    run(player.warp("b.nw", 30, 30))
    run(player._cleanup())
    assert not player.visited_levels.visited("a.nw")


def test_b_a_reload_resets_the_level_for_every_player(tmp_path, clock):
    """Level::reload resets the record of every client (Level.cpp:962-968),
    not just the ones on the level. Mutation: resetting only occupants fails.
    reload_levels is the RC_UPDATELEVELS / /updatelevelall entry point."""
    server = two_levels(tmp_path)
    away, other = make_player(server, 2), make_player(server, 3)
    for player in (away, other):
        run(player.warp("a.nw", 30, 30))
        run(player.warp("b.nw", 30, 30))
    run(server.world.reload_levels())
    assert not away.visited_levels.visited("a.nw")
    assert not other.visited_levels.visited("a.nw")


# -- C. static data, the skip rule and the packet order ------------------------------------

def warp_mod(mod_time, x, y, name):
    return (PacketBuilder().write_gint5(mod_time).write_gchar(int(x * 2))
            .write_gchar(int(y * 2)).write_bytes(name.encode()).build())


def warp_plain(x, y, name):
    return (PacketBuilder().write_gchar(int(x * 2)).write_gchar(int(y * 2))
            .write_bytes(name.encode()).build())


def loaded_player(server, player_id=2):
    """A player past its login: the first-load extras are spent."""
    player = make_player(server, player_id)
    player.level_loaded = True
    return player


def revisit(server, player):
    """Visit a.nw, leave it for b.nw, then forget what was sent."""
    run(player.warp("a.nw", 30, 30))
    run(player.warp("b.nw", 30, 30))
    player.send_raw.reset_mock()


S_FULL = [PLO.LEVELNAME, PLO.BOARDPACKET, PLO.LEVELMODTIME, PLO.LEVELNAME, PLO.ISLEADER]
S_SKIP = [PLO.LEVELNAME, PLO.LEVELBOARD, PLO.LEVELMODTIME, PLO.LEVELNAME, PLO.ISLEADER]
D_EMPTY = [PLO.GHOSTICON, PLO.ISLEADER, PLO.SETACTIVELEVEL]


@pytest.mark.parametrize("client_time", [0, MOD_TIME, MOD_TIME + 100])
def test_c_first_visit_always_gets_the_board(tmp_path, clock, client_time):
    """Case (a) / fact 2: with no record the client's time is ignored
    (PlayerClient.cpp:1471 `!visited ||`). Mutation: testing only the time
    (`client < level`) drops the board for an equal or newer time and fails."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("b.nw", 30, 30))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARPMOD, warp_mod(client_time, 30, 30, "a.nw")))
    assert ids(player) == S_FULL + D_EMPTY


def test_c_revisit_with_an_equal_time_skips_the_board(tmp_path, clock):
    """Case (b): visited and not older gives the empty PLO_LEVELBOARD in
    place of the board (PlayerClient.cpp:1507-1510). Mutation: ignoring the
    record (always full) fails."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    revisit(server, player)
    run(player._dispatch_packet(PLI.LEVELWARPMOD, warp_mod(MOD_TIME, 30, 30, "a.nw")))
    assert ids(player) == S_SKIP + D_EMPTY
    [(_, modtime)] = [p for p in sent(player) if p[0] == PLO.LEVELMODTIME]
    assert PacketReader(modtime).read_gint5() == MOD_TIME


def test_c_revisit_with_an_older_time_gets_the_board(tmp_path, clock):
    """Case (c): `clientCachedTime < levelModTime` sends the board."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    revisit(server, player)
    run(player._dispatch_packet(PLI.LEVELWARPMOD, warp_mod(MOD_TIME - 1, 30, 30, "a.nw")))
    assert ids(player) == S_FULL + D_EMPTY


def test_c_revisit_with_a_newer_time_is_treated_as_equal(tmp_path, clock):
    """Case (c'): the test is `<`, not `!=`. Mutation: `!=` fails."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    revisit(server, player)
    run(player._dispatch_packet(PLI.LEVELWARPMOD, warp_mod(MOD_TIME + 1, 30, 30, "a.nw")))
    assert ids(player) == S_SKIP + D_EMPTY


def test_c_revisit_by_a_plain_warp_counts_as_equal(tmp_path, clock):
    """Case (d) / fact 2: no time becomes the level's own (:1454)."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    revisit(server, player)
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))
    assert ids(player) == S_SKIP + D_EMPTY


def test_c_mod_time_zero_resends_the_board_on_a_revisit(tmp_path, clock):
    """Case (e): `0 < levelModTime`."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    revisit(server, player)
    run(player._dispatch_packet(PLI.LEVELWARPMOD, warp_mod(0, 30, 30, "a.nw")))
    assert ids(player) == S_FULL + D_EMPTY


def test_c_a_client_warp_to_a_plain_level_sends_no_player_warp(tmp_path, clock):
    """Fact 3: msgPLI_LEVELWARP enters a plain level through
    Player::enterLevel, which sends no PLO_PLAYERWARP
    (PlayerClientPackets.cpp:87). Mutation: warping like the server fails."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("b.nw", 30, 30))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(10, 12, "a.nw")))
    assert PLO.PLAYERWARP not in ids(player)
    assert (player.level.name, player.x, player.y) == ("a.nw", 10, 12)


def test_c_a_server_warp_sends_player_warp_before_the_level(tmp_path, clock):
    """Fact 3: PlayerClient::warp sends PLO_PLAYERWARP, then enters
    (PlayerClient.cpp:1261-1274). A server warp has no cached time, so a
    revisit skips the board like a plain warp."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    revisit(server, player)
    run(player.warp("a.nw", 10, 12))
    assert ids(player) == [PLO.PLAYERWARP] + S_SKIP + D_EMPTY
    body = sent(player)[0][1]
    assert body == bytes([20 + 32, 24 + 32]) + b"a.nw"


def test_c_first_load_sends_a_blank_board_first_and_the_z_prop(tmp_path, clock):
    """Before the login is done: an empty PLO_LEVELBOARD before the board
    (:1465-1466) and PLO_PLAYERPROPS {Z} after the entry (:1314-1317)."""
    server = two_levels(tmp_path)
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    assert ids(player) == ([PLO.PLAYERWARP, PLO.LEVELNAME, PLO.LEVELBOARD, PLO.BOARDPACKET,
                            PLO.LEVELMODTIME, PLO.LEVELNAME, PLO.ISLEADER]
                           + D_EMPTY + [PLO.PLAYERPROPS])


def test_c_chests_go_out_even_when_the_board_is_skipped(tmp_path, clock):
    """Chests are always sent (:1503-1505), before the empty board."""
    server = two_levels(tmp_path)
    from pygserver.protocol.constants import LevelItemType
    server.item_manager.add_chest(server.world.get_level("a.nw"), 5, 6,
                                  LevelItemType.GREENRUPEE)
    player = loaded_player(server)
    revisit(server, player)
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))
    assert ids(player)[:3] == [PLO.LEVELNAME, PLO.LEVELCHEST, PLO.LEVELBOARD]


def test_c_the_same_level_only_moves_the_player(tmp_path, clock):
    """Player::enterLevel on the level the player is on sets X2/Y2 as the
    server and returns (Player.cpp:1254-1266): props back to the source, no
    level data."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(10, 30, "a.nw")))
    assert ids(player) == [PLO.PLAYERPROPS]
    assert player.x == 10 and player.visited_levels.left_at("a.nw") is None


def test_c_adjacent_level_honours_the_record_and_writes_nothing(tmp_path, clock):
    """PLI_ADJACENTLEVEL answers with the static block under the visited
    rule and does not write the record (PlayerClientPackets.cpp:994-1016)."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("b.nw", 30, 30))
    payload = PacketBuilder().write_gint5(MOD_TIME).write_bytes(b"a.nw").build()
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.ADJACENTLEVEL, payload))
    assert ids(player) == S_FULL                     # never left a.nw: board
    assert not player.visited_levels.visited("a.nw")
    revisit(server, player)
    run(player._dispatch_packet(PLI.ADJACENTLEVEL, payload))
    assert ids(player) == S_SKIP
    names = [body for packet_id, body in sent(player) if packet_id == PLO.LEVELNAME]
    assert names == [b"a.nw", b"b.nw"]


def test_c_leaving_tells_both_sides(tmp_path, clock):
    """leaveLevel: the old level's next player becomes leader and the
    leaver and those left behind trade JOINLEAVELVL=0 (:1367-1396)."""
    server = two_levels(tmp_path)
    leaver, stayer = loaded_player(server, 2), loaded_player(server, 3)
    run(leaver.warp("a.nw", 30, 30))
    run(stayer.warp("a.nw", 30, 30))
    leaver.send_raw.reset_mock()
    stayer.send_raw.reset_mock()
    run(leaver.warp("b.nw", 30, 30))
    left = lambda pid: (PLO.OTHERPLPROPS, bytes([pid // 128 + 32, pid % 128 + 32,
                                                 PLPROP.JOINLEAVELVL + 32, 32]))
    assert left(3) in sent(leaver)
    assert sent(stayer)[:2] == [(PLO.ISLEADER, b""), left(2)]


def gmap_world(tmp_path, **options):
    """A 2x2 gmap m.gmap of c0.nw c1.nw / c2.nw c3.nw, plus plain a.nw and b.nw."""
    server = two_levels(tmp_path, **options)
    names = ["c0.nw", "c1.nw", "c2.nw", "c3.nw"]
    for name in names:
        server.world.add_level(Level.load(str(write_level(tmp_path, name))))
    gmap = tmp_path / "m.gmap"
    gmap.write_text('GRMAP001\nWIDTH 2\nHEIGHT 2\nLEVELNAMES\n"c0.nw","c1.nw",\n'
                    '"c2.nw","c3.nw"\nLEVELNAMESEND\n')
    server.world.load_gmap(str(gmap))
    return server


def test_c_a_client_warp_into_a_gmap_sends_the_segment_twice(tmp_path, clock):
    """PLI_LEVELWARP to a gmap segment from outside: its static data first,
    then PLO_PLAYERWARP2 naming the gmap, then the entry, whose static block
    runs again (PlayerClientPackets.cpp:77-82, PlayerClient.cpp:1251-1259)."""
    server = gmap_world(tmp_path)
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(10, 12, "c1.nw")))
    packets = sent(player)
    assert ids(player) == (S_FULL + [PLO.PLAYERWARP2] + S_FULL + D_EMPTY)
    names = [body for packet_id, body in packets if packet_id == PLO.LEVELNAME]
    assert names == [b"c1.nw", b"a.nw", b"c1.nw", b"m.gmap"]
    warp2 = packets[len(S_FULL)][1]
    assert warp2 == bytes([20 + 32, 24 + 32, 32, 1 + 32, 0 + 32]) + b"m.gmap"


def test_c_a_reloaded_level_is_a_first_visit_again(tmp_path, clock):
    """Level::reload: the occupants leave, every record of the level is reset
    and they are warped back in, so the board goes out again even to a
    plain warp (Level.cpp:945-996)."""
    server = two_levels(tmp_path)
    occupant, away = loaded_player(server, 2), loaded_player(server, 3)
    run(occupant.warp("a.nw", 30, 30))
    revisit(server, away)
    occupant.send_raw.reset_mock()
    run(server.world.reload_level("a.nw"))
    run(server.resend_level("a.nw"))
    assert ids(occupant)[ids(occupant).index(PLO.PLAYERWARP):][:6] == [PLO.PLAYERWARP] + S_FULL
    away.send_raw.reset_mock()
    run(away._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))
    assert ids(away)[:4] == S_FULL[:4]          # board again; not the leader


# -- D. NPC dynamic data on re-entry -----------------------------------------------------

from pygserver import npc_motion  # noqa: E402
from pygserver.npc import NPC  # noqa: E402
from pygserver.protocol.constants import NPCPROP  # noqa: E402
from pygserver.protocol.packets import build_npc_props  # noqa: E402


def place_npc(server, level_name, npc_id=10001, **attrs):
    npc = NPC(npc_id, "n%d" % npc_id)
    npc.image = "guard.png"
    npc.x, npc.y = 12.0, 14.0
    for key, value in attrs.items():
        setattr(npc, key, value)
    server.world.get_level(level_name).add_npc(npc)
    server.npc_manager._npcs[npc_id] = npc
    return npc


def npc_props_sent(player):
    return [body for packet_id, body in sent(player) if packet_id == PLO.NPCPROPS]


def away_and_back(server, player, clock, change=None):
    """a.nw at t=5000, leave for b.nw at 5010, `change()` at 5020, plain
    warp back to a.nw at 5030; returns what the return sent."""
    clock.now = 5000.0
    run(player.warp("a.nw", 30, 30))
    clock.now = 5010.0
    run(player.warp("b.nw", 30, 30))
    clock.now = 5020.0
    if change is not None:
        change()
    clock.now = 5030.0
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))


def test_d_first_visit_sends_every_prop(tmp_path, clock):
    """since = nullopt: all set props (Level.cpp:1477, NPC.cpp:1777-1820)."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw", message="hello")
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    assert npc_props_sent(player) == [npc.build_props_packet()[1:-1]]


def test_d_an_unchanged_npc_sends_nothing_on_return(tmp_path, clock):
    """Props stamped before the player left are not sent again (`modTime >=
    since`, NPC.cpp:1789). Mutation: sending the full props on every entry
    fails. The NPC block is still framed by PLO_SETACTIVELEVEL."""
    server = two_levels(tmp_path)
    place_npc(server, "a.nw")
    player = loaded_player(server)
    away_and_back(server, player, clock)
    assert npc_props_sent(player) == []
    assert ids(player) == S_SKIP + [PLO.GHOSTICON, PLO.ISLEADER,
                                    PLO.SETACTIVELEVEL, PLO.SETACTIVELEVEL]


def test_d_a_prop_changed_while_away_is_the_only_one_sent(tmp_path, clock):
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw")
    player = loaded_player(server)
    away_and_back(server, player, clock, lambda: setattr(npc, "message", "back soon"))
    assert npc_props_sent(player) == [
        build_npc_props(npc.id, {NPCPROP.MESSAGE: "back soon"})[1:-1]]


def test_d_a_prop_cleared_while_away_goes_out_empty(tmp_path, clock):
    """A cleared value is still a change the returning client must see."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw", message="hello")
    player = loaded_player(server)
    away_and_back(server, player, clock, lambda: setattr(npc, "message", ""))
    assert npc_props_sent(player) == [
        build_npc_props(npc.id, {NPCPROP.MESSAGE: ""})[1:-1]]


def test_d_an_npc_added_while_away_arrives_whole(tmp_path, clock):
    server = two_levels(tmp_path)
    player = loaded_player(server)
    added = []
    away_and_back(server, player, clock,
                  lambda: added.append(place_npc(server, "a.nw", message="new")))
    assert npc_props_sent(player) == [added[0].build_props_packet()[1:-1]]


def test_d_an_npc_moved_in_while_away_arrives_whole(tmp_path, clock):
    """addNPC -> setLevel -> refreshModTimes (NPC.cpp:247-254, :600-607):
    every prop counts as new. Mutation: keeping the old stamps sends nothing."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "b.nw", message="traveller")
    npc.observe_props(1000.0)                       # long-standing props
    player = loaded_player(server)
    away_and_back(server, player, clock,
                  lambda: run(server.npc_manager.warp_npc(npc, "a.nw", 5.0, 5.0)))
    assert npc_props_sent(player) == [npc.build_props_packet()[1:-1]]


def test_d_an_npc_moving_away_is_announced_to_every_player(tmp_path, clock):
    """PLO_NPCMOVED goes to every player (NPC.cpp:210), so one that left the
    level earlier drops its copy."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw")
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    run(player.warp("b.nw", 30, 30))
    player.send_raw.reset_mock()
    server.world.add_level(Level("c.nw"))
    run(server.npc_manager.warp_npc(npc, "c.nw", 5.0, 5.0))
    assert ids(player) == [PLO.NPCMOVED]


def test_d_a_delete_reaches_past_visitors_who_saw_the_npc(tmp_path, clock):
    """Server::deleteNPC (Server.cpp:1625-1641): a client that left the level
    after the NPC arrived gets PLO_NPCDEL2 at once, so nothing is needed on
    its return; one that left before the NPC arrived never knew it; one that
    never visited is told anyway. Mutation: telling only the level fails."""
    server = two_levels(tmp_path)
    saw, missed, never = (loaded_player(server, pid) for pid in (2, 3, 4))
    clock.now = 5000.0
    run(missed.warp("a.nw", 30, 30))
    run(saw.warp("a.nw", 30, 30))
    clock.now = 5005.0
    run(missed.warp("b.nw", 30, 30))
    clock.now = 5006.0
    npc = place_npc(server, "a.nw")
    clock.now = 5010.0
    run(saw.warp("b.nw", 30, 30))
    run(never.warp("b.nw", 30, 30))
    for player in (saw, missed, never):
        player.send_raw.reset_mock()
    run(server.npc_manager.destroy_npc(npc))
    del2 = (PLO.NPCDEL2, bytes([len("a.nw") + 32]) + b"a.nw"
            + PacketBuilder().write_gint3(npc.id).build())
    assert sent(saw) == [del2] and sent(never) == [del2] and sent(missed) == []
    clock.now = 5020.0
    saw.send_raw.reset_mock()
    run(saw._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))
    assert PLO.NPCDEL not in ids(saw) and npc_props_sent(saw) == []


def test_d_move_queue_entries_since_the_leave(tmp_path, clock):
    """getMoveQueuePacketData skips moves older than the leave
    (NPC.cpp:513-517)."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw")
    player = loaded_player(server)
    clock.now = 5000.0
    run(player.warp("a.nw", 30, 30))
    npc_motion.queue_move(npc, server, (16 * 4, 0), 60.0, npc_motion.APPEND)
    run(npc_motion.send_unsent_moves(npc, server, []))
    clock.now = 5010.0
    run(player.warp("b.nw", 30, 30))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))
    assert PLO.MOVE2 not in ids(player)
    run(player.warp("b.nw", 30, 30))
    clock.now = 5020.0
    npc_motion.queue_move(npc, server, (16, 0), 60.0, npc_motion.APPEND)
    run(npc_motion.send_unsent_moves(npc, server, []))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(30, 30, "a.nw")))
    assert ids(player).count(PLO.MOVE2) == 1


def test_d_bytecode_follows_only_a_props_packet(tmp_path, clock):
    """PLO_NPCBYTECODE goes only after a sent PLO_NPCPROPS (Level.cpp:1473-1480)."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw")
    npc.client_bytecode = b"\x01\x02"
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    assert PLO.NPCBYTECODE in ids(player)
    away_and_back(server, player, clock)
    assert PLO.NPCBYTECODE not in ids(player)
    away_and_back(server, player, clock, lambda: setattr(npc, "gani", "walk"))
    assert PLO.NPCBYTECODE in ids(player)


def test_d_show_images_reset_then_only_what_changed(tmp_path, clock):
    """getShowImagesPacket(since): the reset (index 9), then every image with
    its props changed since the leave (NPC.cpp:269-292)."""
    server = two_levels(tmp_path)
    npc = place_npc(server, "a.nw")
    npc.showimgs = {0: {0: "a.png", 1: 10}, 1: {0: "b.png", 1: 20}}
    player = loaded_player(server)
    away_and_back(server, player, clock, lambda: npc.showimgs[1].__setitem__(1, 30))
    [body] = [body for packet_id, body in sent(player) if packet_id == PLO.SHOWIMGNPC]
    expected = (PacketBuilder().write_gint3(npc.id).write_gchar(9)
                .write_gchar(10).write_gchar(11).write_gchar(1).write_gchar(30).build())
    assert body == expected


# -- E. gmap hops by client props --------------------------------------------------------

from pygserver.protocol.packets import build_player_props  # noqa: E402


def map_props(**axes):
    """PLI_PLAYERPROPS body with GMAPLEVELX/Y (and any other props) in order."""
    keys = {"x": PLPROP.GMAPLEVELX, "y": PLPROP.GMAPLEVELY}
    out = b""
    for name, value in axes.items():
        out += build_player_props({keys[name]: value})[1:-1]
    return out


def on_cell(server, player, name="c0.nw"):
    run(player.warp(name, 30, 30))
    player.send_raw.reset_mock()


def test_e_a_hop_records_the_old_cell_and_sends_only_dynamic_data(tmp_path, clock):
    """Fact 5: GMAPLEVELX from the client runs leaveSubLevel(old) and
    sendDynamicLevelData for the new cell (PlayerProps.cpp:917-926): no
    static data, no PLO_LEVELMODTIME, no leave or enter. Mutation: treating
    the hop as a level entry sends LEVELNAME/LEVELMODTIME and fails."""
    server = gmap_world(tmp_path)
    player = loaded_player(server)
    on_cell(server, player)
    clock.now = 5050.0
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert player.level.name == "c1.nw"
    assert player.visited_levels.left_at("c0.nw") == 5050.0
    assert not player.visited_levels.visited("c1.nw")
    assert ids(player) == D_EMPTY
    # The block closes with the gmap's name (PlayerClient.cpp:1555).
    assert sent(player)[-1] == (PLO.SETACTIVELEVEL, b"m.gmap")


def test_e_a_diagonal_hop_marks_the_cell_in_between(tmp_path, clock):
    """X then Y in one packet: the X setter enters (new x, old y), the Y
    setter records that cell too and enters the final one."""
    server = gmap_world(tmp_path)
    player = loaded_player(server)
    on_cell(server, player)
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1, y=1)))
    assert player.level.name == "c3.nw"
    assert player.visited_levels.visited("c0.nw")
    assert player.visited_levels.visited("c1.nw")
    assert ids(player) == D_EMPTY + D_EMPTY


def test_e_board_changes_go_out_on_every_hop(tmp_path, clock):
    """Fact 4: a v6 client gets every board change on every entry and hop,
    with no time filter (Level.cpp:483-530; the `>= since` test exists only
    in the unused style 2). Mutation: filtering by the leave time fails."""
    server = gmap_world(tmp_path)
    player = loaded_player(server)
    clock.now = 4000.0
    server.world.get_level("c1.nw").alter_board(3, 4, 1, 1, [7], now=4000.0, respawn_seconds=0)
    on_cell(server, player, "c1.nw")
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=0)))
    clock.now = 5000.0
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert ids(player).count(PLO.BOARDMODIFY2) == 1


def test_e_an_unchanged_cell_is_ignored_and_a_bad_one_reverted(tmp_path, clock):
    """IGNORE when equal; REVERT (sent back) off a gmap or outside the map
    (PlayerProps.cpp:908-915)."""
    server = gmap_world(tmp_path)
    player = loaded_player(server)
    on_cell(server, player)
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=0)))
    assert ids(player) == [] and player.level.name == "c0.nw"
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=5)))
    assert sent(player) == [(PLO.PLAYERPROPS, build_player_props({PLPROP.GMAPLEVELX: 0})[1:-1])]
    run(player.warp("a.nw", 30, 30))
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert sent(player) == [(PLO.PLAYERPROPS, build_player_props({PLPROP.GMAPLEVELX: 0})[1:-1])]
    assert player.level.name == "a.nw"


def test_e_npcs_of_the_new_cell_come_as_deltas(tmp_path, clock):
    """The dynamic data of a hop uses the new cell's leave time (:1534)."""
    server = gmap_world(tmp_path)
    npc = place_npc(server, "c1.nw")
    player = loaded_player(server)
    on_cell(server, player)
    clock.now = 5010.0
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert npc_props_sent(player) == [npc.build_props_packet()[1:-1]]
    clock.now = 5020.0
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=0)))
    clock.now = 5030.0
    player.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert npc_props_sent(player) == []


def test_e_a_client_warp_inside_the_gmap_is_still_a_hop(tmp_path, clock):
    """A warp to a cell of the gmap the player is on: the cell's static data
    (PlayerClientPackets.cpp:79), then warp's same-level branch, setPosition,
    which runs the hop and sends the position props back (PlayerClient.cpp:
    1240-1244, :1182-1201). No PLO_PLAYERWARP2."""
    server = gmap_world(tmp_path)
    player = loaded_player(server)
    on_cell(server, player)
    run(player._dispatch_packet(PLI.LEVELWARP, warp_plain(0.5, 30, "c1.nw")))
    assert PLO.PLAYERWARP2 not in ids(player)
    assert ids(player) == S_FULL + D_EMPTY + [PLO.PLAYERPROPS]
    assert player.level.name == "c1.nw" and player.visited_levels.visited("c0.nw")
    echo = sent(player)[-1][1]
    assert echo == build_player_props({PLPROP.GMAPLEVELX: 1, PLPROP.X2: 0.5})[1:-1]


# -- F. review fixes: tile writes while away, reload order ------------------------------------

BOARD_MODIFY_IDS = (PLO.BOARDMODIFY, PLO.BOARDMODIFY2)


def return_after_write(tmp_path, clock, write):
    """A visits a.nw, leaves for b.nw, `write(server, level)` changes a tile
    while A is away, then A comes back with a plain warp. Returns A's packet
    ids for the return and the level."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    level = server.world.get_level("a.nw")
    clock.now = 5010.0
    run(player.warp("b.nw", 30, 30))
    write(server, level)
    player.send_raw.reset_mock()
    clock.now = 5020.0
    run(player._handle_level_warp(warp_plain(30, 30, "a.nw")))
    return ids(player), level


def run_gs1(server, level, code):
    from pygserver.gs1_host import compile_gs1, run_npc_event
    from pygserver.npc import NPC

    async def main():
        npc = NPC(1, "t")
        npc.level = level
        npc.gs1_program = compile_gs1(code)
        run_npc_event(npc, "created", server, None)
        for _ in range(3):
            await asyncio.sleep(0)
    run(main())


def modify_tiles(player):
    """The tile of every board-modify packet in `player`'s send log, in order
    (PLO_BOARDMODIFY / BOARDMODIFY2 end with the GSHORT tiles of a 1x1)."""
    out = []
    for packet_id, body in sent(player):
        if packet_id in BOARD_MODIFY_IDS:
            reader = PacketReader(body)
            if packet_id == PLO.BOARDMODIFY2:
                reader.read_gchar(), reader.read_gchar()
            reader.read_gchar(), reader.read_gchar(), reader.read_gchar(), reader.read_gchar()
            out.append(reader.read_gshort())
    return out


def test_f_a_gs1_tiles_write_alone_is_never_seen_or_recorded(tmp_path, clock):
    """GS1 `tiles[x,y] = n` only fills a script copy (GS1Variables.cpp:
    416-442, Level.cpp:3135-3153): nothing is sent to anyone present, nothing
    is recorded, and a player returning later is replayed nothing. Mutation:
    sending from the setter, or recording through alter_board there (the
    old Level.record_tile_write), fails."""
    seen = {}

    def write(server, level):
        other = loaded_player(server, 4)
        run(other.warp("a.nw", 30, 30))
        other.send_raw.reset_mock()
        seen["other"] = other
        seen["old"] = level.get_tile(5, 5)
        run_gs1(server, level, "if (created) { tiles[5,5] = %d; }" % ((seen["old"] + 7) & 0xFFF))

    got, level = return_after_write(tmp_path, clock, write)
    assert not any(i in BOARD_MODIFY_IDS for i in got), got
    assert modify_tiles(seen["other"]) == []
    assert level.board_changes == [] and level.get_tile(5, 5) == seen["old"]


def test_f_a_gs1_updateboard_sends_records_and_replays_while_live(tmp_path, clock):
    """`updateboard` publishes the script tiles as a board change
    (Level::updateBoard, Level.cpp:1847-1850 -> alterBoard :1668-1749): sent
    to the players present, recorded, and replayed to a returning visited
    player while it is live. Mutation: no alter_board in the flush (nothing
    recorded) or no send fails."""
    seen = {}

    def write(server, level):
        other = loaded_player(server, 4)
        run(other.warp("a.nw", 30, 30))
        other.send_raw.reset_mock()
        seen["other"] = other
        seen["old"] = level.get_tile(5, 5)
        seen["new"] = (seen["old"] + 7) & 0xFFF
        run_gs1(server, level, "if (created) { tiles[5,5] = %d; updateboard 5,5,1,1; }" % seen["new"])

    got, level = return_after_write(tmp_path, clock, write)
    assert modify_tiles(seen["other"]) == [seen["new"]]
    assert [(c.x, c.y, c.tiles, c.old_tiles) for c in level.board_changes] == [
        (5, 5, [seen["new"]], [seen["old"]])]
    assert any(i in BOARD_MODIFY_IDS for i in got), got


def test_f_a_gs1_updateboard_change_is_gone_after_it_reverted(tmp_path, clock):
    """The change reverts after `respawntime` (LevelBoardChange.cpp:39-49,
    scripting-gs1-commands.md:3234-3248): the swap is sent to the players on
    the level, and the entry stays in the list holding the old tiles, so a
    player returning after that is replayed the OLD tile, not the new one.
    Mutations: a permanent change (allow_respawn False) replays the new tile;
    an expiry that does not send leaves `other` without the revert."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    level = server.world.get_level("a.nw")
    clock.now = 5010.0
    run(player.warp("b.nw", 30, 30))
    other = loaded_player(server, 4)
    run(other.warp("a.nw", 30, 30))
    old = level.get_tile(5, 5)
    new = (old + 7) & 0xFFF
    run_gs1(server, level, "if (created) { tiles[5,5] = %d; updateboard 5,5,1,1; }" % new)
    other.send_raw.reset_mock()
    change = level.board_changes[0]
    assert run(server.board_changes.tick(change.expires_at + 1)) == 1
    assert modify_tiles(other) == [old]
    assert [c.tiles for c in level.board_changes] == [[old]]
    assert level.get_tile(5, 5) == old
    player.send_raw.reset_mock()
    clock.now = 5020.0
    run(player._handle_level_warp(warp_plain(30, 30, "a.nw")))
    assert modify_tiles(player) == [old]


def test_f_a_gs1_updateboard_on_a_gmap_goes_to_the_3x3_as_boardmodify2(tmp_path, clock):
    """On a gmap the change is PLO_BOARDMODIFY2 {mapX}{mapY}... to the players
    in the 3x3 around the segment (LevelBoardChange.cpp:57-58 ->
    Server.cpp:2390-2414). Mutation: sending to the segment only leaves the
    neighbour without the packet."""
    server = gmap_world(tmp_path)
    near, far = loaded_player(server, 2), loaded_player(server, 3)
    run(near.warp("c1.nw", 30, 30))
    run(far.warp("a.nw", 30, 30))
    level = server.world.get_level("c0.nw")
    near.send_raw.reset_mock()
    far.send_raw.reset_mock()
    run_gs1(server, level, "if (created) { tiles[5,5] = 321; updateboard 5,5,1,1; }")
    packets = [(i, b) for i, b in sent(near) if i == PLO.BOARDMODIFY2]
    assert len(packets) == 1
    reader = PacketReader(packets[0][1])
    assert [reader.read_gchar() for _ in range(6)] == [0, 0, 5, 5, 1, 1]
    assert reader.read_gshort() == 321
    assert [i for i, _ in sent(far) if i in BOARD_MODIFY_IDS] == []


def test_f_a_reload_drops_the_script_layer(tmp_path, clock):
    """Level::reload clears the script tiles (Level.cpp:657-658). Mutation:
    removing level.clear_script_tiles() from World.reload_level fails."""
    server = two_levels(tmp_path)
    level = server.world.get_level("a.nw")
    run_gs1(server, level, "if (created) { tiles[5,5] = 321; }")
    assert level.script_tiles_rect(5, 5, 1, 1) == [321]
    run(server.world.reload_level("a.nw"))
    assert level.script_tiles_rect(5, 5, 1, 1) == [0]


@pytest.mark.parametrize("savelevels,permanent", [(False, False), (True, True)])
def test_f_a_gs1_updateboard2_is_updateboard_unless_savelevels(tmp_path, clock, savelevels, permanent):
    """Level.cpp:1852-1875: with `savelevels` off updateboard2 is exactly
    updateboard (it reverts); with it on the change does not respawn.
    Mutation: ignoring `savelevels` fails one row either way."""
    server = two_levels(tmp_path, savelevels=savelevels)
    level = server.world.get_level("a.nw")
    run_gs1(server, level, "if (created) { tiles[5,5] = 321; updateboard2 5,5,1,1; }")
    change = level.board_changes[0]
    assert (change.expires_at is None) is permanent
    assert (change.old_tiles is None) is permanent


def test_f_a_gs1_updateboard2_with_levelsautosave_saves_the_level_file(tmp_path, clock):
    """`savelevels` and `levelsautosave` together keep the tiles in the level
    file (Level.cpp:1858-1873). Mutation: not saving leaves the file tile."""
    server = two_levels(tmp_path, savelevels=True, levelsautosave=True)
    level = server.world.get_level("a.nw")
    run_gs1(server, level, "if (created) { tiles[5,5] = 321; updateboard2 5,5,1,1; }")
    assert Level.load(str(tmp_path / "a.nw")).get_tile(5, 5) == 321
    assert level.get_base_tile(5, 5) == 321


def test_f_a_gs1_board_write_matches_the_reference_array_write(tmp_path, clock):
    """GS1 `board[i] = n` writes the level's own tile array and nothing else
    (GS1Variables.cpp:41-59): no board change, so a visited player whose client
    time is not older gets the skip block (PlayerClient.cpp:1471), while a
    first-time entrant gets the new tile in the board. Mutation: recording a
    board change in the board[] setter fails the first assertion."""
    def write(server, level):
        run_gs1(server, level, "if (created) { board[330] = 777; }")

    got, level = return_after_write(tmp_path, clock, write)
    assert level.board_changes == [] and level.get_tile(10, 5) == 777
    assert not any(i in BOARD_MODIFY_IDS for i in got) and PLO.BOARDPACKET not in got
    server = make_server(tmp_path)
    server.world.add_level(level)
    fresh = loaded_player(server, 4)
    run(fresh.warp("a.nw", 30, 30))
    assert PLO.BOARDPACKET in ids(fresh)


def test_f_a_blast_while_away_is_replayed_on_return(tmp_path, clock):
    """A bomb tile change is a client PLI_BOARDMODIFY, recorded by alterBoard
    (PlayerClientPackets.cpp:125, Level.cpp:1610-1749) and replayed on entry.
    Mutation: blasting with level.set_tile instead of alter_board loses it."""
    def write(server, level):
        from pygserver.combat import CombatManager
        for (dx, dy), tile in zip(((0, 0), (0, 1), (1, 0), (1, 1)), (0x02, 0x12, 0x03, 0x13)):
            level.set_tile(10 + dx, 10 + dy, tile)
        run(CombatManager(server)._destroy_blast_blocks(level, [(10.0, 10.0)]))

    got, level = return_after_write(tmp_path, clock, write)
    assert any(i in BOARD_MODIFY_IDS for i in got), got
    assert [(c.x, c.y, c.width, c.height) for c in level.board_changes] == [(10, 10, 2, 2)]


def test_f_reload_all_gives_the_occupants_the_new_board(tmp_path, clock):
    """Level::reload resets the records AFTER the occupants left
    (Level.cpp:962-963), then re-warps them, so the next leave writes a fresh
    record that is newer than the reset. Mutation: dropping the resend_level call
    from World.reload_levels (the old reload_levels, which reset before the
    occupants left) leaves the occupants without the new board."""
    server = two_levels(tmp_path)
    player = loaded_player(server)
    run(player.warp("a.nw", 30, 30))
    os.utime(tmp_path / "a.nw", (MOD_TIME + 100, MOD_TIME + 100))
    player.send_raw.reset_mock()
    run(server.world.reload_levels())
    assert PLO.BOARDPACKET in ids(player)       # re-warped in with the new board
