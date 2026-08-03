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
    account = SimpleNamespace(
        admin_rights=0, is_banned=False, ban_reason="")
    server.account_manager = MagicMock()
    server.account_manager.get_account.return_value = account
    server.account_manager.load_player_from_account = MagicMock()
    return server


def make_login_player(server, player_id, account, client_type):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    server.players[player_id] = player
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

    player.disconnect.assert_awaited_once_with("You do not have RC access.")
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
