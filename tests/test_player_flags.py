"""GS1 client flags share persistent player state and synchronize only to their owner."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import AccountManager
from pygserver.config import ServerConfig
from pygserver.gs1_host import compile_gs1, run_npc_event
from pygserver.npc import NPC
from pygserver.player import Player
from pygserver.protocol.constants import PLTYPE
from pygserver.protocol.packets import build_flag_del, build_flag_set
from pygserver.server import GameServer


@pytest.fixture
def world(tmp_path):
    server = GameServer(ServerConfig.from_server_dir(str(tmp_path)))
    players = []
    for pid, name in ((2, "quester"), (3, "observer")):
        player = Player(server, pid, AsyncMock(), MagicMock())
        player.account_name = name
        player.logged_in = player.account_loaded = True
        player.send_raw = AsyncMock()
        server.players[pid] = player
        players.append(player)
    return server, *players


def script(server, player, body):
    npc = NPC(7)
    npc.gs1_program = compile_gs1("if (created) { " + body + " }", raise_errors=True)
    ctx = run_npc_event(npc, "created", server, player)
    return ctx


def packets(player):
    return [call.args[0] for call in player.send_raw.await_args_list]


async def test_script_writes_keep_prefixes_and_sync_only_to_owner(world):
    server, owner, other = world
    ctx = script(server, owner, "set client.q; setstring client.n,5;"
                 "set clientr.r; client.count=7; set bare;")
    await asyncio.sleep(0)
    assert ctx.vars.scopes["client"] is owner.flags
    assert ctx.vars.player_flags is owner.flags
    assert owner.flags == {"client.q": True, "client.n": "5", "clientr.r": True,
                           "client.count": 7.0, "bare": True}
    assert packets(owner) == [
        build_flag_set("client.q", True), build_flag_set("client.n", "5"),
        build_flag_set("clientr.r", True), build_flag_set("client.count", "7"),
    ]
    assert other.flags == {}
    other.send_raw.assert_not_awaited()


async def test_unset_and_empty_string_delete_only_the_owners_flags(world):
    server, owner, other = world
    owner.flags = {"client.q": True, "client.n": "5", "clientr.r": True, "bare": True}
    script(server, owner, "unset client.q; setstring client.n,; unset clientr.r;")
    await asyncio.sleep(0)
    assert owner.flags == {"bare": True}
    assert packets(owner) == [build_flag_del("client.q"), build_flag_del("client.n"),
                              build_flag_del("clientr.r")]
    other.send_raw.assert_not_awaited()


def test_reads_preserve_aliases_dynamic_names_and_flag_key_queries(world):
    server, owner, _ = world
    owner.flags = {"client.q": True, "client.n": "5", "client.2": "two",
                   "clientr.q": "read-only", "bare": True}
    ctx = script(server, owner,
                 "setstring this.n,#s(client.n);"
                 "if (strequals(#s(client.n),5)) set this.equal;"
                 "if (client.q && bare) set this.present;"
                 "this.keys=getflagkeys(client.); this.rkeys=getflagkeys(clientr.);"
                 'this.dynamic=makevar("clientr.q") @ "";'
                 "setstring this.valueof,client.n=;")
    assert ctx.vars.scopes["this"] == {
        "n": "5", "equal": True, "present": True, "keys": [2.0, 0.0, 0.0],
        "rkeys": [0.0], "dynamic": "read-only", "valueof": "5",
    }
    assert "q" not in owner.flags


async def test_dynamic_client_names_write_to_the_prefixed_store(world):
    server, owner, other = world
    script(server, owner, "this.i=3; setstring client.item#v(this.i),quest;"
           "setstring clientr.item#v(this.i),reward;")
    await asyncio.sleep(0)
    assert owner.flags == {"client.item3": "quest", "clientr.item3": "reward"}
    assert packets(owner) == [build_flag_set("client.item3", "quest"),
                              build_flag_set("clientr.item3", "reward")]
    other.send_raw.assert_not_awaited()


async def test_scoped_function_parameter_restores_the_prefixed_flag(world):
    server, owner, other = world
    owner.flags["client.n"] = "5"
    script(server, owner, "function echo(client.n) { setstring this.inside,#s(client.n); }"
           "echo(9);")
    await asyncio.sleep(0)
    assert owner.flags == {"client.n": "5"}
    assert packets(owner) == [build_flag_set("client.n", "9"), build_flag_set("client.n", "5")]
    other.send_raw.assert_not_awaited()


async def test_incoming_client_flags_update_script_state_without_echo(world):
    server, owner, other = world
    await owner._handle_flag_set(b"client.q")
    await owner._handle_flag_set(b"client.n=5")
    ctx = script(server, owner, "if (client.q) set this.present;"
                 "setstring this.n,#s(client.n);")
    assert ctx.vars.scopes["this"] == {"present": True, "n": "5"}
    await owner._handle_flag_set(b"client.n=6")
    assert owner.flags["client.n"] == "6"
    await owner._handle_flag_set(b"client.n=")
    await owner._handle_flag_del(b"client.q")
    await asyncio.sleep(0)
    assert owner.flags == {}
    owner.send_raw.assert_not_awaited()
    other.send_raw.assert_not_awaited()


@pytest.mark.parametrize("serverside", [True, False])
async def test_clientr_incoming_write_and_delete_protection_matches_mode(world, serverside):
    server, owner, other = world
    server.config.serverside = serverside
    owner.flags["clientr.q"] = "protected"
    await owner._handle_flag_set(b"clientr.q=changed")
    await owner._handle_flag_set(b"clientr.new")
    assert owner.flags["clientr.q"] == ("protected" if serverside else "changed")
    assert ("clientr.new" in owner.flags) is not serverside
    await owner._handle_flag_del(b"clientr.q")
    assert "clientr.q" in owner.flags
    await asyncio.sleep(0)
    owner.send_raw.assert_not_awaited()
    other.send_raw.assert_not_awaited()


async def test_flags_survive_account_json_reload_and_remain_script_visible(world):
    server, owner, _ = world
    manager = AccountManager(server, server.config.accounts_dir)
    try:
        account = manager.create_account(owner.account_name)
        script(server, owner, "set client.q; setstring client.n,5; set clientr.r; set bare;")
        manager.save_player_to_account(owner, account)
    finally:
        manager._save_executor.shutdown()
    contents = json.loads((manager.accounts_dir / "quester.json").read_text())
    assert contents["flags"] == {"client.q": True, "client.n": "5", "clientr.r": True,
                                  "bare": True}

    restarted = GameServer(server.config)
    reader = AccountManager(restarted, server.config.accounts_dir)
    try:
        fresh = Player(restarted, 4, AsyncMock(), MagicMock())
        fresh.send_raw = AsyncMock()
        account = reader.get_account("quester")
        reader.load_player_from_account(fresh, account)
        ctx = script(restarted, fresh, "if (client.q && clientr.r && bare) set this.ready;"
                     "setstring this.n,#s(client.n);")
        assert ctx.vars.scopes["this"] == {"ready": True, "n": "5"}
        assert fresh.flags == contents["flags"]
        assert fresh.flags is not account.flags
    finally:
        reader._save_executor.shutdown()


async def test_periodic_save_snapshots_live_flags_without_control_session_overwrite(world):
    server, owner, _ = world
    manager = AccountManager(server, server.config.accounts_dir)
    try:
        manager.create_account(owner.account_name)
        offline = manager.create_account("offline")
        offline.flags = {"client.saved": True}
        for pid, connection, logged_in in ((4, PLTYPE.RC, True), (5, PLTYPE.CLIENT, False)):
            stale = Player(server, pid, AsyncMock(), MagicMock())
            stale.account_name = owner.account_name
            stale.connection_type = connection
            stale.logged_in = stale.account_loaded = logged_in
            stale.flags = {"client.q": "stale"}
            server.players[pid] = stale
        script(server, owner, "set client.q; setstring client.n,5; set clientr.r;")
        await manager.save_all_accounts()
        manager._save_executor.shutdown()
        contents = json.loads((manager.accounts_dir / "quester.json").read_text())
        assert contents["flags"] == owner.flags
        contents = json.loads((manager.accounts_dir / "offline.json").read_text())
        assert contents["flags"] == {"client.saved": True}
    finally:
        manager._save_executor.shutdown()


async def test_login_sends_prefixed_client_flags_and_bare_flags_to_owner(world):
    server, owner, other = world
    owner.flags = {"client.q": True, "client.n": "5", "clientr.r": True, "bare": True}
    owner.logged_in = False
    owner.login_service.send_login_response = AsyncMock()
    owner.warp = AsyncMock()
    await owner.login_service.complete_login()
    assert packets(owner) == [build_flag_set("client.q", True), build_flag_set("client.n", "5"),
                              build_flag_set("clientr.r", True), build_flag_set("bare", True)]
    other.send_raw.assert_not_awaited()


async def test_playerless_client_scope_does_not_leak_to_another_player(world):
    server, owner, other = world
    script(server, None, "set client.q; set clientr.r;")
    ctx = script(server, owner, "setstring this.q,#s(client.q);")
    await asyncio.sleep(0)
    assert ctx.vars.scopes["this"]["q"] == ""
    assert owner.flags == {}
    owner.send_raw.assert_not_awaited()
    other.send_raw.assert_not_awaited()
