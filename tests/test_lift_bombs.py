"""Bomb carry/throw lifecycle and signed carry property regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from reborn_protocol import PLPROP

from pygserver.combat import Bomb, CombatManager
from pygserver.level import Level
from pygserver.handlers.movement import MovementHandlers
from pygserver.protocol.packets import (
    build_player_props, build_other_player_props, parse_player_props,
)


def arena():
    level = Level('lift.nw')
    player = SimpleNamespace(id=2, x=20, y=20, level=level, bombs=0, carrysprite=0,
                             send_raw=AsyncMock(), send_props=AsyncMock())
    server = SimpleNamespace(
        broadcast_to_level=AsyncMock(), get_player=lambda pid: player,
        audience=SimpleNamespace(players_on_level=lambda *args, **kwargs: []),
        world=SimpleNamespace(get_level=lambda name: level),
    )
    manager = CombatManager(server)
    manager._bombs[level.name] = {1: Bomb(1, 99, level.name, 10, 10, 1, 3, created_at=99)}
    return level, player, server, manager


@pytest.mark.parametrize('code', [-1, 0, 1, 3, 5, 7, 200, 201])
def test_carry_byte_round_trip(code):
    props = {PLPROP.CARRYSPRITE: code}
    own = build_player_props(props)
    assert own[1:-1] == bytes([51, code + 32])
    assert parse_player_props(own[1:-1])[PLPROP.CARRYSPRITE] == code
    other = build_other_player_props(8, props)
    assert other[3:-1] == bytes([51, code + 32])


@pytest.mark.parametrize('code', [-1, 0, 200])
def test_carry_update_relay_preserves_bomb_and_empty_values(code):
    sender = SimpleNamespace(id=8, level=SimpleNamespace(name='lift.nw'),
                              server=SimpleNamespace(config=SimpleNamespace(),
                                                     broadcast_nearby=AsyncMock()))
    asyncio.run(MovementHandlers._handle_player_props(sender, bytes([51, code + 32])))
    assert sender.carrysprite == code
    packet = sender.server.broadcast_nearby.await_args.args[1]
    assert packet[3:-1] == bytes([51, code + 32])


def test_nonowner_pickup_pauses_remaining_fuse_without_refunding_ammo(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100)
    level, player, server, manager = arena()
    bomb = manager._bombs[level.name][1]
    asyncio.run(manager.handle_bomb_del(player, 10, 10))
    assert not manager._bombs[level.name]
    assert manager._held_bombs[player.id] is bomb
    assert bomb.time_left == 2
    assert player.bombs == 0
    player.send_props.assert_not_awaited()
    monkeypatch.setattr('time.time', lambda: 110)
    # Held bombs wait for the client report, outside the server expiry timer.
    manager._expire_bomb = AsyncMock()
    asyncio.run(manager._tick())
    manager._expire_bomb.assert_not_awaited()


def test_replacement_resumes_same_fuse_without_inventory_cost(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100)
    level, player, server, manager = arena()
    asyncio.run(manager.handle_bomb_del(player, 10, 10))
    original = manager._held_bombs[player.id]
    asyncio.run(manager.handle_throw_carried(player, 3, 0))
    manager._is_throw_wall = lambda thrown: False
    for _ in range(9):
        asyncio.run(manager._tick())
    monkeypatch.setattr('time.time', lambda: 110)
    result = asyncio.run(manager.handle_bomb_add(player, 29.5, 21.5, 1, 3))
    assert result is original
    assert (result.time_left, result.created_at) == (2, 110)
    assert (result.x, result.y) == (29.5, 21.5)
    assert player.id not in manager._held_bombs
    assert manager._bombs[level.name][1] is result
    assert player.bombs == 0
    player.send_props.assert_not_awaited()


def test_fuse_stays_paused_through_flight(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100)
    level, player, server, manager = arena()
    asyncio.run(manager.handle_bomb_del(player, 10, 10))
    asyncio.run(manager.handle_throw_carried(player, 3, 0))
    manager._is_throw_wall = lambda thrown: False
    # Held bombs wait for the client report, outside the server expiry timer.
    manager._expire_bomb = AsyncMock()
    monkeypatch.setattr('time.time', lambda: 110)

    async def fly():
        for _ in range(9):
            await manager._tick()
    asyncio.run(fly())
    bomb = manager._held_bombs[player.id]
    assert bomb.time_left == 2
    assert (bomb.x, bomb.y) == pytest.approx((29.5, 21.925))
    assert not manager._throws
    manager._expire_bomb.assert_not_awaited()


def test_clear_level_discards_carried_bombs(monkeypatch):
    monkeypatch.setattr('time.time', lambda: 100)
    level, player, server, manager = arena()
    asyncio.run(manager.handle_bomb_del(player, 10, 10))
    manager.clear_level(level.name)
    assert not manager._held_bombs
