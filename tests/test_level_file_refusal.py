"""Level files are refused by the file-request path.

Reference: GServer-v2 origin/beta4 server/src/player/Player.cpp:561-563
(`Player::sendFile`, "Don't let the client request level files"): the first
check, before the known-file record and any lookup, answers PLO_FILESENDFAILED
with the requested name when the path's extension is exactly `.nw`, `.graal` or
`.zelda` (std::filesystem extension, case-sensitive). Callers: PLI_WANTFILE
(PlayerClientPackets.cpp:798-811), the stale branch of PLI_UPDATEFILE (:949) and
PLI_VERIFYWANTSEND (:1469), update packages (:1597), RC download
(PlayerRCPackets.cpp:1280) and the gmap send on warp (PlayerClient.cpp:1253,
a `.gmap`, never refused). Level data reaches clients as board packets.
"""

import asyncio
from types import SimpleNamespace

import pytest

from pygserver.config import ServerConfig
from pygserver.filesystem import FileSystem
from pygserver.level import Level
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import build_file, build_file_send_failed

from .test_level_entry_cache import (
    MOD_TIME, ids, make_player, make_server, run, write_level)

REFUSED = ["a.nw", "a.graal", "a.zelda", "sub/dir/a.nw", "dotted.name.nw"]
# Level extensions are compared exactly: these are served like any file.
SERVED = ["a.NW", "a.Zelda", "a.nwx", "a.gmap", "a.nw.txt"]
# No extension at all by std::filesystem rules.
NO_EXTENSION = ["nw", ".nw", "dir.nw/file", "a.nw/"]


class Recorder:
    def __init__(self):
        self.id = 5
        self.packets = []

    async def send_raw(self, packet):
        self.packets.append(packet)


@pytest.fixture
def fs(tmp_path):
    server = SimpleNamespace(config=ServerConfig(), players={})
    return FileSystem(server, str(tmp_path)), tmp_path


def put(path, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("name", REFUSED)
def test_want_file_refuses_each_level_extension(fs, name):
    filesystem, root = fs
    put(root / "levels" / name.rsplit("/", 1)[-1], b"GLEVNW01\n")
    player = Recorder()
    asyncio.run(filesystem.handle_want_file(player, name))
    # Player.cpp:557 `fs::getANSIFileName(file)` = the final component only.
    bare = name.rsplit("/", 1)[-1]
    assert player.packets == [build_file_send_failed(bare)]
    assert player.packets[0][0] - 32 == PLO.FILESENDFAILED
    assert player.packets[0][1:] == bare.encode() + b"\n"
    # The refusal comes before the known-file record.
    assert not filesystem.has_seen_file(player, name.rsplit("/", 1)[-1])


@pytest.mark.parametrize("name", ["a.nw", "a.graal", "a.zelda"])
def test_refused_even_when_the_file_is_missing_or_large(fs, name):
    """Same reply as a missing file, and a level above the large-file
    threshold is not chunked either."""
    filesystem, root = fs
    filesystem.large_file_threshold = 4
    put(root / "levels" / name, b"x" * 100)
    for requested in (name, "nothere" + name[1:]):
        player = Recorder()
        asyncio.run(filesystem.handle_want_file(player, requested))
        assert player.packets == [build_file_send_failed(requested)]


def test_missing_file_failure_carries_the_bare_name_too(fs):
    """The non-refused failure path of sendFile uses the same filename."""
    filesystem, _ = fs
    player = Recorder()
    asyncio.run(filesystem.handle_want_file(player, "sub/dir/nothere.png"))
    assert player.packets == [build_file_send_failed("nothere.png")]


@pytest.mark.parametrize("name", SERVED)
def test_other_names_are_not_refused(fs, name):
    filesystem, root = fs
    put(root / "images" / name.rsplit("/", 1)[-1], b"payload")
    player = Recorder()
    asyncio.run(filesystem.handle_want_file(player, name))
    assert len(player.packets) == 1
    mtime = int((root / "images" / name).stat().st_mtime)
    assert build_file(name, b"payload", mtime) in player.packets[0]


@pytest.mark.parametrize("name", NO_EXTENSION)
def test_names_without_a_path_extension_are_not_level_files(name):
    assert not FileSystem.is_level_file_name(name)


def test_a_plain_file_is_still_served(fs):
    filesystem, root = fs
    path = put(root / "images" / "pic.png", b"png")
    player = Recorder()
    asyncio.run(filesystem.handle_want_file(player, "pic.png"))
    assert len(player.packets) == 1
    assert build_file("pic.png", b"png", int(path.stat().st_mtime)) in player.packets[0]
    assert filesystem.has_seen_file(player, "pic.png")


def test_update_file_with_a_stale_time_is_refused_and_a_current_time_is_up_to_date(fs):
    filesystem, root = fs
    path = put(root / "levels" / "a.nw", b"GLEVNW01\n")
    stale = Recorder()
    asyncio.run(filesystem.handle_update_file(stale, 1, "a.nw"))
    assert stale.packets == [build_file_send_failed("a.nw")]
    current = Recorder()
    asyncio.run(filesystem.handle_update_file(
        current, int(path.stat().st_mtime), "a.nw"))
    assert current.packets[0][0] - 32 == PLO.FILEUPTODATE


def test_verify_want_send_with_a_wrong_checksum_is_refused(fs):
    filesystem, root = fs
    put(root / "levels" / "a.nw", b"GLEVNW01\n")
    player = Recorder()
    asyncio.run(filesystem.handle_verify_want_send(player, 12345, "a.nw"))
    assert player.packets == [build_file_send_failed("a.nw")]


def test_send_resolved_file_and_send_file_refuse_levels(fs):
    filesystem, root = fs
    path = put(root / "levels" / "a.graal", b"GLEVNW01\n")
    player = Recorder()
    asyncio.run(filesystem.send_resolved_file(player, "a.graal", path))
    assert player.packets == [build_file_send_failed("a.graal")]
    player = Recorder()
    asyncio.run(filesystem.send_file(player, "a.zelda"))
    assert player.packets == [build_file_send_failed("a.zelda")]


def test_entering_a_level_still_delivers_its_board(tmp_path):
    server = make_server(tmp_path)
    server.world.add_level(Level.load(str(write_level(tmp_path, "a.nw", MOD_TIME))))
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    sent_ids = ids(player)
    assert PLO.BOARDPACKET in sent_ids
    assert PLO.FILESENDFAILED not in sent_ids


def _rc_download(tmp_path, name):
    """Run RC_FILEBROWSER_DOWN for `name` in the `levels` folder; return the
    packets the RC player received."""
    from unittest.mock import MagicMock
    from pygserver.protocol.constants import PLPERM
    from pygserver.rc import RCManager, RCSession

    server = MagicMock()
    server.config = ServerConfig()
    server.filesystem = FileSystem(server, str(tmp_path))
    server.account_manager.get_account.return_value = SimpleNamespace(
        folders=["rw levels/*"])
    player = Recorder()
    player.account_name = "admin"
    session = RCSession(
        player=player, rights=PLPERM.VIEWATTRIBUTES,
        file_browser_active=True, file_browser_path="levels")
    asyncio.run(RCManager(server)._handle_file_browser_down(
        session, name.encode("latin-1")))
    return player.packets


def test_rc_download_of_a_level_file_is_refused(tmp_path):
    # PlayerRCPackets.cpp:1280 calls sendFile, whose first check is
    # Player.cpp:561-563.
    put(tmp_path / "levels" / "a.nw", b"GLEVNW01\n")
    assert _rc_download(tmp_path, "a.nw") == [build_file_send_failed("a.nw")]


def test_rc_download_with_a_directory_part_never_reaches_sendfile(tmp_path):
    """The RC browser (a hardening beyond the reference) rejects any name with
    a separator before sendFile, so a directory part is answered with the
    browser message; the bare-name reply is covered below at the helper."""
    put(tmp_path / "levels" / "sub" / "a.nw", b"GLEVNW01\n")
    packets = _rc_download(tmp_path, "sub/a.nw")
    assert packets
    assert packets[0] != build_file_send_failed("a.nw")
    assert b"Invalid filename" in packets[0]


def test_failure_name_is_the_final_component_only():
    # File.cpp:51-58: filename() on the POSIX build; '/' only.
    f = FileSystem.failure_file_name
    assert f("sub/dir/a.nw") == "a.nw"
    assert f("a.nw") == "a.nw"
    assert f("dir/") == ""
    assert f("dir\\a.nw") == "dir\\a.nw"


def test_rc_missing_file_failure_uses_the_bare_name(tmp_path):
    (tmp_path / "levels").mkdir()
    assert _rc_download(tmp_path, "gone.txt") == [
        build_file_send_failed("gone.txt")]


def test_rc_download_of_a_plain_file_still_works(tmp_path):
    put(tmp_path / "levels" / "notes.txt", b"hello")
    packets = _rc_download(tmp_path, "notes.txt")
    assert packets
    assert packets[0] != build_file_send_failed("notes.txt")
    assert b"hello" in b"".join(packets)
