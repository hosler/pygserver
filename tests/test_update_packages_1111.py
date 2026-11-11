"""Update packages (`.gupd`): PLI 159, PLO 105/106/187, PLO_FILE mtime.

Reference (GServer-v2 origin/beta4 25396a18): PlayerClientPackets.cpp:1549-1595
(PLI 159), :1431-1466 (VERIFYWANTSEND), UpdatePackage.cpp:31-75 (FILE lines),
Player.cpp:543-654 (sendFile), Server.Init.cpp:451-495 (PLO 187).
"""

import asyncio
import os
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver import update_packages
from pygserver.config import ServerConfig
from pygserver.filesystem import FileSystem
from pygserver.protocol.constants import PLO, PLTYPE
from pygserver.protocol.packet_codec import PacketReader
from pygserver.protocol.packets import (
    PacketBuilder,
    build_file,
    build_update_package_done,
    build_update_package_is_updated,
    build_update_package_size,
)
from pygserver.update_packages import (
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
    MAX_REQUEST_CHECKSUMS,
    UpdatePackageManager,
    parse_package_file_names,
    parse_package_lines,
)

from .test_ap_system import make_player

VECTOR_159 = bytes.fromhex(
    "bf29626173652e6775706421" "2d95569d8f" "2020202020")
BODY_159 = VECTOR_159[1:]   # handlers get the body after the id byte


class Recorder:
    def __init__(self, player_id=5):
        self.id = player_id
        self.packets = []

    async def send_raw(self, packet):
        self.packets.append(packet)


@pytest.fixture
def world(tmp_path):
    server = SimpleNamespace(config=ServerConfig(), players={})
    server.rc_manager = SimpleNamespace(broadcast_chat=AsyncMock())
    fs = FileSystem(server, str(tmp_path))
    server.filesystem = fs
    return SimpleNamespace(root=tmp_path, server=server, fs=fs)


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("latin-1"))
    return path


def request(name, request_type=1, values=()):
    raw = name.encode("latin-1")
    builder = PacketBuilder().write_gchar(len(raw)).write_bytes(raw)
    builder.write_gchar(request_type)
    for value in values:
        builder.write_gint5(value)
    return builder.build()


def ids(packets):
    """Packet ids in send order, skipping the PLO_RAWDATA announcement."""
    out = []
    for packet in packets:
        while packet:
            pid = packet[0] - 32
            if pid == PLO.RAWDATA:
                size = PacketReader(packet[1:4]).read_gint3()
                packet = packet[5:]
                body, packet = packet[:size], packet[size:]
                out.append((PLO.FILE, body))
                continue
            end = packet.index(b"\n") + 1
            out.append((pid, packet[:end]))
            packet = packet[end:]
    return out


def sent_files(packets):
    names = []
    for pid, body in ids(packets):
        if pid == PLO.FILE:
            reader = PacketReader(body[1:])
            reader.read_gint5()
            names.append(reader.read_string(reader.read_gchar()))
    return names


def run_request(world, data, player=None):
    player = player or Recorder()
    asyncio.run(world.fs.packages.handle_request(player, data))
    return player


# -- wire vectors ---------------------------------------------------------------

def test_reference_vectors():
    assert VECTOR_159[0] - 32 == 159
    assert UpdatePackageManager.parse_request(BODY_159) == (
        "base.gupd", 1, [0xDEADBEEF, 0])
    assert build_update_package_size("base.gupd", 1000) == bytes.fromhex(
        "8929626173652e67757064202020278" "80a")
    assert build_update_package_done("base.gupd") == bytes.fromhex(
        "8a626173652e677570640a")
    assert build_update_package_is_updated("x.png") == bytes.fromhex(
        "db782e706e670a")


def test_plo_file_carries_the_real_mtime(world):
    path = put(world.root / "images" / "pic.png", b"png")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    player = Recorder()
    asyncio.run(world.fs.handle_want_file(player, "pic.png"))
    (_pid, body), = ids(player.packets)
    assert body == build_file("pic.png", b"png", 1_700_000_000)


# -- package model --------------------------------------------------------------

def test_file_lines_basename_order_dedupe_and_client_slots():
    text = ("GRPKG001\nNAME Base\nFILE levels/a.png\nfile b.png\r\n"
            "FILE  c:\\x\\a.png\nFILE \nFILEc.png\nDESCRIPTION\nFILE d.png\n")
    assert parse_package_lines(text) == [
        ("a.png", True), ("b.png", False), ("a.png", True), ("", False),
        ("c.png", False), ("d.png", True)]
    assert parse_package_file_names(text) == ["a.png", "b.png", "c.png", "d.png"]


def test_file_line_cap():
    text = "".join(f"FILE f{i}.png\n" for i in range(MAX_PACKAGE_FILES + 50))
    assert len(parse_package_file_names(text)) == MAX_PACKAGE_FILES


def test_reinstall_sends_everything_in_package_order(world):
    put(world.root / "images" / "a.png", b"A" * 10)
    put(world.root / "sounds" / "b.wav", b"B" * 20)
    put(world.root / "world" / "base.gupd",
        "GRPKG001\nFILE images/b.wav\nFILE x/a.png\nFILE a.png\n")
    player = run_request(world, request("base.gupd", 2, [1, 2]))
    packets = ids(player.packets)
    assert packets[0][1] == build_update_package_size("base.gupd", 30)
    assert sent_files(player.packets) == ["b.wav", "a.png"]
    assert packets[-1][1] == build_update_package_done("base.gupd")


def test_install_compares_kth_value_by_crc_or_mtime(world):
    a = put(world.root / "images" / "a.png", b"A" * 10)
    b = put(world.root / "images" / "b.png", b"B" * 20)
    put(world.root / "images" / "c.png", b"C" * 30)
    put(world.root / "images" / "e.png", b"")
    os.utime(b, (1_600_000_000, 1_600_000_000))
    put(world.root / "world" / "p.gupd",
        "FILE a.png\nFILE b.png\nFILE c.png\nFILE e.png\n")
    crc_a = zlib.crc32(a.read_bytes())
    # a: CRC matches, b: mtime matches, c: stale, e: empty file but the
    # client's "five spaces" (0) never counts as current.
    player = run_request(world, request(
        "p.gupd", 1, [crc_a, 1_600_000_000, 12345, 0]))
    assert sent_files(player.packets) == ["c.png", "e.png"]
    assert ids(player.packets)[0][1] == build_update_package_size("p.gupd", 30)


def test_slots_follow_client_numbering(world):
    a = put(world.root / "images" / "a.png", b"A")
    put(world.root / "images" / "b.png", b"B")
    # `file b.png` is read by the server but not counted by the client
    # (TUpdatePackage.cpp:407), so the client's first value is a.png's.
    put(world.root / "world" / "p.gupd", "file b.png\nFILE a.png\n")
    player = run_request(world, request(
        "p.gupd", 1, [zlib.crc32(a.read_bytes())]))
    assert sent_files(player.packets) == ["b.png"]


def test_type_byte_out_of_range_compares_like_install(world):
    a = put(world.root / "images" / "a.png", b"A")
    put(world.root / "world" / "p.gupd", "FILE a.png\n")
    crc = zlib.crc32(a.read_bytes())
    for type_byte in (0x00, 0x05, 0x20, 0xFF):
        data = b"\x26p.gupd" + bytes([type_byte]) + PacketBuilder().write_gint5(crc).build()
        player = run_request(world, data)
        assert sent_files(player.packets) == [], type_byte
        assert ids(player.packets)[-1][0] == PLO.UPDATEPACKAGEDONE


def test_unknown_package_answers_size_zero_then_done(world):
    player = run_request(world, request("missing.gupd", 2))
    assert player.packets == [build_update_package_size("missing.gupd", 0),
                              build_update_package_done("missing.gupd")]


@pytest.mark.parametrize("name", [
    "../config/serveroptions.gupd", "world/base.gupd", "..\\base.gupd",
    "/etc/base.gupd", ".base.gupd", "base.gupd\x00", "base..gupd",
    "pic.png", ".gupd", "C:base.gupd/", "base.gupd\n",
])
def test_hostile_package_names_are_unknown(world, name):
    put(world.root / "world" / "base.gupd", "FILE pic.png\n")
    put(world.root / "config" / "serveroptions.gupd", "FILE pic.png\n")
    put(world.root / "images" / "pic.png", b"P")
    player = run_request(world, request(name, 2))
    assert sent_files(player.packets) == []
    assert player.packets[0] == build_update_package_size(name, 0)


def test_hostile_file_lines_resolve_to_public_basenames_only(world):
    put(world.root / "config" / "serveroptions.txt", "SECRET")
    put(world.root / "accounts" / "admin.json", "SECRET")
    put(world.root / "images" / "pic.png", b"P")
    put(world.root / "world" / "p.gupd", "\n".join([
        "FILE ../config/serveroptions.txt", "FILE ../../../etc/passwd",
        "FILE accounts/admin.json", "FILE ..", "FILE .", "FILE images/..",
        "FILE C:\\evil\\pic.png", "FILE /abs/pic.png", "FILE pic.png\x00x",
    ]) + "\n")
    player = run_request(world, request("p.gupd", 2))
    assert sent_files(player.packets) == ["pic.png"]
    notices = [call.args[0] for call in
               world.server.rc_manager.broadcast_chat.await_args_list]
    assert "[Server]: Unable to find file 'serveroptions.txt' in package 'p.gupd'" in notices
    assert "[Server]: Unable to find file 'admin.json' in package 'p.gupd'" in notices
    assert "[Server]: Unable to find file 'passwd' in package 'p.gupd'" in notices


def test_file_entry_must_match_case_exactly(world):
    put(world.root / "images" / "Pic.png", b"P")
    put(world.root / "world" / "p.gupd", "FILE pic.png\n")
    player = run_request(world, request("p.gupd", 2))
    assert sent_files(player.packets) == []
    world.server.rc_manager.broadcast_chat.assert_awaited_once_with(
        "[Server]: Unable to find file 'pic.png' in package 'p.gupd'")


def test_symlinks_leaving_the_server_are_refused(world, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    secret = put(outside / "secret.png", b"SECRET")
    put(outside / "evil.gupd", "FILE pic.png\n")
    (world.root / "images").mkdir(exist_ok=True)
    (world.root / "images" / "secret.png").symlink_to(secret)
    (world.root / "images" / "evil.gupd").symlink_to(outside / "evil.gupd")
    put(world.root / "images" / "pic.png", b"P")
    config = put(world.root / "config" / "serveroptions.txt", "SECRET")
    (world.root / "images" / "opts.png").symlink_to(config)
    put(world.root / "world" / "p.gupd", "FILE secret.png\nFILE opts.png\n")
    assert sent_files(run_request(world, request("p.gupd", 2)).packets) == []
    player = run_request(world, request("evil.gupd", 2))
    assert player.packets[0] == build_update_package_size("evil.gupd", 0)


def test_oversized_package_and_request(world):
    put(world.root / "images" / "a.png", b"A")
    put(world.root / "world" / "big.gupd",
        "FILE a.png\n" + "#" * (MAX_PACKAGE_BYTES + 1))
    put(world.root / "world" / "p.gupd", "FILE a.png\n")
    player = run_request(world, request("big.gupd", 2))
    assert player.packets[0] == build_update_package_size("big.gupd", 0)

    data = request("p.gupd", 1, [7] * (MAX_REQUEST_CHECKSUMS * 4))
    assert len(UpdatePackageManager.parse_request(data)[2]) == MAX_REQUEST_CHECKSUMS
    assert sent_files(run_request(world, data).packets) == ["a.png"]


@pytest.mark.parametrize("data", [
    b"", b"\x29base", b"\x29base.gupd", b"\xffx",
])
def test_truncated_requests_are_dropped(world, data):
    put(world.root / "world" / "base.gupd", "FILE a.png\n")
    assert run_request(world, data).packets == []


def test_trailing_partial_value_is_ignored(world):
    a = put(world.root / "images" / "a.png", b"A")
    put(world.root / "world" / "p.gupd", "FILE a.png\n")
    whole = PacketBuilder().write_gint5(zlib.crc32(a.read_bytes())).build()
    assert sent_files(run_request(
        world, b"\x26p.gupd!" + whole[:4]).packets) == ["a.png"]


def test_package_and_entries_reload_when_modified(world):
    a = put(world.root / "images" / "a.png", b"A")
    put(world.root / "images" / "b.png", b"BB")
    gupd = put(world.root / "world" / "p.gupd", "FILE a.png\n")
    crc = zlib.crc32(b"A")
    assert sent_files(run_request(world, request("p.gupd", 1, [crc])).packets) == []
    a.write_bytes(b"AAAA")
    os.utime(a, ns=(a.stat().st_mtime_ns + 10**9,) * 2)
    assert sent_files(run_request(world, request("p.gupd", 1, [crc])).packets) == ["a.png"]
    gupd.write_text("FILE a.png\nFILE b.png\n")
    os.utime(gupd, ns=(gupd.stat().st_mtime_ns + 10**9,) * 2)
    player = run_request(world, request("p.gupd", 1, [zlib.crc32(b"AAAA")]))
    assert sent_files(player.packets) == ["b.png"]


def test_large_entry_uses_the_large_file_path(world):
    put(world.root / "images" / "big.png", b"x" * 40000)
    put(world.root / "world" / "p.gupd", "FILE big.png\n")
    player = run_request(world, request("p.gupd", 2))
    seen = [pid for pid, _ in ids(player.packets)]
    assert seen[:3] == [PLO.UPDATEPACKAGESIZE, PLO.LARGEFILESTART, PLO.LARGEFILESIZE]
    assert seen[-2:] == [PLO.LARGEFILEEND, PLO.UPDATEPACKAGEDONE]


def test_verify_want_send_always_resends_a_package(world):
    gupd = put(world.root / "world" / "base.gupd", "GRPKG001\n")
    player = Recorder()
    asyncio.run(world.fs.handle_verify_want_send(
        player, zlib.crc32(gupd.read_bytes()), "base.gupd"))
    assert sent_files(player.packets) == ["base.gupd"]


# -- dispatch -------------------------------------------------------------------

async def test_pli_159_dispatches_for_game_clients_only(tmp_path):
    server = MagicMock()
    server.config = ServerConfig()
    server.filesystem = FileSystem(server, str(tmp_path))
    put(tmp_path / "images" / "a.png", b"A")
    put(tmp_path / "world" / "base.gupd", "FILE a.png\n")
    player = make_player(server)
    player.send_raw = AsyncMock()
    await player._dispatch_packet(159, BODY_159)
    packets = [call.args[0] for call in player.send_raw.await_args_list]
    assert packets[0] == build_update_package_size("base.gupd", 1)
    assert sent_files(packets) == ["a.png"]
    assert packets[-1] == build_update_package_done("base.gupd")

    rc = make_player(server, player_id=3)
    rc.connection_type = PLTYPE.RC
    rc.send_raw = AsyncMock()
    await rc._dispatch_packet(159, BODY_159)
    rc.send_raw.assert_not_awaited()


# -- PLO 187 --------------------------------------------------------------------

async def test_modified_file_notifies_clients_that_fetched_it(world):
    put(world.root / "images" / "x.png", b"1")
    fetched, other = Recorder(1), Recorder(2)
    await world.fs.handle_want_file(fetched, "x.png")
    await world.fs.handle_want_file(other, "y.png")    # missing: not recorded
    fetched.packets.clear()
    other.packets.clear()
    assert world.fs.write_file("images/x.png", b"2")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert fetched.packets == [build_update_package_is_updated("x.png")]
    assert other.packets == []


async def test_no_187_for_packages_private_files_or_classic(world):
    put(world.root / "images" / "x.png", b"1")
    put(world.root / "world" / "p.gupd", "")
    player = Recorder()
    await world.fs.handle_want_file(player, "x.png")
    world.fs.remember_sent_file(player, "p.gupd")
    world.fs.remember_sent_file(player, "serveroptions.txt")
    player.packets.clear()
    world.fs.write_file("world/p.gupd", b"FILE x.png\n")
    world.fs.write_file("config/serveroptions.txt", b"x")
    world.server.config.generation = "classic"
    world.fs.write_file("images/x.png", b"2")
    for _ in range(3):
        await asyncio.sleep(0)
    assert player.packets == []


# -- review fixes (2026-11-11) ---------------------------------------------------

def sends_secret(world, name="p.gupd"):
    return any(b"SECRET" in packet
               for packet in run_request(world, request(name, 2)).packets)


def test_removed_share_is_not_served_by_a_cached_package(world):
    put(world.root / "share1" / "secret.png", b"SECRET")
    put(world.root / "world" / "p.gupd", "FILE secret.png\n")
    world.server.config.nofoldersconfig = True
    world.server.config.sharefolder = ["share1"]
    assert sends_secret(world)
    world.server.config.sharefolder = []
    assert world.fs._find_file("secret.png") is None
    assert not sends_secret(world)


def test_nofoldersconfig_flip_is_honoured_by_a_cached_package(world):
    put(world.root / "share1" / "secret.png", b"SECRET")
    put(world.root / "world" / "p.gupd", "FILE secret.png\n")
    world.server.config.nofoldersconfig = True
    world.server.config.sharefolder = ["share1"]
    assert sends_secret(world)
    world.server.config.nofoldersconfig = False
    assert not sends_secret(world)


def test_narrowed_foldersconfig_is_honoured_by_a_cached_package(world):
    (world.root / "world" / "extras").mkdir(parents=True)
    put(world.root / "extras" / "secret.png", b"SECRET")
    put(world.root / "world" / "p.gupd", "FILE secret.png\n")
    put(world.root / "config" / "foldersconfig.txt", "file extras/*.png\n")
    world.fs.invalidate_folders_config()
    assert sends_secret(world)
    put(world.root / "config" / "foldersconfig.txt", "level *.nw\n")
    world.fs.invalidate_folders_config()      # what RC_FOLDERCONFIGSET does
    assert not sends_secret(world)


def test_moved_file_is_resolved_again(world):
    world.fs.INDEX_RESCAN_SECONDS = 0
    world.fs._MTIME_SETTLE_NS = 0
    path = put(world.root / "images" / "m.png", b"SECRET1")
    put(world.root / "world" / "p.gupd", "FILE m.png\n")
    assert sends_secret(world)
    (world.root / "sounds").mkdir(exist_ok=True)
    moved = path.rename(world.root / "sounds" / "m.png")
    player = run_request(world, request("p.gupd", 2))
    assert sent_files(player.packets) == ["m.png"]
    (world.root / "accounts").mkdir(exist_ok=True)
    moved.rename(world.root / "accounts" / "m.png")      # private now
    assert not sends_secret(world)


def test_entry_over_max_file_size_is_left_out(world, caplog):
    world.fs.max_file_size = 5
    big = put(world.root / "images" / "big.png", b"B" * 10)
    put(world.root / "images" / "ok.png", b"OK")
    put(world.root / "world" / "p.gupd", "FILE big.png\nFILE ok.png\n")
    for _ in range(2):
        player = run_request(world, request("p.gupd", 2))
        assert [pid for pid, _ in ids(player.packets)] == [
            PLO.UPDATEPACKAGESIZE, PLO.FILE, PLO.UPDATEPACKAGEDONE]
        assert player.packets[0] == build_update_package_size("p.gupd", 2)
    assert str(big) not in world.fs.packages._crc_cache     # never checksummed
    assert sum("big.png is over" in r.getMessage() for r in caplog.records) == 1


async def test_stalled_rc_notice_does_not_block_package_requests(world):
    stall = asyncio.Event()

    async def stuck(message):
        await stall.wait()
    world.server.rc_manager = SimpleNamespace(broadcast_chat=stuck)
    put(world.root / "images" / "a.png", b"A")
    put(world.root / "world" / "p.gupd", "FILE a.png\nFILE gone.png\n")
    first, second = Recorder(1), Recorder(2)
    await asyncio.wait_for(world.fs.packages.handle_request(
        first, request("p.gupd", 2)), 2)
    await asyncio.wait_for(world.fs.packages.handle_request(
        second, request("p.gupd", 2)), 2)
    assert sent_files(first.packets) == sent_files(second.packets) == ["a.png"]
    stall.set()


@pytest.mark.parametrize("generation,sent", [
    ("", True), ("modern", True), (" Modern ", True),
    ("newmain", False), ("classic", False)])
async def test_187_follows_the_modern_generation_rule(world, generation, sent):
    put(world.root / "images" / "x.png", b"1")
    player = Recorder()
    await world.fs.handle_want_file(player, "x.png")
    player.packets.clear()
    world.server.config.generation = generation
    await world.fs.notify_file_modified(world.root / "images" / "x.png")
    assert player.packets == (
        [build_update_package_is_updated("x.png")] if sent else [])


# -- review fixes, round 2: cost of misses and the per-player throttle -----------

def test_many_missing_entries_cost_one_tree_check_per_window(world):
    for index in range(300):
        (world.root / "world" / f"d{index}").mkdir()
    put(world.root / "world" / "big.gupd",
        "".join(f"FILE miss{index}.png\n" for index in range(1024)))
    calls = []
    real = world.fs._public_tree_changed

    def counted():
        calls.append(1)
        return real()
    world.fs._public_tree_changed = counted
    world.fs._public_index_time -= 10           # rescan window open
    run_request(world, request("big.gupd", 2))
    assert len(calls) <= 1
    calls.clear()
    started = __import__("time").perf_counter()
    run_request(world, request("big.gupd", 2))
    assert calls == []                          # inside the window: none
    assert __import__("time").perf_counter() - started < 0.25
    assert {f"miss{index}.png" for index in range(1024)} <= world.fs._miss_cache


def test_miss_cache_is_dropped_when_the_index_changes(world):
    put(world.root / "world" / "p.gupd", "FILE late.png\n")
    assert sent_files(run_request(world, request("p.gupd", 2)).packets) == []
    assert "late.png" in world.fs._miss_cache
    put(world.root / "images" / "late.png", b"L")
    world.fs._public_index_time -= 10
    world.fs._public_signature = None           # the tree changed
    assert sent_files(run_request(world, request("p.gupd", 2)).packets) == ["late.png"]
    assert "late.png" not in world.fs._miss_cache
    world.server.config.nofoldersconfig = True  # folder options change: dropped
    world.fs._find_file("anything.png")
    assert world.fs._miss_cache == set()


async def test_back_to_back_requests_are_all_answered_in_order(world, monkeypatch):
    sleeps = []
    clock = [1000.0]

    async def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(update_packages, "asyncio", SimpleNamespace(
        sleep=fake_sleep, get_running_loop=asyncio.get_running_loop,
        Lock=asyncio.Lock))
    monkeypatch.setattr(update_packages, "time",
                        SimpleNamespace(monotonic=lambda: clock[0]))
    for index in range(6):
        put(world.root / "world" / f"p{index}.gupd", "")
    player = Recorder()
    for index in range(6):
        await world.fs.packages.handle_request(player, request(f"p{index}.gupd", 2))
    assert player.packets == [
        packet for index in range(6) for packet in (
            build_update_package_size(f"p{index}.gupd", 0),
            build_update_package_done(f"p{index}.gupd"))]
    # four free, then each waits out one token (0.5 s at 2/s)
    assert sleeps == [1.0 / update_packages.REQUEST_RATE] * 2


async def test_disconnect_during_the_wait_ends_cleanly(world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(update_packages, "time",
                        SimpleNamespace(monotonic=lambda: clock[0]))
    put(world.root / "world" / "p.gupd", "")
    player = Recorder()
    for _ in range(update_packages.REQUEST_BURST):
        await world.fs.packages.handle_request(player, request("p.gupd", 2))
    player.packets.clear()
    waiting = asyncio.create_task(
        world.fs.packages.handle_request(player, request("p.gupd", 2)))
    await asyncio.sleep(0.01)
    assert not waiting.done()                 # waiting for a token
    waiting.cancel()                          # the connection's task ends
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert player.packets == []
    # a player marked gone while waiting gets nothing either
    player.connected = False
    await asyncio.wait_for(world.fs.packages.handle_request(
        player, request("p.gupd", 2)), 2)
    assert player.packets == []


async def test_rc_upload_is_findable_at_once(world):
    put(world.root / "world" / "p.gupd", "FILE new.png\n")
    await world.fs.packages.handle_request(Recorder(), request("p.gupd", 2))
    assert "new.png" in world.fs._miss_cache
    world.fs._find_file("absent.png")         # a check: window now closed
    assert world.fs.write_file("images/new.png", b"NEW")
    assert world.fs._find_file("new.png") == world.root / "images" / "new.png"
    player = Recorder()
    await world.fs.handle_want_file(player, "new.png")
    assert sent_files(player.packets) == ["new.png"]
    player = Recorder(9)
    await world.fs.packages.handle_request(player, request("p.gupd", 2))
    assert sent_files(player.packets) == ["new.png"]
