"""Classic flight limits and client-initiated revival."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from reborn_protocol import PLSTATUS

from pygserver.combat import Arrow, CombatManager
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLPROP
from pygserver.protocol.packets import PacketBuilder


def props_payload(props):
    builder = PacketBuilder()
    for key, value in sorted(props.items()):
        builder.write_gchar(key).write_gchar(value)
    return builder.build()


def test_arrow_constants_and_travel_cap():
    arrow = Arrow(1, 2, "test.nw", 10, 10, 3)
    assert arrow.speed == 20.0  # TClient.cpp:3653: DOUBLE_004023d8 = 1 tile/50ms tick
    assert arrow.max_distance == 128.0  # GServer Level.cpp:2629
    arrow.created_at = 0  # Wall-clock age alone must not discard flight.
    assert not arrow.expired
    arrow.distance = 127
    assert not arrow.expired
    arrow.distance = 128
    assert arrow.expired


def test_arrow_moves_one_tile_per_tick_and_stops_at_level_boundary():
    async def exercise():
        server = MagicMock()
        server.audience.players_near.return_value = []
        server.baddy_manager.check_arrow_hit = AsyncMock(return_value=False)
        server.npc_manager.get_npcs_on_level.return_value = []
        manager = CombatManager(server)
        level = Level("test.nw")
        arrow = Arrow(1, 2, level.name, 62, 10, 3)
        manager._arrows[level.name] = {arrow.id: arrow}
        await manager._update_arrow(arrow, level)
        assert arrow.x == 63  # GServer LevelArrow.h:47
        assert arrow.distance == 1
        await manager._update_arrow(arrow, level)
        assert not manager.get_arrows_on_level(level.name)

    asyncio.run(exercise())


def test_arrow_cap_is_reached_after_128_simulation_ticks():
    async def exercise():
        server = MagicMock()
        server.audience.players_near.return_value = []
        server.baddy_manager.check_arrow_hit = AsyncMock(return_value=False)
        server.npc_manager.get_npcs_on_level.return_value = []
        # A wide arena lets the range cap fire before a level boundary.
        level = MagicMock(name="wide.nw", WIDTH=256, HEIGHT=64)
        level.name = "wide.nw"
        server.world.get_level.return_value = level
        manager = CombatManager(server)
        arrow = Arrow(1, 2, level.name, 10, 10, 3)
        manager._arrows[level.name] = {arrow.id: arrow}
        for _ in range(128):
            await manager._tick()
        assert arrow.distance == 128  # GServer Level.cpp:2629
        assert arrow.x == 138
        await manager._tick()
        assert not manager.get_arrows_on_level(level.name)

    asyncio.run(exercise())


@pytest.mark.parametrize("with_health", [False, True])
def test_dead_clear_restores_max_health_and_warps_once(with_health):
    async def exercise():
        server = MagicMock()
        server.npc_manager = None
        manager = CombatManager(server)
        assert manager.respawn_time == 3.0  # Bot fallback deviation is intentional.
        server.combat_manager = manager
        player = Player(server, 1, AsyncMock(), MagicMock())
        player.hearts = 0
        player.max_hearts = 5
        player.send_props = AsyncMock()
        player.warp = AsyncMock()
        original = player._handlers[PLI.PLAYERPROPS]
        manager.respawn_time = 0
        await manager.handle_player_death(player)
        await player._dispatch_packet(PLI.PLAYERPROPS, props_payload({PLPROP.STATUS: PLSTATUS.DEAD}))
        assert player.hearts == 0
        update = {PLPROP.STATUS: 0}
        if with_health:
            update[PLPROP.CURPOWER] = 2  # Server restores max even if client reports less.
        await player._dispatch_packet(PLI.PLAYERPROPS, props_payload(update))
        assert player.hearts == 5  # FourPlay TPlayer.cpp:2164
        player.send_props.assert_any_await({PLPROP.CURPOWER: 10, PLPROP.STATUS: 0})
        assert not player.pending_revive
        assert manager.is_invincible(player.id)
        sends = player.send_props.await_count
        await player._dispatch_packet(PLI.PLAYERPROPS, props_payload({PLPROP.STATUS: 0}))
        assert player.hearts == 5
        assert player.send_props.await_count == sends
        assert player._handlers[PLI.PLAYERPROPS] == original
        await asyncio.gather(*manager._respawn_tasks)
        player.warp.assert_awaited_once_with(
            server.config.start_level, server.config.start_x, server.config.start_y)
        assert player._handlers[PLI.PLAYERPROPS] == original

    asyncio.run(exercise())


def test_no_input_keeps_three_second_fallback():
    async def exercise():
        server = MagicMock()
        server.npc_manager = None
        manager = CombatManager(server)
        assert manager.respawn_time == 3.0
        player = Player(server, 1, AsyncMock(), MagicMock())
        player.hearts = 0
        player.send_props = AsyncMock()
        player.warp = AsyncMock()
        original = player._handlers[PLI.PLAYERPROPS]
        player.client_status = PLSTATUS.DEAD | PLSTATUS.PAUSED
        with patch('pygserver.combat.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await manager.handle_player_death(player)
            await asyncio.gather(*manager._respawn_tasks)
        sleep.assert_awaited_once_with(3.0)
        assert not player.pending_revive
        assert player.client_status == PLSTATUS.PAUSED
        assert manager.is_invincible(player.id)
        assert player.hearts == player.max_hearts
        assert player._handlers[PLI.PLAYERPROPS] == original
        player.warp.assert_awaited_once()

    asyncio.run(exercise())


@pytest.mark.parametrize("hearts,status", [(0, 0), (2, PLSTATUS.DEAD)])
def test_damage_ignored_while_dead_even_after_hit_protection_expires(hearts, status):
    async def exercise():
        server = MagicMock()
        server.npc_manager = None
        manager = CombatManager(server)
        player = Player(server, 1, AsyncMock(), MagicMock())
        player.hearts = hearts
        player.client_status = status
        player.send_raw = AsyncMock()
        player.send_props = AsyncMock()
        await manager.apply_damage(player, 10, 0, 0)
        assert player.hearts == hearts
        assert player.deaths == 0
        player.send_raw.assert_not_awaited()
        player.send_props.assert_not_awaited()
        assert not manager._respawn_tasks

    asyncio.run(exercise())


def test_repeated_death_and_damage_do_not_stack_tasks_or_handlers():
    async def exercise():
        server = MagicMock()
        server.npc_manager = None
        server.get_player.return_value = None
        manager = CombatManager(server)
        player = Player(server, 1, AsyncMock(), MagicMock())
        player.hearts = 0.5
        player.send_raw = AsyncMock()
        player.send_props = AsyncMock()
        original = player._handlers[PLI.PLAYERPROPS]
        await manager.apply_damage(player, 1, 0, 0)
        manager._invincible.clear()
        await manager.apply_damage(player, 1, 0, 0)
        await manager.handle_player_death(player)
        # The death counter follows the STATUS prop's transition to DEAD
        # (PlayerProps.cpp:573-580), covered in test_item_drops.py, not hearts.
        assert player.deaths == 0
        assert len(manager._respawn_tasks) == 1
        assert player._handlers[PLI.PLAYERPROPS] == original
        for task in manager._respawn_tasks:
            task.cancel()
        await asyncio.gather(*manager._respawn_tasks, return_exceptions=True)

    asyncio.run(exercise())


def test_old_fallback_cannot_revive_a_later_life():
    async def exercise():
        server = MagicMock()
        server.npc_manager = None
        manager = CombatManager(server)
        player = Player(server, 1, AsyncMock(), MagicMock())
        player.hearts = 0
        player.send_props = AsyncMock()
        player.warp = AsyncMock()
        old_life = object()
        player.pending_revive = old_life
        await manager.revive_player(player)
        player.send_props.reset_mock()
        player.warp.reset_mock()
        next_life = object()
        player.pending_revive = next_life
        player.hearts = 0
        with patch('pygserver.combat.asyncio.sleep', new_callable=AsyncMock):
            await manager._respawn_player(player, old_life)
        assert player.pending_revive is next_life
        assert player.hearts == 0
        player.send_props.assert_not_awaited()
        player.warp.assert_not_awaited()

    asyncio.run(exercise())
