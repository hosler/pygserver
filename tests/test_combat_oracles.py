"""Combat rules checked against the client and reference server sources."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pygserver.audience import Audience
from pygserver.baddy import Baddy, BaddyManager, BaddyType
from pygserver.combat import Arrow, Bomb, CarryObjectSprite, CombatManager
from pygserver.level import Level
from pygserver.protocol.constants import BDMODE, LevelItemType, PLO
from pygserver.protocol.packets import PacketReader


def arena():
    level = Level('arena.nw')
    players = {}
    server = SimpleNamespace(
        world=SimpleNamespace(get_level=lambda name: level,
                              get_gmap_for_level=lambda name: None),
        get_player=players.get,
        broadcast_to_level=AsyncMock(),
        item_manager=SimpleNamespace(spawn_item=AsyncMock()),
    )
    server.audience = Audience(server)
    manager = CombatManager(server)

    def player(pid, x=20.0, y=20.0, **values):
        result = SimpleNamespace(
            id=pid, x=x, y=y, level=level, hearts=3.0, direction=2,
            shield_power=0, gani='idle', arrows=5,
            send_raw=AsyncMock(), send_props=AsyncMock(),
        )
        result.__dict__.update(values)
        players[pid] = result
        level.add_player(result)
        return result

    return server, level, manager, player


@pytest.mark.parametrize('power,hearts', [(1, 2.5), (2, 2.5), (3, 2.0)])
@pytest.mark.parametrize('dx,dy,hit', [
    (0, 0, True), (4, 0, True), (-4, 0, True),
    (0, 4, True), (0, -4, True), (1, 0, True),
    (1.001, 1.001, False), (5.001, 0, False),
])
def test_bomb_cross_cells_and_damage(power, hearts, dx, dy, hit):
    async def run():
        server, level, manager, player = arena()
        target = player(2, 19.5 + dx, 19.0 + dy)
        # Type 2 also covers the diagonal and the more distant arm position.
        expected_hit = hit or power == 2
        await manager._detonate_bomb(Bomb(1, 1, level.name, 20, 20, power, 0))
        assert target.hearts == (hearts if expected_hit else 3.0)
        assert target.send_raw.await_count == int(expected_hit)
    asyncio.run(run())


def test_bomb_type_two_has_corner_crosses_and_eight_tile_arms():
    async def run():
        server, level, manager, player = arena()
        targets = [player(2, 25.5, 17), player(3, 27.5, 19),
                   player(4, 25.5, 25)]
        await manager._detonate_bomb(Bomb(1, 1, level.name, 20, 20, 2, 0))
        assert [p.hearts for p in targets] == [2.5, 2.5, 3.0]
        explosions = []
        for call in server.broadcast_to_level.await_args_list:
            reader = PacketReader(call.args[1])
            if reader.read_gchar() == PLO.EXPLOSION:
                explosions.append(tuple(reader.read_gchar() for _ in range(4)))
        assert sorted(explosions) == sorted([
            (36, 36, 2, 2), (36, 44, 2, 2),
            (44, 36, 2, 2), (44, 44, 2, 2), (40, 40, 4, 2)])
    asyncio.run(run())


@pytest.mark.parametrize('original,replacement', [
    ((2, 0x12, 3, 0x13), (0x2a5, 0x2b5, 0x2a6, 0x2b6)),
    ((0x1a4, 0x1b4, 0x1a5, 0x1b5), (0x2a7, 0x2b7, 0x2a8, 0x2b8)),
    ((0x200, 0x210, 0x201, 0x211), (0x70a, 0x71a, 0x70b, 0x71b)),
    ((0x2ac, 0x2bc, 0x2ad, 0x2bd), (0x6ea, 0x6fa, 0x6eb, 0x6fb)),
    ((0x22, 0x32, 0x23, 0x33), (0x72a, 0x73a, 0x72b, 0x73b)),
    ((0x3de, 0x3ee, 0x3df, 0x3ef), (0x72a, 0x73a, 0x72b, 0x73b)),
])
def test_bomb_crosses_walls_and_replaces_complete_lift_blocks(original, replacement):
    async def run():
        server, level, manager, player = arena()
        target = player(2, 23.5, 19)
        level.set_tile(22, 20, 4)  # A wall between the bomb and victim.
        offsets = ((0, 0), (0, 1), (1, 0), (1, 1))
        for (dx, dy), tile in zip(offsets, original):
            level.set_tile(24 + dx, 20 + dy, tile)
        await manager._detonate_bomb(Bomb(1, 1, level.name, 20, 20, 1, 0))
        assert target.hearts == 2.5
        assert tuple(level.get_tile(24 + dx, 20 + dy)
                     for dx, dy in offsets) == replacement
        packets = [c.args[1] for c in server.broadcast_to_level.await_args_list]
        board = next(p for p in packets if PacketReader(p).read_gchar() == PLO.BOARDMODIFY)
        reader = PacketReader(board)
        assert [reader.read_gchar() for _ in range(5)] == [PLO.BOARDMODIFY, 24, 20, 2, 2]
        assert [reader.read_gshort() for _ in range(4)] == [
            replacement[0], replacement[2], replacement[1], replacement[3]]
    asyncio.run(run())


@pytest.mark.parametrize('direction', range(4))
@pytest.mark.parametrize('gani,shield,power,facing,blocked', [
    ('idle', 1, 1, True, True), ('walk', 1, 0, True, True),
    ('idle', 1, 1, False, False), ('sword', 1, 1, True, False),
    ('idle', 0, 1, True, False), ('idle', 3, 2, True, False),
    ('idle', 2, 1, True, True),
])
def test_arrow_shield_animation_power_and_facing(direction, gani, shield, power, facing, blocked):
    async def run():
        server, level, manager, player = arena()
        target = player(2, direction=(direction + (2 if facing else 0)) % 4,
                        gani=gani, shield_power=shield)
        dx, dy = ((0, -1), (-1, 0), (0, 1), (1, 0))[direction]
        arrow = Arrow(1, 1, level.name, 21 - dx, 21 - dy, direction, power=power)
        manager._arrows[level.name] = {1: arrow}
        await manager._update_arrow(arrow, level)
        assert target.hearts == (3.0 if blocked else 2.5)
        if blocked:
            assert arrow.direction == (direction + 2) % 4
            if shield == 1:
                assert arrow.rebound_ticks == 3
                for _ in range(4):
                    await manager._update_arrow(arrow, level)
                assert not manager.get_arrows_on_level(level.name)
            else:
                assert arrow.player_id == target.id
                assert arrow.rebound_ticks == -1
    asyncio.run(run())


@pytest.mark.parametrize('x,y,hit', [
    (22.499, 21, True), (22.5, 21, False),
    (18.626, 21, True), (18.625, 21, False),
    (20, 22.999, True), (20, 23, False),
    (20, 20.376, True), (20, 20.375, False),
])
def test_arrow_sprite_rectangle_has_strict_edges(x, y, hit):
    async def run():
        server, level, manager, player = arena()
        target = player(2)
        arrow = Arrow(1, 1, level.name, x - 1, y, 3)
        manager._arrows[level.name] = {1: arrow}
        await manager._update_arrow(arrow, level)
        assert target.hearts == (2.5 if hit else 3)
    asyncio.run(run())


def test_mirror_shield_returns_a_live_arrow_to_the_original_shooter():
    async def run():
        server, level, manager, player = arena()
        shooter = player(1, 15.5, 20)
        defender = player(2, 19, 20, shield_power=2, direction=1)
        arrow = Arrow(1, shooter.id, level.name, 18, 21, 3)
        manager._arrows[level.name] = {1: arrow}
        await manager._tick()
        assert defender.hearts == 3
        await manager._tick()
        assert shooter.hearts == 3  # Just touching the body's right edge.
        await manager._tick()
        assert shooter.hearts == 2.5
        assert defender.hearts == 3
        assert not manager.get_arrows_on_level(level.name)
    asyncio.run(run())


@pytest.mark.parametrize('sprite', [CarryObjectSprite.BUSH, CarryObjectSprite.STONE,
                                  CarryObjectSprite.BLACKSTONE, CarryObjectSprite.VASE,
                                  CarryObjectSprite.SIGN])
@pytest.mark.parametrize('hp,remaining', [(3, 2), (1.5, 0.5), (1, 0.5)])
def test_thrown_damage_depends_on_health_for_every_object_type(sprite, hp, remaining):
    async def run():
        server, level, manager, player = arena()
        thrower = player(1)
        target = player(2, 23, 20, hearts=hp)
        await manager.handle_throw_carried(thrower, 3, sprite)
        assert target.hearts == hp
        await manager._tick()
        assert target.hearts == hp  # Centre separation is exactly two tiles.
        await manager._tick()
        assert target.hearts == remaining
        assert manager.is_invincible(target.id)
        assert not manager._throws
        await manager.handle_throw_carried(thrower, 3, sprite)
        for _ in range(9):
            await manager._tick()
        assert target.hearts == remaining
    asyncio.run(run())


@pytest.mark.parametrize('direction', range(4))
def test_throw_travels_nine_ground_tiles_over_nine_ticks(direction):
    async def run():
        server, level, manager, player = arena()
        await manager.handle_throw_carried(player(1), direction, CarryObjectSprite.BUSH)
        thrown = manager._throws[0]
        for _ in range(8):
            await manager._tick()
        assert thrown.ticks_left == 1
        assert manager._throws
        await manager._tick()
        dx, dy = ((0, -9), (-9, 0), (0, 9), (9, 0))[direction]
        assert (thrown.x, thrown.y) == (20.5 + dx, 21 + dy)
        assert not manager._throws
    asyncio.run(run())


@pytest.mark.parametrize('tile_type,stops', [(20, False), (21, True), (22, True), (4, True)])
def test_throw_wall_and_type_twenty_exception(tile_type, stops):
    async def run():
        server, level, manager, player = arena()
        await manager.handle_throw_carried(player(1), 3, CarryObjectSprite.STONE)
        with patch('pygserver.combat.get_tile_type', return_value=tile_type):
            await manager._tick()
        assert bool(manager._throws) is not stops
    asyncio.run(run())


@pytest.mark.parametrize('obstacle', ['bomb', 'chest'])
@pytest.mark.parametrize('tile_type,stops', [(0, True), (20, False)])
def test_throw_stops_at_bombs_and_chests_except_on_type_twenty(obstacle, tile_type, stops):
    async def run():
        server, level, manager, player = arena()
        if obstacle == 'bomb':
            manager._bombs[level.name] = {1: Bomb(1, 3, level.name, 22, 22, 1, 30)}
        else:
            level._chests.append({'x': 22, 'y': 22, 'item': 0})
        await manager.handle_throw_carried(player(1), 3, CarryObjectSprite.BUSH)
        with patch('pygserver.combat.get_tile_type', return_value=tile_type):
            await manager._tick()
        assert bool(manager._throws) is not stops
    asyncio.run(run())


def test_baddy_die_delay_then_dead_then_respawn():
    async def run():
        server, level, combat, player = arena()
        manager = BaddyManager(server)
        baddy = await manager.add_baddy(level, 10, 10, BaddyType.GRAYBALL)
        with patch('pygserver.baddy.time.time', return_value=100):
            await manager.handle_baddy_death(baddy)
        assert baddy.mode == BDMODE.DIE
        assert baddy.dead
        with patch('pygserver.baddy.time.time', return_value=101.999):
            await manager._tick(0.1)
        assert baddy.mode == BDMODE.DIE
        with patch('pygserver.baddy.time.time', return_value=102):
            await manager._tick(0.1)
        assert baddy.mode == BDMODE.DEAD
        assert baddy.death_time == 102
        with patch('pygserver.baddy.time.time', return_value=161.999):
            await manager._tick(0.1)
        assert baddy.mode == BDMODE.DEAD
        with patch('pygserver.baddy.time.time', return_value=162):
            await manager._tick(0.1)
        assert not baddy.dead
        assert baddy.mode == BDMODE.HUNT
    asyncio.run(run())


@pytest.mark.parametrize('baddy_type', [BaddyType.GRAYBALL, BaddyType.REDBALL])
@pytest.mark.parametrize('roll,drop', list(enumerate([0, 1, 2, 3, 4, 5, 0, 0, 0, 0, None, None])))
def test_baddy_uniform_drop_roll_mapping(baddy_type, roll, drop):
    async def run():
        server, level, combat, player = arena()
        manager = BaddyManager(server)
        baddy = Baddy(1, level.name, baddy_type, 10, 10)
        with patch('pygserver.baddy.random.randint', return_value=roll) as random_roll:
            await manager.handle_baddy_death(baddy)
            await manager.handle_baddy_death(baddy)
        random_roll.assert_called_once_with(0, 11)
        if drop is None:
            server.item_manager.spawn_item.assert_not_awaited()
        else:
            server.item_manager.spawn_item.assert_awaited_once_with(
                level, 10, 10, LevelItemType(drop))
    asyncio.run(run())
