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

import pytest
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
            level, 30, 30, 3, 1, script=True, admitted=True)
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


def decode_modify(packet):
    """(x, y, w, h, tiles) of a PLO_BOARDMODIFY, hand-decoded from the
    reference GCHAR/GSHORT layout without a client codec."""
    x, y, w, h = (packet[1 + i] - 32 for i in range(4))
    encoded = packet[5:5 + 2 * w * h]
    tiles = [((encoded[i] - 32) << 7) + encoded[i + 1] - 32
             for i in range(0, len(encoded), 2)]
    return x, y, w, h, tiles


def test_updateboard_sends_the_script_tiles_of_the_requested_rows():
    """The region is the script layer's, level_index()-based: a 2x2 at (3, 2)
    carries the four tiles the script wrote there (Level.cpp:1751-1799).
    Mutation: reading the live board instead (the old model) sends zeros."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { tiles[3,2] = 10; tiles[4,2] = 11;"
                       " tiles[3,3] = 12; tiles[4,3] = 13; updateboard 3,2,2,2; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts, level

    broadcasts, level = asyncio.run(main())
    assert len(broadcasts) == 1
    assert decode_modify(broadcasts[0][1]) == (3, 2, 2, 2, [10, 11, 12, 13])
    assert [(c.x, c.y, c.width, c.height) for c in level.board_changes] == [(3, 2, 2, 2)]


def test_a_tiles_assignment_alone_sends_nothing_and_records_nothing():
    """GS1Variables.cpp:416-442 writes only the script copy: tiles written
    but never flushed are never seen by anyone. Mutation: broadcasting from
    the setter (the old behaviour) fails."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { tiles[12,12] = 314; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts, level

    broadcasts, level = asyncio.run(main())
    assert broadcasts == []
    assert level.board_changes == [] and level.get_tile(12, 12) == 0


def test_updateboard_over_an_area_no_script_wrote_publishes_tile_zero():
    """Level.cpp:1765-1770: a level with no script layer leaves its part of
    the array at 0, which alterBoard then sends. Mutation: publishing the
    live board's tiles fails."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        level.set_tile(3, 2, 10)
        npc = make_npc("if (created) { updateboard 3,2,1,1; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts

    broadcasts = asyncio.run(main())
    assert decode_modify(broadcasts[0][1]) == (3, 2, 1, 1, [0])


@pytest.mark.parametrize("code", ["updateboard 1,2,3;", "updateboard 1,2,3,4,5;"])
def test_updateboard_needs_exactly_four_arguments(code):
    """GS1Commands.cpp:3716-3717 throws invalid_argument otherwise. Mutation:
    accepting more than four (the old `< 4` test) fails the second case."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { " + code + " }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts

    assert asyncio.run(main()) == []


def test_updateboard_records_a_change_that_reverts_after_respawntime():
    """Level::updateBoard forces the respawn (Level.cpp:1847-1850): the
    change keeps the base tiles as its old tiles and expires after
    `respawntime` (default 15 s). Mutation: allow_respawn False (the
    permanent form) fails."""
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        level.set_tile(3, 2, 10)
        npc = make_npc("if (created) { tiles[3,2] = 99; updateboard 3,2,1,1; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return level

    level = asyncio.run(main())
    change = level.board_changes[0]
    assert change.tiles == [99] and change.old_tiles == [10]
    assert change.expires_at is not None
    assert level.get_tile(3, 2) == 99
    level.expire_board_changes(change.expires_at + 1)
    assert level.get_tile(3, 2) == 10


def test_updateboard_clamps_the_region_to_the_board():
    async def main():
        server = FakeServer()
        level = server.add_level("t.nw")
        npc = make_npc("if (created) { tiles[62,62] = 5; updateboard 62,62,10,10; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        return server.broadcasts

    broadcasts = asyncio.run(main())
    assert len(broadcasts) == 1  # clamped to 2x2, not an out-of-range slice
    x, y, w, h, tiles = decode_modify(broadcasts[0][1])
    assert (x, y, w, h) == (62, 62, 2, 2) and tiles[0] == 5


def test_updateboard_across_a_gmap_seam_gives_each_segment_its_part():
    """Level::applyBoardChangeFromScriptTiles gathers the tiles of every
    sub-level the rectangle covers (Level.cpp:1751-1799) and alterBoard
    splits the change per part (:1698-1738): the west segment gets columns
    62-63 and the east one columns 0-1, each sent to its own segment and
    recorded on it. Mutation: the one-segment clamp leaves b.nw without a
    packet or a change."""
    async def main():
        server = FakeServer()
        west = server.add_level("a.nw")
        east = server.add_level("b.nw")
        gmap = GMap("world.gmap")
        gmap.width, gmap.height = 2, 1
        gmap.grid = {(0, 0): "a.nw", (1, 0): "b.nw"}
        server.world._gmaps["world.gmap"] = gmap
        run_npc_event(make_npc("if (created) { tiles[0,5] = 13; tiles[1,5] = 14; }", east),
                      "created", server, None)
        run_npc_event(make_npc("if (created) { tiles[62,5] = 11; tiles[63,5] = 12;"
                               " updateboard 62,5,4,1; }", west), "created", server, None)
        await asyncio.sleep(0)
        return server, west, east

    server, west, east = asyncio.run(main())

    def decode_modify2(packet):
        gx, gy, x, y, w, h = (packet[1 + i] - 32 for i in range(6))
        encoded = packet[7:7 + 2 * w * h]
        tiles = [((encoded[i] - 32) << 7) + encoded[i + 1] - 32
                 for i in range(0, len(encoded), 2)]
        return gx, gy, x, y, w, h, tiles

    assert [(name, decode_modify2(packet)) for name, packet in server.broadcasts] == [
        ("a.nw", (0, 0, 62, 5, 2, 1, [11, 12])),
        ("b.nw", (1, 0, 0, 5, 2, 1, [13, 14]))]
    assert [(c.x, c.y, c.width, c.height, c.tiles) for c in west.board_changes] == [
        (62, 5, 2, 1, [11, 12])]
    assert [(c.x, c.y, c.width, c.height, c.tiles) for c in east.board_changes] == [
        (0, 5, 2, 1, [13, 14])]
