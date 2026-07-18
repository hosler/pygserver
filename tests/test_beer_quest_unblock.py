"""Regression test for the chicken_cave_entrance.nw beer-quest unblock.

Reproduces the full chain server-side, through the real NPCManager/GS1
machinery (not mocks): load the actual level file, spawn its NPCs exactly
like GameServer._register_level_features does, deliver 5 beers to the beer
guard via real playertouchsme events, then run the mountain guard's
`timeout` the same way NPCManager.tick() does and assert it actually reads
`drunkguard` and moves off the doorway tile.

Uses the FakeServer/FakePlayer idiom from test_gs1_hit_events.py rather than
mocks, since the whole point is exercising the real event-firing plumbing
between npc.py/gs1_host.py end to end.
"""
import asyncio
from pathlib import Path

import pytest

from pygserver.level import Level
from pygserver.npc import NPCManager

LEVEL_PATH = (
    Path(__file__).resolve().parents[2] / "funtimes" / "world" / "chicken_cave_entrance.nw"
)


class FakePlayer:
    def __init__(self, pid=1, x=34.5, y=15.5):
        self.id = pid
        self.x = x
        self.y = y
        self.direction = 2
        self.hearts = 3.0
        self.max_hearts = 3.0
        self.rupees = 0
        self.arrows = 0
        self.bombs = 0
        self.weapons = []
        self.account_name = "hosler"
        self.nickname = "Hos"
        self.chat = ""
        self.flags = {}
        self.level = None
        self.sent = []

    def has_weapon(self, name):
        return name in self.weapons

    async def send_raw(self, packet):
        self.sent.append(packet)

    async def send_props(self, props):
        pass

    def mark_dirty(self):
        pass


class FakeWorld:
    def __init__(self):
        self._levels = {}

    def get_level(self, name):
        return self._levels.get(name)


class FakeServer:
    def __init__(self):
        self.world = FakeWorld()
        self.npc_manager = NPCManager(self)
        self._players = {}
        self.broadcasts = []

    def get_player(self, pid):
        return self._players.get(pid)

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet, exclude))


def load_chicken_cave_entrance():
    """Load the real level file and spawn its NPCs the same way
    GameServer._register_level_features does (create_npc + attach_gs1 for
    every NPC def with an image and/or GS1 code)."""
    assert LEVEL_PATH.exists(), f"fixture level missing: {LEVEL_PATH}"
    server = FakeServer()
    level = Level.load(str(LEVEL_PATH))
    server.world._levels[level.name] = level

    for ndef in level.get_npc_defs():
        image = ndef.get('image', '') or ''
        code = ndef.get('code', '') or ''
        if not image and not code.strip():
            continue
        npc = server.npc_manager.create_npc(name="levelnpc", level=level,
                                             x=ndef['x'], y=ndef['y'])
        if image and image != '-':
            npc.image = image
        if code.strip():
            server.npc_manager.attach_gs1(npc, code)

    return server, level


def find_npc_at(server, level, x, y):
    for npc in server.npc_manager.get_npcs_on_level(level):
        if npc.x == x and npc.y == y:
            return npc
    raise AssertionError(f"no NPC at ({x}, {y}) on {level.name}")


def test_beer_quest_sets_drunkguard_on_player():
    """Sanity: 1 touch to open the quest + 5 beers delivered should flip
    beerquest off and drunkguard on, on the PLAYER (bare flags live on
    player.flags per gs1_host.run_npc_event)."""
    async def main():
        server, level = load_chicken_cave_entrance()
        beer_guard = find_npc_at(server, level, 34.5, 15.5)

        player = FakePlayer()
        player.level = level
        player.weapons = ["Beer"]
        level.add_player(player)
        server._players[player.id] = player

        # 1st touch: beerquest isn't set yet, opens the quest (no beer consumed).
        await server.npc_manager.on_player_touches(player, beer_guard)
        assert player.flags.get("beerquest")

        # 5 more touches, each delivering one beer.
        for _ in range(5):
            await server.npc_manager.on_player_touches(player, beer_guard)

        assert player.flags.get("drunkguard"), player.flags
        assert not player.flags.get("beerquest"), player.flags

    asyncio.run(main())


async def _drive_mountain_guard_to_destroy(server, mountain_guard, max_ticks=200):
    """Force-fire the mountain guard's `timeout` repeatedly (bypassing real
    wall-clock waits, same idiom as the pre-existing `set_timer(-1.0)`
    force-fire this test already used) until it destroys itself or
    `max_ticks` is exhausted. Each `sleep` inside the walk-away script now
    genuinely suspends (gs1_host.run_npc_event's resumable-sleep adoption)
    and is only resumed by the NPC's OWN next `timeout` tick -
    npc.set_timer(pending_sleep) is exactly what schedules that tick, so
    forcing the timer to -1.0 before every tick() call is what stands in for
    "real time has passed" here, same as it always was.

    Returns the list of (x, y) positions sampled after each tick that
    actually ran the guard's script (i.e. actually progressed the walk),
    including the starting position.
    """
    positions = [(mountain_guard.x, mountain_guard.y)]
    for _ in range(max_ticks):
        if mountain_guard.id not in server.npc_manager._npcs:
            break
        mountain_guard.set_timer(-1.0)
        await server.npc_manager.tick()
        await asyncio.sleep(0)  # let the scheduled destroy_npc() task run
        if mountain_guard.id not in server.npc_manager._npcs:
            break
        positions.append((mountain_guard.x, mountain_guard.y))
    return positions


def test_mountain_guard_timeout_moves_off_doorway_when_drunkguard_set():
    """Full chain: deliver 5 beers -> drunkguard set on the player -> force
    the mountain guard's timeout to fire through the real NPCManager.tick()
    path (which must give it a player context so the bare `drunkguard` flag
    is even readable) -> assert the guard actually WALKS off (30, 6) across
    many ticks, not a single one-tile jump."""
    async def main():
        server, level = load_chicken_cave_entrance()
        beer_guard = find_npc_at(server, level, 34.5, 15.5)
        mountain_guard = find_npc_at(server, level, 30, 6)

        player = FakePlayer()
        player.level = level
        player.weapons = ["Beer"]
        level.add_player(player)
        server._players[player.id] = player

        await server.npc_manager.on_player_touches(player, beer_guard)
        for _ in range(5):
            await server.npc_manager.on_player_touches(player, beer_guard)
        assert player.flags.get("drunkguard"), "beer quest didn't set drunkguard"

        # With resumable sleep adopted, the script's three walk-away while
        # loops each `sleep .05;`/`sleep .5;` per iteration and genuinely
        # suspend/resume across many NPC timer ticks instead of breaking
        # after one iteration - so driving it to completion takes many
        # forced ticks, and the guard's position should visibly progress
        # (multiple distinct tiles) rather than jump straight to its final
        # spot.
        positions = await _drive_mountain_guard_to_destroy(server, mountain_guard)

        distinct = set(positions)
        assert len(distinct) > 2, (
            f"mountain guard should have walked through several distinct "
            f"positions, only saw {sorted(distinct)}"
        )
        assert positions[0] == (30, 6)
        assert positions[1] == (30, 6), (
            "the FIRST timeout tick only reaches the initial `sleep .5;` "
            "before any movement - the guard must not have moved yet"
        )
        assert positions[-1] != (30, 6), (
            "mountain guard never left the doorway tile"
        )
        assert mountain_guard.id not in server.npc_manager._npcs, (
            "mountain guard should have destroyed itself"
        )
        assert not player.flags.get("drunkguard"), "drunkguard should be unset after the walk-off"

    asyncio.run(main())


def test_mountain_guard_destroy_notifies_client_no_ghost_npc():
    """Regression for a second, independent bug this same chain exposed:
    NPCManager.destroy_npc() used to read npc.level.name AFTER calling
    npc.level.remove_npc(npc) - but Level.remove_npc() clears npc.level back
    to None as part of removing it, so that read always raised
    AttributeError on a None. Since destroy_npc() runs as a fire-and-forget
    asyncio task (GS1 `destroy` -> gs1_host._c_destroy -> _schedule(...)),
    the exception never surfaced anywhere except an unretrieved-task log
    line - the NPC was removed from NPCManager._npcs (so server-side state
    was correct) but PLO_NPCDEL was never sent, so every connected client
    kept rendering it as a ghost at its last position forever. Assert both
    that destroy_npc() doesn't raise and that it actually broadcasts the del
    packet."""
    async def main():
        server, level = load_chicken_cave_entrance()
        beer_guard = find_npc_at(server, level, 34.5, 15.5)
        mountain_guard = find_npc_at(server, level, 30, 6)

        player = FakePlayer()
        player.level = level
        player.weapons = ["Beer"]
        level.add_player(player)
        server._players[player.id] = player

        await server.npc_manager.on_player_touches(player, beer_guard)
        for _ in range(5):
            await server.npc_manager.on_player_touches(player, beer_guard)

        await _drive_mountain_guard_to_destroy(server, mountain_guard)

        from pygserver.protocol.packets import build_npc_del
        expected = build_npc_del(mountain_guard.id)
        assert any(pkt == expected for (_lvl, pkt, _exclude) in server.broadcasts), (
            "PLO_NPCDEL was never broadcast - clients would keep a ghost NPC"
        )

    asyncio.run(main())


def test_mountain_guard_timeout_without_leader_stays_put():
    """No player on the level -> no leader -> the flag can't resolve to
    anything, so the guard correctly stays put (this is what the buggy
    player=None-always call used to do for EVERY case, including when a
    player genuinely had drunkguard set - the fix only changes behaviour
    when a leader is actually present)."""
    async def main():
        server, level = load_chicken_cave_entrance()
        mountain_guard = find_npc_at(server, level, 30, 6)

        mountain_guard.set_timer(-1.0)
        await server.npc_manager.tick()

        assert (mountain_guard.x, mountain_guard.y) == (30, 6)

    asyncio.run(main())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
