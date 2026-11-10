"""Byte-level regression tests for RC response layouts."""

import asyncio
from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pygserver.filesystem import FileInfo, FileSystem
from pygserver.player import Player
from pygserver.protocol import PacketBuilder
from pygserver.protocol.builders.admin import (
    build_rc_account_get,
    build_rc_file_browser_dir,
    build_rc_player_ban,
    build_rc_player_props,
    build_rc_server_flags,
)
from pygserver.protocol.constants import PLI, PLO, PLPERM
from pygserver.rc import RCManager, RCSession


def _gchar(value):
    return bytes([value + 32])


def _gshort(value):
    return bytes([(value >> 7) + 32, (value & 0x7f) + 32])


def _gint5(value):
    return bytes([
        (value >> 28) + 32,
        ((value >> 21) & 0x7f) + 32,
        ((value >> 14) & 0x7f) + 32,
        ((value >> 7) & 0x7f) + 32,
        (value & 0x7f) + 32,
    ])


def _gstring(value):
    raw = value.encode("latin-1")
    return _gchar(len(raw)) + raw


def test_server_flags_packet_matches_counted_reference_layout():
    expected = (
        _gchar(PLO.RC_SERVERFLAGSGET) + _gshort(2)
        + _gstring("weather=sunny") + _gstring("events=on") + b"\n"
    )
    assert build_rc_server_flags({"weather": "sunny", "events": "on"}) == expected


def test_player_props_packet_matches_reference_layout():
    props = _gchar(0) + _gstring("Hero") + _gchar(4) + _gchar(6)
    expected = (
        _gchar(PLO.RC_PLAYERPROPSGET) + _gshort(17)
        + _gstring("alice") + _gstring("main") + _gchar(len(props)) + props
        + _gshort(1) + _gstring("quest=done")
        + _gshort(1) + _gchar(len("level1.nw") + 2)
        + _gchar(10) + _gchar(20) + b"level1.nw"
        + _gchar(2) + _gstring("Bow") + _gstring("Bomb") + b"\n"
    )
    assert build_rc_player_props(
        17, "alice", props, {"quest": "done"},
        [("level1.nw", 10, 20)], ["Bow", "Bomb"]
    ) == expected


def test_player_ban_packet_matches_reference_layout():
    expected = (
        _gchar(PLO.RC_PLAYERBANGET) + _gstring("alice")
        + _gchar(1) + b"Repeated abuse\n"
    )
    assert build_rc_player_ban("alice", True, "Repeated abuse") == expected


def test_account_get_packet_matches_reference_layout():
    expected = (
        _gchar(PLO.RC_ACCOUNTGET) + _gstring("alice") + _gstring("")
        + _gstring("a@example.test") + _gchar(1) + _gchar(0) + _gchar(0)
        + _gstring("main") + _gstring("30 days") + _gstring("abuse") + b"\n"
    )
    assert build_rc_account_get(
        "alice", "a@example.test", True, False, "30 days", "abuse"
    ) == expected


def test_file_browser_directory_packet_matches_block_reference_layout():
    info = FileInfo("readme.txt", 321, False, 123456)
    block = _gstring("readme.txt") + _gstring("rw") + _gint5(321) + _gint5(123456)
    expected = (
        _gchar(PLO.RC_FILEBROWSER_DIR) + _gstring("levels")
        + b" " + _gchar(len(block)) + block + b"\n"
    )
    assert build_rc_file_browser_dir("levels", [info]) == expected


def test_file_browser_start_sends_folders_welcome_and_initial_listing(tmp_path):
    server = MagicMock()
    server.account_manager.get_account.return_value = SimpleNamespace(
        folders=["rw levels/*", "r images/*"])
    server.filesystem.base_path = tmp_path
    server.filesystem.list_directory.return_value = []
    manager = RCManager(server)
    player = SimpleNamespace(account_name="admin", send_raw=AsyncMock())
    session = RCSession(player=player, rights=PLPERM.VIEWATTRIBUTES)

    asyncio.run(manager._handle_file_browser_start(session, b""))

    assert [call.args[0] for call in player.send_raw.await_args_list] == [
        _gchar(PLO.RC_FILEBROWSER_DIRLIST)
        + b'"rw levels/*","r images/*"\n',
        _gchar(PLO.RC_FILEBROWSER_MESSAGE)
        + b"Welcome to the File Browser.\n",
        _gchar(PLO.RC_FILEBROWSER_DIR) + _gstring("images") + b"\n",
    ]


def test_file_browser_cd_sends_entered_folder_listing(tmp_path):
    server = MagicMock()
    server.filesystem.list_directory.return_value = [
        FileInfo("map.nw", 42, False, 1000)
    ]
    server.filesystem.base_path = tmp_path
    server.account_manager.get_account.return_value = SimpleNamespace(
        folders=["rw levels/*"])
    manager = RCManager(server)
    player = SimpleNamespace(account_name="admin", send_raw=AsyncMock())
    session = RCSession(
        player=player, rights=PLPERM.VIEWATTRIBUTES,
        file_browser_active=True,
    )
    block = _gstring("map.nw") + _gstring("rw") + _gint5(42) + _gint5(1000)

    asyncio.run(manager._handle_file_browser_cd(session, b"levels"))

    assert player.send_raw.await_args.args[0] == (
        _gchar(PLO.RC_FILEBROWSER_DIR) + _gstring("levels")
        + b" " + _gchar(len(block)) + block + b"\n"
    )


def _upload_manager(tmp_path, max_upload_size=1024):
    server = SimpleNamespace()
    server.filesystem = FileSystem(server, str(tmp_path))
    server.account_manager = MagicMock()
    server.account_manager.get_account.return_value = SimpleNamespace(
        folders=["rw levels/*"])
    manager = RCManager(server)
    manager.max_upload_size = max_upload_size
    server.rc_manager = manager
    player = SimpleNamespace(
        id=7, account_name="admin", send_raw=AsyncMock())
    session = RCSession(
        player=player,
        rights=PLPERM.SETATTRIBUTES,
        file_browser_path="levels",
        file_browser_active=True,
    )
    manager._sessions[player.id] = session
    return server, manager, player, session


def test_file_browser_upload_raw_newline_round_trips(tmp_path):
    server, manager, player, session = _upload_manager(tmp_path)
    file_data = b"GLEVNW01\nBOARD 0 0 64 0\n\x00tail"
    payload = PacketBuilder().write_gstring("edited.nw").write_bytes(
        file_data).build()
    raw_packet = _gchar(PLI.RC_FILEBROWSER_UP) + payload
    announcement = (
        _gchar(PLI.RAWDATA)
        + PacketBuilder().write_gint3(len(raw_packet)).build()
        + b"\n"
    )
    connection = SimpleNamespace(
        id=player.id,
        server=server,
        _handlers={},
        _unhandled_packet_ids=set(),
        _raw_packet_size=None,
        _raw_packet_data=bytearray(),
    )
    connection._dispatch_packet = MethodType(Player._dispatch_packet, connection)

    async def upload():
        await Player._handle_packets(connection, announcement)
        await Player._handle_packets(connection, raw_packet)

    asyncio.run(upload())

    assert (tmp_path / "levels" / "edited.nw").read_bytes() == file_data
    assert player.send_raw.await_count == 2
    assert b"Uploaded file edited.nw." in player.send_raw.await_args_list[0].args[0]


def test_file_browser_upload_over_ceiling_replies_without_writing(tmp_path):
    server, manager, player, session = _upload_manager(
        tmp_path, max_upload_size=3)
    payload = PacketBuilder().write_gstring("large.nw").write_bytes(
        b"four").build()

    asyncio.run(manager._handle_file_browser_up(session, payload))

    assert not (tmp_path / "levels" / "large.nw").exists()
    assert player.send_raw.await_count == 1
    assert b"exceeds the maximum upload size of 3 bytes" in \
        player.send_raw.await_args.args[0]


def test_file_browser_upload_traversal_replies_without_writing(tmp_path):
    server, manager, player, session = _upload_manager(tmp_path)
    payload = PacketBuilder().write_gstring("../escaped.nw").write_bytes(
        b"lost work").build()

    asyncio.run(manager._handle_file_browser_up(session, payload))

    assert not (tmp_path / "escaped.nw").exists()
    assert player.send_raw.await_count == 1
    assert b"invalid filename ../escaped.nw" in player.send_raw.await_args.args[0]
