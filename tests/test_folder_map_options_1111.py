"""`nofoldersconfig` / `sharefolder` and `bigmap` / `minimap` / `maps`.

Reference (GServer-v2 origin/beta4 25396a18): Server.cpp:471-522, 769-786
(world file system), PlayerClient.cpp:494-512 (login PLO 171/172),
GS1Commands.cpp:2614-2652 (setmap / setminimap).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pygserver.account import Account, AccountManager, RCRight
from pygserver.config import ServerConfig
from pygserver.filesystem import FileSystem
from pygserver.gs1.commands import _COMMANDS
from pygserver.protocol.builders.world import map_option_text
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import (
    PacketReader, build_bigmap, build_minimap)
from pygserver.rc import RCManager, RCSession
from pygserver.server import GameServer

from .test_ap_system import make_player
from .test_player_facade import login_frame


def put(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def make_fs(root, **options):
    config = ServerConfig()
    for key, value in options.items():
        setattr(config, key, value)
    server = SimpleNamespace(config=config)
    return FileSystem(server, str(root)), config


# -- config parsing -------------------------------------------------------------

def test_options_parse(tmp_path):
    path = put(tmp_path / "serveroptions.txt", "\n".join([
        "nofoldersconfig = true", "sharefolder = shared, more/deep ,",
        "bigmap = bigmap.txt,bigmap.png,0,0",
        "minimap = minimap.txt, map2.png ,4.25,9.25 # note",
        "maps = a.txt, b.txt",
    ]) + "\n")
    config = ServerConfig.from_file(str(path))
    assert config.nofoldersconfig is True
    assert config.sharefolder == ["shared", "more/deep"]
    assert config.bigmap == "bigmap.txt,bigmap.png,0,0"
    assert config.minimap == "minimap.txt, map2.png ,4.25,9.25 # note"
    assert config.maps == ["a.txt", "b.txt"]
    defaults = ServerConfig()
    assert (defaults.nofoldersconfig, defaults.sharefolder,
            defaults.bigmap, defaults.minimap, defaults.maps) == (
        False, [], "", "", [])


# -- nofoldersconfig / sharefolder ---------------------------------------------

def test_sharefolder_is_ignored_without_nofoldersconfig(tmp_path):
    put(tmp_path / "shared" / "s.png")
    fs, _ = make_fs(tmp_path, sharefolder=["shared"])
    assert fs._find_file("s.png") is None
    assert tmp_path / "shared" not in fs.public_dirs()


def test_nofoldersconfig_shares_folders_and_drops_foldersconfig_roots(tmp_path):
    put(tmp_path / "shared" / "s.png")
    put(tmp_path / "extras" / "e.png")
    (tmp_path / "world" / "extras").mkdir(parents=True)
    put(tmp_path / "config" / "foldersconfig.txt", "file extras/*.png\n")
    fs, config = make_fs(tmp_path)
    assert fs._find_file("e.png") == tmp_path / "extras" / "e.png"
    assert fs.folders_config_entries() == [("file", "extras/*.png")]

    fs2, _ = make_fs(tmp_path, nofoldersconfig=True, sharefolder=["shared"])
    assert fs2._find_file("s.png") == tmp_path / "shared" / "s.png"
    assert fs2._find_file("e.png") is None
    assert fs2.folders_config_entries() == []


@pytest.mark.parametrize("entry", [
    "/etc", "../outside", "shared/../shared", "./shared", "shared/.", ".git",
    "accounts", "Config", "npcs/sub", "C:\\shared", "c:shared", "missing",
    "linkout", "linkself", "linkconfig", "", "shared\x00", "scripts",
])
def test_refused_share_folders(tmp_path, tmp_path_factory, entry):
    outside = tmp_path_factory.mktemp("outside")
    put(outside / "leak.png")
    put(tmp_path / "shared" / "s.png")
    put(tmp_path / "accounts" / "leak.png")
    put(tmp_path / "config" / "leak.png")
    put(tmp_path / "npcs" / "sub" / "leak.png")
    put(tmp_path / "scripts" / "leak.png")
    put(tmp_path / ".git" / "leak.png")
    (tmp_path / "linkout").symlink_to(outside, target_is_directory=True)
    (tmp_path / "linkself").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "linkconfig").symlink_to(tmp_path / "config", target_is_directory=True)
    fs, _ = make_fs(tmp_path, nofoldersconfig=True, sharefolder=[entry])
    assert fs.share_dir_allowed(entry) is None
    assert fs.share_dirs() == []
    assert fs._find_file("leak.png") is None
    assert not (tmp_path / "missing").exists()      # never created


def test_share_folder_link_inside_the_server_is_allowed(tmp_path):
    put(tmp_path / "assets" / "deep" / "a.png")
    (tmp_path / "alias").symlink_to(tmp_path / "assets", target_is_directory=True)
    fs, _ = make_fs(tmp_path, nofoldersconfig=True,
                    sharefolder=["alias", "assets/deep/"])
    assert fs.share_dirs() == [tmp_path / "alias", tmp_path / "assets/deep"]
    assert fs._find_file("a.png") is not None


def test_options_change_drops_the_index(tmp_path):
    put(tmp_path / "shared" / "s.png")
    fs, config = make_fs(tmp_path, nofoldersconfig=True, sharefolder=["shared"])
    assert fs._find_file("s.png") == tmp_path / "shared" / "s.png"
    config.nofoldersconfig = False
    assert fs._find_file("s.png") is None       # a hit, yet withdrawn at once
    config.nofoldersconfig = True
    config.sharefolder = ["other"]
    assert fs._find_file("s.png") is None


def test_rc_option_set_reaches_the_file_index(tmp_path):
    put(tmp_path / "shared" / "s.png")
    server = SimpleNamespace(config=ServerConfig())
    server.filesystem = FileSystem(server, str(tmp_path))
    manager = RCManager(server)
    admin = SimpleNamespace(id=7, account_name="admin", send_raw=AsyncMock())
    session = RCSession(player=admin, rights=RCRight.SETSERVEROPTIONS)
    manager._sessions[admin.id] = session
    payload = b'"nofoldersconfig=true","sharefolder=shared"'
    fs = server.filesystem
    # Admin-only options (PlayerRC.h:27-34): a plain option setter is ignored.
    asyncio.run(manager._handle_server_options_set(session, payload))
    assert fs._find_file("s.png") is None
    session.rights |= RCRight.MODIFYSTAFFACCOUNT
    asyncio.run(manager._handle_server_options_set(session, payload))
    assert server.config.sharefolder == ["shared"]
    assert fs._find_file("s.png") == tmp_path / "shared" / "s.png"
    asyncio.run(manager._handle_server_options_set(
        session, b'"nofoldersconfig=false"'))
    assert fs._find_file("s.png") is None


# -- bigmap / minimap ------------------------------------------------------------

def test_map_packet_vectors():
    assert build_bigmap("bigmap.txt,bigmap.png,0,0") == (
        b"\xcb" + b"bigmap.txt,bigmap.png,0,0\n")
    assert build_minimap("minimap.txt,map2.png,4.25,9.25") == (
        b"\xcc" + b"minimap.txt,map2.png,4.25,9.25\n")


@pytest.mark.parametrize("value,expected", [
    ("bigmap.txt,bigmap.png,0,0", "bigmap.txt,bigmap.png,0,0"),
    (" a.txt , b.png ,\t1, 2 ", "a.txt,b.png,1,2"),
    ("a.txt,,b.png,1,2", "a.txt,b.png,1,2"),        # strtok drops empties
    ("a.txt,b.png,1", None),
    ("a.txt,b.png,1,2,3", None),
    ("", None),
    (",,,", None),
])
def test_map_option_four_token_rule(value, expected):
    assert map_option_text(value) == expected


@pytest.mark.parametrize("bigmap,minimap,expected", [
    ("bigmap.txt,bigmap.png,0,0", "minimap.txt,map2.png,4.25,9.25",
     [PLO.BIGMAP, PLO.MINIMAP]),
    ("", "minimap.txt,map2.png,4.25,9.25", [PLO.MINIMAP]),
    ("a,b,c", "", []),
])
async def test_login_sends_maps_after_reset(tmp_path, bigmap, minimap, expected):
    server = GameServer(ServerConfig.from_server_dir(str(tmp_path)))
    manager = AccountManager(server, str(tmp_path / "accounts"))
    server.account_manager = manager
    server.config.bigmap = bigmap
    server.config.minimap = minimap
    account = Account("tester")
    account.set_password("pw")
    manager._save_account(account)
    manager._save_executor.shutdown(wait=True)
    player = make_player(server)
    player.logged_in = False
    player.session.read = AsyncMock(return_value=login_frame(username="tester"))
    player.session.send_login_response = AsyncMock()
    player.warp = AsyncMock()
    try:
        assert await player._handle_login()
    finally:
        manager._save_executor.shutdown(wait=True)
    lead = player.session.send_login_response.await_args.args[0]
    packet_ids = []
    for packet in lead.split(b"\n"):
        if packet:
            packet_ids.append(packet[0] - 32)
    reset = packet_ids.index(PLO.RESETCLIENT)
    after = [pid for pid in packet_ids[reset + 1:]
             if pid in (PLO.BIGMAP, PLO.MINIMAP)]
    assert after == expected
    assert packet_ids[reset + 1:reset + 1 + len(expected)] == expected
    if PLO.BIGMAP in expected:
        assert build_bigmap(bigmap) in lead
    if PLO.MINIMAP in expected:
        assert build_minimap(minimap) in lead


async def test_gs1_setmap_and_setminimap_send_to_the_caller():
    player = SimpleNamespace(send_raw=AsyncMock())
    _COMMANDS["setmap"](None, ["map.png", "bigmap.txt", 17.7, 6.6], None, player, None)
    _COMMANDS["setminimap"](None, ["map2.png", "minimap.txt", 1, "6"], None, player, None)
    _COMMANDS["setmap"](None, ["map.png", "bigmap.txt", 1], None, player, None)
    _COMMANDS["setminimap"](None, ["map.png", "bigmap.txt", 1, 2], None, None, None)
    await asyncio.sleep(0)
    assert [call.args[0] for call in player.send_raw.await_args_list] == [
        build_bigmap("map.png,bigmap.txt,17.700000,6.600000"),
        build_minimap("map2.png,minimap.txt,1.000000,6.000000"),
    ]


# -- control bytes (hardening beyond the reference) ----------------------------

@pytest.mark.parametrize("text", [
    "a.png,b.txt,1,2\nX", "a\tb.png,b.txt,1,2", "a.png,b.txt,1,2\x00",
    "a.png,\rb.txt,1,2", "\x1f"])
def test_map_packets_refuse_control_bytes(text):
    assert build_bigmap(text) == b""
    assert build_minimap(text) == b""


async def test_gs1_setmap_with_a_control_byte_sends_nothing():
    player = SimpleNamespace(send_raw=AsyncMock())
    _COMMANDS["setmap"](None, ["map.png\nZ", "bigmap.txt", 1, 2], None, player, None)
    _COMMANDS["setminimap"](None, ["map.png", "big\x0amap.txt", 1, 2], None, player, None)
    await asyncio.sleep(0)
    player.send_raw.assert_not_awaited()


async def test_login_skips_a_map_option_with_a_control_byte(tmp_path):
    server = GameServer(ServerConfig.from_server_dir(str(tmp_path)))
    manager = AccountManager(server, str(tmp_path / "accounts"))
    server.account_manager = manager
    server.config.bigmap = "big\tmap.txt,bigmap.png,0,0"
    server.config.minimap = "minimap.txt,map2.png,1,2"
    account = Account("tester")
    account.set_password("pw")
    manager._save_account(account)
    manager._save_executor.shutdown(wait=True)
    player = make_player(server)
    player.logged_in = False
    player.session.read = AsyncMock(return_value=login_frame(username="tester"))
    player.session.send_login_response = AsyncMock()
    player.warp = AsyncMock()
    try:
        assert await player._handle_login()
    finally:
        manager._save_executor.shutdown(wait=True)
    lead = player.session.send_login_response.await_args.args[0]
    assert b"big\tmap" not in lead
    assert bytes([PLO.BIGMAP + 32]) + b"big" not in lead
    assert build_minimap("minimap.txt,map2.png,1,2") in lead


def test_refused_map_text_is_logged_once_per_value(caplog):
    from pygserver.protocol.builders import world as world_builders
    world_builders._REFUSED_MAP_TEXTS.clear()
    for _ in range(3):
        build_bigmap("one\n,b,c,d")
        build_minimap("one\n,b,c,d")
    build_minimap("two\t,b,c,d")
    refused = [r for r in caplog.records if "control byte" in r.getMessage()]
    assert len(refused) == 2
    assert all(r.levelname == "WARNING" for r in refused)
    world_builders._REFUSED_MAP_TEXTS.update(
        f"x{i}" for i in range(world_builders._MAX_REFUSED_MAP_TEXTS))
    build_bigmap("three\n")                       # bounded: not remembered
    assert "'three\\n'" not in world_builders._REFUSED_MAP_TEXTS
    world_builders._REFUSED_MAP_TEXTS.clear()
