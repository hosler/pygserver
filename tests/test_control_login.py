"""Control-client login authorization and live player roster coverage."""

import asyncio
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from reborn_protocol.codec import Gen2Codec

from pygserver.config import ServerConfig
from pygserver.nc import NCManager
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLO, PLPERM, PLTYPE
from pygserver.protocol.packet_codec import PacketBuilder
from pygserver.protocol.parsers import parse_login_packet
from pygserver.rc import RCManager
from pygserver.server import GameServer

from .test_player_facade import login_frame


def make_server(staff=()):
    config = ServerConfig(staff=list(staff))
    config.name = "Test Server"
    config.start_level = "start.nw"
    server = GameServer(config)
    server.rc_manager = RCManager(server)
    server.listserver = None
    server.irc_manager = None
    server.gs2_manager = MagicMock()
    server.gs2_manager.announce_weapons = AsyncMock()
    # Player.cpp:689 admits RC/NC from staff whose admin-IP range matches.
    account = SimpleNamespace(
        admin_rights=0, admin_ip="127.0.0.1", is_banned=False, ban_reason="")
    server.account_manager = MagicMock()
    server.account_manager.get_account.return_value = account
    server.account_manager.load_player_from_account = MagicMock()
    return server


def make_login_player(server, player_id, account, client_type):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    server.players[player_id] = player
    player.session.writer.get_extra_info.return_value = ("127.0.0.1", 50000)
    player.session.read = AsyncMock(return_value=login_frame(
        username=account, client_type=client_type))
    player.login_service.send_login_response = AsyncMock()
    player.send_raw = AsyncMock()
    player.warp = AsyncMock()
    player.disconnect = AsyncMock()
    return player


def packet_ids(player):
    return [call.args[0][0] - 32 for call in player.send_raw.await_args_list]


def login_body(client_type, include_key):
    builder = PacketBuilder().write_gchar(client_type)
    if include_key:
        builder.write_gchar(7)
    return (builder.write_string("G3D0311C")
            .write_gstring("hosler")
            .write_gstring("testpass")
            .build())


def framed_login(client_type, include_key):
    compressed = zlib.compress(login_body(client_type, include_key))
    return struct.pack(">H", len(compressed)) + compressed


@pytest.mark.parametrize(
    ("client_type", "has_key", "generation"),
    [(0, True, 5), (1, False, 2), (3, False, 2),
     (5, True, 5), (6, True, 5)],
)
def test_login_packet_key_layout_follows_client_type(
        client_type, has_key, generation):
    login = parse_login_packet(login_body(client_type, has_key))

    assert login["client_type"] == client_type
    assert login["encryption_generation"] == generation
    assert login["encryption_key"] == (7 if has_key else 0)
    assert login["protocol"] == "G3D0311C"
    assert login["username"] == "hosler"
    assert login["password"] == "testpass"


@pytest.mark.parametrize(
    ("client_type", "expected"),
    [(5, PLTYPE.CLIENT3), (1, PLTYPE.RC), (3, PLTYPE.NC), (6, PLTYPE.RC2)])
def test_supported_login_type_bytes_are_accepted(client_type, expected):
    server = make_server(staff=("admin",))
    player = make_login_player(server, 3, "admin", client_type)

    assert asyncio.run(player._handle_login()) is True
    assert player.connection_type == expected


@pytest.mark.parametrize("client_type", [0, 2, 4, 7, 8, 9, 12, 100])
def test_unsupported_login_type_bytes_are_dropped(client_type):
    server = make_server(staff=("admin",))
    player = make_login_player(server, 3, "admin", client_type)

    assert asyncio.run(player._handle_login()) is False
    player.login_service.send_login_response.assert_not_awaited()
    server.account_manager.load_player_from_account.assert_not_called()


def test_staff_rc2_login_registers_sends_welcome_and_does_not_warp():
    server = make_server(staff=("admin",))
    player = make_login_player(server, 2, "admin", 6)

    assert asyncio.run(player._handle_login()) is True

    assert player.connection_type == PLTYPE.RC2
    assert server.rc_manager.get_session(player.id) is not None
    assert PLO.RC_CHAT in packet_ids(player)
    player.warp.assert_not_awaited()
    server.gs2_manager.announce_weapons.assert_not_awaited()


def test_nonstaff_rc2_login_is_denied_without_a_session():
    server = make_server()
    player = make_login_player(server, 2, "visitor", 6)

    assert asyncio.run(player._handle_login()) is True

    player.disconnect.assert_awaited_once_with("You do not have RC rights.")
    assert server.rc_manager.get_session(player.id) is None
    assert player.logged_in is False


def test_nc_login_selects_gen2_and_registers_session():
    server = make_server(staff=("hosler",))
    server.nc_manager = NCManager(server)
    player = make_login_player(server, 2, "hosler", 3)
    player.session.read.return_value = framed_login(3, include_key=False)

    assert asyncio.run(player._handle_login()) is True

    assert player.connection_type == PLTYPE.NC
    assert isinstance(player.session.codec, Gen2Codec)
    assert server.nc_manager.get_session(player.id) is not None


def make_wire_player(server, client_type, connected, account="player"):
    """A player whose session records the frames the real login flow emits.

    Nothing in Player or LoginService is replaced: send_raw/send_packet and
    LoginService.send_login_response run for real, and the recorder sits at
    the session boundary. The first frame is the plain-zlib login response, so
    every packet that must lead the login has to be inside it.
    """
    from pygserver.listserver import ServerListClient
    server.listserver = ServerListClient(server)
    server.listserver.connected = connected
    server.listserver._send_packet = AsyncMock()
    player = make_login_player(server, 2, account, client_type)
    del player.send_raw
    del player.login_service.send_login_response
    trace = []

    def ids(data):
        return [chunk[0] - 32 for chunk in data.split(b"\n") if chunk]

    async def login_frame_sent(data):
        trace.append(("login-frame", ids(data)))

    async def frame_sent(data):
        trace.append(("frame", ids(data)))

    async def weapons_announced(target):
        trace.append(("weapons", []))

    player.session.send_login_response = login_frame_sent
    player.session.send = frame_sent
    server.gs2_manager.announce_weapons.side_effect = weapons_announced
    return player, trace


def flat_ids(trace):
    return [packet for _, packets in trace for packet in packets]


@pytest.mark.parametrize('client_type', [5])
@pytest.mark.parametrize('connected', [True, False])
def test_game_login_leads_with_serverlist_connected_then_reset_then_props(
        client_type, connected):
    # GServer-v2 PlayerClient.cpp:487-492 (origin/beta4): SERVERLISTCONNECTED
    # when connected, PLO_RESETCLIENT, then the Stage-1 PLO_PLAYERPROPS.
    server = make_server()
    player, trace = make_wire_player(server, client_type, connected)

    assert asyncio.run(player._handle_login()) is True

    # Literal wire ids: 190 = PLO_SERVERLISTCONNECTED, 194 = PLO_RESETCLIENT.
    lead = [190, 194] if connected else [194]
    first_kind, first = trace[0]
    assert first_kind == "login-frame"
    assert first[:len(lead) + 1] == lead + [PLO.PLAYERPROPS]
    ids = flat_ids(trace)
    assert ids.count(194) == 1
    assert ids.count(190) == int(connected)
    assert PLO.SERVERLISTCONNECTED == 190
    # Weapons are announced only after the reset has gone out.
    weapons = next(i for i, (kind, _) in enumerate(trace) if kind == "weapons")
    assert weapons > 0 and 194 in flat_ids(trace[:weapons])
    assert player.logged_in


@pytest.mark.parametrize(('client_type', 'frame'), [(1, True), (6, False)])
@pytest.mark.parametrize('connected', [True, False])
def test_rc_login_resets_first_then_reports_serverlist(client_type, frame, connected):
    # PlayerRC.cpp:233-252 (origin/beta4): reset, welcome text, then
    # SERVERLISTCONNECTED. No NC counterpart exists.
    server = make_server(staff=("hosler",))
    account = "hosler" if frame else "admin"
    if not frame:
        server.config.staff = ["admin"]
    player, trace = make_wire_player(server, client_type, connected, account)
    if frame:
        player.session.read.return_value = framed_login(client_type, include_key=False)

    assert asyncio.run(player._handle_login()) is True

    expected = [194, PLO.SERVERLISTCONNECTED] if connected else [194]
    assert trace[0][0] == "login-frame"
    assert trace[0][1][:len(expected) + 1] == expected + [PLO.PLAYERPROPS]
    assert flat_ids(trace).count(194) == 1


@pytest.mark.parametrize('connected', [True, False])
def test_nc_login_sends_neither_reset_nor_serverlist_connected(connected):
    # PlayerNC.cpp:181-221 (origin/beta4) has no PLO_RESETCLIENT and no
    # SERVERLISTCONNECTED.
    server = make_server(staff=("hosler",))
    server.nc_manager = NCManager(server)
    player, trace = make_wire_player(server, 3, connected, "hosler")
    player.session.read.return_value = framed_login(3, include_key=False)

    assert asyncio.run(player._handle_login()) is True

    ids = flat_ids(trace)
    assert trace[0][1][0] == PLO.PLAYERPROPS
    assert 194 not in ids
    assert PLO.SERVERLISTCONNECTED not in ids


def test_nc_weapon_list_request_and_reply_round_trip_through_gen2():
    server = make_server(staff=("hosler",))
    server.nc_manager = NCManager(server)
    server.gs2_manager.weapons = {
        "bow": SimpleNamespace(name="bow"),
        "sword": SimpleNamespace(name="sword"),
    }
    writer = MagicMock()
    writer.drain = AsyncMock()
    player = Player(server, 2, AsyncMock(), writer)
    player.account_name = "hosler"
    player.connection_type = PLTYPE.NC
    player.session.start_codec(generation=2)
    server.nc_manager.register_session(player)
    client_codec = Gen2Codec()
    request = client_codec.send_packet(
        bytes([PLI.NC_WEAPONLISTGET + 32]) + b"\n")

    asyncio.run(player._process_data(request))

    encoded_reply = writer.write.call_args.args[0]
    reply_length = struct.unpack(">H", encoded_reply[:2])[0]
    reply = client_codec.recv_packet(encoded_reply[2:2 + reply_length])
    assert reply[0] - 32 == PLO.NC_WEAPONLISTGET
    assert b"bow" in reply
    assert b"sword" in reply


def test_client3_login_still_enters_the_world_without_control_session():
    server = make_server()
    player = make_login_player(server, 2, "player", 5)

    assert asyncio.run(player._handle_login()) is True

    assert player.connection_type == PLTYPE.CLIENT3
    assert server.rc_manager.get_session(player.id) is None
    player.warp.assert_awaited_once_with(
        server.config.start_level, server.config.start_x,
        server.config.start_y)


def test_game_player_join_and_leave_update_control_roster():
    server = make_server(staff=("admin",))
    control = make_login_player(server, 2, "admin", 6)
    control.account_name = "admin"
    control.nickname = "admin"
    server.rc_manager.register_session(control, PLPERM.WARPTO)
    control.send_raw.reset_mock()

    player = make_login_player(server, 3, "player", 5)
    asyncio.run(player._handle_login())

    assert packet_ids(control) == [PLO.ADDPLAYER]
    control.send_raw.reset_mock()

    asyncio.run(server._remove_player(player))

    assert packet_ids(control) == [PLO.DELPLAYER]
