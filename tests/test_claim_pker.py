"""Client kill claims share credit with server-detected combat deaths."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.combat import DamageType
from pygserver.config import ServerConfig
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLPROP
from pygserver.protocol.packets import PacketBuilder
from pygserver.server import GameServer
from pygserver.combat import CombatManager


@pytest.fixture
def combat():
    server = GameServer(ServerConfig())
    server.npc_manager = None
    server.combat_manager = CombatManager(server)
    server.combat_manager._respawn_player = AsyncMock()
    players = []
    for player_id in (2, 3):
        player = Player(server, player_id, AsyncMock(), MagicMock())
        player.logged_in = True
        player.level = Level('combat.nw')
        player.send_props = AsyncMock()
        player.ap, player.ap_counter = 50, 7
        server.players[player_id] = player
        players.append(player)
    return server, *players


async def claim(victim, killer_id):
    await victim._dispatch_packet(
        PLI.CLAIMPKER, PacketBuilder().write_gshort(killer_id).build())


async def report_death(server, victim):
    # Exercise the actual client CURPOWER path, with no attacker on the wire.
    await victim._dispatch_packet(
        PLI.PLAYERPROPS,
        PacketBuilder().write_gchar(PLPROP.CURPOWER).write_gchar(0).build())
    await asyncio.gather(*server.combat_manager._respawn_tasks)
    assert victim.pending_revive


async def test_claim_after_client_reported_death_credits_kills_and_ap_once(combat):
    server, killer, victim = combat
    await report_death(server, victim)
    assert killer.kills == 0
    await claim(victim, killer.id)
    await claim(victim, killer.id)
    assert victim.kill_credited
    assert killer.flags['playerkiller'] is True
    assert killer.kills == 1
    assert (killer.ap, killer.ap_counter) == (44, 300)
    assert killer.send_props.await_count == 2
    killer.send_props.assert_any_await({PLPROP.KILLSCOUNT: 1})
    killer.send_props.assert_any_await({PLPROP.ALIGNMENT: 44})


async def test_alive_claim_is_ignored_but_marks_killer(combat):
    _, killer, victim = combat
    await claim(victim, killer.id)
    assert killer.flags['playerkiller'] is True
    assert killer.kills == 0
    assert (killer.ap, killer.ap_counter) == (50, 7)
    killer.send_props.assert_not_awaited()


@pytest.mark.parametrize('attacker', [3, 999])
async def test_self_and_unknown_claims_are_ignored(combat, attacker):
    server, killer, victim = combat
    await report_death(server, victim)
    await claim(victim, attacker)
    assert not victim.kill_credited
    assert killer.kills == 0
    assert not victim.flags


async def test_sword_kill_then_claim_credits_once(combat):
    server, killer, victim = combat
    victim.hearts = 0.5
    await server.combat_manager.apply_damage(victim, 1, 0, 0, DamageType.SWORD, killer.id)
    await asyncio.gather(*server.combat_manager._respawn_tasks)
    await claim(victim, killer.id)
    assert killer.kills == 1
    assert (killer.ap, killer.ap_counter) == (44, 300)
    assert killer.send_props.await_count == 2


async def test_sparring_claim_does_not_change_kills_or_ap(combat):
    server, killer, victim = combat
    killer.session.writer.get_extra_info.return_value = ('192.0.2.1', 10000)
    victim.session.writer.get_extra_info.return_value = ('192.0.2.2', 20000)
    victim.level.is_sparring_zone = True
    await report_death(server, victim)
    await claim(victim, killer.id)
    await claim(victim, killer.id)
    assert victim.kill_credited
    assert killer.kills == 0
    assert (killer.ap, killer.ap_counter) == (50, 7)
    assert killer.elo_rating > 1500
    killer.send_props.assert_awaited_once_with({
        PLPROP.RATING: (killer.elo_rating, killer.elo_deviation)})


async def test_revival_allows_credit_for_next_death(combat):
    server, killer, victim = combat
    await report_death(server, victim)
    await claim(victim, killer.id)
    victim.warp = AsyncMock()
    await server.combat_manager.revive_player(victim)
    assert not victim.kill_credited
    assert not victim.pending_revive
    await claim(victim, killer.id)
    assert killer.kills == 1
    await report_death(server, victim)
    await claim(victim, killer.id)
    assert killer.kills == 2
