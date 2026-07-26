"""GS2 clientside compilation and bytecode-serving tests.

The wire-format cases decode packets the way pyReborn does (strip the packet id
and the trailing frame newline, then read the header/bytecode) rather than
importing pyReborn, since pygserver's CI only checks out reborn-protocol.
"""

import asyncio
from pathlib import Path

import pytest

from pygserver.gs2 import (
    GS2Compiler,
    GS2Script,
    GS2ScriptManager,
    minify,
    parse_gani_file,
    parse_weapon_file,
    split_clientside,
    to_csv,
)
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import (
    build_load_script_bytecode,
    build_load_script_header,
    build_npc_weapon_script,
    build_raw_data_announcement,
)

WEAPON_FIXTURE = Path(__file__).parent.parent / "weapons" / "weaponqa%095gs2vm.txt"
CLASS_FIXTURE = Path(__file__).parent.parent / "scripts" / "qa_gs2vmclass.txt"

# Bytecode stand-in that contains 0x0a in several places: the exact shape that
# truncates when PLO_NPCWEAPONSCRIPT is sent without PLO_RAWDATA framing.
BINARY_BYTECODE = bytes(range(256)) * 3

#: A PLO_RAWDATA announcement is always [gchar id][gint3 size][\n].
ANNOUNCEMENT_SIZE = len(build_raw_data_announcement(0))


class FakePlayer:
    """Collects what would go on the wire."""

    id = 7

    def __init__(self):
        self.sent = []

    async def send_raw(self, data: bytes):
        self.sent.append(data)


def strip_frame(packet: bytes, expected_id: int) -> bytes:
    """Undo the client's raw-data unwrapping: drop the id and trailing '\\n'."""
    assert packet[0] == expected_id + 32
    assert packet[-1:] == b"\n"
    return packet[1:-1]


def read_gshort(data: bytes, pos: int) -> int:
    return ((data[pos] - 32) << 7) + (data[pos + 1] - 32)


def run(coro):
    """Drive one coroutine to completion (pytest-asyncio is an optional dev dep
    and the rest of this suite is synchronous)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# =============================================================================
# Source splitting
# =============================================================================

def test_minify_drops_comments_but_keeps_directives():
    src = "a = 1; // trailing\n\n  //#CLIENTSIDE\n  b = 2;\n/* block\ncomment */\nc = 3;\n"

    # Two Script::minify quirks reproduced deliberately: only lines past the
    # marker get trimmed, and block comments are cut after the lines have been
    # joined, so removing one can leave a blank line behind.
    assert minify(src) == "a = 1; \n//#CLIENTSIDE\nb = 2;\n\nc = 3;"


def test_split_keeps_only_the_clientside_half():
    serverside, clientside = split_clientside(
        "setplayerprop #s,hi;\n//#CLIENTSIDE\nfunction onCreated() { x = 1; }\n")

    assert serverside == "setplayerprop #s,hi;"
    assert clientside == "function onCreated() { x = 1; }"


def test_split_without_marker_has_no_clientside():
    serverside, clientside = split_clientside("function onCreated() { x = 1; }\n")

    assert serverside == "function onCreated() { x = 1; }"
    assert clientside == ""


def test_split_marker_on_the_last_line_yields_empty_clientside():
    assert split_clientside("x = 1;\n//#CLIENTSIDE") == ("x = 1;", "")


# =============================================================================
# Weapon files and headers
# =============================================================================

def test_parse_weapon_file():
    name, image, script = parse_weapon_file(WEAPON_FIXTURE.read_text(encoding="latin-1"))

    assert (name, image) == ("qa_gs2vm", "qa.png")
    assert "function qaJoinHelper(n)" in script
    assert "SCRIPTEND" not in script


def test_parse_weapon_file_rejects_a_missing_magic_line():
    assert parse_weapon_file("REALNAME nope\n") is None


def test_to_csv_quotes_and_doubles_only_complex_fields():
    assert to_csv(["weapon", "name", "1"]) == "weapon,name,1"
    assert to_csv(['a,b']) == '"a,b"'
    assert to_csv(['a"b\\c']) == '"a""b\\\\c"'


def test_weapon_header_carries_a_decimal_crc_and_class_a_gint5_one():
    weapon = GS2Script(kind="weapon", name="w")
    weapon.build_headers("script body")
    klass = GS2Script(kind="class", name="c")
    klass.build_headers("script body")

    assert weapon.header_with_crc == weapon.header + "," + str(weapon.checksum)
    assert weapon.header_with_crc.rsplit(",", 1)[1].isdigit()
    # ScriptClass puts its GINT5 checksum in the one header it ever sends.
    assert klass.header == klass.header_with_crc
    assert len(klass.header.rsplit(",", 1)[1].strip('"')) == 5


# =============================================================================
# Wire format
# =============================================================================

def test_weapon_script_packet_survives_newlines_in_the_bytecode():
    packet = build_npc_weapon_script("weapon,qa,1,key", BINARY_BYTECODE)
    announcement = build_raw_data_announcement(len(packet))

    # The announcement is what makes the newline-bearing payload survive: the
    # client takes exactly this many bytes instead of reading to the first \n.
    assert announcement[0] == PLO.RAWDATA + 32
    size = (((announcement[1] - 32) << 14) + ((announcement[2] - 32) << 7)
            + (announcement[3] - 32))
    assert size == len(packet)

    body = strip_frame(packet, PLO.NPCWEAPONSCRIPT)
    header_len = read_gshort(body, 0)
    assert body[2:2 + header_len] == b"weapon,qa,1,key"
    assert body[2 + header_len:] == BINARY_BYTECODE


def test_load_script_bytecode_packet_round_trips():
    packet = build_load_script_bytecode("class,qa,1,key", BINARY_BYTECODE)

    body = strip_frame(packet, PLO.LOADSCRIPT)
    header_len = body[0] - 32
    assert body[1:1 + header_len] == b"class,qa,1,key"
    assert body[1 + header_len:] == BINARY_BYTECODE


def test_load_script_header_packet_is_bare_csv():
    packet = build_load_script_header("weapon,qa,1,key,123")

    assert strip_frame(packet, PLO.LOADSCRIPT) == b"weapon,qa,1,key,123"


# =============================================================================
# Manager
# =============================================================================

class FakeConfig:
    def __init__(self, weapons_dir):
        self.weapons_dir = str(weapons_dir)


class FakeFileSystem:
    """Only the read_file() lookup GS2ScriptManager uses to find ganis."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def read_file(self, filename):
        path = self.directory / filename
        return path.read_bytes() if path.is_file() else None


class FakeServer:
    def __init__(self, weapons_dir, gani_dir=None):
        self.config = FakeConfig(weapons_dir)
        self.filesystem = FakeFileSystem(gani_dir) if gani_dir else None


def make_manager(tmp_path, compiler_available=True):
    (tmp_path / "weapons").mkdir(exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    manager = GS2ScriptManager(
        FakeServer(tmp_path / "weapons", tmp_path / "gani"), str(tmp_path))
    if not compiler_available:
        manager.compiler.binary = None
    return manager


def write_fixtures(tmp_path):
    (tmp_path / "weapons").mkdir(exist_ok=True)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "weapons" / WEAPON_FIXTURE.name).write_bytes(WEAPON_FIXTURE.read_bytes())
    (tmp_path / "scripts" / CLASS_FIXTURE.name).write_bytes(CLASS_FIXTURE.read_bytes())


@pytest.mark.skipif(GS2Compiler().available is False,
                    reason="gs2test compiler not built")
def test_load_compiles_the_shipped_fixtures(tmp_path):
    write_fixtures(tmp_path)
    manager = make_manager(tmp_path)

    manager.load()

    weapon = manager.get_weapon("QA_GS2VM")     # lookup is case-insensitive
    assert weapon is not None and weapon.bytecode
    assert manager.get_class("qa_gs2vmclass").bytecode
    # The container's function table is the proof it really compiled.
    from reborn_protocol.gs2.container import parse_container
    assert {f.name for f in parse_container(weapon.bytecode).functions} == {
        "onCreated", "onTimeout", "onActionqa2relay", "qaSend", "qaJoinHelper"}


@pytest.mark.skipif(GS2Compiler().available is False,
                    reason="gs2test compiler not built")
def test_a_gs1_weapon_loads_without_bytecode_and_is_never_served(tmp_path):
    (tmp_path / "weapons").mkdir()
    (tmp_path / "weapons" / "weapongs1only.txt").write_text(
        "GRAWP001\nREALNAME gs1only\nIMAGE w.png\nSCRIPT\n//#CLIENTSIDE\n"
        "if (playerenters) { setstring gr.x,#v(playerx); }\nSCRIPTEND\n")
    manager = make_manager(tmp_path)
    player = FakePlayer()

    manager.load()

    assert manager.get_weapon("gs1only").bytecode == b""
    assert run(manager.send_weapon_bytecode(player, "gs1only")) is False
    assert run(manager.announce_weapon(player, "gs1only")) is False
    assert player.sent == []


def test_precompiled_bytecode_is_used_when_no_compiler_is_available(tmp_path):
    write_fixtures(tmp_path)
    (tmp_path / "weapons" / "weaponqa%095gs2vm.gs2bc").write_bytes(BINARY_BYTECODE)
    manager = make_manager(tmp_path, compiler_available=False)

    manager.load()

    assert manager.get_weapon("qa_gs2vm").bytecode == BINARY_BYTECODE
    assert manager.get_class("qa_gs2vmclass").bytecode == b""


def test_stale_cached_bytecode_is_ignored(tmp_path):
    write_fixtures(tmp_path)
    source = tmp_path / "weapons" / "weaponqa%095gs2vm.txt"
    cache = tmp_path / "weapons" / "weaponqa%095gs2vm.gs2bc"
    cache.write_bytes(BINARY_BYTECODE)
    import os
    os.utime(cache, (0, 0))
    manager = make_manager(tmp_path, compiler_available=False)

    manager.load()

    assert manager.get_weapon("qa_gs2vm").bytecode == b""
    assert source.exists()


def test_send_weapon_bytecode_announces_the_packet_length(tmp_path):
    write_fixtures(tmp_path)
    (tmp_path / "weapons" / "weaponqa%095gs2vm.gs2bc").write_bytes(BINARY_BYTECODE)
    manager = make_manager(tmp_path, compiler_available=False)
    manager.load()
    player = FakePlayer()

    assert run(manager.send_weapon_bytecode(player, "qa_gs2vm")) is True

    stream = b"".join(player.sent)
    announcement = build_raw_data_announcement(len(stream) - ANNOUNCEMENT_SIZE)
    assert stream.startswith(announcement)
    packet = stream[ANNOUNCEMENT_SIZE:]
    assert packet[0] == PLO.NPCWEAPONSCRIPT + 32
    assert packet.endswith(BINARY_BYTECODE + b"\n")


def test_unknown_weapon_is_not_answered(tmp_path):
    manager = make_manager(tmp_path, compiler_available=False)
    manager.load()
    player = FakePlayer()

    assert run(manager.send_weapon_bytecode(player, "nosuchweapon")) is False
    assert player.sent == []


def test_unknown_class_gets_the_empty_bytecode_stub(tmp_path):
    manager = make_manager(tmp_path, compiler_available=False)
    manager.load()
    player = FakePlayer()

    run(manager.send_class_bytecode(player, "nosuchclass"))

    body = strip_frame(player.sent[0], PLO.NPCWEAPONSCRIPT)
    header_len = read_gshort(body, 0)
    assert body[2:2 + header_len].startswith(b"class,nosuchclass,1,")
    assert body[2 + header_len:] == b""


def test_matching_class_checksum_sends_nothing(tmp_path):
    write_fixtures(tmp_path)
    (tmp_path / "scripts" / "qa_gs2vmclass.gs2bc").write_bytes(BINARY_BYTECODE)
    manager = make_manager(tmp_path, compiler_available=False)
    manager.load()
    script = manager.get_class("qa_gs2vmclass")
    player = FakePlayer()

    run(manager.send_class_bytecode(player, "qa_gs2vmclass", script.checksum))
    assert player.sent == []

    run(manager.send_class_bytecode(player, "qa_gs2vmclass", 0))
    assert len(player.sent) == 1


def test_announce_weapon_sends_the_add_then_the_header(tmp_path):
    write_fixtures(tmp_path)
    (tmp_path / "weapons" / "weaponqa%095gs2vm.gs2bc").write_bytes(BINARY_BYTECODE)
    manager = make_manager(tmp_path, compiler_available=False)
    manager.load()
    player = FakePlayer()

    assert run(manager.announce_weapon(player, "qa_gs2vm")) is True

    assert player.sent[0][0] == PLO.NPCWEAPONADD + 32
    header = strip_frame(player.sent[1], PLO.LOADSCRIPT)
    assert header.startswith(b"weapon,qa_gs2vm,1,")


def test_missing_directories_are_not_an_error(tmp_path):
    manager = GS2ScriptManager(FakeServer(tmp_path / "nowhere"), str(tmp_path))

    manager.load()

    assert manager.weapons == {} and manager.classes == {}


# =============================================================================
# Ganis
# =============================================================================

GANI_FIXTURE = (
    "SPRITE 200 BODY 0 0 32 32 body up\n"
    "SETBACKTO idle\n"
    "SCRIPT\n"
    "//#CLIENTSIDE\n"
    "function onPlayerEnters() { this.qa = 1; }\n"
    "SCRIPTEND\n"
    "ANI\n  0 12 34\nANIEND\n"
)


def write_gani(tmp_path, name, text=GANI_FIXTURE):
    (tmp_path / "gani").mkdir(exist_ok=True)
    (tmp_path / "gani" / f"{name}.gani").write_text(text, encoding="latin-1")


def test_parse_gani_file_takes_setbackto_and_the_script_block():
    setbackto, script = parse_gani_file(GANI_FIXTURE)

    assert setbackto == "idle"
    # The marker line stays in: a gani has no serverside half to split off.
    assert script == "//#CLIENTSIDE\nfunction onPlayerEnters() { this.qa = 1; }"


def test_parse_gani_file_without_a_script_block():
    assert parse_gani_file("SPRITE 200 BODY 0 0 32 32\nCONTINUOUS\n") == ("", "")


@pytest.mark.skipif(GS2Compiler().available is False,
                    reason="gs2test compiler not built")
def test_gani_bytecode_is_framed_and_followed_by_loadgani(tmp_path):
    write_gani(tmp_path, "qa_gani")
    manager = make_manager(tmp_path)
    player = FakePlayer()

    run(manager.send_gani(player, "qa_gani"))

    stream = b"".join(player.sent[:-1])
    packet = stream[ANNOUNCEMENT_SIZE:]
    assert stream.startswith(build_raw_data_announcement(len(packet)))
    body = strip_frame(packet, PLO.GANISCRIPT)
    name_len = body[0] - 32
    assert body[1:1 + name_len] == b"qa_gani"
    # A real GS2 container, not the gani text that used to be posted into this
    # bytecode-only packet.
    from reborn_protocol.gs2.container import parse_container
    assert {f.name for f in parse_container(body[1 + name_len:]).functions} == {
        "onPlayerEnters"}

    load = strip_frame(player.sent[-1], PLO.LOADGANI)
    assert load == b"\x27qa_gani\"SETBACKTO idle\""


@pytest.mark.skipif(GS2Compiler().available is False,
                    reason="gs2test compiler not built")
def test_matching_gani_checksum_sends_only_the_loadgani(tmp_path):
    write_gani(tmp_path, "qa_gani")
    manager = make_manager(tmp_path)
    gani = manager.get_gani("qa_gani")
    player = FakePlayer()

    run(manager.send_gani(player, "qa_gani", gani.checksum))

    assert len(player.sent) == 1
    assert player.sent[0][0] == PLO.LOADGANI + 32


def test_scriptless_gani_gets_the_loadgani_only(tmp_path):
    write_gani(tmp_path, "qa_plain", "SETBACKTO walk\nSPRITE 200 BODY 0 0 32 32\n")
    manager = make_manager(tmp_path)
    player = FakePlayer()

    run(manager.send_gani(player, "qa_plain"))

    assert strip_frame(player.sent[0], PLO.LOADGANI).endswith(b'"SETBACKTO walk"')
    assert len(player.sent) == 1


def test_unknown_gani_is_not_answered(tmp_path):
    manager = make_manager(tmp_path)
    player = FakePlayer()

    run(manager.send_gani(player, "nosuchgani"))

    assert player.sent == []
    assert manager.ganis == {}       # misses are never cached


def test_gani_lookup_strips_a_trailing_suffix_and_caches_the_hit(tmp_path):
    """The client asks by bare name, but PLO_GANISCRIPT must never carry the
    suffix either (GameAni::getBytecodePacket strips it)."""
    write_gani(tmp_path, "qa_gani")
    manager = make_manager(tmp_path, compiler_available=False)

    assert manager.get_gani("qa_gani.gani").name == "qa_gani"
    assert manager.get_gani("qa_gani") is manager.get_gani("QA_GANI")


def test_update_gani_handler_skips_the_leading_crc(tmp_path):
    """PLI_UPDATEGANI is [GINT5 crc][name]; reading the whole payload as the
    name prefixed it with 5 bytes of checksum and every lookup missed."""
    from unittest.mock import AsyncMock, MagicMock

    from pygserver.player import Player
    from pygserver.protocol.packets import PacketBuilder

    write_gani(tmp_path, "qa_gani")
    manager = make_manager(tmp_path, compiler_available=False)
    server = MagicMock()
    server.gs2_manager = manager
    player = Player(server, 1, AsyncMock(), MagicMock())
    player.send_raw = AsyncMock()

    payload = PacketBuilder().write_gint5(12345).write_string("qa_gani").build()
    run(player._handle_update_gani(payload))

    assert player.send_raw.await_count == 1
    assert player.send_raw.await_args[0][0][0] == PLO.LOADGANI + 32
