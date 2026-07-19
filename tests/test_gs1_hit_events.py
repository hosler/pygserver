"""Regression tests for the GS1 sword/combat event chain (washit/wasshot/
exploded/compusdied), local. scope lifetime, the new hitobjects/hitnpc/
hitcompu commands, nw* clock variables, and a handful of the newly-backed
flags (isleader/compsdead/visible/playeronhorse/weaponsenabled).

Uses real Level/NPC/NPCManager/BaddyManager/CombatManager instances (per the
existing test_gs1_integration.py idiom) against a small FakeServer/FakePlayer,
rather than mocking the managers - the whole point of these tests is that the
real event-firing plumbing between combat.py/baddy.py/npc.py/gs1_host.py
actually connects end to end.
"""
import asyncio

import pytest

from pygserver.baddy import BaddyManager, BaddyType
from pygserver.combat import Arrow, Bomb, CombatManager
from pygserver.horse import HorseManager
from pygserver.level import BADDY_NAME_TO_TYPE, Level
from pygserver.npc import NPC, NPCManager
from pygserver.gs1_host import _NW_EPOCH, _nw_clock_value, compile_gs1, run_npc_event
from pygserver.world import GMap


class FakePlayer:
    def __init__(self, pid=1):
        self.id = pid
        self.x = 10.0
        self.y = 10.0
        self.direction = 2
        self.hearts = 3.0
        self.max_hearts = 3.0
        self.rupees = 0
        self.arrows = 0
        self.bombs = 0
        self.account_name = "hosler"
        self.nickname = "Hos"
        self.chat = ""
        self.flags = {}
        self.level = None
        self.sent = []

    async def send_raw(self, packet):
        self.sent.append(packet)

    async def send_props(self, props):
        pass

    def mark_dirty(self):
        pass


class FakeWorld:
    """Minimal World stand-in: same get_level/get_gmap_for_level contract as
    pygserver.world.World, without needing a real server.config."""

    def __init__(self):
        self._levels = {}
        self._gmaps = {}

    def get_level(self, name):
        return self._levels.get(name)

    def add_gmap(self, gmap):
        self._gmaps[gmap.name] = gmap

    def get_gmap_for_level(self, level_name):
        for gmap in self._gmaps.values():
            pos = gmap.find_level(level_name)
            if pos is not None:
                return (gmap, pos[0], pos[1])
        return None


class FakeServer:
    def __init__(self):
        self.world = FakeWorld()
        self.npc_manager = NPCManager(self)
        self.baddy_manager = BaddyManager(self)
        self.combat_manager = CombatManager(self)
        self.horse_manager = HorseManager(self)
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


# -- item 1: local. lifetime -------------------------------------------------
def test_local_scope_resets_between_events():
    npc = NPC(1, "t")
    npc.level = Level("t.nw")
    npc.gs1_program = compile_gs1(
        "if (created) { local.seen = 1; }"
        "if (playerchats) {"
        "  if (local.seen == 1) { message stale; } else { message fresh; }"
        "}"
    )
    run_npc_event(npc, "created", None, None)
    run_npc_event(npc, "playerchats", None, FakePlayer())
    # Before the fix local. was a dict created once on the NPC and never
    # cleared, so `created`'s local.seen leaked into the next event.
    assert npc.message == "fresh"


def test_this_scope_still_persists_across_events():
    # local.'s fix must not regress this./thiso. persistence.
    npc = NPC(1, "t")
    npc.level = Level("t.nw")
    npc.gs1_program = compile_gs1(
        "if (created) { this.hits = 0; }"
        "if (playerchats) { this.hits += 1; }"
    )
    run_npc_event(npc, "created", None, None)
    run_npc_event(npc, "playerchats", None, FakePlayer())
    run_npc_event(npc, "playerchats", None, FakePlayer())
    assert npc.gs1_scopes["this"]["hits"] == 2.0


# -- item 2: hitnpc / hitobjects / hitcompu commands + washit/wasshot/exploded
def test_hitnpc_fires_washit_and_damages_target():
    async def main():
        server, level = make_server_and_level()
        mgr = server.npc_manager
        target = mgr.create_npc(name="target", level=level, x=10, y=10)
        mgr.attach_gs1(target, "if (washit) { this.hit = 1; }")
        assert target.hearts == 3.0

        hitter = mgr.create_npc(name="hitter", level=level, x=5, y=5)
        # index 0 == target (added to the level before hitter)
        mgr.attach_gs1(hitter, "if (created) { hitnpc 0,2,0,0; }")
        await asyncio.sleep(0)  # let the scheduled on_npc_washit task run

        assert target.hearts == 2.0  # 2 halfhearts = 1 heart
        assert target.gs1_scopes["this"]["hit"] == 1.0

    asyncio.run(main())


def test_hitobjects_command_only_broadcasts_and_does_not_damage():
    # Matches upstream (Server::hitObjectsAtPoint, NPC-source overload): a
    # serverside `hitobjects` call is a pure client notification, not a
    # server-side hit-detection primitive - see gs1_host._c_hitobjects.
    async def main():
        server, level = make_server_and_level()
        mgr = server.npc_manager
        target = mgr.create_npc(name="target", level=level, x=10, y=10)
        mgr.attach_gs1(target, "if (washit) { this.hit = 1; }")

        caller = mgr.create_npc(name="caller", level=level, x=10, y=10)
        mgr.attach_gs1(caller, "if (created) { hitobjects 2,10,10; }")
        await asyncio.sleep(0)

        assert target.hearts == 3.0  # untouched
        assert "hit" not in target.gs1_scopes["this"]
        assert server.broadcasts  # but a relay packet was sent
        _, packet, _ = server.broadcasts[-1]
        from pygserver.protocol.constants import PLO
        assert packet[0] - 32 == PLO.HITOBJECTS

    asyncio.run(main())


def test_hitcompu_damages_a_baddy_via_the_real_baddy_hurt_path():
    # Deliberate deviation from upstream's leader-only notify-quirk (see
    # _c_hitcompu docstring): this applies real server-authoritative damage,
    # matching how every other baddy-damage path in pygserver works.
    async def main():
        server, level = make_server_and_level()
        leader = FakePlayer(1)
        leader.level = level
        level.add_player(leader)
        server._players[1] = leader

        baddy = await server.baddy_manager.add_baddy(level, 10, 10, BaddyType.GRAYSNAKE)
        starting_health = baddy.health
        starting_pos = (baddy.x, baddy.y)

        mgr = server.npc_manager
        caller = mgr.create_npc(name="caller", level=level, x=5, y=5)
        mgr.attach_gs1(caller, "if (created) { hitcompu 0,2,0,0; }")
        await asyncio.sleep(0)

        assert baddy.health == starting_health - 2
        # from=(0,0), target=(10,10): normalized diagonal, then pygserver's
        # authoritative baddy movement applies its standard 0.5-tile push.
        expected = 0.5 / (2 ** 0.5)
        assert baddy.x == starting_pos[0] + expected
        assert baddy.y == starting_pos[1] + expected

    asyncio.run(main())


def test_player_sword_swing_hitobjects_damages_npc_and_fires_washit():
    # The REAL sword-hit-detection path (combat.handle_hit_objects, wired
    # from player._handle_hit_objects's PLI_HITOBJECTS handler).
    async def main():
        server, level = make_server_and_level()
        mgr = server.npc_manager
        target = mgr.create_npc(name="target", level=level, x=10, y=10)
        mgr.attach_gs1(target, "if (washit) { this.hit = 1; }")

        attacker = FakePlayer(1)
        attacker.level = level

        await server.combat_manager.handle_hit_objects(attacker, 10.0, 10.0, 1.0)

        assert target.hearts == 2.0  # power=1.0 hearts
        assert target.gs1_scopes["this"]["hit"] == 1.0
        assert server.broadcasts  # relay for nearby clients

    asyncio.run(main())


def test_bomb_explosion_fires_exploded_on_npc_in_radius():
    async def main():
        server, level = make_server_and_level()
        mgr = server.npc_manager
        target = mgr.create_npc(name="target", level=level, x=10, y=10)
        mgr.attach_gs1(target, "if (exploded) { this.hit = 1; }")
        far = mgr.create_npc(name="far", level=level, x=60, y=60)
        mgr.attach_gs1(far, "if (exploded) { this.hit = 1; }")

        bomb = Bomb(id=1, player_id=0, level_name=level.name,
                    x=10, y=10, power=1, time_left=0)
        await server.combat_manager._detonate_bomb(bomb)

        assert target.gs1_scopes["this"]["hit"] == 1.0
        assert target.hearts < 3.0
        assert "hit" not in far.gs1_scopes["this"]  # out of radius

    asyncio.run(main())


def test_arrow_hits_npc_fires_wasshot_with_shotbyplayer_flag():
    async def main():
        server, level = make_server_and_level()
        mgr = server.npc_manager
        target = mgr.create_npc(name="target", level=level, x=10, y=10)
        mgr.attach_gs1(
            target,
            "if (wasshot) { this.shot = 1; this.byplayer = shotbyplayer;"
            " this.bybaddy = shotbybaddy; }",
        )
        shooter = FakePlayer(1)
        server._players[1] = shooter

        arrow = Arrow(id=1, player_id=1, level_name=level.name,
                      x=10.0, y=10.0, direction=2)
        await server.combat_manager._update_arrow(arrow, level)

        assert target.gs1_scopes["this"]["shot"] == 1.0
        assert target.gs1_scopes["this"]["byplayer"] == 1.0
        assert target.gs1_scopes["this"]["bybaddy"] == 0.0

    asyncio.run(main())


def test_compusdied_fires_when_last_baddy_dies():
    async def main():
        server, level = make_server_and_level()
        mgr = server.npc_manager
        npc = mgr.create_npc(name="watcher", level=level, x=1, y=1)
        mgr.attach_gs1(npc, "if (compusdied) { this.cleared = 1; }")

        baddy = await server.baddy_manager.add_baddy(level, 5, 5, BaddyType.GRAYBALL)
        await server.baddy_manager.handle_baddy_death(baddy, None)

        assert npc.gs1_scopes["this"]["cleared"] == 1.0

    asyncio.run(main())


# -- item 3: a handful of newly-backed flags ---------------------------------
def test_isleader_compsdead_and_visible_flags():
    server, level = make_server_and_level()
    leader = FakePlayer(1)
    level.add_player(leader)
    server._players[1] = leader

    npc = NPC(1, "t")
    npc.level = level
    npc.visible = False
    npc.gs1_program = compile_gs1(
        "if (playertouchsme) {"
        " this.leader = isleader; this.dead = compsdead; this.vis = visible; }"
    )
    run_npc_event(npc, "playertouchsme", server, leader)
    assert npc.gs1_scopes["this"]["leader"] == 1.0
    assert npc.gs1_scopes["this"]["dead"] == 1.0  # no baddies at all -> vacuously true
    assert npc.gs1_scopes["this"]["vis"] == 0.0


def test_compsdead_false_while_a_baddy_is_alive():
    async def main():
        server, level = make_server_and_level()
        await server.baddy_manager.add_baddy(level, 5, 5, BaddyType.GRAYBALL)
        npc = NPC(1, "t")
        npc.level = level
        npc.gs1_program = compile_gs1("if (created) { this.dead = compsdead; }")
        run_npc_event(npc, "created", server, None)
        assert npc.gs1_scopes["this"]["dead"] == 0.0

    asyncio.run(main())


def test_playeronhorse_and_weaponsenabled_flags():
    server, level = make_server_and_level()
    p = FakePlayer(1)
    p.weapons_disabled = True
    npc = NPC(1, "t")
    npc.level = level
    npc.gs1_program = compile_gs1(
        "if (playertouchsme) { this.horse = playeronhorse; this.weap = weaponsenabled; }"
    )
    run_npc_event(npc, "playertouchsme", server, p)
    assert npc.gs1_scopes["this"]["horse"] == 0.0
    assert npc.gs1_scopes["this"]["weap"] == 0.0

    server.horse_manager._mounted[p.id] = object()
    p.weapons_disabled = False
    run_npc_event(npc, "playertouchsme", server, p)
    assert npc.gs1_scopes["this"]["horse"] == 1.0
    assert npc.gs1_scopes["this"]["weap"] == 1.0


def test_isonmap_and_onmapx_onmapy():
    server, level = make_server_and_level("level_00.nw")
    gmap = GMap("overworld")
    gmap.width = 2
    gmap.height = 2
    gmap.grid[(0, 0)] = "level_00.nw"
    gmap.grid[(1, 0)] = "level_10.nw"
    server.world.add_gmap(gmap)

    npc = NPC(1, "t")
    npc.level = level
    npc.gs1_program = compile_gs1(
        "if (created) {"
        " this.onmap = isonmap;"
        " this.x1 = onmapx(level_10.nw); this.y1 = onmapy(level_10.nw);"
        " this.missing = onmapx(nope.nw); }"
    )
    run_npc_event(npc, "created", server, None)
    assert npc.gs1_scopes["this"]["onmap"] == 1.0
    assert npc.gs1_scopes["this"]["x1"] == 1.0
    assert npc.gs1_scopes["this"]["y1"] == 0.0
    # not actually in the grid -> defaults to (0,0), not -1 (matches the C++
    # .value_or(MapPosition{0,0})).
    assert npc.gs1_scopes["this"]["missing"] == 0.0

    off_map = Level("standalone.nw")
    npc2 = NPC(2, "t")
    npc2.level = off_map
    npc2.gs1_program = compile_gs1("if (created) { this.onmap = isonmap; }")
    run_npc_event(npc2, "created", server, None)
    assert npc2.gs1_scopes["this"]["onmap"] == 0.0


# -- item 4: nw* clock variables ---------------------------------------------
def test_nwtime_family_matches_upstream_formula():
    ticks = 12345  # arbitrary fixed tick count

    class _FixedTime:
        @staticmethod
        def time():
            return _NW_EPOCH + ticks * 5

    import pygserver.gs1_host as gs1_host
    orig_time = gs1_host.time
    gs1_host.time = _FixedTime
    try:
        assert _nw_clock_value("nwtime") == float(ticks % 1440)
        assert _nw_clock_value("nwmin") == float(ticks % 60)
        assert _nw_clock_value("nwhour") == float((ticks // 60) % 24)
        assert _nw_clock_value("nwday") == float((ticks // 1440) % 28 + 1)
        assert _nw_clock_value("nwweekday") == float((ticks // 1440) % 7 + 1)
        assert _nw_clock_value("nwweek") == float((ticks // 10080) % 40 + 1)
        assert _nw_clock_value("nwmonth") == float((ticks // 40320) % 10 + 1)
        assert _nw_clock_value("nwyear") == float((ticks // 403200) + 1000)
    finally:
        gs1_host.time = orig_time


def test_nwtime_builtin_reachable_from_a_script():
    npc = NPC(1, "t")
    npc.level = Level("t.nw")
    npc.gs1_program = compile_gs1("if (created) { this.t = nwtime; this.hr = nwhour; }")
    run_npc_event(npc, "created", None, None)
    assert 0 <= npc.gs1_scopes["this"]["t"] < 1440
    assert 0 <= npc.gs1_scopes["this"]["hr"] < 24


# -- item 6: spider baddy alias -----------------------------------------------
def test_spider_is_an_octopus_alias():
    assert BADDY_NAME_TO_TYPE["spider"] == BADDY_NAME_TO_TYPE["octopus"] == 6
