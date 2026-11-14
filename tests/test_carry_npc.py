"""Carrying an NPC across levels and on a gmap, against the reference server
(GServer-v2 beta4).

- A client cannot start (or end) a carry on a gmap: the change is reverted and
  the old value goes back to the sender alone (PlayerProps.cpp:661-675).
- A level change releases the carry and leaves the NPC where it is (the
  reference moves it with the player; pygserver does not yet).
"""

from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import PacketBuilder

from tests.test_gmap_npc_range import gmap_world, new_npc
from tests.test_level_entry_cache import loaded_player, run, sent
from tests.test_gmap_npc_range import clock  # noqa: F401  (fixture)


def carry_payload(npc_id):
    return PacketBuilder().write_gchar(PLPROP.CARRYNPC).write_gint3(npc_id).build()


def received_ids(player):
    return [packet_id for packet_id, _body in sent(player)]


def test_a_carry_change_on_a_gmap_is_reverted_to_the_sender_only(tmp_path, clock):
    server = gmap_world(tmp_path)
    carrier = loaded_player(server, 2)
    other = loaded_player(server, 3)
    run(carrier.warp("c00.nw", 30, 30))
    run(other.warp("c00.nw", 30, 30))
    carrier.send_raw.reset_mock()
    other.send_raw.reset_mock()
    run(carrier._handle_player_props(carry_payload(300)))
    assert carrier.npc_id == 0
    assert b"".join(c.args[0] for c in carrier.send_raw.await_args_list) == (
        bytes([PLO.PLAYERPROPS + 32, PLPROP.CARRYNPC + 32, 32, 32, 32]) + b"\n")
    other.send_raw.assert_not_awaited()


def test_an_unchanged_carry_value_on_a_gmap_is_ignored(tmp_path, clock):
    server = gmap_world(tmp_path)
    carrier = loaded_player(server, 2)
    run(carrier.warp("c00.nw", 30, 30))
    carrier.send_raw.reset_mock()
    run(carrier._handle_player_props(carry_payload(0)))
    carrier.send_raw.assert_not_awaited()


def test_a_carry_change_off_a_gmap_is_stored_and_relayed(tmp_path, clock):
    server = gmap_world(tmp_path)
    carrier = loaded_player(server, 2)
    other = loaded_player(server, 3)
    run(carrier.warp("a.nw", 30, 30))
    run(other.warp("a.nw", 30, 30))
    other.send_raw.reset_mock()
    run(carrier._handle_player_props(carry_payload(300)))
    assert carrier.npc_id == 300
    assert b"".join(c.args[0] for c in other.send_raw.await_args_list) == (
        bytes([PLO.OTHERPLPROPS + 32, 32, 32 + 2, PLPROP.CARRYNPC + 32,
               32, 32 + 2, 32 + 44]) + b"\n")


def carry_across(tmp_path, destination):
    """A carrier on a.nw holding an NPC, a watcher on a.nw, a watcher on the
    destination and a bystander on b.nw (unless that is the destination)."""
    server = gmap_world(tmp_path)
    npc = new_npc(server, "a.nw")
    carrier = loaded_player(server, 2)
    old_watcher = loaded_player(server, 3)
    new_watcher = loaded_player(server, 4)
    bystander = loaded_player(server, 5)
    run(carrier.warp("a.nw", 30, 30))
    run(old_watcher.warp("a.nw", 30, 30))
    run(new_watcher.warp(destination, 30, 30))
    run(bystander.warp("b.nw", 30, 30))
    carrier.npc_id = npc.id
    for player in (carrier, old_watcher, new_watcher, bystander):
        player.send_raw.reset_mock()
    run(carrier.warp(destination, 20, 20))
    return server, npc, carrier, old_watcher, new_watcher, bystander


def test_a_warp_releases_the_carry_and_leaves_the_npc_on_its_level(tmp_path, clock):
    server, npc, carrier, old_watcher, new_watcher, bystander = carry_across(
        tmp_path, "c11.nw")
    assert carrier.npc_id == 0
    assert npc.level is server.world.get_level("a.nw")
    assert npc in server.world.get_level("a.nw").get_npcs()
    assert npc not in server.world.get_level("c11.nw").get_npcs()
    for player in (old_watcher, new_watcher, bystander):
        assert PLO.NPCDEL not in received_ids(player)


def test_a_carrier_warp_does_not_remove_an_npc_of_another_level(tmp_path, clock):
    """CARRYNPC can name any NPC off a gmap; the warp must not take it off
    its own level's list (the players there would keep a ghost)."""
    server = gmap_world(tmp_path)
    foreign = new_npc(server, "b.nw")
    carrier = loaded_player(server, 2)
    watcher = loaded_player(server, 3)
    run(carrier.warp("a.nw", 30, 30))
    run(watcher.warp("b.nw", 30, 30))
    run(carrier._handle_player_props(carry_payload(foreign.id)))
    assert carrier.npc_id == foreign.id
    watcher.send_raw.reset_mock()
    run(carrier.warp("c11.nw", 20, 20))
    assert foreign in server.world.get_level("b.nw").get_npcs()
    assert foreign.level is server.world.get_level("b.nw")
    assert PLO.NPCDEL not in received_ids(watcher)
