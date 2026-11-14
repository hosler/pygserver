"""Who the player-combat and effect relays reach on a gmap, against the
reference server (GServer-v2 beta4).

Three observers around a sender in cell (0, 0) of a 3x3 gmap: one in the same
segment, one in the diagonal neighbour (inside the 3x3) and one two cells away
(outside it). The senders that use Server::sendPacketToNearby reach the first
two; the senders that use sendPacketToOneLevelPart reach only the first and are
pinned here too, because they were the ones the review note wrongly called 3x3.
A plain level keeps its level audience.
"""

import asyncio

import pytest

from pygserver.combat import _BLAST_BLOCKS, CombatManager
from pygserver.protocol.constants import PLPROP
from pygserver.protocol.packets import (
    PacketBuilder, build_arrow_add, build_board_modify2, build_bomb_add,
    build_bomb_del, build_hit_objects, build_other_player_props,
    build_show_img_player)
from pygserver.trigger_commands import dispatch_trigger_command

from tests.test_gmap_npc_range import gmap_world
from tests.test_level_entry_cache import loaded_player, run


SENDER, SAME, NEIGHBOUR, FAR = 2, 3, 4, 5


@pytest.fixture
def clock(monkeypatch):
    from pygserver import level_cache
    from tests.test_level_entry_cache import FakeClock
    monkeypatch.setattr(level_cache, "clock", FakeClock())


def arena(tmp_path, cells):
    """gmap_world with players placed by {id: level name}."""
    server = gmap_world(tmp_path)
    server.combat_manager = CombatManager(server)
    players = {}
    for player_id, name in cells.items():
        players[player_id] = loaded_player(server, player_id)
        run(players[player_id].warp(name, 30, 30))
    for player in players.values():
        player.send_raw.reset_mock()
    return server, players


def gmap_arena(tmp_path):
    return arena(tmp_path, {SENDER: "c00.nw", SAME: "c00.nw",
                            NEIGHBOUR: "c11.nw", FAR: "c22.nw"})


def received(player):
    return b"".join(call.args[0] for call in player.send_raw.await_args_list)


def audience(players):
    """The player ids that were sent anything."""
    return {player_id for player_id, player in players.items() if received(player)}


def shoot2_bytes():
    builder = PacketBuilder()
    for value in (300, 400, 500):
        builder.write_gshort(value)
    builder.write_gchar(0).write_gchar(0)
    builder.write_gchar(10).write_gchar(20).write_gchar(30)
    builder.write_gchar(8).write_gshort(4).write_bytes(b"ball")
    builder.write_gchar(0)
    return builder.build()


@pytest.mark.parametrize("npc_id,expected", [
    (None, b'N \x22\x22\x34\x38\n'),
    (257, b'N  \x22\x34\x38 \x22!\n'),
    (0, b'N  \x22\x34\x38   \n'),
])
def test_hit_objects_reach_the_3x3_and_not_the_sender(tmp_path, clock, npc_id, expected):
    """msgPLI_HITOBJECTS: sendPacketToNearby, sender excluded
    (PlayerClientPackets.cpp:1049)."""
    server, players = gmap_arena(tmp_path)
    run(server.combat_manager.handle_hit_objects(
        players[SENDER], 10.0, 12.0, 1.0, npc_id))
    assert audience(players) == {SAME, NEIGHBOUR}
    assert received(players[SAME]) == expected
    assert received(players[NEIGHBOUR]) == expected


def test_shoot_reaches_the_3x3_in_each_clients_layout(tmp_path, clock):
    """msgPLI_SHOOT2: sendPacketToNearby per layout, sender excluded, a client
    below 5.07 gets PLO_SHOOT (PlayerClientPackets.cpp:1401-1402)."""
    server, players = gmap_arena(tmp_path)
    players[SAME].client_version = 2.22
    run(server.combat_manager.handle_shoot2(players[SENDER], shoot2_bytes()))
    assert audience(players) == {SAME, NEIGHBOUR}
    modern, classic = received(players[NEIGHBOUR]), received(players[SAME])
    assert modern[0] - 32 == 191 and classic[0] - 32 == 175
    assert modern[1:3] == classic[1:3] == bytes([32, 32 + SENDER])


def test_the_classic_shoot_reaches_the_3x3_too(tmp_path, clock):
    """msgPLI_SHOOT (PlayerClientPackets.cpp:1368-1369)."""
    server, players = gmap_arena(tmp_path)
    classic = PacketBuilder().write_gint3(0).write_gchar(4).write_gchar(5)
    classic.write_gchar(55).write_gchar(10).write_gchar(20).write_gchar(30)
    classic.write_gchar(4).write_bytes(b"ball").write_gchar(0)
    run(server.combat_manager.handle_shoot(players[SENDER], classic.build()))
    assert audience(players) == {SAME, NEIGHBOUR}


def test_show_image_relay_is_the_player_packet_for_the_3x3(tmp_path, clock):
    """msgPLI_SHOWIMGPLAYER: the client's bytes behind the sender id,
    sendPacketToNearby, sender excluded (PlayerClientPackets.cpp:814-818)."""
    server, players = gmap_arena(tmp_path)
    run(players[SENDER]._handle_show_img(b"\x21abc"))
    expected = build_show_img_player(SENDER, b"\x21abc")
    assert audience(players) == {SAME, NEIGHBOUR}
    assert received(players[SAME]) == received(players[NEIGHBOUR]) == expected
    assert expected == bytes([32 + 32]) + bytes([32, 32 + SENDER]) + b"\x21abc\n"


def test_gr_attr_reaches_the_3x3(tmp_path, clock):
    """gr.attr sets the prop through the property path, whose shared-local
    props go to the players around the player (PlayerClientPackets.cpp:1242-1257,
    PlayerProps.cpp:1244)."""
    server, players = gmap_arena(tmp_path)
    server.config.triggerhack_props = True
    assert run(dispatch_trigger_command(server, players[SENDER], "gr.attr,1,hat"))
    expected = build_other_player_props(SENDER, {PLPROP.GATTRIB1: "hat"})
    assert audience(players) == {SENDER, SAME, NEIGHBOUR}
    assert received(players[SAME]) == received(players[NEIGHBOUR]) == expected


def test_a_script_hit_objects_reaches_the_3x3_and_excludes_nobody(tmp_path, clock):
    """Server::hitObjectsAtPoint for an NPC source: sendPacketToNearby, no
    exclusion (Server.cpp:1952-1956)."""
    from types import SimpleNamespace
    from pygserver.gs1.commands.combat import _c_hitobjects
    server, players = gmap_arena(tmp_path)
    level = server.world.get_level("c00.nw")

    async def go():
        host = SimpleNamespace(server=server, _level_of=lambda ctx: level)
        _c_hitobjects(host, [1, 10, 12], SimpleNamespace(id=7), None, None)
        await asyncio.sleep(0)
    run(go())
    expected = build_hit_objects(0, 2, 10, 12, npc_id=7)
    assert audience(players) == {SENDER, SAME, NEIGHBOUR}
    assert all(received(players[pid]) == expected for pid in (SENDER, SAME, NEIGHBOUR))


def test_a_blast_board_change_without_a_manager_reaches_the_3x3(tmp_path, clock):
    """LevelBoardChange::sendToPlayersOnLevel on a gmap: PLO_BOARDMODIFY2 to
    the 3x3 (LevelBoardChange.cpp:52-71). The fallback path is the one a server
    with no board-change manager takes."""
    server, players = gmap_arena(tmp_path)
    server.board_changes = None
    level = server.world.get_level("c00.nw")
    original, replacement = _BLAST_BLOCKS[0]
    for (x, y), tile in zip(((10, 10), (10, 11), (11, 10), (11, 11)), original):
        level.set_tile(x, y, tile)
    run(server.combat_manager._destroy_blast_blocks(level, [(10, 10)]))
    tiles = PacketBuilder()
    for tile in (replacement[0], replacement[2], replacement[1], replacement[3]):
        tiles.write_gshort(tile)
    expected = build_board_modify2(0, 0, 10, 10, 2, 2, tiles.build())
    assert audience(players) == {SENDER, SAME, NEIGHBOUR}
    assert received(players[SAME]) == received(players[NEIGHBOUR]) == expected


# -- the senders the reference keeps on one segment --------------------------------------

def test_bomb_and_arrow_relays_stay_on_the_senders_segment(tmp_path, clock):
    """msgPLI_BOMBADD, msgPLI_BOMBDEL and msgPLI_ARROWADD use
    sendPacketToOneLevelPart, which is the players of the sender's own segment
    (PlayerClientPackets.cpp:237, :248, :301; Level.cpp:3384-3394)."""
    server, players = gmap_arena(tmp_path)
    sender = players[SENDER]
    sender.bombs = sender.arrows = 5
    manager = server.combat_manager
    run(manager.handle_bomb_add(sender, 10.0, 12.0, 1, timer_ticks=59))
    run(manager.handle_bomb_del(sender, 10.0, 12.0))
    run(manager.handle_arrow_add(sender, 10.0, 12.0, 1))
    # The sender is also sent its own bomb and arrow counts.
    assert audience(players) - {SENDER} == {SAME}
    assert received(players[SAME]) == (
        build_bomb_add(SENDER, 10.0, 12.0, 1, 59) + build_bomb_del(10.0, 12.0)
        + build_arrow_add(SENDER, 10.0, 12.0, 1, 0, 1))


# -- a plain level keeps its level audience -----------------------------------------------

def test_plain_level_audience_is_the_level(tmp_path, clock):
    server, players = arena(tmp_path, {SENDER: "a.nw", SAME: "a.nw", FAR: "b.nw"})
    server.config.triggerhack_props = True
    manager = server.combat_manager
    run(manager.handle_hit_objects(players[SENDER], 10.0, 12.0, 1.0))
    assert audience(players) == {SAME}
    run(manager.handle_shoot2(players[SENDER], shoot2_bytes()))
    run(players[SENDER]._handle_show_img(b"\x21abc"))
    run(dispatch_trigger_command(server, players[SENDER], "gr.attr,1,hat"))
    assert audience(players) == {SENDER, SAME}
    hit = build_hit_objects(SENDER, 2, 10.0, 12.0, None)
    assert received(players[SAME]).startswith(hit)
    assert received(players[SAME]).endswith(
        build_show_img_player(SENDER, b"\x21abc")
        + build_other_player_props(SENDER, {PLPROP.GATTRIB1: "hat"}))
