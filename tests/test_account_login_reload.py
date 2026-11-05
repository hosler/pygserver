"""Login refreshes idle accounts while preserving accounts used by live sessions."""

import json
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
    server, manager, cached, path = world
    live = make_login_player(server, 3)
    live.account_name = 'TESTER'
    live.logged_in = logged_in
    live.bombs, live.arrows = 2, 1
    player = make_login_player(server)
    stored = cached.to_dict()
    stored.update(bombs=10, arrows=5)
    path.write_text(json.dumps(stored))

    assert await player._handle_login()

    assert manager.get_account('tester') is cached
    assert (player.bombs, player.arrows) == (0, 0)
    assert (live.bombs, live.arrows) == (2, 1)


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
