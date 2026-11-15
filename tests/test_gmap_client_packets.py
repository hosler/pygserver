"""Client packets that carry a position, sent from a gmap segment.

A client sends the trigger and baddy positions local to its own segment and
unwrapped, so a value of 64 or more names the next segment. The reference loads a gmap as one
Level and resolves them against the whole map:

* PLI_TRIGGERACTION: segment origin + local (PlayerClientPackets.cpp:1059),
  delivered to the NPCs at that point (:1315), relayed to the sender's cell
  (:1311);
* PLI_BADDYADD: wrapped into the level, mod 64 tiles
  (include/utilities/Extents.h:432-437, PlayerClientPackets.cpp:565);
* PLI_PUTNPC: the reference server reads the position as whole-map tiles
  (the cell is the raw value / 64, Server.cpp:1485-1498); the reference
  client has no sender for this packet. The segment holding the point gets
  the NPC, and only a loaded one counts.

Each test has the sender in a non-origin cell and a target across a seam.
"""
from pathlib import Path

import pytest

from pygserver.gs1_host import compile_gs1
from pygserver.handlers import entities
from pygserver.protocol.codec import PacketBuilder
from pygserver.protocol.constants import PLO
from tests.test_gmap_npc_range import gmap_world, new_npc, npc_props
from tests.test_gs1_wholemap import cell, level, player_on, wire
from tests.test_level_entry_cache import run


def trigger_payload(x, y, action):
    return (PacketBuilder().write_gint3(0).write_gchar(int(x * 2))
            .write_gchar(int(y * 2)).write_string(action).build())


def probe(server, level_name, x, y):
    """A shapeless NPC (32x32 event box at x, y) that records its trigger."""
    npc = new_npc(server, level_name, x, y)
    npc.gs1_program = compile_gs1("if (actionzzzprobe) { this.hit = this.hit + 1; }")
    return npc


def hits(npc):
    return (getattr(npc, "gs1_scopes", None) or {}).get("this", {}).get("hit", 0)


# -- PLI_TRIGGERACTION ---------------------------------------------------------------

def test_client_trigger_past_the_seam_reaches_the_npc_in_the_next_segment(tmp_path):
    """Sender in (1,1) aims at local 66: that is local 2 of (2,1)."""
    server = gmap_world(tmp_path)
    sender = player_on(server, 1, 1, px=62.0, py=30.0, player_id=2)
    there = probe(server, cell(2, 1), 2.0, 30.0)
    wrapped = probe(server, cell(1, 1), 2.0, 30.0)       # the mod-64 spot
    run(sender._handle_trigger_action(trigger_payload(66.5, 30.5, "zzzprobe,1")))
    assert hits(there) == 1
    assert hits(wrapped) == 0


def test_client_trigger_inside_its_own_segment_is_unchanged(tmp_path):
    server = gmap_world(tmp_path)
    sender = player_on(server, 1, 1, player_id=2)
    here = probe(server, cell(1, 1), 10.0, 10.0)
    other = probe(server, cell(2, 1), 10.0, 10.0)
    run(sender._handle_trigger_action(trigger_payload(10.5, 10.5, "zzzprobe")))
    assert (hits(here), hits(other)) == (1, 0)


def test_client_trigger_is_relayed_to_the_senders_cell_only(tmp_path):
    """sendPacketToOneLevelPart(getGlobalPosition()) (:1311): the sender's
    cell, sender excluded; a player in the target's cell does not get it."""
    server = gmap_world(tmp_path)
    sender = player_on(server, 1, 1, px=62.0, player_id=2)
    mate = player_on(server, 1, 1, px=20.0, player_id=3)
    target_side = player_on(server, 2, 1, px=2.0, player_id=4)
    run(sender._handle_trigger_action(trigger_payload(66.5, 30.5, "zzzprobe,1")))
    got = wire(mate, PLO.TRIGGERACTION)
    assert len(got) == 1
    # {GSHORT sender}{GINT3 npc 0}{x*2}{y*2}{action}: the raw local values.
    assert got[0][:2] == PacketBuilder().write_gshort(2).build()
    assert (got[0][5] - 32, got[0][6] - 32) == (133, 61)
    assert got[0][7:] == b"zzzprobe,1"
    assert not wire(target_side, PLO.TRIGGERACTION)
    assert not wire(sender, PLO.TRIGGERACTION)


def test_script_trigger_hits_an_npc_whose_box_crosses_the_seam(tmp_path):
    """A GS1 triggeraction at whole-map 128.5 lands in (2,1) at local 0.5;
    an NPC of (1,1) at local 63.5 covers it (its box runs to 65.5)."""
    server = gmap_world(tmp_path)
    covering = probe(server, cell(1, 1), 63.5, 20.0)
    from tests.test_gs1_wholemap import script
    caller = new_npc(server, cell(0, 0))
    script(server, caller, "triggeraction 128.5,84.5,zzzprobe,1;")
    assert hits(covering) == 1


def test_client_trigger_on_a_plain_level_is_unchanged(tmp_path):
    server = gmap_world(tmp_path)
    sender = player_on(server, 0, 0, player_id=2)
    run(sender.warp("a.nw", 30.0, 30.0))
    here = probe(server, "a.nw", 10.0, 10.0)
    run(sender._handle_trigger_action(trigger_payload(10.5, 10.5, "zzzprobe")))
    assert hits(here) == 1


# -- PLI_BADDYADD --------------------------------------------------------------------

def baddy_payload(x, y, kind=0, power=3, image=b""):
    return bytes([int(x * 2) + 32, int(y * 2) + 32, kind + 32, power + 32]) + image


@pytest.mark.parametrize("wire_xy,local", [((66.5, 30.0), (2.5, 30.0)),
                                           ((10.0, 100.0), (10.0, 36.0)),
                                           ((63.5, 63.5), (63.5, 63.5))])
def test_client_baddy_wraps_into_the_senders_own_segment(tmp_path, wire_xy, local):
    server = gmap_world(tmp_path)
    sender = player_on(server, 1, 1, px=62.0, player_id=2)
    mate = player_on(server, 1, 1, px=20.0, player_id=3)
    neighbour = player_on(server, 2, 1, player_id=4)
    run(sender._handle_baddy_add(baddy_payload(*wire_xy)))
    made = server.baddy_manager.get_baddies_on_level(cell(1, 1))
    assert [(b.x, b.y) for b in made] == [local]
    assert not server.baddy_manager.get_baddies_on_level(cell(2, 1))
    assert not server.baddy_manager.get_baddies_on_level(cell(1, 2))
    # Sent to the sender's cell (sendPacketToOneLevelPart, :569).
    assert wire(mate, PLO.BADDYPROPS) and wire(sender, PLO.BADDYPROPS)
    assert not wire(neighbour, PLO.BADDYPROPS)


# -- PLI_PUTNPC ----------------------------------------------------------------------

def putnpc_payload(x, y, image=b"guard.png", script=b"guard.txt"):
    return (bytes([len(image) + 32]) + image + bytes([len(script) + 32]) + script
            + bytes([int(x * 2) + 32, int(y * 2) + 32]))


@pytest.fixture
def putnpc_world(tmp_path, monkeypatch):
    server = gmap_world(tmp_path, serverside=False, putnpc_enabled=True)
    code = tmp_path / "guard.txt"
    code.write_text("if (created) { this.made = 1; }")
    server.filesystem = object()
    monkeypatch.setattr(entities, "find_putnpc_script",
                        lambda _fs, name: Path(code) if name == "guard.txt" else None)
    return server


def created(server):
    return [n for n in server.npc_manager._npcs.values() if n.image == "guard.png"]


def test_client_putnpc_whole_map_point_in_the_senders_own_cell(putnpc_world):
    """Sender in (1,1), wire (94, 94): whole-map tile, cell (1,1) local (30,30)."""
    server = putnpc_world
    sender = player_on(server, 1, 1, player_id=2)
    near = player_on(server, 0, 1, player_id=3)
    run(sender._handle_putnpc(putnpc_payload(94.0, 94.0)))
    made = created(server)
    assert len(made) == 1
    assert made[0].level is level(server, 1, 1)
    assert (made[0].x, made[0].y) == (30.0, 30.0)
    assert made[0].id in npc_props(sender) and made[0].id in npc_props(near)


def test_client_putnpc_whole_map_point_in_another_cell(putnpc_world):
    """Sender in (1,1), wire (70, 10): cell (1,0) local (6,10), sent around
    the NPC."""
    server = putnpc_world
    sender = player_on(server, 1, 1, player_id=2)
    far = player_on(server, 2, 1, player_id=3)
    outside = player_on(server, 0, 2, player_id=4)
    run(sender._handle_putnpc(putnpc_payload(70.0, 10.5)))
    made = created(server)
    assert len(made) == 1
    assert made[0].level is level(server, 1, 0)
    assert (made[0].x, made[0].y) == (6.0, 10.5)
    assert made[0].id in npc_props(sender) and made[0].id in npc_props(far)
    assert made[0].id not in npc_props(outside)
    assert not level(server, 1, 1).get_npcs()


def test_client_putnpc_on_a_hole_creates_nothing(putnpc_world):
    server = putnpc_world
    sender = player_on(server, 1, 1, player_id=2)
    gmap = server.world.get_gmap_for_level(cell(1, 1))[0]
    del gmap.grid[(1, 0)]
    run(sender._handle_putnpc(putnpc_payload(70.0, 10.0)))
    assert created(server) == []


def test_client_putnpc_never_loads_a_segment(putnpc_world):
    """A segment nobody has loaded is absent: nothing is created and the
    level is not read from disk."""
    server = putnpc_world
    sender = player_on(server, 1, 1, player_id=2)
    manager = server.world._level_manager
    manager._levels.pop(cell(1, 0))
    run(sender._handle_putnpc(putnpc_payload(70.0, 10.0)))
    assert created(server) == []
    assert cell(1, 0) not in manager._levels


def test_client_trigger_next_to_an_unloaded_neighbour_does_not_load_it(tmp_path):
    server = gmap_world(tmp_path)
    sender = player_on(server, 1, 1, px=62.0, py=30.0, player_id=2)
    manager = server.world._level_manager
    for name in (cell(2, 1), cell(2, 2), cell(0, 0)):
        manager._levels.pop(name)
    here = probe(server, cell(1, 1), 62.0, 30.0)
    run(sender._handle_trigger_action(trigger_payload(62.5, 30.5, "zzzprobe")))
    assert hits(here) == 1
    assert not {cell(2, 1), cell(2, 2), cell(0, 0)} & set(manager._levels)


def test_client_putnpc_on_a_plain_level_is_unchanged(putnpc_world):
    server = putnpc_world
    sender = player_on(server, 0, 0, player_id=2)
    run(sender.warp("a.nw", 30.0, 30.0))
    run(sender._handle_putnpc(putnpc_payload(20.0, 21.5)))
    made = created(server)
    assert len(made) == 1 and made[0].level.name == "a.nw"
    assert (made[0].x, made[0].y) == (20.0, 21.5)
