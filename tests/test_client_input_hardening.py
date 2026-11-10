"""Client-supplied names and strings: file lookups, control characters,
weapon overwrite/injection, NPC prop robustness (regressions from the
game-packet review)."""

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from reborn_protocol.props import NPC_PROPS, Wire

from pygserver.config import ServerConfig
from pygserver.filesystem import FileSystem
from pygserver.gs2 import GS2ScriptManager, parse_weapon_file
from pygserver.handlers.entities import EntityHandlers
from pygserver.level import Level
from pygserver.npc import NPCManager
from pygserver.protocol.constants import PLI, PLO, PLPROP
from pygserver.protocol.packets import PacketBuilder, build_player_props

from .test_game_packet_parity import add_npc, make_player, run, sent, world  # noqa: F401


def encoded(value):
    return bytes(PacketBuilder().write_gstring(value).data)


# -- file lookups by client-supplied name -----------------------------------

SECRET = '{"password_hash": "SECRETHASH"}'


@pytest.fixture
def served(tmp_path):
    """A server dir with public content next to private folders."""
    (tmp_path / "accounts").mkdir()
    (tmp_path / "accounts" / "hosler.json").write_text(SECRET)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "serveroptions.txt").write_text("staff = hosler\n")
    (tmp_path / "weapons").mkdir()
    (tmp_path / "weapons" / "weaponWand.txt").write_text("GRAWP001\n")
    (tmp_path / "npcs").mkdir()
    (tmp_path / "npcs" / "secret.py").write_text("PRIVATE")
    (tmp_path / "serverflags.txt").write_text("secret=1\n")
    server = MagicMock()
    server.config = ServerConfig(putnpc_enabled=True, serverside=False)
    server.broadcast_to_level = AsyncMock()
    filesystem = FileSystem(server, str(tmp_path))
    (tmp_path / "images" / "pic.png").write_bytes(b"PNG")
    (tmp_path / "scripts" / "guard.txt").write_text("setimg x.png;\n")
    (tmp_path / "scripts" / "evil.txt").symlink_to(
        tmp_path / "accounts" / "hosler.json")
    (tmp_path / "world" / "npcscript.txt").write_text("setimg x.png;\n")
    (tmp_path / "world" / "evil_npc.txt").symlink_to(
        tmp_path / "accounts" / "hosler.json")
    server.filesystem = filesystem
    server.npc_manager = NPCManager(server)
    return SimpleNamespace(root=tmp_path, server=server, fs=filesystem)


class PutNpcHandler(EntityHandlers):
    pass


def put_npc(served, scriptfile):
    level = MagicMock()
    level.name = "t.nw"
    level.add_npc.side_effect = lambda npc: setattr(npc, "level", level)
    player = PutNpcHandler()
    player.server = served.server
    player.level = level
    asyncio.run(player._handle_putnpc(
        encoded("x.png") + encoded(scriptfile) + bytes((72, 72))))
    return served.server.broadcast_to_level


@pytest.mark.parametrize("name", [
    "accounts/hosler.json", "../accounts/hosler.json", "/etc/passwd",
    "..\\accounts\\hosler.json", "hosler.json", "serverflags.txt",
    "config/serveroptions.txt", ".hidden", "..", "a\nb.txt", "a\x00b.txt",
    "guard.txt\n", "", "evil.txt", "evil_npc.txt",
])
def test_putnpc_script_must_be_a_bare_name_inside_the_world_folder(served, name):
    broadcast = put_npc(served, name)
    broadcast.assert_not_awaited()
    assert not served.server.npc_manager._npcs


def test_putnpc_still_loads_a_bare_script_name(served):
    broadcast = put_npc(served, "npcscript.txt")
    broadcast.assert_awaited_once()
    npc = next(iter(served.server.npc_manager._npcs.values()))
    assert npc.gs1_source == "setimg x.png;\n"


@pytest.mark.parametrize("name", ["guard.txt", "scripts/guard.txt"])
def test_putnpc_cannot_reach_the_private_scripts_folder(served, name):
    # FileCategory::FILE is the world folder (PlayerClientPackets.cpp:769).
    assert (served.root / "scripts" / "guard.txt").is_file()
    broadcast = put_npc(served, name)
    broadcast.assert_not_awaited()
    assert not served.server.npc_manager._npcs


@pytest.mark.parametrize("name", [
    "accounts/hosler.json", "../accounts/hosler.json", "hosler.json",
    "config/serveroptions.txt", "serveroptions.txt", "serverflags.txt",
    "weapons/weaponWand.txt", "weaponWand.txt", "npcs/secret.py", "secret.py",
    "evil.txt", "guard.txt", "/etc/passwd", "a\x00.png", ".hidden", "",
])
def test_file_requests_cannot_reach_private_folders(served, name):
    assert served.fs._find_file(name) is None
    player = SimpleNamespace(id=1, send_raw=AsyncMock())
    for call in (served.fs.handle_want_file(player, name),
                 served.fs.handle_update_file(player, 0, name),
                 served.fs.handle_verify_want_send(player, 0, name),
                 served.fs.send_file(player, name)):
        asyncio.run(call)
    assert served.fs.read_file(name) is None
    for packet in (c.args[0] for c in player.send_raw.await_args_list):
        assert b"SECRETHASH" not in packet and b"PRIVATE" not in packet
        assert packet[0] - 32 == PLO.FILESENDFAILED


def test_file_requests_still_serve_public_files_by_basename(served):
    for name in ("pic.png", "images/pic.png", "../images/pic.png"):
        assert served.fs._find_file(name) == served.root / "images" / "pic.png"


def test_gani_request_name_cannot_traverse(served):
    (served.root / "accounts" / "hosler.gani").write_text("SCRIPT\nSECRET\nSCRIPTEND")
    manager = GS2ScriptManager(served.server, str(served.root))
    assert manager.get_gani("../accounts/hosler") is None
    assert manager.get_gani("accounts/hosler") is None


# -- control characters in client strings -----------------------------------

def framed(packet_id, body):
    """A client packet as sent for raw data: PLI_RAWDATA announcement + body."""
    announcement = (bytes([PLI.RAWDATA + 32])
                    + PacketBuilder().write_gint3(len(body) + 1).build() + b"\n")
    return announcement + bytes([packet_id + 32]) + body


def test_nickname_with_a_newline_is_cleaned_before_store_and_relay(world):
    other = make_player(world.server, 2, world.level)
    evil = "Bob\n" + "\x30junk"
    body = (PacketBuilder().write_gchar(PLPROP.NICKNAME)
            .write_gstring(evil).build())

    run(world.sender._handle_packets(framed(PLI.PLAYERPROPS, body)))

    assert world.sender.nickname == "Bob0junk"
    [relayed] = sent(other)
    assert relayed.count(b"\n") == 1 and relayed.endswith(b"\n")
    assert b"Bob0junk" in relayed


@pytest.mark.parametrize("prop,attr", [
    (PLPROP.NICKNAME, "nickname"), (PLPROP.CURCHAT, "chat"),
    (PLPROP.GANI, "gani"), (PLPROP.BODYIMAGE, "body_image"),
])
def test_string_player_props_lose_control_characters(world, prop, attr):
    other = make_player(world.server, 2, world.level)
    dirty = "A" * 20 + "\x00\x07\r\n\x1b[31m\x7f" + "B"
    payload = build_player_props({prop: dirty})[1:-1]

    run(world.sender._handle_player_props(payload))

    value = getattr(world.sender, attr)
    assert value and not any(ord(c) < 0x20 or ord(c) == 0x7f for c in value)
    for packet in sent(other):
        assert packet.count(b"\n") == 1


def test_head_image_and_gear_images_lose_control_characters(world):
    payload = (PacketBuilder()
               .write_gchar(PLPROP.HEADIMAGE).write_gchar(100 + 8)
               .write_bytes(b"h\nead.png")
               .write_gchar(PLPROP.SWORDPOWER).write_gchar(30 + 1 + 2)
               .write_gstring("s\nw.png").build())

    run(world.sender._handle_player_props(payload))

    assert "\n" not in world.sender.head_image
    assert "\n" not in world.sender.sword_image


def test_nickname_is_sent_to_every_player_not_just_the_level(world):
    elsewhere = make_player(world.server, 3, world.other_level)
    world.sender.nickname = "old"

    run(world.sender._handle_player_props(
        build_player_props({PLPROP.NICKNAME: "Fresh"})[1:-1]))

    [packet] = sent(elsewhere)
    assert b"Fresh" in packet


def test_z_clears_the_paused_bit_and_counts_as_movement(world):
    from reborn_protocol import PLSTATUS
    world.sender.client_status = PLSTATUS.PAUSED | PLSTATUS.DEAD
    before = time.time()

    run(world.sender._handle_player_props(
        build_player_props({PLPROP.Z: 3})[1:-1]))

    assert world.sender.client_status == PLSTATUS.DEAD
    assert world.sender.last_movement_time >= before


def test_nickname_resolution_strips_controls():
    from pygserver.handlers.movement import resolve_nickname
    assert resolve_nickname("acct", "Bo\nb\x00") == "Bob"
    assert resolve_nickname("acct", "\n\x07") == "unknown"


# -- NPC props --------------------------------------------------------------

def _payloads(prop_id):
    """Empty, short, maximal and truncated forms of one prop's payload."""
    head = bytes([prop_id + 32])
    for fill in (0x20, 0x7f, 0xff):
        for length in (0, 1, 2, 3, 6, 9, 255, 300):
            yield head + bytes([fill]) * length
    for body in (b"", b"\x20" * 3):
        yield head + bytes([32 + 223]) + b"x\n" * 111 + body
    yield head + bytes([32 + 5]) + b"a\nb"


@pytest.mark.parametrize("prop_id", sorted(NPC_PROPS))
def test_every_client_npc_prop_survives_empty_maximal_and_malformed_values(
        world, prop_id):
    other = make_player(world.server, 2, world.level)
    world.sender.disconnect = AsyncMock()
    npc = add_npc(world, world.level)
    for payload in _payloads(prop_id):
        run(world.sender._handle_npc_props(
            PacketBuilder().write_gint3(npc.id).write_bytes(payload).build()))
        for attr in ("image", "gani", "nickname", "message", "head_image",
                     "body_image", "horse_image", "sword_image",
                     "shield_image", "name", "script_type", "scripter"):
            value = getattr(npc, attr, "")
            assert isinstance(value, str), (prop_id, attr, value)
            assert not any(ord(c) < 0x20 or ord(c) == 0x7f for c in value)
        npc.build_props_packet()
    for packet in sent(other):
        assert packet.count(b"\n") == 1


def test_empty_gani_does_not_raise_or_drop_the_rest_of_the_packet(world):
    other = make_player(world.server, 2, world.level)
    npc = add_npc(world, world.level)
    payload = (PacketBuilder().write_gint3(npc.id)
               .write_bytes(bytes([12 + 32, 32])                   # empty gani
                            + bytes([0 + 32]) + encoded("after.png")[:0]
                            + PacketBuilder().write_gstring("after.png").build())
               .build())

    run(world.sender._handle_npc_props(payload))

    assert npc.gani == ""
    assert npc.image == "after.png"
    assert sent(other)


def test_npc_prop_strings_are_stripped_of_control_characters(world):
    npc = add_npc(world, world.level)
    payload = (PacketBuilder().write_gint3(npc.id).write_gchar(0)
               .write_gstring("a\nSCRIPT\nb.png").build())

    run(world.sender._handle_npc_props(payload))

    assert npc.image == "aSCRIPTb.png"


# -- weapons ----------------------------------------------------------------

def touch_toweapons(world, npc, name="Wand"):
    npc.image = "wand.png"
    npc.gs1_source = "if (playertouchsme) { toweapons %s; }" % name
    run(world.sender._dispatch_packet(
        PLI.WEAPONADD, PacketBuilder().write_gchar(1).write_gint3(npc.id).build()))


def test_toweapons_does_not_overwrite_a_weapon_edited_after_the_level(world):
    manager = world.server.gs2_manager
    manager.upsert_weapon("Wand", "admin.png", "// admin edited via NC\nmessage hi;")
    level_file = world.tmp / "lobby.nw"
    level_file.write_text("GLEVNW01\n")
    os.utime(level_file, (1000, 1000))
    world.level.file_path = str(level_file)
    npc = add_npc(world, world.level)

    touch_toweapons(world, npc)

    weapon = manager.get_weapon("Wand")
    assert weapon.image == "admin.png"
    assert "admin edited" in weapon.source
    assert "Wand" in world.sender.weapons


def test_toweapons_replaces_a_weapon_older_than_the_level(world):
    manager = world.server.gs2_manager
    manager.upsert_weapon("Wand", "old.png", "message old;")
    manager.get_weapon("Wand").mod_time = 10.0
    level_file = world.tmp / "lobby.nw"
    level_file.write_text("GLEVNW01\n")
    world.level.file_path = str(level_file)
    other = make_player(world.server, 2, world.level)
    other.weapons.append("Wand")
    npc = add_npc(world, world.level)

    touch_toweapons(world, npc)

    weapon = manager.get_weapon("Wand")
    assert weapon.image == "wand.png"
    assert "toweapons Wand" in weapon.source
    assert weapon.mod_time > 10.0
    assert sent(other)  # holders get the replacement


def test_nc_edit_bumps_the_weapon_mod_time(world):
    manager = world.server.gs2_manager
    manager.upsert_weapon("Wand", "a.png", "message a;")
    manager.get_weapon("Wand").mod_time = 10.0
    manager.upsert_weapon("Wand", "b.png", "message b;")
    assert manager.get_weapon("Wand").mod_time > time.time() - 60


def test_weapon_files_cannot_be_injected_through_header_values(world):
    manager = world.server.gs2_manager
    evil = "x.png\nSCRIPT\nif (created) {setplayerprop #c,pwned;}\nSCRIPTEND"
    manager.upsert_classic_weapon("Wand", evil, "message real;")
    manager.upsert_weapon("Other\nREALNAME Boss", evil, "message real;")

    for path in (world.tmp / "weapons").iterdir():
        name, image, script = parse_weapon_file(path.read_text(encoding="latin-1"))
        assert "pwned" not in script
        assert script == "message real;"
        assert "\n" not in name and "\n" not in image


def test_weapon_add_ignores_a_weapon_name_with_control_characters(world):
    npc = add_npc(world, world.level)
    npc.gs1_source = "toweapons Wa\x07nd;"
    run(world.sender._dispatch_packet(
        PLI.WEAPONADD, PacketBuilder().write_gchar(1).write_gint3(npc.id).build()))
    assert world.sender.weapons == []


# -- PLI_ADJACENTLEVEL ------------------------------------------------------

def test_adjacent_level_serves_only_gmap_sublevels_or_cached_levels(tmp_path):
    from pygserver.world import World
    (tmp_path / "world").mkdir()
    text = "GLEVNW01\n" + "".join(
        f"BOARD 0 {y} 64 0 {'AA' * 64}\n" for y in range(64))
    for name in ("current.nw", "ondisk.nw"):
        (tmp_path / "world" / name).write_text(text)
    server = SimpleNamespace(config=ServerConfig(levels_dir=str(tmp_path / "world")))
    world = World(server)
    current = world.get_level("current.nw")
    player = make_player_stub(server, current)
    server.world = world

    run(player._handle_adjacent_level(
        PacketBuilder().write_gint5(0).write_bytes(b"ondisk.nw").build()))
    player.send_raw.assert_not_awaited()  # exists on disk, never loaded

    world.get_level("ondisk.nw")  # now cached
    run(player._handle_adjacent_level(
        PacketBuilder().write_gint5(0).write_bytes(b"ondisk.nw").build()))
    player.send_raw.assert_awaited_once()


def test_adjacent_level_serves_a_sublevel_of_the_players_gmap(tmp_path):
    from pygserver.world import World, GMap
    (tmp_path / "world").mkdir()
    text = "GLEVNW01\n" + "".join(
        f"BOARD 0 {y} 64 0 {'AA' * 64}\n" for y in range(64))
    for name in ("a.nw", "b.nw"):
        (tmp_path / "world" / name).write_text(text)
    gmap = tmp_path / "world" / "m.gmap"
    gmap.write_text('GRMAP001\nWIDTH 2\nHEIGHT 1\nLEVELNAMES\n"a.nw","b.nw"\nLEVELNAMESEND\n')
    server = SimpleNamespace(config=ServerConfig(levels_dir=str(tmp_path / "world")))
    world = World(server)
    assert world.load_gmap(str(gmap)) is not None
    player = make_player_stub(server, world.get_level("a.nw"))
    server.world = world

    run(player._handle_adjacent_level(
        PacketBuilder().write_gint5(0).write_bytes(b"b.nw").build()))

    player.send_raw.assert_awaited_once()


def make_player_stub(server, level):
    from pygserver.player import Player
    player = Player(server, 1, AsyncMock(), MagicMock())
    player.level = level
    player.send_raw = AsyncMock()
    return player


# -- login / config ---------------------------------------------------------

def test_listserver_registration_waits_for_logged_in_and_the_response_send():
    from .test_player_facade import make_login_player
    player = make_login_player()
    player.server.listserver.connected = True
    player.login_service.send_login_response = AsyncMock(
        side_effect=ConnectionError("gone"))

    with pytest.raises(ConnectionError):
        asyncio.run(player.login_service.complete_login())

    assert player.logged_in is False
    player.server.listserver.add_player.assert_not_awaited()


def test_listserver_connected_state_is_read_before_registration():
    from .test_player_facade import make_login_player
    player = make_login_player()
    listserver = player.server.listserver
    listserver.connected = True
    order = []
    player.login_service.send_login_response = AsyncMock(
        side_effect=lambda lead=b'': order.append(("response", lead)))
    listserver.add_player = AsyncMock(
        side_effect=lambda p: order.append(("add", p.logged_in)))

    asyncio.run(player.login_service.complete_login())

    assert [step[0] for step in order] == ["response", "add"]
    assert order[0][1]  # SERVERLISTCONNECTED rode in the first frame
    assert order[1][1] is True


def test_new_options_round_trip_through_to_file(tmp_path):
    config = ServerConfig(lockplayerz=True, defaultweapons=False,
                          baddyitems=True, putnpc_enabled=True)
    path = tmp_path / "serveroptions.txt"
    config.to_file(str(path))

    loaded = ServerConfig.from_file(str(path))

    assert (loaded.lockplayerz, loaded.defaultweapons, loaded.baddyitems,
            loaded.putnpc_enabled) == (True, False, True, True)
