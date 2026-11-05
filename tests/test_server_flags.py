"""Server flag persistence and the shared script/RC/player view."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pygserver.config import ServerConfig
from pygserver.gs1_host import compile_gs1, run_npc_event
from pygserver.npc import NPC
from pygserver.player import Player
from pygserver.protocol.constants import PLPERM, PLO
from pygserver.protocol.packets import (
    PacketBuilder, PacketReader, build_flag_del, build_flag_set,
)
from pygserver.rc import RCManager, RCSession
from pygserver.server import GameServer


def make_server(path, serverside=True):
    config = ServerConfig.from_server_dir(str(path))
    config.serverside = serverside
    return GameServer(config)


def execute(server, code):
    npc = NPC(7)
    npc.gs1_program = compile_gs1("if (created) { " + code + " }")
    assert npc.gs1_program is not None
    return run_npc_event(npc, "created", server)


def test_script_flags_round_trip_with_prefixes_and_boolean(tmp_path):
    server = make_server(tmp_path)
    ctx = execute(server, "setstring server.a,hello; set serverr.b;"
                  "setstring server.b,private; setstring global.unsaved,temporary;")
    assert ctx.vars.scopes["server"] is server.server_flags
    assert server.flags is server.server_flags
    assert server.get_flag("serverr.b") is True
    server.server_flags.save()
    assert not server.server_flags.dirty
    assert (tmp_path / "config/serverflags.txt").read_text() == (
        "server.a=hello\nserver.b=private\nserverr.b\n")

    restarted = make_server(tmp_path)
    assert restarted.get_flag("server.a") == "hello"
    assert restarted.get_flag("serverr.b") is True
    ctx = execute(restarted, "setstring this.a,#s(server.a);"
                  "if (serverr.b) set this.b;"
                  "setstring this.private,#s(server.b);"
                  "this.dynamic=makevar(\"serverr.b\") @ \"\";"
                  "setstring this.global,#s(global.unsaved);")
    assert ctx.vars.scopes["this"] == {
        "a": "hello", "b": True, "private": "private", "dynamic": "1", "global": "",
    }


def test_loader_trims_lines_and_preserves_string_values(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "serverflags.txt").write_text(
        "\n \t\n server.enabled \n server.message = a=b # text \n"
        "server.empty=\nserver.message=duplicate\n")
    server = make_server(tmp_path)
    assert server.server_flags == {
        "server.enabled": True, "server.message": "a=b # text", "server.empty": True,
    }
    assert not server.server_flags.dirty


def test_flags_file_loads_and_saves_latin1_bytes(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    path = directory / "serverflags.txt"
    contents = b"server.greeting=caf\xe9\n"
    path.write_bytes(contents)
    server = make_server(tmp_path)
    assert server.get_flag("server.greeting") == "caf\xe9"
    server.server_flags["server.greeting"] = "caf\xe9"
    server.server_flags.save()
    assert path.read_bytes() == contents


@pytest.mark.parametrize("origin", ["api", "player"])
def test_empty_flag_normalizes_to_boolean_in_store_broadcast_and_file(tmp_path, origin):
    async def run():
        server = make_server(tmp_path, serverside=False)
        recipient = SimpleNamespace(id=2, logged_in=True, send_raw=AsyncMock())
        server.players[recipient.id] = recipient
        if origin == "api":
            await server.set_flag("server.x", "")
        else:
            player = Player(server, 3, AsyncMock(), SimpleNamespace())
            await player._handle_flag_set(b"server.x")
        assert server.get_flag("server.x") is True
        packet = bytes([PLO.FLAGSET + 32]) + b"server.x\n"
        recipient.send_raw.assert_awaited_once_with(packet)
        assert build_flag_set("server.x", True) == packet
        server.server_flags.save()
        assert server.server_flags.path.read_bytes() == b"server.x\n"
        assert make_server(tmp_path).get_flag("server.x") is True
    asyncio.run(run())


@pytest.mark.parametrize("serverside", [True, False])
def test_broadcast_rule_for_script_api_and_deletion(tmp_path, serverside):
    async def run():
        server = make_server(tmp_path, serverside)
        player = SimpleNamespace(id=2, logged_in=True, send_raw=AsyncMock())
        server.players[player.id] = player
        execute(server, "setstring server.secret,hidden; set serverr.public;")
        await asyncio.sleep(0)
        expected = [] if serverside else [build_flag_set("server.secret", "hidden")]
        expected.append(build_flag_set("serverr.public", True))
        assert [call.args[0] for call in player.send_raw.await_args_list] == expected

        player.send_raw.reset_mock()
        await server.set_flag("server.secret", "changed")
        await server.set_flag("serverr.public", "visible")
        await server.del_flag("server.secret")
        execute(server, "unset serverr.public;")
        await asyncio.sleep(0)
        expected = [] if serverside else [build_flag_set("server.secret", "changed")]
        expected.append(build_flag_set("serverr.public", "visible"))
        if not serverside:
            expected.append(build_flag_del("server.secret"))
        expected.append(build_flag_del("serverr.public"))
        assert [call.args[0] for call in player.send_raw.await_args_list] == expected
        server.server_flags.save()
        assert make_server(tmp_path).server_flags == {}
    asyncio.run(run())


@pytest.mark.parametrize("serverside", [True, False])
def test_login_sends_personal_flags_then_public_server_flags(tmp_path, serverside):
    async def run():
        server = make_server(tmp_path, serverside)
        server.server_flags.data.update({"server.secret": "hidden", "serverr.public": True})
        player = Player(server, 2, AsyncMock(), SimpleNamespace())
        player.flags = {"personal": True, "greeting": "hello"}
        player.login_service.send_login_response = AsyncMock()
        player.warp = AsyncMock()
        player.send_raw = AsyncMock()
        await player.login_service.complete_login()
        expected = [build_flag_set("personal", True), build_flag_set("greeting", "hello")]
        if not serverside:
            expected.append(build_flag_set("server.secret", "hidden"))
        expected.append(build_flag_set("serverr.public", True))
        assert [call.args[0] for call in player.send_raw.await_args_list] == expected
    asyncio.run(run())


def test_rc_flags_are_visible_to_scripts_and_persisted(tmp_path):
    async def run():
        server = make_server(tmp_path)
        player = SimpleNamespace(id=2, account_name="admin", send_raw=AsyncMock())
        manager = RCManager(server)
        session = RCSession(player, rights=PLPERM.SETATTRIBUTES | PLPERM.VIEWATTRIBUTES)
        payload = (PacketBuilder().write_gshort(2).write_gstring("server.fromrc=hello")
                   .write_gstring("serverr.enabled").build())
        await manager._handle_server_flags_set(session, payload)
        assert server.server_flags.dirty
        ctx = execute(server, "setstring this.value,#s(server.fromrc);"
                      "if (serverr.enabled) set this.enabled;")
        assert ctx.vars.scopes["this"] == {"value": "hello", "enabled": True}
        await manager._handle_server_flags_get(session, b"")
        packet = player.send_raw.await_args.args[0]
        reader = PacketReader(packet[1:-1])
        assert reader.read_gshort() == 2
        assert {reader.read_gstring(), reader.read_gstring()} == {
            "server.fromrc=hello", "serverr.enabled",
        }
        server.server_flags.save()
        assert make_server(tmp_path).get_flag("server.fromrc") == "hello"
        assert make_server(tmp_path).get_flag("serverr.enabled") is True
    asyncio.run(run())


def test_periodic_tick_and_shutdown_save_dirty_flags(tmp_path):
    async def run():
        server = make_server(tmp_path)
        server.server_flags["server.periodic"] = "saved"
        server.running = True
        server._last_flag_save -= 61
        async def tick():
            server.running = False
        server.npc_manager.tick = tick
        await server._main_loop()
        assert make_server(tmp_path).get_flag("server.periodic") == "saved"
        server.server_flags["server.shutdown"] = True
        await server.stop()
        assert make_server(tmp_path).get_flag("server.shutdown") is True
    asyncio.run(run())


def test_failed_periodic_save_still_yields_and_does_not_retry_each_tick(tmp_path, caplog):
    async def run():
        server = make_server(tmp_path)
        (tmp_path / "config").write_bytes(b"blocks flag writes")
        server.server_flags["server.pending"] = "unsaved"
        server.running = True
        server._last_flag_save -= 61
        previous_save = server._last_flag_save
        ticks = 0

        async def tick():
            nonlocal ticks
            ticks += 1
            # Bound a regression's retry loop so pytest fails instead of hanging.
            if ticks >= 5:
                server.running = False

        server.npc_manager.tick = tick
        task = asyncio.create_task(server._main_loop())
        try:
            await asyncio.wait_for(asyncio.sleep(0.11), timeout=1)
            assert not task.done()
            assert ticks >= 2
            assert server._last_flag_save > previous_save
            assert server.server_flags.dirty
            errors = [record for record in caplog.records
                      if record.message == "Failed to save server flags"]
            assert len(errors) == 1
            assert errors[0].exc_info is not None
        finally:
            server.running = False
            await asyncio.wait_for(task, timeout=1)
    asyncio.run(run())


def test_failed_shutdown_save_still_closes_listener(tmp_path, caplog):
    async def run():
        server = make_server(tmp_path)
        (tmp_path / "config").write_bytes(b"blocks flag writes")
        server.server_flags["server.pending"] = "unsaved"
        listener = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        server._server = listener
        await server.stop()
        listener.close.assert_called_once_with()
        listener.wait_closed.assert_awaited_once_with()
        assert server.server_flags.dirty
        errors = [record for record in caplog.records
                  if record.message == "Failed to save server flags during shutdown"]
        assert len(errors) == 1
        assert errors[0].exc_info is not None
    asyncio.run(run())


def test_atomic_save_retains_previous_file_and_dirty_on_failure(tmp_path, monkeypatch):
    server = make_server(tmp_path)
    server.server_flags["server.value"] = "old"
    server.server_flags.save()
    server.server_flags["server.value"] = "new"
    def fail_replace(*args):
        raise OSError("replace failed")
    monkeypatch.setattr("pygserver.server_flags.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        server.server_flags.save()
    assert make_server(tmp_path).get_flag("server.value") == "old"
    assert server.server_flags.dirty
    assert not server.server_flags.path.with_suffix(".txt.tmp").exists()


def test_save_omits_nonserializable_values_and_skips_clean_store(tmp_path, monkeypatch):
    server = make_server(tmp_path)
    execute(server, "set server.enabled; setstring server.text,hello;"
            "server.number=5; server.array={1,2}; server.false=false;")
    server.server_flags.save()
    assert server.server_flags.path.read_text() == "server.enabled\nserver.text=hello\n"
    def fail_replace(*args):
        raise AssertionError("a clean store must not write")
    monkeypatch.setattr("pygserver.server_flags.os.replace", fail_replace)
    server.server_flags.save()


def test_dynamic_prefix_resolution_and_global_scope(tmp_path):
    server = make_server(tmp_path)
    ctx = execute(server, "this.i=2; setstring serverr.item#v(this.i),public;"
                  "setstring server.item2,private;"
                  "this.dynamic=makevar(\"serverr.item2\") @ \"\";"
                  "setstring this.valueof,serverr.item2=;"
                  "this.keys=getflagkeys(serverr.item);"
                  "setstring global.shared,unsaved;")
    assert server.get_flag("serverr.item2") == "public"
    assert server.get_flag("server.item2") == "private"
    assert ctx.vars.scopes["this"]["dynamic"] == "public"
    assert ctx.vars.scopes["this"]["valueof"] == "public"
    assert ctx.vars.scopes["this"]["keys"] == [2.0]
    assert execute(server, "setstring this.g,#s(global.shared);").vars.scopes["this"]["g"] == "unsaved"
