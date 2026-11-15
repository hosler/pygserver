"""PLO_ADDPLAYER for control clients: reference layout and when it is sent.

Reference: one layout for every RC version, {GSHORT id}{GCHAR len}{account}
then LEVEL(20) string, PLAYERLISTSTATUS(53) GBYTE1, NICKNAME(0) string,
COMMUNITYNAME(82) string (src/player/Player.cpp:789, :824-829). Sent to the
RCs at login (exchangeMyPropsWithOthers, which skips NC, :801-802) and again
to every RC and NC at the end of each level entry (PLTYPE_ANYCONTROL,
src/player/PlayerClient.cpp:1341-1347). A gmap hop is no level entry.
"""
from unittest.mock import AsyncMock, MagicMock

from pygserver.nc import NCManager
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLO
from pygserver.rc import RCManager
from tests.test_gmap_npc_range import gmap_world
from tests.test_level_entry_cache import loaded_player, map_props, run, sent


def _gchar(value):
    return bytes([value + 32])


def _gshort(value):
    return bytes([(value >> 7) + 32, (value & 0x7f) + 32])


def _gstring(value):
    raw = value.encode("latin-1")
    return _gchar(len(raw)) + raw


def reference_entry(player_id, account, level, status, nick, community):
    """Hand-written reference bytes (Player.cpp:789)."""
    return (_gchar(PLO.ADDPLAYER) + _gshort(player_id) + _gstring(account)
            + _gchar(20) + _gstring(level)
            + _gchar(53) + _gchar(status)
            + _gchar(0) + _gstring(nick)
            + _gchar(82) + _gstring(community) + b"\n")


def parse_entry(body):
    """The control-client reading of the entry (pyReborn
    packet_codec/rc.py parse_rc_add_player): id, account, then string props
    0/20/34/82 and byte props 53/81; an unknown id stops the scan."""
    pos = 2
    out = {"id": (body[0] - 32) << 7 | (body[1] - 32)}
    size = body[pos] - 32
    out["account"] = body[pos + 1:pos + 1 + size].decode("latin-1")
    pos += 1 + size
    names = {0: "nickname", 20: "level", 34: "account", 82: "community",
             53: "status", 81: "category"}
    while pos < len(body):
        prop = body[pos] - 32
        pos += 1
        if prop in (0, 20, 34, 82):
            size = body[pos] - 32
            out[names[prop]] = body[pos + 1:pos + 1 + size].decode("latin-1")
            pos += 1 + size
        elif prop in (53, 81):
            out[names[prop]] = body[pos] - 32
            pos += 1
        else:
            break
    return out, pos


def control_player(server, player_id, account):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    player.send_raw = AsyncMock()
    player.logged_in = True
    player.account_name = account
    server.players[player_id] = player
    return player


def with_controls(server):
    server.rc_manager = RCManager(server)
    server.nc_manager = NCManager(server)
    rc = control_player(server, 40, "staff")
    nc = control_player(server, 41, "npcs")
    server.rc_manager.register_session(rc, 0)
    server.nc_manager.register_session(nc)
    return rc, nc


def entries(player):
    return [body for pid, body in sent(player) if pid == PLO.ADDPLAYER]


# -- layout --------------------------------------------------------------------------

def test_entry_matches_the_reference_bytes_on_a_gmap(tmp_path):
    server = gmap_world(tmp_path)
    player = loaded_player(server, 7)
    player.account_name, player.nickname, player.status_msg = "alice", "Hero", 3
    run(player.warp("c11.nw", 30.0, 30.0))
    packet = RCManager.build_add_player(player)
    assert packet == reference_entry(7, "alice", "m.gmap", 3, "Hero", "alice")


def test_entry_matches_the_reference_bytes_on_a_plain_level(tmp_path):
    server = gmap_world(tmp_path)
    player = loaded_player(server, 300)
    player.account_name, player.nickname = "bob", "Bob (Guild)"
    run(player.warp("a.nw", 30.0, 30.0))
    packet = RCManager.build_add_player(player)
    expected = reference_entry(300, "bob", "a.nw", 0, "Bob (Guild)", "bob")
    assert packet == expected
    # The whole body parses, every prop known, nothing left over.
    parsed, end = parse_entry(packet[1:-1])
    assert end == len(packet) - 2
    assert parsed == {"id": 300, "account": "bob", "level": "a.nw", "status": 0,
                      "nickname": "Bob (Guild)", "community": "bob"}


def test_external_entries_keep_nickname_and_category():
    """IRC pseudo-players (PlayerRequestText.cpp:189): NICKNAME then 81."""
    packet = RCManager.build_add_player_fields(16000, "irc:#chan", "#chan (2,0)", 3)
    assert packet == (_gchar(PLO.ADDPLAYER) + _gshort(16000) + _gstring("irc:#chan")
                      + _gchar(0) + _gstring("#chan (2,0)") + _gchar(81) + _gchar(3)
                      + b"\n")


# -- when it is sent -----------------------------------------------------------------

def test_every_level_entry_resends_the_entry_to_rc_and_nc(tmp_path):
    server = gmap_world(tmp_path)
    rc, nc = with_controls(server)
    player = loaded_player(server, 2)
    player.account_name, player.nickname = "alice", "Hero"
    run(player.warp("a.nw", 30.0, 30.0))
    run(player.warp("c00.nw", 30.0, 30.0))
    for control in (rc, nc):
        got = [parse_entry(body)[0]["level"] for body in entries(control)]
        assert got == ["a.nw", "m.gmap"]
    assert not entries(player)


def test_a_gmap_hop_and_a_same_level_warp_send_no_entry(tmp_path):
    """The hop runs no enterLevel (PlayerProps.cpp:905-973); a warp inside the
    level is setPosition only (PlayerClient.cpp:1240-1243)."""
    server = gmap_world(tmp_path)
    rc, nc = with_controls(server)
    player = loaded_player(server, 2)
    run(player.warp("c00.nw", 30.0, 30.0))
    rc.send_raw.reset_mock()
    nc.send_raw.reset_mock()
    run(player._dispatch_packet(PLI.PLAYERPROPS, map_props(x=1)))
    assert player.level.name == "c10.nw"
    run(player.warp("c10.nw", 20.0, 20.0))
    assert not entries(rc) and not entries(nc)


def test_login_join_goes_to_rc_only(tmp_path):
    """exchangeMyPropsWithOthers skips NC (Player.cpp:801-802)."""
    server = gmap_world(tmp_path)
    rc, nc = with_controls(server)
    player = loaded_player(server, 2)
    run(player.warp("a.nw", 30.0, 30.0))
    rc.send_raw.reset_mock()
    nc.send_raw.reset_mock()
    run(server.rc_manager.notify_player_join(player))
    assert len(entries(rc)) == 1 and not entries(nc)


def test_real_login_gives_an_rc_the_join_entry_then_the_level_entry(tmp_path):
    """The login stream of a game client (PlayerClient.cpp:618 then :634):
    exchangeMyPropsWithOthers sends the RC the entry with the level the
    player is about to enter, and the first level entry sends the second one
    with the level it entered. Two entries, in that order, in the RC's own
    byte stream; an NC gets only the level entry. The start level is a gmap
    segment so the two entries differ: the segment, then the gmap."""
    from types import SimpleNamespace
    from tests.test_player_facade import login_frame

    server = gmap_world(tmp_path, start_level="c00.nw")
    server.listserver = None
    server.irc_manager = None
    account = SimpleNamespace(admin_rights=0, admin_ip="127.0.0.1",
                              is_banned=False, ban_reason="")
    server.account_manager = MagicMock()
    server.account_manager.get_account.return_value = account
    server.account_manager.load_player_from_account = MagicMock()
    rc, nc = with_controls(server)

    writer = MagicMock()
    writer.drain = AsyncMock()
    player = Player(server, 2, AsyncMock(), writer)
    server.players[2] = player
    player.session.writer.get_extra_info.return_value = ("127.0.0.1", 50000)
    player.session.read = AsyncMock(return_value=login_frame(
        username="alice", client_type=5))
    player.send_raw = AsyncMock()
    assert run(player._handle_login()) is True

    got = [parse_entry(body)[0] for body in entries(rc)]
    assert [(e["id"], e["account"], e["level"]) for e in got] == [
        (2, "alice", "c00.nw"), (2, "alice", "m.gmap")]
    assert [e["community"] for e in got] == ["alice", "alice"]
    assert [parse_entry(body)[0]["level"] for body in entries(nc)] == ["m.gmap"]
    # Both are the reference bytes.
    assert entries(rc) == [
        reference_entry(2, "alice", level, 0, player.nickname, "alice")[1:-1]
        for level in ("c00.nw", "m.gmap")]
