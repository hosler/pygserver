"""NC class/weapon encodings, weapon list refresh and notices, database-NPC
scope, gmap NPC placement, and PUTNPC script lookup (2026-11-10 batch).

Reference: GServer-v2 server/src/player/packets/PlayerNCPackets.cpp,
PlayerClientPackets.cpp msgPLI_PUTNPC, filesystem/FileSystem.cpp.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pygserver.filesystem import FileSystem
from pygserver.gs2 import GS2ScriptManager
from pygserver.handlers.entities import find_putnpc_script
from pygserver.nc import NCManager, NCSession
from pygserver.npc import NPCManager
from pygserver.protocol.builders.admin import to_csv
from pygserver.protocol.constants import PLO, PLTYPE
from pygserver.protocol.packets import (
    PacketBuilder, build_nc_class_get, build_nc_weapon_get, build_npc_weapon_del,
    build_rc_chat)
from pygserver.world import GMap

from .test_nc_npc_handlers import FakeLevel, gint3


def run(coro):
    return asyncio.run(coro)


def gchar(value):
    return bytes([value + 32])


def sent(player):
    return [call.args[0] for call in player.send_raw.await_args_list]


def make_nc(tmp_path, players=()):
    (tmp_path / "weapons").mkdir()
    (tmp_path / "scripts").mkdir()
    server = SimpleNamespace(
        config=SimpleNamespace(weapons_dir=str(tmp_path / "weapons"), serverside=True),
        npc_manager=None,
        get_all_players=lambda: list(players),
    )
    server.gs2_manager = GS2ScriptManager(server, str(tmp_path))
    server.gs2_manager.compiler.binary = None
    nc = NCManager(server)
    editor = SimpleNamespace(id=1, account_name="editor", send_raw=AsyncMock())
    other = SimpleNamespace(id=2, account_name="watcher", send_raw=AsyncMock())
    session = nc.register_session(editor)
    nc.register_session(other)
    return nc, session, server, other


def weapon_add(name, image, script):
    return PacketBuilder().write_gstring(name).write_gstring(image).build() + script


# -- classes ---------------------------------------------------------------------

def test_class_get_sends_lines_as_one_raw_csv_row():
    packet = build_nc_class_get("util", 'a = "x, y";\nb();')
    assert packet == (gchar(PLO.NC_CLASSGET) + gchar(4) + b"util"
                      + b'"a = ""x, y"";",b();' + b"\n")


def test_class_add_decodes_the_csv_row_and_announces_only_new_classes(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    source_lines = ['function onCreated() {', '  echo("a, b");', '}']
    body = to_csv(source_lines).encode("latin-1")
    payload = PacketBuilder().write_gstring("util").build() + body

    run(nc._handle_class_add(session, payload))
    assert server.gs2_manager.get_class("util").source == "\n".join(source_lines)
    assert sent(other) == [gchar(PLO.NC_CLASSADD) + b"util\n",
                           build_rc_chat("Script util added by editor")]

    other.send_raw.reset_mock()
    run(nc._handle_class_add(session, payload))
    assert sent(other) == [build_rc_chat("Script util updated by editor")]


def test_class_add_refuses_a_control_character_name(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    run(nc._handle_class_add(
        session, PacketBuilder().write_gstring("bad\nname").build() + b"x"))
    assert server.gs2_manager.get_class("bad\nname") is None
    assert not list((tmp_path / "scripts").iterdir())


# -- weapons ---------------------------------------------------------------------

def test_weapon_get_maps_newlines_to_section_sign():
    assert build_nc_weapon_get("w", "i.png", "a\nb") == (
        gchar(PLO.NC_WEAPONGET) + gchar(1) + b"w" + gchar(5) + b"i.png"
        + b"a\xa7b\n")


def test_weapon_add_maps_section_sign_to_newline_and_refreshes(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    run(nc._handle_weapon_add(session, weapon_add("Wand", "w.png", b"a = 1;\xa7b = 2;")))
    assert server.gs2_manager.get_weapon("Wand").source == "a = 1;\nb = 2;"
    assert sent(session.player)[-1][0] == PLO.NC_WEAPONLISTGET + 32
    assert build_rc_chat("Weapon/GUI-script Wand added by editor") in sent(other)

    session.player.send_raw.reset_mock()
    other.send_raw.reset_mock()
    run(nc._handle_weapon_add(session, weapon_add("Wand", "w2.png", b"c = 3;")))
    # PlayerNCPackets.cpp:677-679: an update does not resend the list.
    assert not any(packet[0] == PLO.NC_WEAPONLISTGET + 32
                   for packet in sent(session.player))
    assert sent(other) == [build_rc_chat("Weapon/GUI-script Wand updated by editor")]


def test_weapon_add_built_in_check_ignores_case(tmp_path):
    # Stored weapons are keyed by lowercase name, so `Bow` would shadow `bow`.
    nc, session, server, other = make_nc(tmp_path)
    for name in ("Bow", "BOMB", "JoltBomb", " bow"):
        run(nc._handle_weapon_add(session, weapon_add(name, "x.png", b"// script")))
        assert server.gs2_manager.get_weapon(name) is None, name
        assert server.gs2_manager.get_weapon(name.strip()) is None, name
    session.player.send_raw.assert_not_awaited()
    assert not list((tmp_path / "weapons").iterdir())
    # A name that merely contains a built-in one is an ordinary weapon.
    run(nc._handle_weapon_add(session, weapon_add("Longbow", "x.png", b"// script")))
    assert server.gs2_manager.get_weapon("longbow") is not None


def test_weapon_add_never_replaces_a_built_in_weapon(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    run(nc._handle_weapon_add(session, weapon_add("bomb", "b.png", b"x = 1;")))
    assert server.gs2_manager.get_weapon("bomb") is None
    session.player.send_raw.assert_not_awaited()
    assert not list((tmp_path / "weapons").iterdir())


def test_weapon_add_refuses_control_characters(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    run(nc._handle_weapon_add(session, weapon_add("Wa\nnd", "w.png", b"x = 1;")))
    run(nc._handle_weapon_add(session, weapon_add("Wand", "w\x00.png", b"x = 1;")))
    assert not server.gs2_manager.weapons


def test_weapon_get_missing_is_told_to_every_nc(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    run(nc._handle_weapon_get(session, b"Nope"))
    notice = build_rc_chat("editor prob: weapon Nope doesn't exist")
    assert sent(session.player) == [notice] and sent(other) == [notice]


def test_weapon_delete_notifies_refreshes_and_drops_it_from_clients(tmp_path):
    client = SimpleNamespace(connection_type=PLTYPE.CLIENT, send_raw=AsyncMock())
    rc = SimpleNamespace(connection_type=PLTYPE.RC, send_raw=AsyncMock())
    nc, session, server, other = make_nc(tmp_path, players=[client, rc])
    run(nc._handle_weapon_add(session, weapon_add("Wand", "w.png", b"x = 1;")))
    session.player.send_raw.reset_mock()
    other.send_raw.reset_mock()

    run(nc._handle_weapon_delete(session, b"Wand"))
    assert server.gs2_manager.get_weapon("Wand") is None
    assert sent(other) == [build_rc_chat("Weapon Wand deleted by editor")]
    assert sent(session.player)[-1] == gchar(PLO.NC_WEAPONLISTGET) + b"\n"
    assert sent(client) == [build_npc_weapon_del("Wand")]
    rc.send_raw.assert_not_awaited()


def test_weapon_delete_missing_sends_no_list(tmp_path):
    nc, session, server, other = make_nc(tmp_path)
    run(nc._handle_weapon_delete(session, b"Nope"))
    notice = build_rc_chat("editor prob: weapon Nope doesn't exist")
    assert sent(session.player) == [notice]


# -- database NPCs -----------------------------------------------------------------

def make_npc_nc(levels=None, gmap=None):
    levels = levels or {name: FakeLevel(name) for name in ("town.nw", "cave.nw")}
    world = SimpleNamespace(get_level=levels.get,
                            get_gmap=lambda name: gmap if gmap and name == gmap.name else None)
    server = SimpleNamespace(config=SimpleNamespace(serverside=True), world=world,
                             broadcast_to_level=AsyncMock(), broadcast_to_all=AsyncMock(),
                             get_all_players=lambda: [])
    server.npc_manager = NPCManager(server)
    nc = NCManager(server)
    player = SimpleNamespace(id=7, account_name="editor", send_raw=AsyncMock())
    return nc, nc.register_session(player), server, levels


def test_delete_and_reset_leave_non_database_npcs_alone():
    nc, session, server, levels = make_npc_nc()

    async def go():
        level_npc = server.npc_manager.create_npc(name="levelnpc", level=levels["town.nw"])
        level_npc.flags["seen"] = "1"
        await nc._handle_npc_reset(session, gint3(level_npc.id))
        await nc._handle_npc_delete(session, gint3(level_npc.id))
        return level_npc

    npc = run(go())
    assert server.npc_manager.get_npc(npc.id) is npc
    assert npc.flags == {"seen": "1"}
    session.player.send_raw.assert_not_awaited()
    server.broadcast_to_level.assert_not_awaited()
    server.broadcast_to_all.assert_not_awaited()


def test_nc_added_npc_is_a_database_npc():
    nc, session, server, levels = make_npc_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,t,s,town.nw,1,1")
        npc = server.npc_manager.get_npc(10050)
        npc.flags["seen"] = "1"
        await nc._handle_npc_reset(session, gint3(10050))
        assert npc.flags == {}
        await nc._handle_npc_delete(session, gint3(10050))

    run(go())
    assert server.npc_manager.get_npc(10050) is None


def test_npc_add_on_a_gmap_name_places_it_on_the_segment():
    gmap = GMap("world.gmap")
    gmap.grid = {(0, 0): "a.nw", (1, 0): "b.nw"}
    levels = {name: FakeLevel(name) for name in ("a.nw", "b.nw")}
    nc, session, server, _ = make_npc_nc(levels, gmap)

    async def go():
        await nc._handle_npc_add(session, b"Far,10060,t,s,world.gmap,70.5,3")
        await nc._handle_npc_add(session, b"Near,10061,t,s,b.nw,6.5,3")

    run(go())
    far = server.npc_manager.get_npc(10060)
    near = server.npc_manager.get_npc(10061)
    assert far.level is levels["b.nw"] and (far.x, far.y) == (6.5, 3.0)
    # Named on the segment, the position is already segment-local.
    assert near.level is levels["b.nw"] and (near.x, near.y) == (6.5, 3.0)


# -- PUTNPC script lookup ----------------------------------------------------------

def make_fs(tmp_path, folders=None):
    for folder in ("world/npcs", "images", "config", "accounts"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / "world/top.txt").write_text("top")
    (tmp_path / "world/npcs/guard.txt").write_text("guard")
    (tmp_path / "images/pic.txt").write_text("pic")
    (tmp_path / "accounts/admin.txt").write_text("secret")
    (tmp_path / "config/serveroptions.txt").write_text("secret")
    if folders is not None:
        (tmp_path / "config/foldersconfig.txt").write_text(folders)
    return FileSystem(SimpleNamespace(), str(tmp_path))


def test_putnpc_without_folders_config_stays_in_the_world_folder(tmp_path):
    fs = make_fs(tmp_path)
    assert find_putnpc_script(fs, "top.txt") == tmp_path / "world/top.txt"
    assert find_putnpc_script(fs, "guard.txt") is None


def test_putnpc_honours_file_globs(tmp_path):
    fs = make_fs(tmp_path, "# cfg\nfile    npcs/*.txt\nlevel *.nw\n")
    assert find_putnpc_script(fs, "guard.txt") == tmp_path / "world/npcs/guard.txt"
    assert find_putnpc_script(fs, "top.txt") is None
    assert find_putnpc_script(fs, "pic.txt") is None


def test_putnpc_globs_never_reach_private_files_or_paths(tmp_path):
    fs = make_fs(tmp_path, "file *.txt\nfile *\nfile ../*\nfile accounts/*\n")
    assert find_putnpc_script(fs, "pic.txt") == tmp_path / "images/pic.txt"
    for name in ("admin.txt", "serveroptions.txt", "npcs/guard.txt",
                 "../accounts/admin.txt", "/etc/passwd", "world/top.txt"):
        assert find_putnpc_script(fs, name) is None


def test_putnpc_with_no_file_lines_finds_nothing(tmp_path):
    fs = make_fs(tmp_path, "level *.nw\n")
    assert find_putnpc_script(fs, "top.txt") is None
