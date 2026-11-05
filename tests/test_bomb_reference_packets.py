"""Bomb authority and hand-written reference wire layouts.

Oracles: FourPlay TClient.cpp:5430-5473; GServer-v2 Level.cpp:2085/2211
and PlayerClientPackets.cpp msgPLI_BOMBADD/msgPLI_EXPLOSION.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from pygserver.combat import Bomb
from pygserver.gs1_host import run_npc_event
from pygserver.handlers.combat import CombatHandlers
from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import build_bomb_add, build_explosion
from .test_gs1_hit_events import FakePlayer, make_server_and_level


@pytest.mark.parametrize('flags', [3, 7, 11, 15])
def test_bomb_add_matches_reference_bytes(flags):
    # id=11, owner=300, x=20.5, y=10, flags, ticks=54; every GCHAR adds 32.
    expected = bytes([43, 34, 76, 73, 52, 32 + flags, 86, 10])
    assert build_bomb_add(300, 20.5, 10, flags, 54) == expected


@pytest.mark.parametrize('owner,flags,owner_bytes', [(300, 11, [34, 76]), (0, 3, [32, 32])])
def test_explosion_matches_reference_bytes(owner, flags, owner_bytes):
    # id=36, GSHORT owner, radius=4 BEFORE x=20.5/y=10, then flags.
    expected = bytes([68, *owner_bytes, 36, 73, 52, 32 + flags, 10])
    assert build_explosion(owner, 20.5, 10, 4, flags) == expected


@pytest.mark.parametrize('landing', [False, True])
def test_bomb_add_relays_every_tick_byte_without_float_round_trip(landing):
    async def run():
        for ticks in range(224):
            server, level = make_server_and_level()
            player = FakePlayer(300)
            player.server, player.level, player.bombs = server, level, 1
            if landing:
                held = Bomb(1, 99, level.name, 20.5, 10, 1, 3, in_flight=True)
                server.combat_manager._held_bombs[player.id] = held
            # PLI payload has no owner id. TServerLevel.cpp:1116-1121 already
            # compensated this byte; neither fresh placement nor landing may
            # reconstruct it from seconds (notably ticks 12, 18, 54).
            await CombatHandlers._handle_bomb_add(
                player, bytes([73, 52, 43, 32 + ticks]))
            expected = bytes([43, 34, 76, 73, 52, 43, 32 + ticks, 10])
            server_packet = server.broadcasts[-1]
            assert server_packet == (level.name, expected, {player.id})
            bomb, = server.combat_manager.get_bombs_on_level(level.name)
            assert bomb.power == 3
            assert bomb.time_left == pytest.approx((ticks + 1) * .05)
            assert player.bombs == (1 if landing else 0)
    asyncio.run(run())


@pytest.mark.parametrize('held', [False, True])
@pytest.mark.parametrize('flags', [3, 11])
def test_client_explosion_relays_owner_and_effects_without_player_damage(held, flags):
    async def run():
        server, level = make_server_and_level()
        carrier = FakePlayer(300)
        carrier.server, carrier.level = server, level
        carrier.send_props = AsyncMock()
        victim = FakePlayer(301)
        victim.level = level
        victim.x, victim.y = 20, 9
        victim.send_props = AsyncMock()
        for player in (carrier, victim):
            level.add_player(player)
            server._players[player.id] = player
        target = server.npc_manager.create_npc(name='target', level=level, x=20, y=10)
        server.npc_manager.attach_gs1(target, 'if (exploded) { this.hit = 1; }')
        server.baddy_manager.handle_explosion = AsyncMock()
        offsets = ((0, 0), (0, 1), (1, 0), (1, 1))
        for (dx, dy), tile in zip(offsets, (2, 0x12, 3, 0x13)):
            level.set_tile(24 + dx, 10 + dy, tile)
        if held:
            # Pickup removes the other clients' copies. Attribute this report
            # to the carrier, even if someone else placed the original bomb.
            bomb = Bomb(1, 99, level.name, 20.5, 10, 3, 3)
            server.combat_manager._held_bombs[carrier.id] = bomb
        await CombatHandlers._handle_explosion(
            carrier, bytes([36, 73, 52, 32 + flags]))
        relayed_flags = flags | 8 if held else flags
        expected = bytes([68, 34, 76, 36, 73, 52, 32 + relayed_flags, 10])
        explosions = [call for call in server.broadcasts if call[1][0] == PLO.EXPLOSION + 32]
        assert explosions == [(level.name, expected, {carrier.id})]
        assert not any(call[1][0] in (PLO.BOMBDEL + 32, PLO.HURTPLAYER + 32)
                       for call in server.broadcasts)
        assert carrier.hearts == victim.hearts == 3
        assert carrier.sent == victim.sent == []
        victim.send_props.assert_not_awaited()
        if held:
            carrier.send_props.assert_awaited_once_with({PLPROP.CARRYSPRITE: -1})
        else:
            carrier.send_props.assert_not_awaited()
        assert not server.combat_manager._held_bombs
        assert target.gs1_scopes['this']['hit'] == 1
        assert target.hearts == 2
        assert tuple(level.get_tile(24 + dx, 10 + dy) for dx, dy in offsets) == (
            0x2a5, 0x2b5, 0x2a6, 0x2b6)
        server.baddy_manager.handle_explosion.assert_awaited_once_with(level.name, 20.5, 10, 4, 2)
    asyncio.run(run())


@pytest.mark.parametrize('command,expected', [
    ('putbomb 3,20.5,10', bytes([43, 32, 32, 73, 52, 35, 92, 10])),
    ('putexplosion 4,20.5,10', bytes([68, 32, 32, 36, 73, 52, 33, 10])),
    ('putexplosion2 3,4,20.5,10', bytes([68, 32, 32, 36, 73, 52, 35, 10])),
])
def test_script_effects_use_zero_owner_without_player_flag(command, expected):
    async def run():
        server, level = make_server_and_level()
        player = FakePlayer(300)
        player.level = level
        level.add_player(player)
        server._players[player.id] = player
        npc = server.npc_manager.create_npc(name='script', level=level, x=20, y=10)
        server.npc_manager.attach_gs1(npc, f'if (playerchats) {{ {command}; }}')
        run_npc_event(npc, 'playerchats', server, player)
        await asyncio.sleep(0)
        assert (level.name, expected, None) in server.broadcasts
        assert player.hearts == 3 and player.sent == []
        if command.startswith('putexplosion'):
            assert npc.hearts < 3
    asyncio.run(run())
