"""Alignment timers, kill penalties, configuration and persistence."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pygserver.account import Account, AccountManager
from pygserver.ap_system import ap_threshold, kill_penalty, regen_step
from pygserver.combat import CombatManager
from pygserver.config import ServerConfig
from pygserver.level import Level
from pygserver.npc import NPC
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLPROP, PLSTATUS
from pygserver.protocol.packets import PacketBuilder
from pygserver.server import GameServer


APTIMES = [30, 90, 300, 600, 1200]


@pytest.mark.parametrize('ap,threshold', [
    (0, 30), (19, 30), (20, 90), (39, 90), (40, 300),
    (59, 300), (60, 600), (79, 600), (80, 1200), (100, 1200),
])
def test_ap_threshold(ap, threshold):
    assert ap_threshold(ap, APTIMES) == threshold


@pytest.mark.parametrize('ap,counter,expected', [
    (50, 2, (50, 1, False)),
    (50, 1, (50, 0, False)),
    (50, 0, (51, 300, True)),
    (19, 0, (20, 90, True)),
    (39, 0, (40, 300, True)),
    (59, 0, (60, 600, True)),
    (79, 0, (80, 1200, True)),
    (99, 0, (100, 1200, True)),
    (100, 0, (100, 1200, False)),
    (100, 1, (100, 0, False)),
])
def test_regen_step(ap, counter, expected):
    assert regen_step(ap, counter, APTIMES) == expected


@pytest.mark.parametrize('killer,victim,expected', [
    (50, 50, (44, 300)),
    (50, 19, None),
    (0, 50, None),
    (1, 100, (0, 30)),
    (20, 20, (18, 30)),
    (40, 20, (37, 90)),
    (60, 20, (56, 300)),
    (80, 20, (75, 600)),
    (100, 100, (70, 600)),
])
def test_kill_penalty(killer, victim, expected):
    assert kill_penalty(killer, victim, APTIMES) == expected


def test_ap_config_defaults_parse_and_write(tmp_path):
    defaults = ServerConfig()
    assert defaults.apsystem is True
    assert defaults.aptime == APTIMES
    path = tmp_path / 'serveroptions.txt'
    path.write_text(
        'apsystem = false# disabled\n'
        'aptime0 = 1 # seconds\n'
        'aptime1 = 2# seconds\n'
        'aptime2 = 3\n'
        'aptime3 = 4\n'
        'aptime4 = 5\n'
    )
    config = ServerConfig.from_file(str(path))
    assert config.apsystem is False
    assert config.aptime == [1, 2, 3, 4, 5]
    assert defaults.aptime == APTIMES
    config.to_file(str(path))
    restored = ServerConfig.from_file(str(path))
    assert restored.apsystem is False
    assert restored.aptime == config.aptime
    path.write_text('apsystem = true # enabled\naptime2 = 42\n')
    assert ServerConfig.from_file(str(path)).apsystem is True
    assert ServerConfig.from_file(str(path)).aptime == [30, 90, 42, 600, 1200]


def make_player(server, player_id=2):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    player.logged_in = True
    player.level = Level('test.nw')
    player.send_props = AsyncMock()
    return player


def test_ap_counter_account_round_trip_and_legacy_default(tmp_path):
    manager = AccountManager(MagicMock(), str(tmp_path))
    try:
        player = make_player(MagicMock())
        assert player.ap_counter == 0
        assert Account('new').ap_counter == 0
        player.ap_counter = 1199
        account = Account('tester')
        manager.save_player_to_account(player, account)
        manager._save_executor.shutdown(wait=True)
        stored = json.loads((tmp_path / 'tester.json').read_text())
        assert stored['ap_counter'] == 1199
        loaded = manager._load_account('tester')
        assert loaded.ap_counter == 1199
        restored_player = make_player(MagicMock())
        manager.load_player_from_account(restored_player, loaded)
        assert restored_player.ap_counter == 1199
        del stored['ap_counter']
        (tmp_path / 'legacy.json').write_text(json.dumps(stored))
        assert manager._load_account('legacy').ap_counter == 0
    finally:
        manager._save_executor.shutdown(wait=True)


@pytest.mark.parametrize('enabled,logged_in,has_level,paused,sparring,ap,counter,expected', [
    (True, True, True, False, False, 19, 0, (20, 90)),
    (True, True, True, False, False, 50, 1, (50, 0)),
    (True, True, True, False, False, 100, 0, (100, 1200)),
    (False, True, True, False, False, 50, 0, (50, 0)),
    (True, False, True, False, False, 50, 0, (50, 0)),
    (True, True, False, False, False, 50, 0, (50, 0)),
    (True, True, True, True, False, 50, 7, (50, 7)),
    (True, True, True, False, True, 50, 7, (50, 7)),
])
async def test_server_regen_eligibility_and_sends(
        enabled, logged_in, has_level, paused, sparring, ap, counter, expected):
    server = GameServer(ServerConfig(apsystem=enabled))
    player = make_player(server)
    player.logged_in = logged_in
    player.ap, player.ap_counter = ap, counter
    player.level.is_sparring_zone = sparring
    if not has_level:
        player.level = None
    if paused:
        # Exercise incoming STATUS storage rather than setting it directly.
        packet = (PacketBuilder().write_gchar(PLPROP.STATUS)
                  .write_gchar(PLSTATUS.PAUSED).build())
        await player._dispatch_packet(PLI.PLAYERPROPS, packet)
        assert player.client_status == PLSTATUS.PAUSED
        player.send_props.reset_mock()
    server.players[player.id] = player
    await server._regen_ap()
    assert (player.ap, player.ap_counter) == expected
    if player.ap != ap:
        player.send_props.assert_awaited_once_with({PLPROP.ALIGNMENT: expected[0]})
    else:
        player.send_props.assert_not_awaited()


async def test_main_loop_runs_one_ap_step_per_second():
    server = GameServer()
    player = make_player(server)
    player.ap = 19
    server.players[player.id] = player
    server.npc_manager.tick = AsyncMock()
    server._send_heartbeat = AsyncMock()
    times = [100, 100.05, 100.99, 101, 101.5, 102]
    ticks = 0

    async def tick_sleep(delay):
        nonlocal ticks
        assert delay == 0.05
        ticks += 1
        if ticks == len(times):
            server.running = False

    server.running = True
    with patch('pygserver.server.time.time', side_effect=times), \
            patch('pygserver.server.asyncio.sleep', side_effect=tick_sleep):
        await server._main_loop()
    assert player.ap == 20
    assert player.ap_counter == 88
    player.send_props.assert_awaited_once_with({PLPROP.ALIGNMENT: 20})


@pytest.mark.parametrize('enabled,sparring,victim_ap,expected', [
    (True, False, 50, (44, 300)),
    (False, False, 50, (50, 7)),
    (True, True, 50, (50, 7)),
    (True, False, 19, (50, 7)),
])
async def test_pvp_kill_ap_update(enabled, sparring, victim_ap, expected):
    server = GameServer(ServerConfig(apsystem=enabled))
    server.npc_manager = None
    manager = CombatManager(server)
    manager._respawn_player = AsyncMock()
    killer = make_player(server, 2)
    victim = make_player(server, 3)
    killer.ap, killer.ap_counter = 50, 7
    victim.ap = victim_ap
    victim.level.is_sparring_zone = sparring
    server.players[killer.id] = killer
    await manager.handle_player_death(victim, killer.id)
    await asyncio.gather(*manager._respawn_tasks)
    assert (killer.ap, killer.ap_counter) == expected
    killer.send_props.assert_any_await({PLPROP.KILLSCOUNT: 1})
    if killer.ap != 50:
        killer.send_props.assert_any_await({PLPROP.ALIGNMENT: expected[0]})
        assert killer.send_props.await_count == 2
    else:
        assert killer.send_props.await_count == 1


@pytest.mark.parametrize('serverside,code,sparring', [
    (True, '//#CLIENTSIDE\n  \nsparringzone;', True),
    (False, '  \nsparringzone;', True),
    (True, 'sparringzone;', False),
    (False, '// comment\nsparringzone;', False),
    (False, 'Sparringzone;', False),
])
async def test_sparring_zone_cached_from_client_script(serverside, code, sparring):
    server = GameServer(ServerConfig(serverside=serverside))
    level = Level('test.nw')
    npc = NPC(1)
    level.add_npc(npc)
    server.npc_manager.attach_gs1(npc, code, fire_created=False)
    assert level.is_sparring_zone is sparring
    level.remove_npc(npc)
    assert level.is_sparring_zone is sparring
    destination = Level('destination.nw')
    destination.add_npc(npc)
    assert destination.is_sparring_zone is sparring
