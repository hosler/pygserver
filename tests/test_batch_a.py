"""Batch A packet-ordering, NPC gesture, revival and item boundary regressions."""

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from reborn_protocol import PLPROP

from pygserver.combat import CombatManager
from pygserver.items import ItemManager
from pygserver.level import Level
from pygserver.protocol.constants import LevelItemType
from pygserver.protocol.packets import build_bomb_add
from .test_lift_bombs import arena
from .test_npc_trigger_relay import make_world, add_npc


@pytest.mark.parametrize('direction', range(4))
@pytest.mark.parametrize('landing_tick', [0, 1, 5, 9])
def test_client_bomb_landing_precedes_server_simulation(monkeypatch, direction, landing_tick):
    """A landing packet can arrive before even the first server flight tick."""
    monkeypatch.setattr('time.time', lambda: 100.0)
    async def run():
        level, player, server, manager = arena()
        player.bombs = 10
        bomb = await manager.handle_bomb_add(player, 20.5, 20, 1)
        bomb.created_at = 100
        await manager.handle_bomb_del(player, 20.5, 20)
        await manager.handle_throw_carried(player, direction, 0)
        assert bomb.landing_position is None
        server.broadcast_to_level.reset_mock()
        player.send_props.reset_mock()
        dx, dy = ((0, -1), (-1, 0), (0, 1), (1, 0))[direction]
        x = math.floor((20.5 + dx * landing_tick) * 2) / 2
        y = math.floor((21 + dy * landing_tick - 2.375 + .2 * landing_tick + 1.5) * 2) / 2
        result = await manager.handle_bomb_add(player, x, y, 1)
        assert result is bomb and player.bombs == 9
        player.send_props.assert_not_awaited()
        server.broadcast_to_level.assert_awaited_once_with(
            level.name, build_bomb_add(player.id, x, y, 1, round(bomb.time_left / .05) - 1), exclude={player.id})
        assert not manager._throws and player.id not in manager._held_bombs
        # Clients own detonation; the server timer only expires level state.
        manager._expire_bomb = AsyncMock()
        for _ in range(12):
            await manager._tick()
        assert list(manager._bombs[level.name].values()) == [bomb]
        assert server.broadcast_to_level.await_count == 1
        manager._expire_bomb.assert_not_awaited()
    asyncio.run(run())


def test_distant_bomb_packet_does_not_consume_the_held_bomb(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100.0)
    async def run():
        _, player, _, manager = arena()
        player.bombs = 10
        await manager.handle_bomb_del(player, 10, 10)
        held = manager._held_bombs[player.id]
        await manager.handle_throw_carried(player, 3, 0)
        placed = await manager.handle_bomb_add(player, 50, 50, 1)
        assert placed is not held and manager._held_bombs[player.id] is held
        assert player.bombs == 9 and len(manager._throws) == 1
    asyncio.run(run())


@pytest.mark.parametrize('action,direction', [('push', 0), ('pull', 2)])
def test_gesture_trigger_reaches_real_server_gs1(tmp_path, action, direction):
    async def run():
        server, level, players = make_world(tmp_path)
        npc = add_npc(server, level, 10001, 9, 9,
                      f'if (action{action}) {{ this.hits++; this.direction=#p(0); }}')
        await server.npc_manager.on_trigger_action(players[0], 10, 10, f'{action},{direction}')
        assert npc.gs1_scopes['this'] == {'hits': 1.0, 'direction': float(direction)}
        assert npc._gs1_ctx.player is players[0]
    asyncio.run(run())


@pytest.mark.parametrize('ap,maximum,expected', [
    (0, 10, 3), (19, 10, 3), (20, 10, 5), (39, 10, 5),
    (20, 4, 4), (39, 3, 3), (40, 10, 10), (100, 10, 10),
])
@pytest.mark.parametrize('fallback', [False, True])
def test_server_revival_uses_alignment_rule(ap, maximum, expected, fallback):
    async def run():
        server = SimpleNamespace(config=SimpleNamespace(start_level='start.nw', start_x=30, start_y=30))
        manager = CombatManager(server)
        manager.respawn_time = 0
        player = SimpleNamespace(id=2, pending_revive=object(), connected=True,
                                 ap=ap, hearts=0, max_hearts=maximum, client_status=8,
                                 send_props=AsyncMock(), warp=AsyncMock(), nickname='test')
        if fallback:
            await manager._respawn_player(player, player.pending_revive)
        else:
            await manager.revive_player(player)
        assert player.hearts == expected and player.pending_revive is None
        player.send_props.assert_awaited_once_with({PLPROP.CURPOWER: expected * 2, PLPROP.STATUS: 0})
        player.warp.assert_awaited_once_with('start.nw', 30, 30)
    asyncio.run(run())


def item_arena(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('time.time', lambda: now[0])
    level = Level('items.nw')
    server = SimpleNamespace(broadcast_to_level=AsyncMock())
    manager = ItemManager(server)
    player = SimpleNamespace(id=2, level=level, x=10, y=10, layer=0)
    manager.give_item_to_player = AsyncMock()
    async def spawn():
        item = await manager.spawn_item(level, 10, 10, LevelItemType.GREENRUPEE)
        item.created_at = now[0]
        server.broadcast_to_level.reset_mock()
        return item
    item = asyncio.run(spawn())
    return now, level, server, manager, player, item


@pytest.mark.parametrize('age,allowed', [(0, False), (.1999, False), (.20, False),
                                        (.2499, False), (.25, True), (8.1999, True), (8.2, False)])
def test_item_take_age_boundaries(monkeypatch, age, allowed):
    now, level, _, manager, player, item = item_arena(monkeypatch)
    now[0] += age
    assert asyncio.run(manager.handle_item_take(player, item.x, item.y)) is allowed
    assert manager.give_item_to_player.await_count == int(allowed)
    if age == 8.2:
        assert not manager._items[level.name]


@pytest.mark.parametrize('axis', ['x', 'y'])
@pytest.mark.parametrize('delta,allowed', [(-2, False), (-1.999, True), (1.999, True), (2, False)])
def test_item_pickup_strict_body_centres(monkeypatch, axis, delta, allowed):
    now, _, _, manager, player, item = item_arena(monkeypatch)
    now[0] += .25
    player.x, player.y = item.x - .5, item.y - 1
    setattr(player, axis, getattr(player, axis) + delta)
    assert asyncio.run(manager.handle_item_take(player, item.x, item.y)) is allowed


def test_item_take_uses_player_position_and_layer_not_packet_position(monkeypatch):
    now, level, _, manager, player, item = item_arena(monkeypatch)
    now[0] += .25
    player.layer = 1
    assert not asyncio.run(manager.handle_item_take(player, item.x, item.y))
    player.layer, player.x = 0, 50
    assert not asyncio.run(manager.handle_item_take(player, item.x, item.y))
    player.x = 10
    assert asyncio.run(manager.handle_item_take(player, item.x, item.y))
    assert not manager._items[level.name]


def test_item_expires_at_server_8200ms_boundary(monkeypatch):
    now, level, server, manager, _, item = item_arena(monkeypatch)
    assert item.despawn_time == 8.2
    now[0] = 108.1999
    asyncio.run(manager._tick())
    assert manager._items[level.name]
    now[0] = 108.2
    asyncio.run(manager._tick())
    assert not manager._items[level.name]
    server.broadcast_to_level.assert_awaited_once()


def test_overlapping_item_takes_only_grant_once(monkeypatch):
    now, _, _, manager, player, item = item_arena(monkeypatch)
    now[0] += .25
    async def run():
        return await asyncio.gather(manager.handle_item_take(player, item.x, item.y),
                                    manager.handle_item_take(player, item.x, item.y))
    assert sorted(asyncio.run(run())) == [False, True]
    manager.give_item_to_player.assert_awaited_once()


def test_early_bomb_landing_can_cross_segment_before_server_tick(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100.0)
    async def run():
        origin, player, server, manager = arena()
        target = Level('next.nw')
        server.world.get_level = lambda name: origin if name == origin.name else target
        server.world.get_adjacent_levels = lambda name: {'e': target.name} if name == origin.name else {'w': origin.name}
        player.x, player.y = 63, 20
        await manager.handle_bomb_del(player, 10, 10)
        bomb = manager._held_bombs[player.id]
        await manager.handle_throw_carried(player, 3, 0)
        server.broadcast_to_level.reset_mock()
        restored = await manager.handle_bomb_add(player, .5, 20.5, 1)
        assert restored is bomb and bomb.level_name == target.name
        assert manager._bombs[target.name][bomb.id] is bomb
        assert not manager._throws and player.level is origin
        assert server.broadcast_to_level.await_args.args[0] == target.name
    asyncio.run(run())
