"""Carry sentinel, held-bomb lifecycle and GMAP throw regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from reborn_protocol import PacketBuilder

from pygserver.audience import Audience
from pygserver.combat import Bomb, CombatManager, ThrownObject
from pygserver.gs1.builtins import _b_carrying, _b_carries_object
from pygserver.handlers.combat import CombatHandlers
from pygserver.level import Level
from pygserver.player import Player
from pygserver.server import GameServer
from pygserver.world import GMap, World


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100.0)
    server = SimpleNamespace(config=SimpleNamespace(levels_dir=str(tmp_path)),
                             broadcast_to_level=AsyncMock(), broadcast_to_world=AsyncMock(),
                             players={})
    server.world = World(server)
    level = Level('source.nw')
    server.world.add_level(level)
    server.get_player = server.players.get
    server.audience = Audience(server)
    server.combat_manager = CombatManager(server)

    def add_player(pid, current=level, x=20, y=20):
        player = SimpleNamespace(server=server, id=pid, x=x, y=y, level=current, bombs=3, hearts=3,
                                 direction=3, carrysprite=-1, send_raw=AsyncMock(),
                                 send_props=AsyncMock(), account_name='test')
        server.players[pid] = player
        current.add_player(player)
        return player

    return server, level, server.combat_manager, add_player


@pytest.mark.parametrize('code,kind', [(-1, None), (0, None), (200, None),
    (1, 'bush'), (3, 'stone'), (5, 'vase'), (7, 'sign'), (201, 'blackstone')])
def test_server_carry_builtins_distinguish_empty_from_bombs(code, kind):
    player = SimpleNamespace(carrysprite=code)
    assert _b_carrying(None, 'carrying', [], None, None, player) is (code != -1)
    for name in ('bush', 'stone', 'vase', 'sign', 'blackstone'):
        assert _b_carries_object(None, 'carries' + name, [], None, None, player) is (name == kind)
    assert not _b_carrying(None, 'carrying', [], None, None, None)


def hold(manager, player, level):
    bomb = Bomb(17, player.id, level.name, 10, 10, 1, 2)
    manager._held_bombs[player.id] = bomb
    player.carrysprite = 0
    return bomb


def test_disconnect_releases_held_bomb_and_flight(arena):
    server, level, manager, add = arena
    player = add(2)
    bomb = hold(manager, player, level)
    manager._throws.append(ThrownObject(player, level, 20, 20, 3, None, bomb=bomb))
    server.rc_manager = server.listserver = server.irc_manager = None
    player.build_leave_packet = Mock(return_value=b'left')
    asyncio.run(GameServer._remove_player(server, player))
    assert player.id not in manager._held_bombs
    assert not manager._throws
    assert player.id not in server.players


def test_warp_releases_held_bomb_and_carry_state(arena):
    server, level, manager, add = arena
    player = add(2)
    hold(manager, player, level)
    target = Level('target.nw')
    server.world.add_level(target)
    server.npc_manager = SimpleNamespace(on_player_leaves=AsyncMock(), on_player_enters=AsyncMock())
    player._send_level = AsyncMock()
    asyncio.run(Player.warp(player, target.name, 5, 5))
    assert player.id not in manager._held_bombs
    assert player.carrysprite == -1
    assert player.level is target


def test_reported_held_blast_applies_damage_and_board_changes_once(arena):
    server, level, manager, add = arena
    carrier = add(2)
    victim = add(3, x=9.5, y=9)
    hold(manager, carrier, level)
    pattern, replacement = (2, 18, 3, 19), (0x2a5, 0x2b5, 0x2a6, 0x2b6)
    for (x, y), tile in zip(((10, 10), (10, 11), (11, 10), (11, 11)), pattern):
        level.set_tile(x, y, tile)
    carrier.server = server
    packet = PacketBuilder()
    for value in (2, 20, 20, 1):
        packet.write_gchar(value)

    async def report():
        await CombatHandlers._handle_explosion(carrier, packet.build())
        await CombatHandlers._handle_explosion(carrier, packet.build())
    asyncio.run(report())
    assert victim.hearts == 2.5
    victim.send_raw.assert_awaited_once()
    assert carrier.id not in manager._held_bombs
    assert tuple(level.get_tile(x, y) for x, y in ((10, 10), (10, 11), (11, 10), (11, 11))) == replacement


def test_unrelated_bomb_add_consumes_ammo_and_keeps_held_bomb(arena):
    server, level, manager, add = arena
    player = add(2)
    held = hold(manager, player, level)
    held.landing_position = (10, 10)
    manager._next_bomb_id = 18
    placed = asyncio.run(manager.handle_bomb_add(player, 30, 30, 3, .75))
    assert placed is not held
    assert (placed.power, placed.time_left) == (3, .75)
    assert player.bombs == 2
    assert manager._held_bombs[player.id] is held


def test_matching_bomb_add_reuses_landing_with_one_tile_tolerance(arena):
    server, level, manager, add = arena
    player = add(2)
    held = hold(manager, player, level)
    held.landing_position = (10.25, 10.75)
    placed = asyncio.run(manager.handle_bomb_add(player, 10, 10.5, 1, 2))
    assert placed is held
    assert player.bombs == 3
    assert player.id not in manager._held_bombs


def grid(server):
    gmap = GMap('map.gmap')
    gmap.width = gmap.height = 3
    levels = {}
    for y in range(3):
        for x in range(3):
            level = Level(f'{x}-{y}.nw')
            server.world.add_level(level)
            gmap.grid[(x, y)] = level.name
            levels[(x, y)] = level
    server.world.add_gmap(gmap)
    return levels


@pytest.mark.parametrize('direction,x,y,target,expected', [
    (0, 20, 0, (1, 0), (20.5, 56)),
    (1, 0, 20, (0, 1), (55.5, 21)),
    (2, 20, 63, (1, 2), (20.5, 9)),
    (3, 63, 20, (2, 1), (8.5, 21)),
])
def test_throw_handoff_preserves_nine_tick_world_range(arena, direction, x, y, target, expected):
    server, _level, manager, add = arena
    levels = grid(server)
    source = levels[(1, 1)]
    thrower = add(2, current=source, x=x, y=y)
    thrown = ThrownObject(thrower, source, x + .5, y + 1, direction, None)
    for tick in range(1, 10):
        assert asyncio.run(manager._update_throw(thrown)) is (tick == 9)
    assert thrown.level is levels[target]
    assert (thrown.x, thrown.y) == expected


def test_throw_handoff_can_damage_victim_in_next_segment(arena):
    server, _level, manager, add = arena
    levels = grid(server)
    thrower = add(2, current=levels[(1, 1)], x=63, y=20)
    victim = add(3, current=levels[(2, 1)], x=0, y=20)
    thrown = ThrownObject(thrower, thrower.level, 63.5, 21, 3, None)
    assert not asyncio.run(manager._update_throw(thrown))
    assert asyncio.run(manager._update_throw(thrown))
    assert victim.hearts == 2


def test_throw_without_adjacent_segment_lands_at_edge(arena):
    server, level, manager, add = arena
    thrower = add(2, x=63, y=20)
    thrown = ThrownObject(thrower, level, 63.5, 21, 3, None)
    assert asyncio.run(manager._update_throw(thrown))
    assert thrown.level is level


def test_last_tick_handoff_defers_landing_until_next_tick(arena):
    server, _level, manager, add = arena
    levels = grid(server)
    thrower = add(2, current=levels[(1, 1)])
    thrown = ThrownObject(thrower, thrower.level, 55, 21, 3, None)
    for _ in range(9):
        assert not asyncio.run(manager._update_throw(thrown))
    assert asyncio.run(manager._update_throw(thrown))
    assert thrown.ticks_left == -1


def test_bomb_handoff_replacement_uses_destination_segment(arena):
    server, _level, manager, add = arena
    levels = grid(server)
    player = add(2, current=levels[(1, 1)], x=63, y=20)
    bomb = hold(manager, player, player.level)
    asyncio.run(manager.handle_throw_carried(player, 3, 0))
    for _ in range(9):
        asyncio.run(manager._tick())
    assert bomb.level_name == levels[(2, 1)].name
    result = asyncio.run(manager.handle_bomb_add(player, 8.5, 21.5, 1, 2))
    assert result is bomb
    assert manager._bombs[levels[(2, 1)].name][bomb.id] is bomb
    assert player.bombs == 3
