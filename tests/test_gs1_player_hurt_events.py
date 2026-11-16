"""GS1 `playerhurt` / `playerdies` for server-side NPC scripts, through the
real packet handlers.

Reference: the client's HALFHEARTS prop dropping queues `playerhurt`
(GServer-v2/server/src/player/PlayerProps.cpp:242) and its STATUS prop turning
DEAD queues `playerdies` (:597). Both go through Server::queueNPCEvent
(include/Server.h:449-458): every NPC of a plain level, or the gmap's NPCs
within `eventdistance` tiles (default 64) of the player's whole-map position.
The running player is the hurt one.
"""

from pygserver.combat import CombatManager
from pygserver.gs1_host import compile_gs1
from pygserver.protocol.constants import PLI, PLPROP
from pygserver.protocol.packets import PacketBuilder
from reborn_protocol import PLSTATUS

from tests.test_gmap_npc_range import gmap_world, new_npc
from tests.test_level_entry_cache import loaded_player, run

HURT, DIES = 2, 3   # victim, attacker (ids)

SCRIPT = (
    "if (playerhurt) { this.hurt = this.hurt + 1; this.hp = playerhearts; }"
    "if (playerhurted) { this.alias = this.alias + 1; }"
    "if (playerdies) { this.died = this.died + 1; this.dhp = playerhearts; }"
)


def listener(server, level_name, x=12.0, y=14.0):
    npc = new_npc(server, level_name, x=x, y=y)
    npc.gs1_program = compile_gs1(SCRIPT)
    return npc


def seen(npc, name):
    return npc.gs1_scopes.get("this", {}).get(name, 0.0)


def arena(tmp_path, victim_level, attacker_level=None, **options):
    server = gmap_world(tmp_path, **options)
    server.combat_manager = CombatManager(server)
    victim = loaded_player(server, HURT)
    attacker = loaded_player(server, DIES)
    run(victim.warp(victim_level, 30, 30))
    run(attacker.warp(attacker_level or victim_level, 31, 30))
    return server, victim, attacker


def hurt_packet(target_id, power):
    return (PacketBuilder().write_gshort(target_id).write_gchar(0)
            .write_gchar(0).write_gchar(power).build())


def props(**by_id):
    builder = PacketBuilder()
    for key, value in by_id.items():
        builder.write_gchar(int(key.lstrip("p"))).write_gchar(value)
    return builder.build()


def curpower(player, halves):
    run(player._dispatch_packet(PLI.PLAYERPROPS, props(**{f"p{PLPROP.CURPOWER}": halves})))


def status(player, value):
    run(player._dispatch_packet(PLI.PLAYERPROPS, props(**{f"p{PLPROP.STATUS}": value})))


# -- playerhurt ------------------------------------------------------------------------

def test_hurt_packet_fires_playerhurt_once_on_the_level_with_the_victim_as_player(tmp_path):
    server, victim, attacker = arena(tmp_path, "a.nw")
    here, elsewhere = listener(server, "a.nw"), listener(server, "b.nw")
    run(attacker._dispatch_packet(PLI.HURTPLAYER, hurt_packet(HURT, 2)))
    assert seen(here, "hurt") == 1.0
    assert seen(here, "alias") == 1.0          # `playerhurted` spelling
    assert seen(here, "hp") == 2.0             # the victim's hearts, already reduced
    assert seen(here, "died") == 0.0
    assert seen(elsewhere, "hurt") == 0.0      # other level: not heard


def test_client_hearts_drop_fires_but_a_rise_or_repeat_does_not(tmp_path):
    server, victim, _ = arena(tmp_path, "a.nw")
    npc = listener(server, "a.nw")
    curpower(victim, 4)                        # 3 hearts -> 2
    assert seen(npc, "hurt") == 1.0 and seen(npc, "hp") == 2.0
    curpower(victim, 4)                        # unchanged: ignored (PlayerProps.cpp:228)
    curpower(victim, 6)                        # rise: nothing
    assert seen(npc, "hurt") == 1.0


def test_gs1_hurt_command_fires_playerhurt(tmp_path):
    server, victim, _ = arena(tmp_path, "a.nw")
    npc = listener(server, "a.nw")
    npc.gs1_program = compile_gs1(SCRIPT + "if (playerchats) { hurt 1; }")
    from pygserver.gs1_host import run_npc_event

    async def go():
        run_npc_event(npc, "playerchats", server, victim)
        import asyncio
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    run(go())
    assert seen(npc, "hurt") == 1.0 and seen(npc, "hp") == 2.5


# -- playerdies -----------------------------------------------------------------------

def test_status_dead_fires_playerdies_once(tmp_path):
    server, victim, _ = arena(tmp_path, "a.nw")
    here, elsewhere = listener(server, "a.nw"), listener(server, "b.nw")
    curpower(victim, 0)
    assert seen(here, "died") == 0.0           # hearts at 0 is not the trigger
    status(victim, PLSTATUS.DEAD)
    status(victim, PLSTATUS.DEAD)              # unchanged: ignored
    assert seen(here, "died") == 1.0 and seen(here, "dhp") == 0.0
    assert seen(elsewhere, "died") == 0.0


# -- gmap ------------------------------------------------------------------------------

def test_gmap_scope_is_eventdistance_tiles_across_segments(tmp_path):
    """Victim at whole-map (30, 30) in c00. c11 (10, 10) is (74, 74): 62.2
    tiles away, heard. c22 (10, 10) is (138, 138): out of range."""
    server, victim, attacker = arena(tmp_path, "c00.nw")
    same = listener(server, "c00.nw")
    near = listener(server, "c11.nw", x=10.0, y=10.0)
    far = listener(server, "c22.nw", x=10.0, y=10.0)
    plain = listener(server, "a.nw")
    run(attacker._dispatch_packet(PLI.HURTPLAYER, hurt_packet(HURT, 2)))
    assert [seen(n, "hurt") for n in (same, near, far, plain)] == [1.0, 1.0, 0.0, 0.0]
    status(victim, PLSTATUS.DEAD)
    assert [seen(n, "died") for n in (same, near, far, plain)] == [1.0, 1.0, 0.0, 0.0]


def test_eventdistance_option_narrows_the_gmap_scope(tmp_path):
    server, victim, _ = arena(tmp_path, "c00.nw", event_distance=10)
    same = listener(server, "c00.nw", x=34.0, y=30.0)
    near = listener(server, "c11.nw", x=10.0, y=10.0)
    curpower(victim, 2)
    assert (seen(same, "hurt"), seen(near, "hurt")) == (1.0, 0.0)


# -- script hearts writes and echoes ----------------------------------------------------

def run_script(server, npc, victim, code):
    import asyncio
    from pygserver.gs1_host import run_npc_event
    npc.gs1_program = compile_gs1(SCRIPT + f"if (playerchats) {{ {code} }}")

    async def go():
        run_npc_event(npc, "playerchats", server, victim)
        for _ in range(5):
            await asyncio.sleep(0)
    run(go())


def test_script_hearts_write_fires_on_a_drop_only(tmp_path):
    """The HALFHEARTS case ignores who set the prop (PlayerProps.cpp:228-242)."""
    server, victim, _ = arena(tmp_path, "a.nw")
    npc = listener(server, "a.nw")
    run_script(server, npc, victim, "playerhearts = playerhearts - 1;")
    assert seen(npc, "hurt") == 1.0 and seen(npc, "hp") == 2.0
    run_script(server, npc, victim, "playerhearts = playerhearts + 1;")
    run_script(server, npc, victim, "playerhearts = playerhearts;")
    assert seen(npc, "hurt") == 1.0


def test_victim_echo_of_the_same_hearts_does_not_fire_again(tmp_path):
    server, victim, attacker = arena(tmp_path, "a.nw")
    npc = listener(server, "a.nw")
    run(attacker._dispatch_packet(PLI.HURTPLAYER, hurt_packet(HURT, 2)))
    assert seen(npc, "hurt") == 1.0
    curpower(victim, 4)                        # the client's own lowered value, same as stored
    assert seen(npc, "hurt") == 1.0


def test_victim_echo_of_a_lower_value_is_a_second_drop(tmp_path):
    server, victim, attacker = arena(tmp_path, "a.nw")
    npc = listener(server, "a.nw")
    run(attacker._dispatch_packet(PLI.HURTPLAYER, hurt_packet(HURT, 2)))
    curpower(victim, 3)                        # client lost more than the server stored
    # A real further drop of the stored value: the reference queues one per drop.
    assert seen(npc, "hurt") == 2.0 and seen(npc, "hp") == 1.5
