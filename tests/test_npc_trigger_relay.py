"""NPC and player triggeractions share point containment and parameter delivery."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pygserver.config import ServerConfig
from pygserver.gs1_host import run_npc_event
from pygserver.level import Level
from pygserver.npc import NPC
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import PacketReader, build_trigger_action
from pygserver.server import GameServer


def make_world(tmp_path):
    server = GameServer(ServerConfig(base_dir=str(tmp_path)))
    level = Level("room.nw")
    server.world.add_level(level)
    players = []
    for pid in (2, 3):
        player = SimpleNamespace(id=pid, level=level, flags={}, logged_in=True,
                                 send_raw=AsyncMock())
        level.add_player(player)
        server.players[pid] = player
        players.append(player)
    return server, level, players


def add_npc(server, level, npc_id, x, y, code, fire_created=True):
    npc = NPC(npc_id)
    npc.x, npc.y, npc.shape = x, y, (32, 32)
    level.add_npc(npc)
    server.npc_manager._npcs[npc_id] = npc
    server.npc_manager.attach_gs1(npc, code, fire_created=fire_created, raise_errors=True)
    return npc


def trigger_packets(player):
    return [call.args[0] for call in player.send_raw.await_args_list
            if call.args[0][0] - 32 == PLO.TRIGGERACTION]


@pytest.mark.parametrize("event", ["created", "timeout"])
def test_npc_trigger_dispatches_and_relays_without_player_context(tmp_path, event):
    async def run():
        server, level, players = make_world(tmp_path)
        target = add_npc(server, level, 10002, 9, 9,
                         "if (actionping) { setstring server.got,#p(0);"
                         "this.indexed=params[0] @ \"\"; }")
        off_point = add_npc(server, level, 10003, 20, 20,
                            "if (actionping) set this.hit;")
        other_level = Level("other.nw")
        server.world.add_level(other_level)
        elsewhere = add_npc(server, other_level, 10004, 9, 9,
                            "if (actionping) set this.hit;")
        source = add_npc(server, level, 10001, 1, 1,
                         "if (" + event + ") triggeraction 10,10,ping,hello;",
                         fire_created=event == "created")
        if event == "timeout":
            run_npc_event(source, event, server, None)
        await asyncio.sleep(0)
        await server.npc_manager.tick()
        assert server.get_flag("server.got") == "hello"
        assert target.gs1_scopes["this"]["indexed"] == "hello"
        assert "hit" not in off_point.gs1_scopes["this"]
        assert "hit" not in elsewhere.gs1_scopes["this"]
        for player in players:
            packet, = trigger_packets(player)
            reader = PacketReader(packet[1:-1])
            assert reader.read_gshort() == 0
            assert reader.read_gint3() == source.id
            assert (reader.read_gchar() / 2, reader.read_gchar() / 2) == (10, 10)
            assert reader.remaining() == b"ping,hello"
    asyncio.run(run())


@pytest.mark.parametrize("contains_source", [True, False])
def test_source_is_excluded_but_overlapping_target_receives_trigger(tmp_path, contains_source):
    async def run():
        server, level, _ = make_world(tmp_path)
        target = add_npc(server, level, 10002, 9, 9,
                         "if (actionping) { this.hits++; setstring this.value,#p(0); }")
        source = add_npc(server, level, 10001, 9 if contains_source else 1, 9,
                         "if (created) triggeraction 10,10,ping,hello;"
                         "if (actionping) set this.hit;")
        await asyncio.sleep(0)
        assert "hit" not in source.gs1_scopes["this"]
        assert target.gs1_scopes["this"] == {"hits": 1.0, "value": "hello"}
    asyncio.run(run())


@pytest.mark.parametrize("with_player", [True, False])
def test_clientside_reaches_only_the_triggering_player(tmp_path, with_player):
    async def run():
        server, level, players = make_world(tmp_path)
        target = add_npc(server, level, 10002, 9, 9,
                         "if (actionclientside) set this.hit;")
        source = add_npc(server, level, 10001, 9, 9,
                         "if (timeout) triggeraction 10,10,clientside,echo,hello;")
        run_npc_event(source, "timeout", server, players[0] if with_player else None)
        await asyncio.sleep(0)
        assert "hit" not in target.gs1_scopes["this"]
        assert trigger_packets(players[0]) == (
            [build_trigger_action(0, 0, 0, 0, "clientside,echo,hello")] if with_player else [])
        assert trigger_packets(players[1]) == []
    asyncio.run(run())


def test_player_trigger_passes_csv_params_and_keeps_inclusive_containment(tmp_path):
    async def run():
        server, level, players = make_world(tmp_path)
        target = add_npc(server, level, 10002, 9, 9,
                         "if (actionping) { setstring this.first,#p(0);"
                         "this.second=params[1] @ \"\"; this.hits++; }")
        await server.npc_manager.on_trigger_action(
            players[0], 11, 11, 'ping,"hello,world",tail')
        await server.npc_manager.on_trigger_action(players[0], 11.01, 11, "ping,miss")
        assert target.gs1_scopes["this"] == {
            "first": "hello,world", "second": "tail", "hits": 1.0,
        }
        assert target._gs1_ctx.player is players[0]
    asyncio.run(run())


def test_npc_trigger_preserves_csv_params_and_player_context(tmp_path):
    async def run():
        server, level, players = make_world(tmp_path)
        target = add_npc(server, level, 10002, 9, 9,
                         "if (actionping) { setstring this.value,#p(0); }")
        source = add_npc(server, level, 10001, 1, 1,
                         'if (timeout) triggeraction(10,10,"ping","hello,world");')
        run_npc_event(source, "timeout", server, players[0])
        await asyncio.sleep(0)
        assert target.gs1_scopes["this"]["value"] == "hello,world"
        assert target._gs1_ctx.player is players[0]
        assert trigger_packets(players[0]) == [
            build_trigger_action(0, source.id, 10, 10, 'ping,"hello,world"')]
    asyncio.run(run())


def test_consumed_serverside_action_does_not_relay(tmp_path):
    async def run():
        server, level, players = make_world(tmp_path)
        source = add_npc(server, level, 10001, 1, 1,
                         "if (timeout) triggeraction 10,10,serverside,heal,1;")
        server.handle_trigger_action = AsyncMock(return_value=True)
        run_npc_event(source, "timeout", server, players[0])
        await asyncio.sleep(0)
        server.handle_trigger_action.assert_awaited_once_with(
            players[0], 10, 10, "serverside,heal,1")
        assert trigger_packets(players[0]) == []
    asyncio.run(run())


def test_named_npc_carrier_accepts_params_without_player_and_is_consumed(tmp_path):
    async def run():
        server, level, players = make_world(tmp_path)
        target = add_npc(server, level, 10002, 20, 20,
                         "if (actionserverside) setstring this.value,#p(0);")
        target.name = "echo"
        add_npc(server, level, 10001, 1, 1,
                "if (created) triggeraction 10,10,servernpc,echo,hello;")
        await asyncio.sleep(0)
        assert target.gs1_scopes["this"]["value"] == "hello"
        assert trigger_packets(players[0]) == []
    asyncio.run(run())
