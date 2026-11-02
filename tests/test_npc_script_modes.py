"""GS1 script modes and client script property transport."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from reborn_protocol.props import NPC_PROPS, Wire, encode_value

from pygserver.config import ServerConfig
from pygserver.level import Level
from pygserver.nc import NCManager, NCSession
from pygserver.npc import NPC, NPCManager, split_gs1_script
from pygserver.protocol.constants import NPCPROP
from pygserver.protocol.packets import PacketBuilder
from pygserver.server import GameServer
from .test_npc_prop_stream import parse_as_client


@pytest.mark.parametrize("source, server, client", [
    (" \nserver;\n//#CLIENTSIDE\nclient;\nnext;\n ",
     "server;", "client;\nnext;"),
    ("server;\n//#CLIENTSIDE trailing text\nclient;", "server;", "client;"),
    ("server;\n//#CLIENTSIDE trailing text", "server;", ""),
    ("server;\n//#CLIENTSIDE\nclient;\n//#CLIENTSIDE\nmore;",
     "server;", "client;\n//#CLIENTSIDE\nmore;"),
    (" \nserver;\nnext;\n", "server;\nnext;", ""),
    ("//#CLIENTSIDE", "", ""),
    ("//#CLIENTSIDE\n", "", ""),
    (" \t\n", "", ""),
    ("\xa7server;\xa7", "server;", ""),
])
def test_split_with_npc_server(source, server, client):
    assert split_gs1_script(source, True) == (server, client)


@pytest.mark.parametrize("source", [
    "server;\n//#CLIENTSIDE\nclient;\nnext;",
    "//#CLIENTSIDE trailing text\nclient;",
    "server;\nnext;", "//#CLIENTSIDE", "//#CLIENTSIDE\n", " \n",
])
def test_split_without_npc_server(source):
    assert split_gs1_script(" \t" + source + "\n ", False) == (
        "", source.strip())


def test_attach_compiles_and_runs_only_server_half():
    manager = NPCManager(SimpleNamespace(config=ServerConfig()))
    npc = NPC(7, "guard")
    source = ("if (created) { this.side = 1; }\n//#CLIENTSIDE\n"
              "if (created) { this.side = 2; }\nfunction ( {")

    manager.attach_gs1(npc, source, raise_errors=True)

    assert npc.gs1_program is not None
    assert npc.gs1_scopes["this"]["side"] == 1
    assert npc.client_script == source.split("//#CLIENTSIDE\n")[1]
    assert npc.gs1_source == source


def test_attach_defaults_to_server_mode_without_config():
    npc = NPC(7)
    NPCManager(SimpleNamespace()).attach_gs1(
        npc, "if (created) { this.side = 1; }\n//#CLIENTSIDE\nclient;")
    assert npc.gs1_program is not None
    assert npc.gs1_scopes["this"]["side"] == 1
    assert npc.client_script == "client;"


@pytest.mark.parametrize("enabled, source, client", [
    (True, "//#CLIENTSIDE\nfunction ( {", "function ( {"),
    (True, " \n ", ""),
    (False, "function ( {\n//#CLIENTSIDE\nclient;",
     "function ( {\n//#CLIENTSIDE\nclient;"),
])
def test_attach_skips_empty_server_half(monkeypatch, enabled, source, client):
    import pygserver.gs1_host as host
    compiler = Mock(side_effect=AssertionError("must not compile"))
    monkeypatch.setattr(host, "compile_gs1", compiler)
    npc = NPC(7)
    npc.gs1_program = object()  # Replacing a server script clears its program.
    manager = NPCManager(SimpleNamespace(config=ServerConfig(serverside=enabled)))

    manager.attach_gs1(npc, source)

    compiler.assert_not_called()
    assert npc.gs1_program is None
    assert npc.gs1_source == source
    assert npc.client_script == client


def test_script_prop_is_long_string_and_mangles_newlines():
    npc = NPC(7)
    npc.client_script = "if (created) {\n  message caf\xe9;\n}"
    npc.gani = "idle"

    packet = npc.build_props_packet()
    npc_id, props, clean = parse_as_client(packet)

    assert NPC_PROPS[1].wire is Wire.LONGSTRING
    assert (npc_id, clean) == (7, True)
    assert props[NPCPROP.SCRIPT] == npc.client_script.replace("\n", "\xa7")
    assert props[NPCPROP.GANI] == "idle"
    assert packet.count(b"\n") == 1


def test_empty_client_script_omits_script_prop():
    assert NPCPROP.SCRIPT not in parse_as_client(NPC(7).build_props_packet())[1]


def test_full_send_includes_script_independently_of_dirty_flags():
    npc = NPC(7)
    npc.client_script = "message client;"
    for script_dirty in (False, True):
        npc._script_dirty = script_dirty
        for _ in range(2):
            _, props, clean = parse_as_client(npc.build_props_packet())
            assert clean
            assert props[NPCPROP.SCRIPT] == npc.client_script
            assert npc._script_dirty is script_dirty
        _, props, clean = parse_as_client(
            npc.build_props_packet(include_script=False))
        assert clean
        assert NPCPROP.SCRIPT not in props


@pytest.mark.parametrize("update_via_nc", [False, True])
def test_dirty_flush_sends_script_once_after_attachment(update_via_nc):
    async def run():
        server = SimpleNamespace(config=ServerConfig(), broadcast_to_level=AsyncMock())
        manager = server.npc_manager = NPCManager(server)
        npc = NPC(7)
        Level("room.nw").add_npc(npc)
        manager._npcs[npc.id] = npc
        npc.client_script = "message original;"

        async def flush():
            server.broadcast_to_level.reset_mock()
            await manager.tick()
            server.broadcast_to_level.assert_awaited_once()
            level, packet = server.broadcast_to_level.await_args.args
            assert level == "room.nw"
            _, props, clean = parse_as_client(packet)
            assert clean
            assert not npc._dirty
            assert not npc._script_dirty
            return props

        npc.x = 4
        npc.mark_dirty()
        props = await flush()
        assert NPCPROP.SCRIPT not in props
        assert props[NPCPROP.X2] == 4

        source = "//#CLIENTSIDE\nmessage updated;\nmessage again;"
        if update_via_nc:
            player = SimpleNamespace(account_name="editor", send_raw=AsyncMock())
            payload = PacketBuilder().write_gint3(npc.id).build() + source.encode()
            await NCManager(server)._handle_npc_script_set(NCSession(player), payload)
            player.send_raw.assert_not_awaited()
        else:
            manager.attach_gs1(npc, source)
        assert npc._script_dirty
        props = await flush()
        assert props[NPCPROP.SCRIPT] == "message updated;\xa7message again;"

        npc.gani = "walk"
        npc.mark_dirty()
        props = await flush()
        assert NPCPROP.SCRIPT not in props
        assert props[NPCPROP.GANI] == "walk"
        assert parse_as_client(npc.build_props_packet())[1][NPCPROP.SCRIPT] == (
            "message updated;\xa7message again;")

    asyncio.run(run())


@pytest.mark.parametrize("length", [0x705F, 0x7060, 40000])
def test_script_length_limit_keeps_packet_valid(caplog, length):
    npc = NPC(7, "long-script-guard")
    npc.client_script = "\u2603\n" * (length // 2) + "x" * (length % 2)
    npc.gani = "idle"

    with caplog.at_level(logging.WARNING, logger="pygserver.npc"):
        packet = npc.build_props_packet()
    _, props, clean = parse_as_client(packet)
    expected = npc.client_script.replace("\n", "\xa7").encode(
        "latin-1", errors="replace")[:0x705F].decode("latin-1")

    assert clean
    assert props[NPCPROP.SCRIPT] == expected
    assert props[NPCPROP.GANI] == "idle"
    assert packet.count(b"\n") == 1
    assert encode_value(NPC_PROPS[1], "x" * 0x705F)[:2] == b"\xff\xff"
    if length > 0x705F:
        assert "long-script-guard" in caplog.text
        assert "truncating" in caplog.text
    else:
        assert not caplog.records


@pytest.mark.parametrize("value, expected", [
    ("false", False), ("FALSE # classic", False),
    ("true# enabled", True), ("TrUe", True),
])
def test_config_script_mode_parse_and_roundtrip(tmp_path, value, expected):
    path = tmp_path / "serveroptions.txt"
    path.write_text(f"serverside = {value}\n")
    config = ServerConfig.from_file(str(path))
    assert config.serverside is expected
    config.to_file(str(path))
    assert f"serverside = {str(expected).lower()}\n" in path.read_text()
    assert ServerConfig.from_file(str(path)).serverside is expected


def test_config_defaults_to_server_scripting():
    assert ServerConfig().serverside is True


@pytest.mark.parametrize("enabled", [True, False])
def test_level_load_spawns_client_only_npc(enabled):
    async def run():
        level = Level("room.nw")
        code = "//#CLIENTSIDE\nif (created) { message client; }"
        level.get_npc_defs = lambda: [dict(image="", code=code, x=1, y=2)]
        server = SimpleNamespace(config=ServerConfig(serverside=enabled))
        server.npc_manager = NPCManager(server)

        await GameServer._register_level_features(server, level)

        npc, = server.npc_manager._npcs.values()
        assert npc.gs1_program is None
        assert npc.client_script == (code.split("\n", 1)[1] if enabled else code)
        assert npc.level is level
    asyncio.run(run())


@pytest.mark.parametrize("enabled", [True, False])
def test_nc_save_uses_script_mode_without_firing_created(enabled):
    server = SimpleNamespace(config=ServerConfig(serverside=enabled))
    server.npc_manager = NPCManager(server)
    npc = NPC(7)
    server.npc_manager._npcs[npc.id] = npc
    player = SimpleNamespace(account_name="editor", send_raw=AsyncMock())
    source = "if (created) { this.side = 1; }\n//#CLIENTSIDE\nfunction ( {"
    payload = PacketBuilder().write_gint3(npc.id).build() + source.encode()

    asyncio.run(NCManager(server)._handle_npc_script_set(NCSession(player), payload))

    player.send_raw.assert_not_awaited()
    assert npc.gs1_source == source
    assert npc.client_script == (source.split("\n", 2)[2] if enabled else source)
    assert (npc.gs1_program is not None) is enabled
    assert npc.gs1_scopes["this"] == {}
    assert npc._dirty
