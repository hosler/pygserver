"""Login refreshes idle accounts while preserving accounts used by live sessions."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import Account, AccountManager
from pygserver.config import ServerConfig
from pygserver.player import Player
from pygserver.server import GameServer

from .test_player_facade import login_frame


@pytest.fixture
def world(tmp_path):
    config = ServerConfig.from_server_dir(str(tmp_path))
    config.verify_login = False
    server = GameServer(config)
    manager = AccountManager(server, str(tmp_path / 'accounts'))
    server.account_manager = manager
    path = manager.accounts_dir / 'tester.json'
    path.write_text(json.dumps(Account('tester', bombs=0, arrows=0).to_dict()))
    cached = manager.get_account('tester')
    try:
        yield server, manager, cached, path
    finally:
        manager._save_executor.shutdown(wait=True)


def make_login_player(server, player_id=2):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    player.session.read = AsyncMock(return_value=login_frame(username='tester'))
    player.login_service.send_login_response = AsyncMock()
    player.warp = AsyncMock()
    server.players[player_id] = player
    return player


@pytest.mark.parametrize('other_name,connected', [('someone_else', True), ('TESTER', False)])
async def test_login_replaces_cached_account_with_disk_values(world, other_name, connected):
    server, manager, cached, path = world
    player = make_login_player(server)
    other = make_login_player(server, 3)
    other.account_name = other_name
    other.connected = connected
    other.logged_in = True
    stored = cached.to_dict()
    stored.update(bombs=10, arrows=5, level_name='reset.nw', x=12.5, y=19.0)
    path.write_text(json.dumps(stored))
    assert manager.get_account('tester') is cached

    assert await player._handle_login()

    refreshed = manager.get_account('tester')
    assert refreshed is not cached
    assert (refreshed.bombs, refreshed.arrows) == (10, 5)
    assert (refreshed.level_name, refreshed.x, refreshed.y) == ('reset.nw', 12.5, 19.0)
    assert (player.bombs, player.arrows) == (10, 5)


@pytest.mark.parametrize('logged_in', [True, False])
async def test_duplicate_login_preserves_connected_players_account(world, logged_in):
    # Player.cpp:719-741: a same-class login of an account whose live
    # connection sent data within 30 seconds is refused, and the live
    # connection's cached account is neither reloaded nor touched.
    server, manager, cached, path = world
    live = make_login_player(server, 3)
    live.account_name = 'TESTER'
    live.logged_in = logged_in
    live.bombs, live.arrows = 2, 1
    player = make_login_player(server)
    player.disconnect = AsyncMock()
    stored = cached.to_dict()
    stored.update(bombs=10, arrows=5)
    path.write_text(json.dumps(stored))

    await player._handle_login()

    player.disconnect.assert_awaited_once_with("Account is already in use.")
    assert not player.logged_in and not player.account_loaded
    assert manager.get_account('tester') is cached
    assert (cached.bombs, cached.arrows) == (0, 0)
    assert (live.bombs, live.arrows) == (2, 1)


async def test_idle_duplicate_is_kicked_and_its_state_carried_over(world):
    # Player.cpp:729-731: an idle (>30 s) same-class connection is kicked.
    # Its state is flushed first, so the new login loads it and the kicked
    # connection's own cleanup cannot write an older copy back afterwards.
    server, manager, cached, path = world
    live = make_login_player(server, 3)
    live.account_name = 'tester'
    live.logged_in = live.account_loaded = True
    live.bombs, live.arrows = 2, 1
    live.last_data_time = time.time() - 31
    live.disconnect = AsyncMock()
    player = make_login_player(server)

    assert await player._handle_login()

    live.disconnect.assert_awaited_once_with("Someone else has logged into your account.")
    assert not live.account_loaded
    assert player.logged_in and player.account_loaded
    assert (player.bombs, player.arrows) == (2, 1)
    live.bombs = 99
    assert not manager.save_player(live)  # what Player._cleanup calls
    manager._save_executor.submit(lambda: None).result()
    assert json.loads(path.read_text())['bombs'] == 2


@pytest.mark.parametrize('file_state', ['missing', 'invalid_json', 'unreadable'])
async def test_login_reload_falls_back_to_cache(world, file_state):
    server, manager, cached, path = world
    player = make_login_player(server)
    if file_state == 'invalid_json':
        path.write_text('{')
    else:
        path.unlink()
        if file_state == 'unreadable':
            path.mkdir()

    assert manager.get_account('tester', reload=True) is cached
    assert await player._handle_login()

    assert manager.get_account('tester') is cached
    assert (player.bombs, player.arrows) == (cached.bombs, cached.arrows)
