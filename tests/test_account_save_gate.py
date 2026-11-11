"""A connection that never completed its login never writes an account.

Drives the real Player.run() (login, read loop, _cleanup) against a real
AccountManager on a temp dir; only the socket and the outbound login frame
are stubbed. Every refusal path must leave the stored account file
byte-identical. Reference: GServer-v2 origin/beta4 Player.cpp:352-362 saves
only when `m_loaded` (set at Player.cpp:771-773 and, for clients, only at
PlayerClient.cpp:659), and only for clients whose account is not loadOnly.
"""

import asyncio
import json
import struct
import time
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import Account, AccountManager
from pygserver.config import ServerConfig
from pygserver.horse import HorseManager
from pygserver.listserver import ServerListClient
from pygserver.nc import NCManager
from pygserver.player import Player
from pygserver.protocol.packet_codec import PacketBuilder
from pygserver.protocol.packets import PacketReader
from pygserver.rc import RCManager
from pygserver.server import GameServer

LOOPBACK = ("127.0.0.1", 50000)


def login_bytes(account, client_type, password="pw"):
    builder = PacketBuilder().write_gchar(client_type)
    if client_type not in (1, 3):
        builder.write_gchar(7)  # RC and NC send no key byte
    body = (builder.write_string("G3D0311C").write_gstring(account)
            .write_gstring(password).build())
    compressed = zlib.compress(body)
    return struct.pack(">H", len(compressed)) + compressed


@pytest.fixture
def world(tmp_path):
    config = ServerConfig.from_server_dir(str(tmp_path))
    config.verify_login = False
    config.staff = ["victim"]
    config.start_level = "start.nw"
    server = GameServer(config)
    server.rc_manager = RCManager(server)
    server.nc_manager = NCManager(server)
    server.horse_manager = HorseManager(server)
    manager = AccountManager(server, str(tmp_path / "accounts"))
    server.account_manager = manager
    victim = Account("victim", rupees=777, weapons=["Bow"], admin_ip="127.0.0.1")
    victim.set_password("secret")
    manager._save_account(victim)
    drain(manager)
    path = manager.accounts_dir / "victim.json"
    manager._accounts.clear()  # start every case from disk, like a fresh boot
    try:
        yield server, manager, path
    finally:
        manager._save_executor.shutdown(wait=True)


def drain(manager):
    manager._save_executor.submit(lambda: None).result()


def scripted_reads(reads):
    """session.read stand-in: each item is bytes, an exception to raise, or
    an async callable run at that point of the session; then EOF."""
    script = list(reads)

    async def read(*_args, **_kwargs):
        if not script:
            return b""
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return await item()
        return item

    return read


def connect(server, reads, player_id=2):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    server.players[player_id] = player
    player.session.writer.get_extra_info.return_value = LOOPBACK
    player.session.read = scripted_reads(reads)
    player.login_service.send_login_response = AsyncMock()
    player.send_raw = AsyncMock()
    player.warp = AsyncMock()
    return player


async def run_session(server, manager, reads, player_id=2):
    player = connect(server, reads, player_id)
    await player.run()
    server.players.pop(player_id, None)
    drain(manager)
    return player


def stored(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("type_byte", [9, 2, 0, 4, 8, 7])
async def test_unsupported_type_byte_leaves_account_untouched(world, type_byte):
    server, manager, path = world
    before = path.read_bytes()
    player = await run_session(server, manager, [login_bytes("victim", type_byte)])
    assert path.read_bytes() == before
    assert player.account_name == ""  # dropped before taking an identity


@pytest.mark.parametrize("type_byte", [5, 1, 3])
async def test_onlystaff_refusal_leaves_account_untouched(world, type_byte):
    server, manager, path = world
    server.config.onlystaff = True
    server.config.staff = ["admin"]
    before = path.read_bytes()
    await run_session(server, manager, [login_bytes("victim", type_byte)])
    assert path.read_bytes() == before


@pytest.mark.parametrize("type_byte", [1, 6, 3])
async def test_control_login_without_staff_leaves_account_untouched(world, type_byte):
    server, manager, path = world
    server.config.staff = ["admin"]
    before = path.read_bytes()
    player = await run_session(server, manager, [login_bytes("victim", type_byte)])
    assert not player.logged_in
    assert path.read_bytes() == before


@pytest.mark.parametrize("type_byte", [1, 6, 3])
async def test_control_login_from_wrong_admin_ip_leaves_account_untouched(world, type_byte):
    server, manager, path = world
    data = stored(path)
    data["admin_ip"] = "10.9.8.7"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    player = await run_session(server, manager, [login_bytes("victim", type_byte)])
    assert not player.logged_in
    assert path.read_bytes() == before


@pytest.mark.parametrize("type_byte", [1, 6, 3])
async def test_accepted_control_login_does_not_save_its_own_state(world, type_byte):
    # Player.cpp:360: RC and NC never write their own state on disconnect.
    server, manager, path = world
    before = path.read_bytes()

    async def mutate_then_eof(*_args, **_kwargs):
        player.rupees, player.weapons = 1, []
        return b""

    player = connect(server, [login_bytes("victim", type_byte), mutate_then_eof])
    await player.run()
    drain(manager)
    assert player.logged_in
    assert path.read_bytes() == before


async def test_banned_account_is_left_untouched(world):
    server, manager, path = world
    data = stored(path)
    data.update(is_banned=True, ban_reason="cheating", hearts=0)
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    player = await run_session(server, manager, [login_bytes("victim", 5)])
    assert not player.logged_in
    assert path.read_bytes() == before


async def test_wrong_password_leaves_account_untouched(world):
    server, manager, path = world
    server.config.verify_login = True
    before = path.read_bytes()
    player = await run_session(server, manager, [login_bytes("victim", 5, "wrong")])
    assert not player.logged_in
    assert path.read_bytes() == before


@pytest.mark.parametrize("reads", [
    [b""], [b"\x00"], [b"\x00\x05garbage"], [asyncio.TimeoutError()],
    [struct.pack(">H", 3) + zlib.compress(b"\x25")[:3]],
])
async def test_malformed_short_or_missing_login_leaves_account_untouched(world, reads):
    server, manager, path = world
    before = path.read_bytes()
    player = await run_session(server, manager, reads)
    assert not player.logged_in
    assert path.read_bytes() == before


async def test_exception_inside_the_loader_leaves_account_untouched(world):
    server, manager, path = world
    before = path.read_bytes()
    real_load = manager.load_player_from_account

    def half_load(player, account):
        real_load(player, account)
        player.rupees = 0
        player.weapons = []
        raise RuntimeError("loader failed half way")

    manager.load_player_from_account = half_load
    player = await run_session(server, manager, [login_bytes("victim", 5)])
    assert not player.logged_in
    assert path.read_bytes() == before


async def test_disconnect_while_awaiting_list_server_leaves_account_untouched(world):
    server, manager, path = world
    server.config.verify_login = True
    server.listserver = MagicMock()
    server.listserver.verify_account = AsyncMock()  # verdict never arrives
    before = path.read_bytes()
    player = await run_session(
        server, manager, [login_bytes("victim", 5, "secret"), b""])
    server.listserver.verify_account.assert_awaited_once()
    assert not player.logged_in
    assert path.read_bytes() == before


async def test_list_server_refusal_leaves_account_untouched(world):
    server, manager, path = world
    server.config.verify_login = True
    listserver = ServerListClient(server)
    listserver.connected = True
    listserver._send_packet = AsyncMock()
    server.listserver = listserver
    before = path.read_bytes()

    async def refuse(*_args, **_kwargs):
        verdict = (PacketBuilder().write_gstring("victim").write_gshort(2)
                   .write_gchar(5).write_string("Account is banned").build())
        await listserver._handle_verify_account(PacketReader(verdict))
        return b""

    player = await run_session(
        server, manager, [login_bytes("victim", 5, "secret"), refuse])
    assert not player.logged_in
    player.session.writer.close.assert_called()  # the refusal really disconnects
    assert path.read_bytes() == before


async def test_refused_double_login_leaves_live_account_untouched(world):
    # Player.cpp:735-738: a second same-class login while the first is active.
    server, manager, path = world
    live = connect(server, [login_bytes("victim", 5)], player_id=3)
    assert await live._handle_login()
    assert live.account_loaded
    before = path.read_bytes()
    second = await run_session(server, manager, [login_bytes("victim", 5)])
    assert not second.logged_in and not second.account_loaded
    assert path.read_bytes() == before
    assert live.account_loaded  # the live session still owns the save


@pytest.mark.parametrize("type_byte", [1, 3])
async def test_refused_double_control_login_keeps_live_session_state(world, type_byte):
    # Player.cpp:718-742: the double-login refusal comes before the login is
    # recorded, so the refused login's cleanup must not touch the live
    # session's state (the RC eval NPC is keyed by account name).
    server, manager, path = world
    live = connect(server, [login_bytes("victim", type_byte)], player_id=3)
    assert await live._handle_login()
    sessions = server.rc_manager if type_byte == 1 else server.nc_manager
    assert sessions.get_session(3) is not None
    eval_npc = SimpleNamespace(id=999, level=None)
    if type_byte == 1:
        server.rc_manager._eval_npcs["victim"] = eval_npc
    second = await run_session(server, manager, [login_bytes("victim", type_byte)])
    assert not second.logged_in
    assert sessions.get_session(second.id) is None
    assert sessions.get_session(3) is not None
    if type_byte == 1:
        assert server.rc_manager._eval_npcs.get("victim") is eval_npc


async def test_normal_session_saves_its_changes_on_disconnect(world):
    server, manager, path = world

    async def play_then_eof(*_args, **_kwargs):
        player.rupees += 5
        return b""

    player = connect(server, [login_bytes("victim", 5), play_then_eof])
    await player.run()
    drain(manager)
    assert player.logged_in
    data = stored(path)
    assert data["rupees"] == 782
    assert data["weapons"] == ["Bow"]
    assert not player.account_loaded  # cleanup saves exactly once


async def test_load_only_account_is_never_saved(world):
    # Player.cpp:360 / PlayerClient.cpp:271: `!account.loadOnly`.
    server, manager, path = world
    data = stored(path)
    data["load_only"] = True
    path.write_text(json.dumps(data))
    before = path.read_bytes()

    async def play_then_eof(*_args, **_kwargs):
        player.rupees = 1
        return b""

    player = connect(server, [login_bytes("victim", 5), play_then_eof])
    await player.run()
    drain(manager)
    assert player.logged_in
    assert path.read_bytes() == before


async def test_new_account_is_created_on_first_accepted_login(world):
    server, manager, path = world
    fresh = manager.accounts_dir / "newcomer.json"
    assert not fresh.exists()
    player = await run_session(
        server, manager, [login_bytes("newcomer", 5), b""])
    assert player.logged_in
    assert stored(fresh)["account_name"] == "newcomer"


async def test_refused_login_of_an_unknown_name_creates_no_account(world):
    server, manager, path = world
    server.config.verify_login = True
    await run_session(server, manager, [login_bytes("stranger", 5)])
    assert not (manager.accounts_dir / "stranger.json").exists()
