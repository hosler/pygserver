"""GS1 host queries after the audience/coords migration.

Two things these pin that nothing else does:

* gs1_host asks Audience for level membership, but the audience resolves a
  level NAME through world.get_level - so a level the world does not hold
  (detached, or hand-built by a test) has to keep working. Both paths are
  exercised here.
* putexplosion relays the effect and leaves player damage to each client,
  including players at the boundary and corners of the former server box.

Uses real Level/Audience instances against a small FakeServer, per the
test_gs1_hit_events.py idiom.
"""
import asyncio
from unittest.mock import AsyncMock

from pygserver.gs1_host import (
    compile_gs1,
    leader_player_for_level,
    players_on_level_for,
    run_npc_event,
)
from pygserver.level import Level
from pygserver.npc import NPC
from pygserver.world import GMap


class FakePlayer:
    def __init__(self, pid=1, x=10.0, y=10.0):
        self.id = pid
        self.x = x
        self.y = y
        self.direction = 2
        self.hearts = 3.0
        self.chat = ""
        self.nickname = f"P{pid}"
        self.account_name = f"acct{pid}"
        self.flags = {}
        self.level = None

    def mark_dirty(self):
        pass


class FakeCombatMgr:
    def __init__(self):
        self.damaged = []
        self.blast_level_objects = AsyncMock()

    async def apply_damage(self, player, dmg, kx, ky, dtype=None, attacker=None):
        self.damaged.append(player.id)


class FakeWorld:
    """Same get_level/get_gmap_for_level contract as pygserver.world.World."""

    def __init__(self):
        self._levels = {}
        self._gmaps = {}

    def get_level(self, name):
        return self._levels.get(name)

    def get_gmap_for_level(self, level_name):
        for gmap in self._gmaps.values():
            pos = gmap.find_level(level_name)
            if pos is not None:
                return (gmap, pos[0], pos[1])
        return None


class FakeServer:
    """A server the Audience can answer for: its world holds levels by name."""

    def __init__(self):
        from pygserver.audience import Audience

        self.world = FakeWorld()
        self.audience = Audience(self)
        self.combat_manager = FakeCombatMgr()
        self._players = {}
        self.broadcasts = []
        self.audience_calls = []
        real = self.audience.players_on_level

        def spy(name, exclude=None):
            self.audience_calls.append(name)
            return real(name, exclude)

        self.audience.players_on_level = spy

    def get_player(self, pid):
        return self._players.get(pid)

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet))

    def add_level(self, name):
        level = Level(name)
        self.world._levels[name] = level
        return level

    def join(self, level, player):
        player.level = level
        self._players[player.id] = player
        level.add_player(player)
        return player


class DetachedServer:
    """No world, no audience - the shape every hand-built unit fixture has."""

    def __init__(self):
        self._players = {}

    def get_player(self, pid):
        return self._players.get(pid)


def make_npc(code, level):
    npc = NPC(1, "t")
    npc.level = level
    npc.gs1_program = compile_gs1(code)
    return npc


# -- membership --------------------------------------------------------------

def test_players_on_level_goes_through_the_audience():
    server = FakeServer()
    level = server.add_level("t.nw")
    first = server.join(level, FakePlayer(pid=1))
    second = server.join(level, FakePlayer(pid=2))

    assert players_on_level_for(server, level) == [first, second]
    assert server.audience_calls == ["t.nw"]


def test_players_on_level_falls_back_for_a_level_the_world_does_not_hold():
    """The audience resolves by name, so a level never registered with the
    world would come back empty; the level's own player set answers instead."""
    server = FakeServer()
    stray = Level("stray.nw")            # deliberately NOT added to the world
    player = FakePlayer(pid=7)
    server._players[7] = player
    stray.add_player(player)

    assert server.audience.players_on_level("stray.nw") == []
    assert players_on_level_for(server, stray) == [player]
    assert server.audience_calls == ["stray.nw"]  # only the direct probe above


def test_players_on_level_works_without_an_audience_at_all():
    server = DetachedServer()
    level = Level("t.nw")
    player = FakePlayer(pid=3)
    server._players[3] = player
    level.add_player(player)

    assert players_on_level_for(server, level) == [player]


def test_players_on_level_is_empty_without_a_server_or_level():
    assert players_on_level_for(None, Level("t.nw")) == []
    assert players_on_level_for(FakeServer(), None) == []


# -- leader ------------------------------------------------------------------

def test_leader_is_the_first_player_to_join_via_the_audience():
    server = FakeServer()
    level = server.add_level("t.nw")
    first = server.join(level, FakePlayer(pid=9))
    server.join(level, FakePlayer(pid=2))   # lower id, joined second

    assert leader_player_for_level(server, level) is first
    assert server.audience_calls == ["t.nw"]


def test_leader_handoff_follows_join_order():
    server = FakeServer()
    level = server.add_level("t.nw")
    first = server.join(level, FakePlayer(pid=9))
    second = server.join(level, FakePlayer(pid=2))
    level.remove_player(first)

    assert leader_player_for_level(server, level) is second


def test_leader_is_none_on_an_empty_level():
    server = FakeServer()
    assert leader_player_for_level(server, server.add_level("t.nw")) is None


# -- client-owned putexplosion damage -----------------------------------------------------

def _explode_and_collect(radius, *players):
    """Run `putexplosion radius,30,30` and return the damaged player ids."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        for player in players:
            server.join(level, player)
        npc = make_npc(f"if (created) {{ putexplosion {radius},30,30; }}", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.combat_manager.damaged

    return asyncio.run(main())


def test_explosion_boundary_players_keep_server_hearts():
    # TExplosion.cpp:48-96: each victim client applies its own damage.
    on_edge = FakePlayer(pid=1, x=33.0, y=30.0)
    just_outside = FakePlayer(pid=2, x=33.0625, y=30.0)  # +1/16 tile, one wire step

    assert _explode_and_collect(3, on_edge, just_outside) == []


def test_explosion_corner_players_keep_server_hearts():
    # Script explosions use the same client damage authority as timed bombs.
    corner = FakePlayer(pid=1, x=33.0, y=33.0)

    assert _explode_and_collect(3, corner) == []


def test_explosion_near_and_far_players_keep_server_hearts():
    far = FakePlayer(pid=2, x=50.0, y=50.0)
    near = FakePlayer(pid=1, x=30.0, y=31.0)

    assert _explode_and_collect(3, near, far) == []


def test_script_explosion_runs_level_object_effects():
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { putexplosion 3,30,30; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        server.combat_manager.blast_level_objects.assert_awaited_once_with(
            level, 30, 30, 3, 1)
        assert len(server.broadcasts) == 1
    asyncio.run(main())


# -- coordinate frames -------------------------------------------------------

def test_tiles_read_crosses_into_the_adjacent_gmap_segment():
    """tiles[70,5] on a gmap reads x=6 of the segment one cell east."""
    server = FakeServer()
    here = server.add_level("a.nw")
    east = server.add_level("b.nw")
    east.set_tile(6, 5, 123)
    gmap = GMap("world.gmap")
    gmap.width, gmap.height = 2, 1
    gmap.grid = {(0, 0): "a.nw", (1, 0): "b.nw"}
    server.world._gmaps["world.gmap"] = gmap

    npc = make_npc("if (created) { this.t = tiles[70,5]; }", here)
    run_npc_event(npc, "created", server, None)

    assert npc.gs1_scopes["this"]["t"] == 123.0


def test_tiles_read_at_exactly_64_stays_on_the_current_segment():
    """Upstream's bound is `tileX > 64`, not `>=` (GS1Variables.cpp:400), so
    tiles[64,y] wraps to column 0 of the CURRENT level."""
    server = FakeServer()
    here = server.add_level("a.nw")
    east = server.add_level("b.nw")
    here.set_tile(0, 5, 11)
    east.set_tile(0, 5, 22)
    gmap = GMap("world.gmap")
    gmap.width, gmap.height = 2, 1
    gmap.grid = {(0, 0): "a.nw", (1, 0): "b.nw"}
    server.world._gmaps["world.gmap"] = gmap

    npc = make_npc("if (created) { this.t = tiles[64,5]; }", here)
    run_npc_event(npc, "created", server, None)

    assert npc.gs1_scopes["this"]["t"] == 11.0


def test_board_index_is_the_flat_level_index():
    server = FakeServer()
    level = server.add_level("t.nw")
    level.set_tile(3, 2, 77)

    npc = make_npc("if (created) { this.t = board[131]; }", level)  # 2*64 + 3
    run_npc_event(npc, "created", server, None)

    assert npc.gs1_scopes["this"]["t"] == 77.0


def test_board_out_of_range_index_is_zero():
    server = FakeServer()
    level = server.add_level("t.nw")

    npc = make_npc("if (created) { this.t = board[4096]; }", level)
    run_npc_event(npc, "created", server, None)

    assert npc.gs1_scopes["this"]["t"] == 0.0


def test_updateboard_sends_the_requested_rows():
    """The region slice is level_index()-based; a 2x2 at (3, 2) must carry the
    four tiles written there, not some other row."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        for x, y, tile in ((3, 2, 10), (4, 2, 11), (3, 3, 12), (4, 3, 13)):
            level.set_tile(x, y, tile)
        npc = make_npc("if (created) { updateboard 3,2,2,2; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts

    broadcasts = asyncio.run(main())
    assert len(broadcasts) == 1
    packet = broadcasts[0][1]
    # Hand-decode the reference GSHORT layout without using a client codec.
    encoded = packet[5:13]
    tiles = [((encoded[i] - 32) << 7) + encoded[i + 1] - 32
             for i in range(0, len(encoded), 2)]
    assert tiles == [10, 11, 12, 13]


def test_tiles_assignment_broadcasts_a_gshort_tile():
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { tiles[12,12] = 314; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts[0][1]

    packet = asyncio.run(main())
    encoded = packet[5:7]
    tile_id = ((encoded[0] - 32) << 7) + encoded[1] - 32
    assert tile_id == 314


def test_updateboard_clamps_the_region_to_the_board():
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { updateboard 62,62,10,10; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts

    broadcasts = asyncio.run(main())
    assert len(broadcasts) == 1  # clamped to 2x2, not an out-of-range slice
