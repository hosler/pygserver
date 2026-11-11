"""Explosion / spy-fire NPC events by event-box rectangle, and NPC-fired arrows.

Reference: GServer-v2 Level.cpp addExplosion :2228-2264, addSpyFire :2268-2320,
moveArrow :2750-2819, GS1Commands.cpp fn_shootarrow..fn_shootnuke :3010-3125,
fn_spyfire :3365-3383, PlayerClientPackets.cpp msgPLI_FIRESPY :314-327."""
import asyncio
import functools

import pytest

from pygserver.combat import (
    ARROW_FIREBLAST, ARROW_NORMAL, MAX_SCRIPT_ARROWS_PER_LEVEL,
    explosion_event_rects, spyfire_event_rect)
from pygserver.gs1_host import run_npc_event
from pygserver.protocol.constants import NPCBLOCKFLAG, PLO
from pygserver.protocol.packets import build_arrow_add, build_fire_spy

from tests.test_gs1_hit_events import FakePlayer, make_server_and_level

HIT = "if (exploded) { this.n = this.n + 1; }"
SHOT = "if (wasshot) { this.shot = this.shot + 1; this.bynpc = shotbynpc; }"


def _npc(server, level, name, x, y, script=HIT):
    npc = server.npc_manager.create_npc(name=name, level=level, x=x, y=y)
    server.npc_manager.attach_gs1(npc, script)
    return npc


def _count(npc, key="n"):
    return npc.gs1_scopes["this"].get(key, 0.0)


def aio(test):
    """Run an async test body on a fresh loop (NPC creation needs one)."""
    @functools.wraps(test)
    def wrapper(**kwargs):
        asyncio.run(test(**kwargs))
    return wrapper


async def _blast(server, level, x, y, radius, power=1):
    await server.combat_manager.blast_level_objects(level, x, y, radius, power)


@aio
async def test_explosion_rects_follow_the_reference_arms():
    # radius 1 at tile (10, 10) = pixel (160, 160): column and row 32 px wide,
    # reaching radius * 32 px either side (Level.cpp:2243-2244).
    assert explosion_event_rects(10, 10, 1) == [(160, 128, 32, 96), (128, 160, 96, 32)]


@aio
async def test_spyfire_rect_matches_the_reference_for_each_facing():
    # Player top-left (160, 160), length 1: starting offsets and strip sizes
    # from Level.cpp:2276-2312.
    assert spyfire_event_rect(160, 160, 0, 1) == (168, 72, 32, 64)
    assert spyfire_event_rect(160, 160, 2, 1) == (168, 195, 32, 64)
    assert spyfire_event_rect(160, 160, 1, 1) == (64, 163, 64, 32)
    assert spyfire_event_rect(160, 160, 3, 1) == (208, 163, 64, 32)


@aio
async def test_explosion_hits_npcs_on_the_arms_not_in_the_corners():
    server, level = make_server_and_level()
    arm = _npc(server, level, "arm", 10, 13)        # 2.x tiles below, in the column
    row = _npc(server, level, "row", 8.5, 10)       # left of the centre, in the row
    corner = _npc(server, level, "corner", 13, 13)  # outside both rectangles
    await _blast(server, level, 10, 10, 1)
    assert _count(arm) == 1.0 and _count(row) == 1.0
    assert "n" not in corner.gs1_scopes["this"]
    assert arm.hearts == 2.5 and corner.hearts == 3.0


@aio
async def test_npc_in_the_centre_cell_runs_exploded_once():
    # The reference tests the column and the row separately and queues the
    # event for each; we deliberately run it once per blast.
    server, level = make_server_and_level()
    centre = _npc(server, level, "centre", 10, 10)
    await _blast(server, level, 10, 10, 1)
    assert _count(centre) == 1.0
    assert centre.hearts == 2.5


@aio
async def test_explosion_skips_invisible_but_not_noblock_npcs():
    server, level = make_server_and_level()
    hidden = _npc(server, level, "hidden", 10, 10)
    hidden.visible = False
    solid_free = _npc(server, level, "free", 10, 11)
    solid_free.block_flags = NPCBLOCKFLAG.NOBLOCK
    await _blast(server, level, 10, 10, 1)
    assert "n" not in hidden.gs1_scopes["this"]
    assert _count(solid_free) == 1.0


@aio
async def test_explosion_uses_the_character_feet_box():
    # A character NPC's event box is 32x32 at +(8, 16) px, whatever its x/y:
    # an origin well above the blast still reaches it.
    server, level = make_server_and_level()
    char = _npc(server, level, "char", 10, 8)
    char.image = "#c#"
    await _blast(server, level, 10, 10, 1)
    assert _count(char) == 1.0


@aio
async def test_explosion_edges_are_inclusive():
    server, level = make_server_and_level()
    # Column spans x 160..192 px: tile 12 is exactly the right edge.
    edge = _npc(server, level, "edge", 12, 11)
    past = _npc(server, level, "past", 12.1, 13)
    await _blast(server, level, 10, 10, 1)
    assert _count(edge) == 1.0
    assert "n" not in past.gs1_scopes["this"]


@aio
async def test_explosion_radius_is_bounded_before_the_cell_loop():
    from pygserver.combat import MAX_EXPLOSION_RADIUS
    server, level = make_server_and_level()
    target = _npc(server, level, "t", 10, 10)
    cells = []

    async def spy(_level, touched):
        cells.append(set(touched))
    server.combat_manager._destroy_blast_blocks = spy
    await _blast(server, level, 10, 10, 10 ** 9)  # would never end unclamped
    assert _count(target) == 1.0
    assert len(cells[0]) == 4 * MAX_EXPLOSION_RADIUS + 1


@aio
async def test_timed_power_two_bomb_reaches_npcs_by_the_reference_radii():
    from pygserver.combat import Bomb
    server, level = make_server_and_level()
    near = _npc(server, level, "near", 10, 18)   # 8 tiles down: radius-4 arm
    far = _npc(server, level, "far", 10, 26)
    bomb = Bomb(id=1, player_id=0, level_name=level.name, x=10, y=10,
                power=2, time_left=0)
    server.combat_manager._bombs[level.name] = {1: bomb}
    await server.combat_manager._tick()
    assert _count(near) == 1.0
    assert "n" not in far.gs1_scopes["this"]


@aio
async def test_serverside_off_raises_no_npc_events():
    server, level = make_server_and_level()
    from types import SimpleNamespace
    target = _npc(server, level, "t", 10, 10)  # has its script before the switch
    assert target.gs1_program is not None
    server.config = SimpleNamespace(serverside=False)
    await _blast(server, level, 10, 10, 1)
    assert "n" not in target.gs1_scopes["this"]
    server.config = SimpleNamespace(serverside=True)
    await _blast(server, level, 10, 10, 1)
    assert _count(target) == 1.0


# -- spy fire -------------------------------------------------------------------

def _spy_player(server, level, x, y, direction):
    player = FakePlayer(1)
    player.x, player.y, player.direction, player.level = x, y, direction, level
    server._players[1] = player
    return player


@aio
async def test_fire_spy_packet_raises_exploded_on_npcs_in_the_strip():
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)  # facing up
    ahead = _npc(server, level, "ahead", 11, 6)      # inside (168..200, 72..136)
    behind = _npc(server, level, "behind", 11, 12)
    await server.combat_manager.handle_fire_spy(player, (1 << 3) | 2)
    assert server.broadcasts == [(level.name, build_fire_spy(1, 10), {1})]
    assert _count(ahead) == 1.0
    assert "n" not in behind.gs1_scopes["this"]


@aio
async def test_fire_spy_power_three_is_a_joltbomb():
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)
    target = _npc(server, level, "t", 11, 6,
                  "if (exploded) { this.j = explodedwithjolt; }")
    await server.combat_manager.handle_fire_spy(player, (1 << 3) | 3)
    assert _count(target, "j") == 1.0


@aio
async def test_spyfire_command_broadcasts_to_everyone_and_hits_npcs():
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)
    ahead = _npc(server, level, "ahead", 11, 6)
    caster = _npc(server, level, "caster", 40, 40, "if (created) { spyfire 33,10; }")
    run_npc_event(caster, "created", server, player)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # length 33 & 31 = 1, power 10 & 7 = 2; no `exclude`: the caster sees it.
    assert server.broadcasts == [(level.name, build_fire_spy(1, (1 << 3) | 2), None)]
    assert _count(ahead) == 1.0


# -- NPC-fired arrows -----------------------------------------------------------

async def _fire(server, level, command, x, y, direction, image=None,
                extra="", shape=None):
    shooter = server.npc_manager.create_npc(name="shooter", level=level, x=x, y=y)
    if image:
        shooter.image = image
    if shape:
        shooter.shape = shape
    server.npc_manager.attach_gs1(
        shooter, "if (created) { %s %d; } %s" % (command, direction, extra))
    await asyncio.sleep(0)  # the NPC's own `created` event fires the command
    return shooter


async def _fly(server, ticks):
    for _ in range(ticks):
        await server.combat_manager._tick()


@aio
async def test_script_arrow_hits_an_npc_with_wasshot_and_is_spent():
    server, level = make_server_and_level()
    target = _npc(server, level, "target", 16, 20, SHOT)
    # The arrow starts inside the shooter's 64 px box, so only the self-skip
    # (Level.cpp:2796) keeps `wasshot` from running on it.
    shooter = await _fire(server, level, "shootarrow", 10, 20, 3,
                          extra="if (wasshot) { this.hit = 1; }", shape=(64, 64))
    assert server.broadcasts[0] == (
        level.name, build_arrow_add(0, 10.0, 20.0, 3, 107 + 3, ARROW_NORMAL), None)
    assert len(server.combat_manager._script_arrows[level.name]) == 1
    await _fly(server, 20)
    assert _count(target, "shot") == 1.0
    assert target.gs1_scopes["this"]["bynpc"] == 1.0
    assert target.hearts == 2.5
    assert not server.combat_manager._script_arrows
    assert "hit" not in shooter.gs1_scopes["this"]  # never hits its own shooter


@aio
async def test_fireball_does_two_half_hearts():
    server, level = make_server_and_level()
    target = _npc(server, level, "target", 16, 20, SHOT)
    await _fire(server, level, "shootfireball", 10, 20, 3)
    await _fly(server, 20)
    assert target.hearts == 2.0


@aio
async def test_fireblast_explodes_where_it_hits_an_npc():
    server, level = make_server_and_level()
    target = _npc(server, level, "target", 16, 20, SHOT + HIT)
    bystander = _npc(server, level, "bystander", 16, 21)  # in the blast, not shot
    await _fire(server, level, "shootfireblast", 10, 20, 3)
    await _fly(server, 20)
    assert _count(target, "shot") == 1.0
    assert _count(target) == 1.0       # ...and caught in its own blast
    assert _count(bystander) == 1.0
    assert not server.combat_manager._script_arrows


@aio
async def test_fireblast_explodes_on_a_wall_and_a_plain_arrow_does_not():
    server, level = make_server_and_level()
    for ty in range(0, 64):
        level.set_tile(30, ty, 4)  # tile 4 is BLOCKING
    near = _npc(server, level, "near", 29, 21)  # below the shot's path: only the blast reaches it
    await _fire(server, level, "shootfireblast", 10, 20, 3)
    await _fly(server, 25)
    assert _count(near) == 1.0
    assert not server.combat_manager._script_arrows

    server2, level2 = make_server_and_level()
    for ty in range(0, 64):
        level2.set_tile(30, ty, 4)
    near2 = _npc(server2, level2, "near", 29, 21)
    await _fire(server2, level2, "shootarrow", 10, 20, 3)
    await _fly(server2, 25)
    assert "n" not in near2.gs1_scopes["this"]


@aio
async def test_script_arrow_expires_after_two_sublevels_of_travel():
    server, level = make_server_and_level()
    await _fire(server, level, "shootarrow", 10, 20, 2)  # straight down, off the level
    await _fly(server, 128)
    assert server.combat_manager._script_arrows
    await _fly(server, 2)
    assert not server.combat_manager._script_arrows


@aio
async def test_script_arrows_per_level_are_capped():
    server, level = make_server_and_level()
    shooter = _npc(server, level, "s", 10, 10, "")
    kept = [server.combat_manager.spawn_script_arrow(
        level, shooter, 2, ARROW_FIREBLAST, 10, 10)
        for _ in range(MAX_SCRIPT_ARROWS_PER_LEVEL + 5)]
    assert kept.count(None) == 5
    assert len(server.combat_manager._script_arrows[level.name]) == MAX_SCRIPT_ARROWS_PER_LEVEL


@aio
async def test_character_npc_fires_from_its_offset_position():
    server, level = make_server_and_level()
    await _fire(server, level, "shootfireblast", 20, 30, 1, image="#c#")
    assert server.broadcasts == [
        (level.name, build_arrow_add(0, 19.5, 31.5, 1, 108, ARROW_FIREBLAST), None)]
    arrow = next(iter(server.combat_manager._script_arrows[level.name].values()))
    assert (arrow.x, arrow.y) == (19.5 * 16, 31.5 * 16)


@aio
async def test_script_arrows_need_the_npc_server():
    from types import SimpleNamespace
    server, level = make_server_and_level()
    server.config = SimpleNamespace(serverside=False)
    await _fire(server, level, "shootfireblast", 10, 20, 3)
    assert server.broadcasts == []
    assert not server.combat_manager._script_arrows


# -- review round: caps, throttles, wire range ----------------------------------

class _Clock:
    """Stand-in for CombatManager._clock that only moves when told to."""
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _count_blasts(manager):
    counter = {"n": 0}
    real = manager._blast_level_objects

    async def counted(*args, **kwargs):
        counter["n"] += 1
        return await real(*args, **kwargs)
    manager._blast_level_objects = counted
    return counter


@aio
async def test_exploded_handlers_that_putexplosion_cannot_multiply_without_end():
    from pygserver.combat import MAX_BLASTS_PER_LEVEL_TICK
    server, level = make_server_and_level()
    manager = server.combat_manager
    manager._clock = _Clock()  # one 50 ms window for the whole cascade
    counter = _count_blasts(manager)
    for i in range(3):
        _npc(server, level, f"n{i}", 10 + i, 10,
             "if (exploded) { putexplosion 1,10,10; }")
    await manager.blast_level_objects(level, 10, 10, 1, 1, script=True)
    for _ in range(40):
        await asyncio.sleep(0)
    assert counter["n"] == MAX_BLASTS_PER_LEVEL_TICK


@aio
async def test_a_blast_chain_stops_after_the_generation_cap():
    from pygserver.combat import MAX_BLAST_DEPTH
    server, level = make_server_and_level()
    manager = server.combat_manager
    clock = _Clock()
    manager._clock = clock
    counter = _count_blasts(manager)
    _npc(server, level, "loop", 10, 10, "if (exploded) { putexplosion 1,10,10; }")
    real = manager.reserve_script_blast

    def spaced(name):
        clock.now += 1.0  # every blast gets a fresh budget window
        return real(name)
    manager.reserve_script_blast = spaced
    await manager.blast_level_objects(level, 10, 10, 1, 1, script=True)
    for _ in range(60):
        await asyncio.sleep(0)
    assert counter["n"] == MAX_BLAST_DEPTH


@aio
async def test_a_player_blast_starts_the_chain_count_but_is_never_dropped():
    from pygserver.combat import MAX_BLAST_DEPTH
    server, level = make_server_and_level()
    manager = server.combat_manager
    clock = _Clock()
    manager._clock = clock
    counter = _count_blasts(manager)
    _npc(server, level, "loop", 10, 10, "if (exploded) { putexplosion 1,10,10; }")
    real = manager.reserve_script_blast

    def spaced(name):
        clock.now += 1.0
        return real(name)
    manager.reserve_script_blast = spaced
    await manager.blast_level_objects(level, 10, 10, 1, 1)  # a player's bomb
    for _ in range(60):
        await asyncio.sleep(0)
    # the bomb itself plus seven script generations: it already counts as one
    assert counter["n"] == MAX_BLAST_DEPTH


@aio
async def test_the_blast_budget_refills_on_the_next_tick():
    from pygserver.combat import MAX_BLASTS_PER_LEVEL_TICK
    server, level = make_server_and_level()
    manager = server.combat_manager
    clock = _Clock()
    manager._clock = clock
    counter = _count_blasts(manager)
    for _ in range(MAX_BLASTS_PER_LEVEL_TICK + 10):
        await manager.blast_level_objects(level, 10, 10, 1, 1, script=True)
    assert counter["n"] == MAX_BLASTS_PER_LEVEL_TICK
    clock.now += 0.06
    await manager.blast_level_objects(level, 10, 10, 1, 1, script=True)
    assert counter["n"] == MAX_BLASTS_PER_LEVEL_TICK + 1


@aio
async def test_a_script_flood_never_starves_a_player_blast():
    server, level = make_server_and_level()
    manager = server.combat_manager
    manager._clock = _Clock()
    target = _npc(server, level, "t", 40, 40)
    for _ in range(40):
        await manager.blast_level_objects(level, 5, 5, 1, 1, script=True)
    await manager.blast_level_objects(level, 40, 40, 1, 1)  # a player's bomb
    assert _count(target) == 1.0
    # ...and a script blast in the same window loses wire packet and effect alike
    caster = server.npc_manager.create_npc(name="c", level=level, x=50, y=50)
    server.broadcasts.clear()
    server.npc_manager.attach_gs1(caster, "if (created) { putexplosion 1,40,40; }")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert _count(target) == 1.0
    assert not [b for b in server.broadcasts if b[1][0] == PLO.EXPLOSION + 32]


@aio
async def test_a_player_fire_spy_is_not_charged_to_the_script_budget():
    server, level = make_server_and_level()
    manager = server.combat_manager
    manager._clock = _Clock()
    player = _spy_player(server, level, 10, 10, 0)
    target = _npc(server, level, "t", 11, 6)
    for _ in range(40):
        await manager.blast_level_objects(level, 5, 5, 1, 1, script=True)
    await manager.handle_fire_spy(player, (1 << 3) | 2)
    assert _count(target) == 1.0


@aio
async def test_script_arrow_tick_stays_cheap_with_a_crowded_level():
    import time
    best = float("inf")
    for _ in range(3):  # best of three: one run can lose the CPU under load
        server, level = make_server_and_level()
        for i in range(1024):
            _npc(server, level, f"n{i}", 2 + i % 60, 2 + (i // 60) * 3, "")
        shooter = _npc(server, level, "s", 0, 60, "")
        for _ in range(64):
            server.combat_manager.spawn_script_arrow(level, shooter, 3, ARROW_NORMAL, 0.5, 62)
        start = time.perf_counter()
        await server.combat_manager._tick()
        best = min(best, time.perf_counter() - start)
    assert best < 0.05  # ~5 ms measured; 275 ms before the grid


@aio
async def test_npcs_late_in_a_crowded_level_are_still_hit():
    from pygserver.combat import MAX_EVENT_NPC_PROBES
    server, level = make_server_and_level()
    for i in range(MAX_EVENT_NPC_PROBES + 200):  # far from the action
        _npc(server, level, f"far{i}", 2 + i % 20, 2 + (i // 20) % 20, "")
    late = _npc(server, level, "late", 50, 50)
    await _blast(server, level, 50, 50, 1)
    assert _count(late) == 1.0
    target = _npc(server, level, "target", 56, 20, SHOT)
    shooter = _npc(server, level, "shooter", 40, 20, "")
    server.combat_manager.spawn_script_arrow(level, shooter, 3, ARROW_NORMAL, 40, 20)
    await _fly(server, 20)
    assert _count(target, "shot") == 1.0


@aio
async def test_live_script_arrows_are_capped_across_levels_too():
    from pygserver.combat import MAX_SCRIPT_ARROWS_TOTAL
    server, level = make_server_and_level()
    shooter = _npc(server, level, "s", 10, 10, "")
    from pygserver.level import Level
    granted = 0
    for n in range(6):
        other = Level(f"l{n}.nw")
        for _ in range(MAX_SCRIPT_ARROWS_PER_LEVEL):
            if server.combat_manager.spawn_script_arrow(
                    other, shooter, 2, ARROW_NORMAL, 10, 10) is not None:
                granted += 1
    assert granted == MAX_SCRIPT_ARROWS_TOTAL


@aio
async def test_a_refused_script_arrow_is_not_broadcast():
    server, level = make_server_and_level()
    shooter = _npc(server, level, "s", 10, 10, "")
    for _ in range(MAX_SCRIPT_ARROWS_PER_LEVEL):
        server.combat_manager.spawn_script_arrow(level, shooter, 2, ARROW_NORMAL, 10, 10)
    await _fire(server, level, "shootarrow", 12, 12, 2)
    assert not [b for b in server.broadcasts if b[1][0] == PLO.ARROWADD + 32]


def _clean_wire(packet):
    """No byte below 0x20 (a newline would split the packet) before its end."""
    body = packet[:-1]
    assert packet.endswith(b"\n") and all(b >= 0x20 for b in body), packet


def _shoot_ctx(server, level, npc):
    from types import SimpleNamespace
    return (SimpleNamespace(server=server, _level_of=lambda ctx: level),
            SimpleNamespace(this_obj=npc))


class _ApprovingManager:
    """Accepts every arrow, so only the command's own wire guard can stop it."""
    def spawn_script_arrow(self, *args):
        return object()


@pytest.mark.parametrize("x", [-11, -16, 117, 120, 1e30, float("nan"), float("inf")])
def test_every_layer_refuses_an_arrow_position_a_byte_cannot_carry(x):
    from pygserver.combat import wire_tile_ok
    from pygserver.gs1.commands import objects
    assert not wire_tile_ok(x, 10)
    server, level = make_server_and_level()
    # layer 1: the manager refuses to register it
    assert server.combat_manager.spawn_script_arrow(level, None, 3, ARROW_NORMAL, x, 10) is None
    # layer 2: the command stops before it reaches the wire, even if the
    # manager would have accepted the arrow
    sent = []

    async def broadcast(name, packet, exclude=None):
        sent.append(packet)
    server.broadcast_to_level = broadcast
    server.combat_manager = _ApprovingManager()
    npc = type("N", (), {"x": x, "y": 10.0, "image": "", "id": 1})()
    me, ctx = _shoot_ctx(server, level, npc)

    async def go():
        objects._shoot_arrow(me, [3], ctx, ARROW_FIREBLAST)
        await asyncio.sleep(0)
    asyncio.run(go())
    assert sent == []


@pytest.mark.parametrize("direction", [float("nan"), float("inf")])
def test_a_non_finite_arrow_direction_is_ignored_not_raised(direction):
    from pygserver.gs1.commands import objects
    server, level = make_server_and_level()
    server.combat_manager = _ApprovingManager()
    npc = type("N", (), {"x": 10.0, "y": 10.0, "image": "", "id": 1})()
    me, ctx = _shoot_ctx(server, level, npc)
    objects._shoot_arrow(me, [direction], ctx, ARROW_FIREBLAST)  # must not raise
    assert server.broadcasts == []


def test_the_encodable_range_is_exactly_what_one_byte_carries():
    from pygserver.combat import wire_tile_ok
    assert wire_tile_ok(0, 0) and wire_tile_ok(111.5, 111.5)
    assert not wire_tile_ok(112, 0) and not wire_tile_ok(0, -0.5)


@aio
async def test_arrow_at_the_edge_of_the_encodable_range_is_sent_clean():
    server, level = make_server_and_level()
    await _fire(server, level, "shootnuke", 111.5, 0, 3)
    sent = [b[1] for b in server.broadcasts if b[1][0] == PLO.ARROWADD + 32]
    assert len(sent) == 1
    _clean_wire(sent[0])
    assert len(server.combat_manager._script_arrows[level.name]) == 1


@aio
@pytest.mark.parametrize("radius", [234, 255, 300, 10 ** 12])
async def test_putexplosion_radius_is_clamped_on_the_wire(radius):
    from pygserver.combat import MAX_WIRE_RADIUS
    server, level = make_server_and_level()
    caster = server.npc_manager.create_npc(name="c", level=level, x=40, y=40)
    server.npc_manager.attach_gs1(
        caster, "if (created) { putexplosion %d,10,10; }" % int(radius))
    await asyncio.sleep(0)
    sent = [b[1] for b in server.broadcasts if b[1][0] == PLO.EXPLOSION + 32]
    assert len(sent) == 1
    _clean_wire(sent[0])
    assert sent[0][3] - 32 == min(int(radius), MAX_WIRE_RADIUS)


@aio
@pytest.mark.parametrize("args,where", [
    ("1,-11,10", (-11, 10)), ("1,120,10", (120, 10)), ("1,10,-1", (10, -1)),
    ("1,99999999999,10", None)])
async def test_putexplosion_positions_a_byte_cannot_carry_skip_only_the_packet(args, where):
    server, level = make_server_and_level()
    target = _npc(server, level, "t", *where) if where else None
    caster = server.npc_manager.create_npc(name="c", level=level, x=40, y=40)
    server.npc_manager.attach_gs1(caster, "if (created) { putexplosion %s; }" % args)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not [b for b in server.broadcasts if b[1][0] == PLO.EXPLOSION + 32]
    if target is not None:
        assert _count(target) == 1.0  # the server-side effect still ran


@aio
@pytest.mark.parametrize("args", ["1,-11,10", "1,120,10", "1,10,-1"])
async def test_putbomb_positions_a_byte_cannot_carry_are_not_announced(args):
    server, level = make_server_and_level()
    caster = server.npc_manager.create_npc(name="c", level=level, x=40, y=40)
    server.npc_manager.attach_gs1(caster, "if (created) { putbomb %s; }" % args)
    await asyncio.sleep(0)
    assert not [b for b in server.broadcasts if b[1][0] == PLO.BOMBADD + 32]


@aio
async def test_putbomb_inside_the_range_is_announced_clean():
    server, level = make_server_and_level()
    caster = server.npc_manager.create_npc(name="c", level=level, x=40, y=40)
    server.npc_manager.attach_gs1(caster, "if (created) { putbomb 1,10,10; }")
    await asyncio.sleep(0)
    sent = [b[1] for b in server.broadcasts if b[1][0] == PLO.BOMBADD + 32]
    assert len(sent) == 1
    _clean_wire(sent[0])


@aio
@pytest.mark.parametrize("radius,power", [(float("nan"), 1), (float("inf"), 1),
                                          (1, float("nan")), (1, float("inf"))])
async def test_explode_ignores_a_non_finite_radius_or_power(radius, power):
    from pygserver.gs1.commands import combat as commands
    server, level = make_server_and_level()
    me, ctx = _shoot_ctx(server, level, None)
    commands._explode(me, ctx, radius, power, 10, 10)  # must not raise
    await asyncio.sleep(0)
    assert server.broadcasts == []


@aio
@pytest.mark.parametrize("power,length", [("2", "29"), ("7", "31"), ("0", "30")])
async def test_spyfire_packed_byte_never_wraps_onto_a_newline(power, length):
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)
    caster = server.npc_manager.create_npc(name="c", level=level, x=40, y=40)
    server.npc_manager.attach_gs1(caster, "if (created) { spyfire %s,%s; }" % (length, power))
    run_npc_event(caster, "created", server, player)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    sent = [b[1] for b in server.broadcasts if b[1][0] == PLO.FIRESPY + 32]
    assert len(sent) == 1
    _clean_wire(sent[0])
    assert (sent[0][3] - 32) & 7 == int(power)  # the power survives the clamp


@aio
async def test_a_stale_grid_hit_is_rechecked_against_the_live_box():
    server, level = make_server_and_level()
    target = _npc(server, level, "t", 20, 20,
                  "if (wasshot) { this.n = this.n + 1; x = 50; }")
    target.shape = (32, 32)
    shooter = _npc(server, level, "s", 0, 0, "")
    for dy in (0.0, 0.5):
        server.combat_manager.spawn_script_arrow(
            level, shooter, 3, ARROW_NORMAL, 18.0, 20.5 + dy)
    await server.combat_manager._tick()
    assert target.x == 50.0
    assert _count(target) == 1.0  # the second arrow no longer finds it


@aio
async def test_a_removed_npc_is_not_hit_from_a_stale_grid():
    server, level = make_server_and_level()
    mgr = server.npc_manager
    first = _npc(server, level, "first", 20, 20, "")
    first.shape = (32, 32)
    victim = _npc(server, level, "victim", 20, 20, SHOT)
    victim.shape = (32, 32)
    shooter = _npc(server, level, "s", 0, 0, "")
    cm = server.combat_manager
    grid = cm._collision_grid(level)
    del mgr._npcs[victim.id]
    hits = cm._grid_hits(grid, (300, 320, 32, 0))
    assert victim not in hits and first in hits


@aio
async def test_event_probe_cap_bounds_explosion_work():
    from pygserver.combat import MAX_EVENT_NPC_PROBES
    server, level = make_server_and_level()
    for i in range(MAX_EVENT_NPC_PROBES + 50):
        server.npc_manager.create_npc(name=f"p{i}", level=level, x=10, y=10)
    seen = []

    async def exploded(npc, attacker, carry):
        seen.append(npc)
    server.npc_manager.on_npc_exploded = exploded
    await _blast(server, level, 10, 10, 1)
    assert len(seen) == MAX_EVENT_NPC_PROBES


@aio
async def test_event_probe_cap_bounds_script_arrow_work():
    from pygserver.combat import MAX_EVENT_NPC_PROBES
    server, level = make_server_and_level()
    for i in range(MAX_EVENT_NPC_PROBES + 50):
        server.npc_manager.create_npc(name=f"p{i}", level=level, x=20, y=20)
    shooter = _npc(server, level, "s", 0, 0, "")
    seen = []

    async def shot(npc, source, player):
        seen.append(npc)
    server.npc_manager.on_npc_wasshot = shot
    server.combat_manager.spawn_script_arrow(level, shooter, 3, ARROW_NORMAL, 18.0, 20.0)
    await _fly(server, 6)
    assert len(seen) == MAX_EVENT_NPC_PROBES


@aio
async def test_a_blast_at_a_non_finite_position_is_ignored():
    server, level = make_server_and_level()
    target = _npc(server, level, "t", 10, 10)
    for x in (float("nan"), float("inf")):
        await _blast(server, level, x, 10, 255)
    assert "n" not in target.gs1_scopes["this"]


@aio
async def test_fire_spy_packets_are_rate_limited_per_player():
    from pygserver.combat import MAX_FIRESPY_PER_SECOND
    server, level = make_server_and_level()
    clock = _Clock()
    server.combat_manager._clock = clock
    player = _spy_player(server, level, 10, 10, 0)
    target = _npc(server, level, "t", 11, 6)
    for _ in range(MAX_FIRESPY_PER_SECOND + 5):
        await server.combat_manager.handle_fire_spy(player, (1 << 3) | 2)
    assert len(server.broadcasts) == MAX_FIRESPY_PER_SECOND
    assert _count(target) == float(MAX_FIRESPY_PER_SECOND)
    clock.now += 1.01
    await server.combat_manager.handle_fire_spy(player, (1 << 3) | 2)
    assert len(server.broadcasts) == MAX_FIRESPY_PER_SECOND + 1
    other = _spy_player(server, level, 10, 10, 0)
    other.id = 2
    await server.combat_manager.handle_fire_spy(other, (1 << 3) | 2)
    assert len(server.broadcasts) == MAX_FIRESPY_PER_SECOND + 2  # own budget


@aio
async def test_fire_spy_raises_the_event_but_leaves_npc_hearts_alone():
    # hurtAndPush takes hearts only from a character NPC that enabled damage
    # reactions (NPC.cpp:647,661; NPC.h:454 default off); pygserver has no such
    # switch, so a spy fire never lowers hearts.
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)
    target = _npc(server, level, "t", 11, 6)
    await server.combat_manager.handle_fire_spy(player, (1 << 3) | 3)
    assert _count(target) == 1.0
    assert target.hearts == 3.0


# -- review round 2: the layers that other guards used to mask -------------------

@aio
async def test_a_script_spyfire_flood_loses_packet_and_effect_together():
    server, level = make_server_and_level()
    manager = server.combat_manager
    manager._clock = _Clock()
    player = _spy_player(server, level, 10, 10, 0)
    target = _npc(server, level, "t", 11, 6)
    caster = server.npc_manager.create_npc(name="c", level=level, x=40, y=40)
    server.npc_manager.attach_gs1(caster, "if (created) { spyfire 1,2; }")
    for _ in range(40):
        await manager.blast_level_objects(level, 5, 5, 1, 1, script=True)
    server.broadcasts.clear()
    run_npc_event(caster, "created", server, player)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not [b for b in server.broadcasts if b[1][0] == PLO.FIRESPY + 32]
    assert "n" not in target.gs1_scopes["this"]


@aio
async def test_spyfire_command_ignores_non_finite_arguments():
    from pygserver.gs1.commands import objects
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)
    me, ctx = _shoot_ctx(server, level, None)
    ctx.player = player
    for args in ([float("nan"), 1], [1, float("inf")]):
        objects._c_spyfire(me, args, None, player, ctx)  # must not raise
    await asyncio.sleep(0)
    assert server.broadcasts == []


@aio
async def test_a_stale_grid_entry_for_an_npc_that_changed_level_is_skipped():
    from pygserver.level import Level
    server, level = make_server_and_level()
    victim = _npc(server, level, "victim", 20, 20, "")
    victim.shape = (32, 32)
    cm = server.combat_manager
    grid = cm._collision_grid(level)
    assert cm._grid_hits(grid, (320, 320, 32, 0)) == [victim]
    victim.level = Level("elsewhere.nw")
    assert cm._grid_hits(grid, (320, 320, 32, 0)) == []


@aio
async def test_fire_spy_needs_the_npc_server_too():
    from types import SimpleNamespace
    server, level = make_server_and_level()
    player = _spy_player(server, level, 10, 10, 0)
    target = _npc(server, level, "t", 11, 6)
    server.config = SimpleNamespace(serverside=False)
    await server.combat_manager.handle_fire_spy(player, (1 << 3) | 2)
    assert "n" not in target.gs1_scopes["this"]


@aio
async def test_the_manager_itself_refuses_script_arrows_without_the_npc_server():
    from types import SimpleNamespace
    server, level = make_server_and_level()
    shooter = _npc(server, level, "s", 10, 10, "")
    server.config = SimpleNamespace(serverside=False)
    assert server.combat_manager.spawn_script_arrow(
        level, shooter, 3, ARROW_NORMAL, 10, 10) is None


@aio
async def test_an_ordinary_timed_bomb_reaches_only_the_radius_one_arm():
    from pygserver.combat import Bomb
    server, level = make_server_and_level()
    inside = _npc(server, level, "inside", 10, 13)     # within 2 tiles of the arm end
    outside = _npc(server, level, "outside", 10, 15.5)  # past it; radius 2 would reach
    bomb = Bomb(id=1, player_id=0, level_name=level.name, x=10, y=10,
                power=1, time_left=0)
    server.combat_manager._bombs[level.name] = {1: bomb}
    await server.combat_manager._tick()
    assert _count(inside) == 1.0
    assert "n" not in outside.gs1_scopes["this"]
