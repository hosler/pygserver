"""Regression tests for player-vs-player arrow damage (PLI_ARROWADD ->
CombatManager.handle_arrow_add -> _update_arrow's player-hit path).

Uses the same real-CombatManager/FakePlayer/FakeServer idiom as
test_gs1_hit_events.py rather than mocks, since the point is verifying the
actual flight-simulation/collision plumbing between handle_arrow_add and the
combat tick, not just that CombatManager can be constructed.
"""
import asyncio

import pytest

from pygserver.combat import CombatManager
from pygserver.level import Level


class FakePlayer:
    def __init__(self, pid, x=10.0, y=10.0, direction=2):
        self.id = pid
        self.x = x
        self.y = y
        self.direction = direction
        self.hearts = 3.0
        self.max_hearts = 3.0
        self.arrows = 5
        self.level = None
        self.sent = []
        self.props_sent = []

    async def send_raw(self, packet):
        self.sent.append(packet)

    async def send_props(self, props):
        self.props_sent.append(props)


class FakeWorld:
    def __init__(self):
        self._levels = {}

    def get_level(self, name):
        return self._levels.get(name)


class FakeServer:
    def __init__(self):
        self.world = FakeWorld()
        self.combat_manager = CombatManager(self)
        self._players = {}
        self.broadcasts = []

    def get_player(self, pid):
        return self._players.get(pid)

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet, exclude))


def make_server_and_level(level_name="t.nw"):
    server = FakeServer()
    level = Level(level_name)
    server.world._levels[level_name] = level
    return server, level


async def _run_arrow_to_hit(server, level, shooter, target, direction,
                             fire_x=None, fire_y=None, max_ticks=80):
    """Fire an arrow from `shooter` toward `target` and tick combat until it
    either hits (target.hearts drops) or `max_ticks` (4s of sim time) pass
    without a hit."""
    cm = server.combat_manager
    x = shooter.x if fire_x is None else fire_x
    y = shooter.y if fire_y is None else fire_y
    arrow = await cm.handle_arrow_add(shooter, x, y, flags=direction)
    assert arrow is not None, "handle_arrow_add refused to fire"

    for _ in range(max_ticks):
        await cm._tick()
        if target.hearts < target.max_hearts:
            break
    return arrow


def test_arrow_hits_player_reduces_hearts_and_sends_hurt_packet():
    """The core PvP path: A fires an arrow, walks it down the level, and it
    must land on B - hearts drop and a PLO_HURTPLAYER-carrying packet is
    relayed to the victim."""
    async def main():
        server, level = make_server_and_level()
        shooter = FakePlayer(1, x=30.0, y=30.0, direction=2)  # facing down
        target = FakePlayer(2, x=30.0, y=36.0)  # 6 tiles straight down
        shooter.level = level
        target.level = level
        level.add_player(shooter)
        level.add_player(target)
        server._players[1] = shooter
        server._players[2] = target

        await _run_arrow_to_hit(server, level, shooter, target, direction=2)

        assert target.hearts == pytest.approx(2.5)
        assert len(target.sent) == 1, "victim should get exactly one hurt packet"
        # Ammo was consumed for the shot regardless of hit/miss.
        assert shooter.arrows == 4

    asyncio.run(main())


def test_arrow_does_not_hit_shooter_or_off_axis_player():
    """Sanity check the collision box: the shooter can't hit itself, and a
    player well off the flight line stays untouched."""
    async def main():
        server, level = make_server_and_level()
        shooter = FakePlayer(1, x=30.0, y=30.0, direction=3)  # facing right
        bystander = FakePlayer(3, x=30.0, y=40.0)  # 10 tiles away, different axis
        shooter.level = level
        bystander.level = level
        level.add_player(shooter)
        level.add_player(bystander)
        server._players[1] = shooter
        server._players[3] = bystander

        cm = server.combat_manager
        arrow = await cm.handle_arrow_add(shooter, shooter.x, shooter.y, flags=3)
        for _ in range(80):
            await cm._tick()

        assert shooter.hearts == 3.0
        assert bystander.hearts == 3.0
        assert bystander.sent == []

    asyncio.run(main())


def test_arrow_uses_server_tracked_position_not_client_reported_xy():
    """Regression for the GMAP PvP-arrow-always-misses bug: pyReborn's
    Client.player.x/y are WORLD coordinates on a GMAP (unlike Client.move(),
    which explicitly converts to local before sending), so a client firing
    from a gmap segment can report wire x/y far outside the current
    (LOCAL 0-63) Level's bounds - e.g. x=94 on a Level.WIDTH=64 segment.
    _update_arrow's own bounds check then treated that as instantly
    out-of-map and dropped the arrow on its very first tick, before the
    player-hit check ever ran, so PvP arrows always dealt zero damage while
    playing on a GMAP even though ammo still decremented normally.

    handle_arrow_add must simulate flight from the server's own tracked
    player.x/y (always kept in the current level's local space by
    _handle_player_props / player.warp()), not the wire-reported x/y, so a
    bogus/out-of-frame client-reported spawn point doesn't break hit
    detection.
    """
    async def main():
        server, level = make_server_and_level()
        shooter = FakePlayer(1, x=30.0, y=30.0, direction=2)
        target = FakePlayer(2, x=30.0, y=36.0)
        shooter.level = level
        target.level = level
        level.add_player(shooter)
        level.add_player(target)
        server._players[1] = shooter
        server._players[2] = target

        # Simulate a gmap-world-coordinate client: reports x/y way outside
        # this segment's 0-63 local bounds, even though the server's own
        # tracked shooter.x/y (30, 30) is correctly local.
        await _run_arrow_to_hit(server, level, shooter, target, direction=2,
                                 fire_x=94.0, fire_y=94.0)

        assert target.hearts == pytest.approx(2.5)
        assert len(target.sent) == 1

    asyncio.run(main())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
