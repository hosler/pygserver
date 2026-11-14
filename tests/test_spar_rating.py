"""Sparring float math, login decay and kill-credit integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from reborn_protocol.props import (
    COLORS_CLASSIC, PLAYER_PROPS, StreamPolicy, parse_prop_stream,
)

from pygserver.account import Account, AccountManager
from pygserver.combat import CombatManager
from pygserver.config import ServerConfig
from pygserver.protocol.constants import PLPROP, PLO
from pygserver.protocol.packets import PacketReader
from pygserver.server import GameServer
from pygserver.spar_rating import decay_deviation, glicko_update

from .test_ap_system import make_player
from .test_player_facade import login_frame


@pytest.mark.parametrize('inputs,expected', [
    ((1500, 350, 1500, 350),
     (1662.212158203125, 290.2302551269531,
      1337.787841796875, 290.2302551269531)),
    ((1000, 80, 2200, 120),
     (2200.095703125, 119.96916198730469,
      999.9439697265625, 79.98751831054688)),
    ((0, 50, 4000, 50), (4000, 50, 0, 50)),
    ((1500, 350, 1500, 50),
     (1506.9659423828125, 50, 1337.787841796875, 290.2302551269531)),
    ((4000, 350, 0, 350),
     (471.80657958984375, 349.99993896484375,
      3528.193359375, 349.9999084472656)),
    ((4000, 350, 4000, 350),
     (4000, 290.23025512695312, 3837.787841796875, 290.23025512695312)),
    ((0, 350, 0, 350),
     (162.21218872070312, 290.23025512695312, 0, 290.23025512695312)),
    ((1500, 500, 1500, 500),
     (1741.4759521484375, 350, 1258.5240478515625, 350)),
    ((319.16790769450995, 119.83726590830905,
      404.00571763891645, 133.39208093302761),
     (436.7049255371094, 126.11624908447266,
      291.7842712402344, 114.31996154785156)),
])
def test_glicko_matches_compiled_reference(inputs, expected):
    # Expected values come from compiling the oracle expressions with g++
    # (C++20), preserving pow/powf overloads. Allow 1e-6 absolute rounding
    # variation from the platform math library; no relative tolerance.
    assert glicko_update(*inputs) == pytest.approx(expected, abs=1e-6, rel=0)


@pytest.mark.parametrize('days,expected', [
    (0, 50), (1, 67.08180236816406), (1000, 350),
])
def test_decay_matches_compiled_reference(days, expected):
    assert decay_deviation(50, days) == pytest.approx(expected, abs=1e-6, rel=0)


def test_rating_defaults_for_players_and_legacy_accounts():
    player = make_player(MagicMock())
    for value in (player, player.character, Account('new'), Account.from_dict({})):
        assert (value.elo_rating, value.elo_deviation, value.last_spar_time) == (
            1500.0, 350.0, 0)


@pytest.mark.parametrize('disabled', [True, False])
def test_rating_config_defaults_parse_and_write(tmp_path, disabled):
    assert ServerConfig().dontupdateratingd is False
    path = tmp_path / 'serveroptions.txt'
    path.write_text(f'dontupdateratingd = {str(disabled).upper()}# spar option\n')
    config = ServerConfig.from_file(str(path))
    assert config.dontupdateratingd is disabled
    config.to_file(str(path))
    assert ServerConfig.from_file(str(path)).dontupdateratingd is disabled


@pytest.mark.parametrize('peer,expected', [
    (('192.0.2.1', 12000), '192.0.2.1'),
    (('2001:db8::1', 12000, 0, 0), '2001:db8::1'),
    (None, None),
])
def test_remote_ip_reads_the_session_peername(peer, expected):
    player = make_player(MagicMock())
    player.session.writer.get_extra_info.return_value = peer
    assert player.remote_ip == expected
    player.session.writer.get_extra_info.assert_called_once_with('peername')


@pytest.mark.parametrize('same_ip,disabled', [
    (False, False), (True, False), (False, True), (True, True),
])
async def test_spar_credit_updates_only_for_allowed_remote_ips(same_ip, disabled):
    server = GameServer(ServerConfig(dontupdateratingd=disabled))
    manager = CombatManager(server)
    killer, victim = make_player(server, 2), make_player(server, 3)
    killer.session.writer.get_extra_info.return_value = ('192.0.2.1', 10000)
    victim.session.writer.get_extra_info.return_value = (
        '192.0.2.1' if same_ip else '192.0.2.2', 20000)
    victim.level.is_sparring_zone = True
    killer.last_spar_time, victim.last_spar_time = 123, 456
    killer.ap, victim.ap = 50, 100
    killer.ap_counter = 7
    with patch('pygserver.combat.time.time', return_value=1700000000):
        await manager.credit_kill(killer, victim)
        await manager.credit_kill(killer, victim)

    assert (killer.kills, victim.kills) == (0, 0)
    assert (killer.ap, killer.ap_counter, victim.ap) == (50, 7, 100)
    if same_ip or disabled:
        for player in (killer, victim):
            assert (player.elo_rating, player.elo_deviation) == (1500, 350)
            player.send_props.assert_not_awaited()
        assert (killer.last_spar_time, victim.last_spar_time) == (123, 456)
    else:
        win_rating, win_dev, lose_rating, lose_dev = glicko_update(
            1500, 350, 1500, 350)
        assert (killer.elo_rating, killer.elo_deviation) == (win_rating, win_dev)
        assert (victim.elo_rating, victim.elo_deviation) == (lose_rating, lose_dev)
        killer.send_props.assert_awaited_once_with({PLPROP.RATING: (win_rating, win_dev)})
        victim.send_props.assert_awaited_once_with({PLPROP.RATING: (lose_rating, lose_dev)})
        assert (killer.last_spar_time, victim.last_spar_time) == (1700000000,) * 2


@pytest.mark.parametrize('loser_rating', [0, 4000])
async def test_spar_sends_only_changed_ratings_but_always_sets_time(loser_rating):
    server = GameServer()
    manager = CombatManager(server)
    killer, victim = make_player(server, 2), make_player(server, 3)
    killer.session.writer.get_extra_info.return_value = ('192.0.2.1', 10000)
    victim.session.writer.get_extra_info.return_value = ('192.0.2.2', 20000)
    victim.level.is_sparring_zone = True
    killer.elo_rating, killer.elo_deviation = 4000, 50
    victim.elo_rating, victim.elo_deviation = loser_rating, 50
    with patch('pygserver.combat.time.time', return_value=1700000000):
        await manager.credit_kill(killer, victim)
    killer.send_props.assert_not_awaited()
    if loser_rating:
        assert victim.elo_rating < loser_rating
        victim.send_props.assert_awaited_once_with({
            PLPROP.RATING: (victim.elo_rating, victim.elo_deviation)})
    else:
        victim.send_props.assert_not_awaited()
    assert (killer.last_spar_time, victim.last_spar_time) == (1700000000,) * 2
    assert killer.kills == 0


@pytest.mark.parametrize('entry_point', ['local', 'listserver'])
@pytest.mark.parametrize('elapsed,initial_dev', [
    (0, 50), (86399, 50), (86400, 50), (86400, 350),
    (86400000, 50), (1700000000, 50), (-86399, 50), (-86400, 50),
])
async def test_login_decay_precedes_props_and_runs_once(
        tmp_path, entry_point, elapsed, initial_dev):
    server = GameServer(ServerConfig.from_server_dir(str(tmp_path)))
    manager = AccountManager(server, str(tmp_path / 'accounts'))
    server.account_manager = manager
    server.config.verify_login = entry_point == 'listserver'
    if entry_point == 'listserver':
        server.listserver = MagicMock()
        server.listserver.verify_account = AsyncMock()
        server.listserver.add_player = AsyncMock()
    now = 1700000000
    account = Account('tester', elo_rating=1723.5, elo_deviation=initial_dev,
                      last_spar_time=now - elapsed)
    account.set_password('pw')
    manager._save_account(account)
    manager._save_executor.shutdown(wait=True)
    player = make_player(server)
    player.logged_in = False
    player.session.read = AsyncMock(return_value=login_frame(username='tester'))
    player.session.send_login_response = AsyncMock()
    player.warp = AsyncMock()

    async def check_props(packet):
        assert player.elo_deviation == expected_dev
        assert player.last_spar_time == expected_time
        reader = PacketReader(packet)
        # The login-response frame leads with the reset (and, with the list
        # server up, SERVERLISTCONNECTED), then the stage-0 staff guilds and
        # status list, ahead of the props. Those two carry text, so the loop
        # reads them to the packet terminator.
        packet_id = reader.read_gchar()
        while packet_id in (PLO.SERVERLISTCONNECTED, PLO.RESETCLIENT,
                            PLO.STAFFGUILDS, PLO.STATUSLIST):
            while reader.read_byte() != ord('\n'):
                pass
            packet_id = reader.read_gchar()
        assert packet_id == PLO.PLAYERPROPS
        props, clean, _pos = parse_prop_stream(
            reader.remaining()[:-1], 0,
            StreamPolicy(table=PLAYER_PROPS, max_prop_id=100,
                         colors_len=COLORS_CLASSIC, require_full_consume=True), {
                PLPROP.RATING: lambda props, value: props.__setitem__(PLPROP.RATING, value),
            })
        assert clean
        assert props[PLPROP.RATING] == (1723, int(expected_dev))

    days = int(elapsed / 86400)
    expected_dev = decay_deviation(initial_dev, days) if days else initial_dev
    expected_time = now if days else account.last_spar_time
    player.session.send_login_response.side_effect = check_props
    try:
        with patch('pygserver.player_login.time.time', return_value=now):
            assert await player._handle_login()
            if entry_point == 'listserver':
                # Player.cpp:663: nothing is loaded (or decayed) before
                # the list server's verdict.
                assert not player.logged_in and not player.account_loaded
                assert (player.elo_deviation, player.last_spar_time) == (350.0, 0)
                await player.send_login()
        with patch('pygserver.player_login.time.time', return_value=now + 86400):
            await player.send_login()
        assert player.logged_in
        assert player.elo_deviation == expected_dev
        assert player.last_spar_time == expected_time
        player.session.send_login_response.assert_awaited_once()
    finally:
        manager._save_executor.shutdown(wait=True)
