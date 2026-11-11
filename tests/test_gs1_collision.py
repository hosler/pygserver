"""Server-side GS1 collision foundation: tile types, onwall/onwater/tiletype,
NPC bounding boxes, board[] writes, and the small script-visible fixes from
the 2026-11 upstream delta (351966c1, 56ac0f20, 86df2119, 1641f88b, 25396a18,
ca160f9e, b46a9c03, 07932208, 0baf970a)."""
import asyncio
from types import SimpleNamespace

import pytest

from pygserver import collision, tiletypes
from pygserver.combat import CombatManager, CarryObjectType
from pygserver.config import ServerConfig
from pygserver.gs1_host import compile_gs1, run_npc_event
from pygserver.level import Level, LevelLoadError, LevelManager
from pygserver.npc import NPC

WALL = tiletypes.TILE_TYPES.index(22)      # a blocking tile id
WALKABLE = tiletypes.TILE_TYPES.index(0)
THROW = tiletypes.TILE_TYPES.index(20)
WATER = tiletypes.TILE_TYPES.index(11)
NW_DIFF = next(i for i in range(4096)
               if tiletypes.TILE_TYPES[i] != tiletypes.TILE_TYPES_NW[i])


class FakeWorld:
    def __init__(self, levels, gmap=None, grid=None):
        self.levels = {level.name: level for level in levels}
        self.gmap = gmap
        self.grid = grid or {}

    def get_level(self, name):
        return self.levels.get(name)

    def get_gmap_for_level(self, name):
        for pos, level_name in self.grid.items():
            if level_name == name:
                return SimpleNamespace(get_level_at=lambda x, y: self.grid.get((x, y))), *pos
        return None


class FakeServer:
    def __init__(self, world=None, config=None, players=()):
        self.world = world
        self.config = config or ServerConfig()
        self.players = {p.id: p for p in players}

    def get_player(self, pid):
        return self.players.get(pid)


def make_player(pid, x, y, level, **extra):
    player = SimpleNamespace(id=pid, x=x, y=y, level=level, flags={},
                             logged_in=True, weapons=[], hearts=3.0,
                             attach_npc=(0, 0), **extra)
    level.add_player(player)
    return player


def make_npc(code, level, npc_id=1, x=10.0, y=10.0):
    npc = NPC(npc_id, f"n{npc_id}")
    npc.x, npc.y = x, y
    level.add_npc(npc)
    npc.gs1_program = compile_gs1(code)
    return npc


def run(npc, server=None, event="created", player=None):
    run_npc_event(npc, event, server or FakeServer(), player)
    return npc.gs1_scopes["this"]


def flat_level(name="a.nw", tile=WALKABLE):
    level = Level(name)
    for y in range(64):
        for x in range(64):
            level.set_tile(x, y, tile)
    return level


# -- conversions ------------------------------------------------------------

def test_fix_epsilon_pixel_vectors():
    assert [collision.tiles_to_pixels(v) for v in
            (-0.25, -1.0, 0.25, 0.3, 0.0625, 2.5)] == [-4, -16, 4, 4, 1, 40]


def test_pixel_to_tile_truncates_toward_zero():
    assert [collision.pixel_to_tile(p) for p in (0, 15, 16, -1, -15, -16)] == [
        0, 0, 1, 0, 0, -1]


# -- Level tile lookup ------------------------------------------------------

def test_level_tile_type_and_wall_helpers():
    level = flat_level()
    level.set_tile(3, 4, WALL)
    level.set_tile(5, 5, THROW)
    level.set_tile(6, 6, 5000)
    assert level.tile_type_at(3, 4) == 22
    assert level.is_on_wall(3, 4) and level.is_blocking(3, 4)
    assert level.is_on_wall(5, 5)               # throw-through is a wall
    assert level.tile_type_at(6, 6) == 22       # id >= 4096
    assert level.tile_type_at(64, 0) == 22 and level.tile_type_at(0, -1) == 22
    assert not level.is_on_wall(0, 0)


def test_newformat_tileset_follows_options():
    level = flat_level("hall.nw", NW_DIFF)
    classic = tiletypes.TILE_TYPES[NW_DIFF]
    new = tiletypes.TILE_TYPES_NW[NW_DIFF]
    config = ServerConfig()
    assert collision.tile_type_at(FakeServer(config=config), level, 1, 1) == classic
    config.newtilesetlevels = ["hal*"]
    assert collision.tile_type_at(FakeServer(config=config), level, 1, 1) == new
    config.newtilesetlevels, config.newtilesets = [], True
    assert collision.tile_type_at(FakeServer(config=config), level, 1, 1) == new


def test_tiletype_function_bounds_and_values():
    level = flat_level()
    level.set_tile(3, 4, WALL)
    level.set_tile(0, 5, THROW)
    npc = make_npc("if (created) { this.a = tiletype(3,4); this.b = tiletype(64,10);"
                   " this.c = tiletype(-1,5); this.d = tiletype(-0.5,5);"
                   " this.e = tiletype(0,0); }", level)
    scope = run(npc)
    assert (scope["a"], scope["b"], scope["c"], scope["d"], scope["e"]) == (
        22.0, 22.0, 22.0, 20.0, 0.0)


def test_tiletype_reads_gmap_neighbour_and_missing_segment():
    west, east = flat_level("w.nw"), flat_level("e.nw")
    east.set_tile(6, 3, WALL)
    world = FakeWorld([west, east], grid={(0, 0): "w.nw", (1, 0): "e.nw"})
    server = FakeServer(world)
    npc = make_npc("if (created) { this.a = tiletype(70,3); this.b = tiletype(5,64); }",
                   west)
    scope = run(npc, server)
    assert scope["a"] == 22.0      # segment (1,0) column 6 row 3
    assert scope["b"] == 22.0      # no segment below


def _grid_world(columns=3, rows=3, tile=WALL):
    """A gmap of solid segments named s<x><y>; returns (world, {(x, y): level})."""
    levels = {(x, y): flat_level(f"s{x}{y}.nw", tile)
              for x in range(columns) for y in range(rows)}
    grid = {pos: level.name for pos, level in levels.items()}
    return FakeWorld(list(levels.values()), grid=grid), levels


@pytest.mark.parametrize("script", [(x, y) for x in range(3) for y in range(3)])
@pytest.mark.parametrize("step", [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                                  if (dx, dy) != (0, 0)])
def test_gmap_probe_reaches_the_neighbour_in_every_direction(script, step):
    # Every segment is solid except tile (7, 9), so a probe is clear exactly
    # when it lands on that tile of a segment that exists.
    world, levels = _grid_world()
    for level in levels.values():
        level.set_tile(7, 9, WALKABLE)
    server = FakeServer(world)
    here = levels[script]
    x, y = 7.5 + 64 * step[0], 9.5 + 64 * step[1]
    npc = make_npc(f"if (created) {{ this.w = onwall({x},{y}); this.t = tiletype({x},{y});"
                   f" this.w2 = onwall2({x},{y},0.5,0.5); }}", here)
    scope = run(npc, server)
    target = (script[0] + step[0], script[1] + step[1])
    exists = target in levels
    assert (scope["w"], scope["w2"]) == ((0.0, 0.0) if exists else (1.0, 1.0))
    assert scope["t"] == (0.0 if exists else 22.0)


def test_gmap_probe_just_past_an_edge_matches_the_next_column():
    west, east = flat_level("w.nw"), flat_level("e.nw")
    west.set_tile(63, 5, WALL)
    server = FakeServer(FakeWorld([west, east], grid={(0, 0): "w.nw", (1, 0): "e.nw"}))
    npc = make_npc("if (created) { this.a = tiletype(-0.5,5); this.b = tiletype(-1,5);"
                   " this.c = tiletype(-0.01,5); this.d = onwall(-0.5,5.5);"
                   " this.e = onwall(-1,5.5); }", east)
    scope = run(npc, server)
    assert (scope["a"], scope["b"], scope["c"]) == (22.0, 22.0, 22.0)
    assert (scope["d"], scope["e"]) == (1.0, 1.0)
    west.set_tile(63, 5, WALKABLE)
    scope = run(npc, server)
    assert (scope["a"], scope["b"], scope["c"]) == (0.0, 0.0, 0.0)


def test_probe_past_the_whole_map_edge_follows_the_reference():
    # toWholeTilePosition truncates the global position, so the strip just
    # outside the left edge reads tile 0 and a full tile outside is blocking.
    only = flat_level("only.nw")
    only.set_tile(0, 5, THROW)
    server = FakeServer(FakeWorld([only], grid={(0, 0): "only.nw"}))
    npc = make_npc("if (created) { this.a = tiletype(-0.5,5); this.b = tiletype(-1,5); }",
                   only)
    scope = run(npc, server)
    assert (scope["a"], scope["b"]) == (20.0, 22.0)


class CountingWorld(FakeWorld):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gmap_lookups = self.level_lookups = 0

    def get_level(self, name):
        self.level_lookups += 1
        return super().get_level(name)

    def get_gmap_for_level(self, name):
        self.gmap_lookups += 1
        return super().get_gmap_for_level(name)


def test_big_rect_resolves_gmap_and_segments_once():
    levels = [flat_level(f"c{x}{y}.nw") for y in range(2) for x in range(2)]
    grid = {(x, y): f"c{x}{y}.nw" for x in range(2) for y in range(2)}
    world = CountingWorld(levels, grid=grid)
    npc = make_npc("if (created) { this.a = onwall2(0,0,255,255); }", levels[0])
    assert run(npc, FakeServer(world))["a"] == 1.0     # runs off the 2x2 map
    assert world.gmap_lookups == 1
    assert world.level_lookups <= 3                    # the three neighbours, once each


def test_tiletype_without_a_level_is_blocking():
    assert collision.tile_type_at(FakeServer(), None, 0, 0) == 22


# -- onwall / onwater -------------------------------------------------------

def test_onwall_tile_wall_and_out_of_bounds():
    level = flat_level()
    level.set_tile(12, 12, WALL)
    npc = make_npc("if (created) { this.a = onwall(12.5,12.5); this.b = onwall(1,1);"
                   " this.c = onwall(70,1); }", level, x=1, y=1)
    scope = run(npc)
    assert (scope["a"], scope["b"], scope["c"]) == (1.0, 0.0, 1.0)


def test_onwall_ignores_the_probing_npc_but_sees_another():
    level = flat_level()
    probe = make_npc("if (created) { this.a = onwall(10.5,10.5); }", level, 1)
    probe.shape = (32, 32)
    other = make_npc("", level, 2)
    other.shape = (32, 32)
    other.visible = False
    assert run(probe)["a"] == 0.0                 # only itself, and B invisible
    other.visible = True
    assert run(probe)["a"] == 1.0                 # B overlaps
    other.block_flags = 1                         # NOBLOCK
    assert run(probe)["a"] == 0.0


def test_other_npc_listed_after_probe_is_not_hidden_by_the_probe():
    # The reference returns at its first intersecting NPC, so a probe listed
    # first would mask an overlapping neighbour; ours keeps scanning.
    level = flat_level()
    probe = make_npc("", level, 1)
    probe.shape = (32, 32)
    other = make_npc("", level, 2)
    other.shape = (32, 32)
    assert collision.find_npc_at(level, point=(170, 170), initiator=probe) == 1
    assert collision.find_npc_at(level, point=(900, 900), initiator=probe) == -2
    other.visible = False
    assert collision.find_npc_at(level, point=(170, 170), initiator=probe) == -1


def test_character_npc_box_is_feet_square_and_ignores_setshape():
    level = flat_level()
    char = make_npc("", level, 2, x=10, y=10)
    char.image = "#c#"
    char.shape = (6, 6)
    assert collision.npc_collision_rect(char) == (168, 176, 32, 32)
    assert collision.npc_event_rect(char) == (168, 176, 32, 32)
    probe = make_npc("if (created) { this.a = onwall(11,12); this.b = onwall(10,10); }",
                     level, 1, x=1, y=1)
    scope = run(probe)
    assert (scope["a"], scope["b"]) == (1.0, 0.0)


def test_noblock_removes_collision_but_not_event_box():
    level = flat_level()
    npc = make_npc("", level)
    npc.shape = (32, 32)
    npc.block_flags = 1
    assert collision.npc_collision_rect(npc) is None
    assert collision.npc_event_rect(npc) == (160, 160, 32, 32)
    npc.visible = False
    assert collision.npc_event_rect(npc) is None


def test_testnpc_uses_collision_box_rules():
    level = flat_level()
    target = make_npc("", level, 2, x=10, y=10)
    target.image = "#c#"
    target.shape = (4, 4)                 # ignored for a character
    prober = make_npc("if (created) { this.a = testnpc(11,12); this.b = testnpc(10,10); }",
                      level, 1, x=1, y=1)
    scope = run(prober)
    assert scope["a"] == 0.0 and scope["b"] == -1.0   # index of target in level order
    target.visible = False
    assert run(prober)["a"] == -1.0


def test_onwall_sees_players_unless_noplayeronwall():
    level = flat_level()
    player = make_player(7, 10.0, 10.0, level)
    server = FakeServer(players=[player])
    npc = make_npc("if (created) { this.a = onwall(11,12); }", level, 1, x=1, y=1)
    assert run(npc, server)["a"] == 1.0
    npc.gs1_program = compile_gs1(
        "if (created) { noplayeronwall; this.a = onwall(11,12); }")
    assert run(npc, server)["a"] == 0.0


def test_onwall2_rect_covers_every_touched_tile():
    level = flat_level()
    level.set_tile(3, 3, WALL)
    npc = make_npc("if (created) { this.a = onwall2(2.5,2.5,1,1); this.b = onwall2(5,5,1,1);"
                   " this.c = onwall2(3,3,0,0); }", level, x=30, y=30)
    scope = run(npc)
    assert (scope["a"], scope["b"], scope["c"]) == (1.0, 0.0, 1.0)


def test_onwater_is_water_only():
    level = flat_level()
    level.set_tile(4, 4, WATER)
    level.set_tile(6, 6, tiletypes.TILE_TYPES.index(12))
    npc = make_npc("if (created) { this.a = onwater(4.5,4.5); this.b = onwater(6.5,6.5);"
                   " this.c = onwater2(3.5,3.5,2,2); }", level, x=30, y=30)
    scope = run(npc)
    assert (scope["a"], scope["b"], scope["c"]) == (1.0, 0.0, 1.0)


# -- board[] ------------------------------------------------------------------

def test_board_is_writable_without_a_broadcast():
    level = flat_level()
    npc = make_npc("if (created) { board[66] = 515; this.t = tiles[2,1];"
                   " this.r = board[66]; board[4096] = 1; board[-1] = 1;"
                   " board = 5; }", level)
    scope = run(npc)
    assert scope["t"] == 515.0 and scope["r"] == 515.0
    assert level.get_tile(2, 1) == 515
    assert level.get_tile(0, 0) == WALKABLE     # out-of-range writes did nothing


# -- explodedwithjolt ---------------------------------------------------------

def _explode(npc, carry, server=None):
    run_npc_event(npc, "exploded", server or FakeServer(), None,
                  carryobject_type=carry)


JOLT_SCRIPT = "if (exploded) { this.j = explodedwithjolt; }"


def test_explodedwithjolt_reads_the_blast_type():
    level = flat_level()
    npc = make_npc(JOLT_SCRIPT, level)
    _explode(npc, CarryObjectType.JOLTBOMB)
    assert npc.gs1_scopes["this"]["j"] == 1.0
    _explode(npc, CarryObjectType.BOMB)
    assert npc.gs1_scopes["this"]["j"] == 0.0
    _explode(npc, CarryObjectType.HOTJOLTBOMB)
    assert npc.gs1_scopes["this"]["j"] == 1.0


def test_explodedwithjolt_clears_on_other_events():
    # Deliberate divergence from the reference's stale flag after a jolt blast.
    npc = make_npc("if (exploded) { this.j = explodedwithjolt; }"
                   "if (washit) { this.after = explodedwithjolt; }", flat_level())
    _explode(npc, CarryObjectType.JOLTBOMB)
    run_npc_event(npc, "washit", FakeServer(), None)
    assert npc.gs1_scopes["this"]["j"] == 1.0
    assert npc.gs1_scopes["this"]["after"] == 0.0


def test_explodedwithjolt_option_off():
    config = ServerConfig()
    config.explodedwithjolt = False
    npc = make_npc(JOLT_SCRIPT, flat_level())
    _explode(npc, CarryObjectType.JOLTBOMB, FakeServer(config=config))
    assert npc.gs1_scopes["this"]["j"] == 0.0


def test_scriptengine_gs1_option_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "scriptengine-gs1.txt").write_text(
        "# x\nexplodedwithjolt=false\n")
    assert ServerConfig.from_server_dir(str(tmp_path)).explodedwithjolt is False
    assert ServerConfig.from_server_dir(str(tmp_path / "missing")).explodedwithjolt is True


def test_blast_passes_joltbomb_for_power_three():
    calls = []
    level = flat_level()
    npc = make_npc("", level, x=10, y=10)

    class Manager:
        # Blast targets are now picked by event-box rectangle from the
        # manager's own NPC list, not through audience.npcs_near.
        def get_npcs_on_level(self, level):
            return [npc]

        async def on_npc_exploded(self, npc, player, carry):
            calls.append((npc, carry))

    server = SimpleNamespace(npc_manager=Manager(), get_player=lambda pid: None)
    manager = CombatManager.__new__(CombatManager)
    manager.server = server

    async def go():
        for power in (3, 1):
            await manager.blast_level_objects(level, 10, 10, 2, power)
    asyncio.run(go())
    assert [c for _, c in calls] == [CarryObjectType.JOLTBOMB, CarryObjectType.BOMB]


# -- getnearestplayers ----------------------------------------------------------

def _two_players():
    level = flat_level()
    a = make_player(1, 30.0, 30.0, level)
    b = make_player(2, 11.0, 11.0, level)
    b.hearts = 0.0
    return level, FakeServer(players=[a, b])


@pytest.mark.parametrize("call,expected", [
    ("getnearestplayers(10,10)", [1.0, 0.0]),
    ("getnearestplayers(10,10,playerhearts>0)", [0.0]),
    ("getnearestplayers(10,10,playerhearts==0)", [1.0]),
    ("getnearestplayers(10,10,1)", [1.0, 0.0]),
])
def test_getnearestplayers_condition_and_indices(call, expected):
    level, server = _two_players()
    npc = make_npc(f"if (created) {{ this.r = {call}; }}", level, x=1, y=1)
    assert run(npc, server)["r"] == expected


def test_getnearestplayers_restores_the_acting_player():
    level, server = _two_players()
    acting = server.players[1]
    npc = make_npc("if (created) { this.r = getnearestplayers(10,10,playerhearts==0);"
                   " this.h = playerhearts; }", level, x=1, y=1)
    assert run(npc, server, player=acting)["h"] == 3.0


@pytest.mark.parametrize("owner_index", [0, 1])
@pytest.mark.parametrize("condition", ["client.vip==1", "vip==1"])
def test_getnearestplayers_condition_reads_each_candidates_flags(condition, owner_index):
    level = flat_level()
    a = make_player(1, 30.0, 30.0, level)
    b = make_player(2, 11.0, 11.0, level)
    a.flags.update({"client.vip": 1.0, "vip": 1.0})
    b.flags.update({"client.vip": 0.0, "vip": 0.0})
    server = FakeServer(players=[a, b])
    owner = (a, b)[owner_index]
    npc = make_npc(f"if (created) {{ this.r = getnearestplayers(10,10,{condition});"
                   " this.v = client.vip; }", level, x=1, y=1)
    scope = run(npc, server, player=owner)
    assert scope["r"] == [0.0]
    assert scope["v"] == owner.flags["client.vip"]     # restored afterwards


# -- weaponscount / levelorgx / levelorgy ---------------------------------------

def test_weaponscount_and_levelorg():
    level = flat_level()
    player = make_player(3, 5.0, 5.0, level)
    player.weapons = ["a", "b", "c"]
    carrier = make_npc("", level, 9, x=20.0, y=12.5)
    server = FakeServer(players=[player])
    server.npc_manager = SimpleNamespace(get_npc=lambda i: carrier if i == 9 else None)
    player.server = server
    npc = make_npc("if (created) { this.w = weaponscount; this.pw = playerweaponscount;"
                   " this.x = levelorgx; this.y = playerlevelorgy; }", level, x=1, y=1)
    scope = run(npc, server, player=player)
    assert scope["w"] == 3.0 and scope["pw"] == 3.0
    assert scope["x"] == 0.0 and scope["y"] == 0.0     # not attached
    player.attach_npc = (0, 9)
    scope = run(npc, server, player=player)
    assert scope["x"] == -20.0 and scope["y"] == -12.5


# -- level loading --------------------------------------------------------------

def test_short_level_files_are_refused(tmp_path):
    (tmp_path / "empty.nw").write_bytes(b"")
    (tmp_path / "short.nw").write_bytes(b"GLEVNW0")
    (tmp_path / "ok.nw").write_bytes(b"GLEVNW01\n")
    manager = LevelManager(str(tmp_path))
    assert manager.get_level("empty.nw") is None
    assert manager.get_level("short.nw") is None
    assert manager.get_level("ok.nw") is not None
    with pytest.raises(LevelLoadError):
        Level.load(str(tmp_path / "empty.nw"))
