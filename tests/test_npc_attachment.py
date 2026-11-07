"""Attachment relay, entry state and teardown without sockets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pygserver.gs1.builtins import _b_carriesnpc
from pygserver.attachments import absolute_position
from pygserver.level import Level
from pygserver.protocol.constants import PLPROP, PLO
from pygserver.protocol.packets import PacketReader, parse_player_props
from pygserver.server import GameServer
from .test_player_props_inbound import make_player, props_payload, relayed_props


def packet_props(packet):
    reader = PacketReader(packet)
    assert reader.read_gchar() == PLO.OTHERPLPROPS
    reader.read_gshort()
    return parse_player_props(reader.remaining()[:-1])


@pytest.mark.parametrize('npc_id', [9, 0])
def test_carrynpc_stored_relayed_and_in_entry_properties(npc_id):
    server, player = make_player()
    player.npc_id = 7
    asyncio.run(player._handle_player_props(props_payload([(PLPROP.CARRYNPC, npc_id)])))
    assert player.npc_id == npc_id
    assert relayed_props(server)[PLPROP.CARRYNPC] == npc_id
    assert packet_props(player.build_props_packet())[PLPROP.CARRYNPC] == npc_id
    assert _b_carriesnpc(None, 'carriesnpc', [], None, None, player) is bool(npc_id)
    assert server.broadcasts[-1][0] == player.level.name
    assert server.broadcasts[-1][2] == {player.id}


def test_attachnpc_stored_separately_and_relay_axes_are_relative():
    server, player = make_player()
    npc = SimpleNamespace(id=9, x=10, y=20)
    player.level._npcs[9] = npc
    asyncio.run(player._handle_player_props(props_payload([
        (PLPROP.ATTACHNPC, (0, 9)), (PLPROP.X2, -1.25), (PLPROP.Y2, 1.625)])))
    assert player.attach_npc == (0, 9)
    assert player.npc_id == 0
    assert (player.x, player.y) == (-1.25, 1.625)
    assert absolute_position(player) == (8.75, 21.625)
    for props in (relayed_props(server), packet_props(player.build_props_packet())):
        assert props[PLPROP.ATTACHNPC] == (0, 9)
        assert (props[PLPROP.X2], props[PLPROP.Y2]) == (-1.25, 1.625)
    npc.x, npc.y = 11, 22
    assert absolute_position(player) == (9.75, 23.625)
    asyncio.run(player._handle_player_props(props_payload([(PLPROP.Y2, -.5)])))
    assert relayed_props(server)[PLPROP.Y2] == -.5
    assert PLPROP.X2 not in relayed_props(server)
    assert absolute_position(player) == (9.75, 21.5)
    asyncio.run(player._handle_player_props(props_payload([
        (PLPROP.ATTACHNPC, (0, 0)), (PLPROP.X2, 8.75), (PLPROP.Y2, 19.5)])))
    assert player.attach_npc == (0, 0)
    assert relayed_props(server)[PLPROP.Y2] == 19.5
    assert absolute_position(player) == (8.75, 19.5)


@pytest.mark.parametrize('elsewhere', [False, True])
def test_missing_attachment_relays_verbatim_and_keeps_last_absolute_position(elsewhere):
    server, player = make_player()
    if elsewhere:
        other_level = Level('elsewhere.nw')
        npc = SimpleNamespace(id=99, x=50, y=50, level=other_level)
        other_level._npcs[99] = npc
        server.npc_manager.get_npc.return_value = npc
    player.x, player.y = 8.75, 21.625
    asyncio.run(player._handle_player_props(props_payload([
        (PLPROP.ATTACHNPC, (0, 99)), (PLPROP.X2, -1.25), (PLPROP.Y2, 1.625)])))
    assert player.attach_npc == (0, 0)
    assert absolute_position(player) == (8.75, 21.625)
    for props in (relayed_props(server), packet_props(player.build_props_packet())):
        assert props[PLPROP.ATTACHNPC] == (0, 99)
        assert (props[PLPROP.X2], props[PLPROP.Y2]) == (-1.25, 1.625)
    asyncio.run(player._handle_player_props(props_payload([(PLPROP.Y2, -2.5)])))
    assert relayed_props(server)[PLPROP.Y2] == -2.5
    assert absolute_position(player) == (8.75, 21.625)
    asyncio.run(player._handle_player_props(props_payload([
        (PLPROP.ATTACHNPC, (0, 0)), (PLPROP.X2, 5), (PLPROP.Y2, 6)])))
    assert absolute_position(player) == (5, 6)


def test_disappearing_npc_keeps_last_absolute_position():
    server, player = make_player()
    player.level._npcs[9] = SimpleNamespace(id=9, x=10, y=20)
    asyncio.run(player._handle_player_props(props_payload([
        (PLPROP.ATTACHNPC, (0, 9)), (PLPROP.X2, -1.25), (PLPROP.Y2, 1.625)])))
    del player.level._npcs[9]
    asyncio.run(player._handle_player_props(props_payload([(PLPROP.Y2, -2.5)])))
    assert player.attach_npc == (0, 0)
    assert absolute_position(player) == (8.75, 21.625)
    assert relayed_props(server)[PLPROP.Y2] == -2.5


@pytest.mark.parametrize('missing', [False, True])
def test_attached_gameplay_uses_absolute_position_for_scripts_touches_and_hits(missing):
    from pygserver.audience import Shape
    from pygserver.gs1_host import compile_gs1, run_npc_event
    from pygserver.npc import NPC, NPCManager

    server, player = make_player()
    player.x, player.y = 11, 21
    parent = NPC(9, 'parent')
    parent.x, parent.y = 10, 20
    if not missing:
        player.level.add_npc(parent)
    observer = NPC(10, 'observer')
    observer.x, observer.y = 11, 21
    observer.gs1_program = compile_gs1(
        'if (playertouchsme) { this.px = playerx; this.py = playery; }')
    player.level.add_npc(observer)
    asyncio.run(player._handle_player_props(props_payload([
        (PLPROP.ATTACHNPC, (0, 9)), (PLPROP.X2, 1), (PLPROP.Y2, 1)])))
    run_npc_event(observer, 'playertouchsme', server, player)
    assert observer.gs1_scopes['this']['px'] == 11
    assert observer.gs1_scopes['this']['py'] == 21
    manager = NPCManager(server)
    manager.on_player_touches = AsyncMock()
    asyncio.run(manager.check_touches(player))
    assert any(call.args == (player, observer)
               for call in manager.on_player_touches.await_args_list)
    assert server.audience.players_near(player.level.name, 11, 21, 1, Shape.BOX) == [player]
    assert server.audience.players_near(player.level.name, 1, 1, 1, Shape.BOX) == []


def test_unsupported_attach_type_is_ignored():
    server, player = make_player()
    asyncio.run(player._handle_player_props(props_payload([(PLPROP.ATTACHNPC, (1, 9))])))
    assert player.attach_npc == (0, 0)
    assert not server.broadcasts


def test_warp_clears_both_states_and_own_client():
    server, player = make_player()
    player.npc_id, player.attach_npc = 8, (0, 9)
    target = Level('next.nw')
    server.world.get_level.side_effect = lambda name: target
    server.npc_manager.on_player_leaves = AsyncMock()
    server.npc_manager.on_player_enters = AsyncMock()
    player._send_level = AsyncMock()
    asyncio.run(player.warp(target.name, 4, 5))
    assert player.npc_id == 0
    assert player.attach_npc == (0, 0)
    payload = player.send_raw.await_args_list[0].args[0]
    assert parse_player_props(payload[1:-1])[PLPROP.ATTACHNPC] == (0, 0)
    assert packet_props(player.build_props_packet())[PLPROP.ATTACHNPC] == (0, 0)


def test_failed_warp_keeps_attachment():
    server, player = make_player()
    player.level._npcs[9] = SimpleNamespace(id=9, x=10, y=20)
    player.npc_id, player.attach_npc = 8, (0, 9)
    player._send_level = AsyncMock()
    asyncio.run(player.warp('missing.nw', 4, 5))
    assert (player.npc_id, player.attach_npc) == (8, (0, 9))


def test_disconnect_clears_attachment_and_announces_leave():
    server, player = make_player()
    server.rc_manager = server.listserver = server.irc_manager = None
    player.npc_id, player.attach_npc = 8, (0, 9)
    asyncio.run(GameServer._remove_player(server, player))
    assert (player.npc_id, player.attach_npc) == (0, (0, 0))
    assert player.id not in server.players
    assert server.broadcasts[-1][1] == player.build_leave_packet()


def test_server_attached_builtin_names_the_action_npc():
    from pygserver.gs1.builtins import _b_attached
    _server, player = make_player()
    player.attach_npc = (0, 9)
    assert _b_attached(None, 'attached', [], None, SimpleNamespace(id=9), player)
    assert not _b_attached(None, 'attached', [], None, SimpleNamespace(id=8), player)
    assert not _b_attached(None, 'attached', [], None, None, player)
