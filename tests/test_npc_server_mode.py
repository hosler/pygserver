"""NPC-server login announcements and client flag routing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.config import ServerConfig
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLO, PLTYPE
from pygserver.protocol.packets import build_flag_del, build_flag_set
from pygserver.server import GameServer

from .test_player_facade import login_frame


@pytest.fixture
def world(tmp_path):
    config = ServerConfig.from_server_dir(str(tmp_path))
    config.verify_login = False
    server = GameServer(config)
    players = []
    for player_id in (2, 3):
        player = Player(server, player_id, AsyncMock(), MagicMock())
        player.logged_in = True
        player.send_raw = AsyncMock()
        server.players[player_id] = player
        players.append(player)
    return server, *players


@pytest.mark.parametrize('serverside', [True, False])
@pytest.mark.parametrize('connection_type', [
    PLTYPE.CLIENT, PLTYPE.CLIENT2, PLTYPE.CLIENT3, PLTYPE.WEB,
    PLTYPE.RC, PLTYPE.RC2, PLTYPE.NC, PLTYPE.NPCSERVER,
])
async def test_login_response_advertises_npc_server_only_to_game_clients(
        world, serverside, connection_type):
    server, player, _ = world
    server.config.serverside = serverside
    player.connection_type = connection_type
    player.session.send_login_response = AsyncMock()

    await player.login_service.send_login_response()

    player.session.send_login_response.assert_awaited_once()
    if serverside and connection_type & PLTYPE.ANYCLIENT:
        # The reference packet has no boolean payload.
        player.send_raw.assert_awaited_once_with(bytes([PLO.HASNPCSERVER + 32, 10]))
    else:
        player.send_raw.assert_not_awaited()


@pytest.mark.parametrize('serverside', [True, False])
@pytest.mark.parametrize('entry_point', ['local', 'listserver'])
async def test_login_paths_announce_mode_before_warp_and_only_once(
        world, serverside, entry_point):
    server, player, _ = world
    server.config.serverside = serverside
    player.logged_in = False
    player.session.send_login_response = AsyncMock()
    player.session.read = AsyncMock(return_value=login_frame())
    events = []

    async def record_send(packet):
        events.append(packet)

    async def record_warp(*args):
        events.append('warp')

    player.send_raw.side_effect = record_send
    player.warp = AsyncMock(side_effect=record_warp)
    if entry_point == 'local':
        assert await player._handle_login()
    else:
        player.account_name = 'hosler'  # set by _handle_login before verification
        await player.send_login()
    await player.send_login()

    announcement = bytes([PLO.HASNPCSERVER + 32, 10])
    assert events == ([announcement, 'warp'] if serverside else ['warp'])
    player.session.send_login_response.assert_awaited_once()
    assert player.logged_in


@pytest.mark.parametrize('serverside', [True, False])
@pytest.mark.parametrize('bare', [True, False])
@pytest.mark.parametrize('delete_packet', [PLI.FLAGSET, PLI.FLAGDEL])
async def test_client_server_flag_routes_to_the_correct_store_and_audience(
        world, serverside, bare, delete_packet):
    server, player, other = world
    server.config.serverside = serverside
    name = 'server.x'
    value = True if bare else '1'
    payload = b'server.x' if bare else b'server.x=1'

    await player._dispatch_packet(PLI.FLAGSET, payload)
    await asyncio.sleep(0)

    if serverside:
        assert player.flags[name] == value
        assert name not in server.server_flags
        player.send_raw.assert_not_awaited()
        other.send_raw.assert_not_awaited()
        # A personal deletion must not touch a same-named server flag.
        server.server_flags.data[name] = 'server-owned'
    else:
        assert name not in player.flags
        assert server.get_flag(name) == value
        player.send_raw.assert_awaited_once_with(build_flag_set(name, value))
        other.send_raw.assert_awaited_once_with(build_flag_set(name, value))
    player.send_raw.reset_mock()
    other.send_raw.reset_mock()

    deletion = b'server.x=' if delete_packet == PLI.FLAGSET else b'server.x'
    await player._dispatch_packet(delete_packet, deletion)
    await asyncio.sleep(0)

    assert name not in player.flags
    if serverside:
        assert server.get_flag(name) == 'server-owned'
        player.send_raw.assert_not_awaited()
        other.send_raw.assert_not_awaited()
    else:
        assert name not in server.server_flags
        player.send_raw.assert_awaited_once_with(build_flag_del(name))
        other.send_raw.assert_awaited_once_with(build_flag_del(name))


@pytest.mark.parametrize('prefix', ['clientr.', 'serverr.'])
async def test_npc_server_protects_read_only_flags_from_client_sets_and_deletes(
        world, prefix):
    server, player, other = world
    name = prefix + 'x'
    player.flags[name] = 'personal'
    server.server_flags.data[name] = 'server-owned'

    for packet_id, suffix in [(PLI.FLAGSET, '=1'), (PLI.FLAGSET, ''),
                              (PLI.FLAGSET, '='), (PLI.FLAGDEL, '')]:
        await player._dispatch_packet(packet_id, (name + suffix).encode('ascii'))

    await asyncio.sleep(0)
    assert player.flags[name] == 'personal'
    assert server.get_flag(name) == 'server-owned'
    player.send_raw.assert_not_awaited()
    other.send_raw.assert_not_awaited()
