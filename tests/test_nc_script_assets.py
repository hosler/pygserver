"""NC weapon and class editing through the persistent GS2 script store."""

import asyncio
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pygserver.gs2 import GS2ScriptManager, parse_weapon_file
from pygserver.nc import NCManager, NCSession
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import PacketBuilder


def run(coro):
    return asyncio.run(coro)


def make_nc(tmp_path):
    weapons = tmp_path / "weapons"
    scripts = tmp_path / "scripts"
    weapons.mkdir()
    scripts.mkdir()
    server = SimpleNamespace(
        config=SimpleNamespace(weapons_dir=str(weapons), serverside=True),
        npc_manager=None,
        get_all_players=lambda: [],
    )
    server.gs2_manager = GS2ScriptManager(server, str(tmp_path))
    server.gs2_manager.compiler.binary = None
    player = SimpleNamespace(account_name="editor", send_raw=AsyncMock())
    return NCManager(server), NCSession(player=player), server


def weapon_add(name, image, script):
    return (PacketBuilder().write_gstring(name).write_gstring(image).build()
            + script.encode("latin-1"))


def class_add(name, script):
    return PacketBuilder().write_gstring(name).build() + script.encode("latin-1")


def test_weapon_nc_round_trip_and_replacement_persists(tmp_path):
    nc, session, server = make_nc(tmp_path)

    run(nc._handle_weapon_add(session, weapon_add("bow", "bow.png", "setcharani idle,;")))
    run(nc._handle_weapon_list_get(session, b""))
    assert b"bow" in session.player.send_raw.await_args.args[0]
    run(nc._handle_weapon_get(session, b"bow"))
    assert b"bow.png" in session.player.send_raw.await_args.args[0]
    assert b"setcharani idle,;" in session.player.send_raw.await_args.args[0]

    run(nc._handle_weapon_add(session, weapon_add("bow", "new.png", "setcharani walk,;")))
    weapon = server.gs2_manager.get_weapon("bow")
    parsed = parse_weapon_file(Path(weapon.source_path).read_text(encoding="latin-1"))
    assert parsed == ("bow", "new.png", "setcharani walk,;")

    run(nc._handle_weapon_delete(session, b"bow"))
    assert server.gs2_manager.get_weapon("bow") is None
    assert not Path(weapon.source_path).exists()


def test_weapon_add_refreshes_list_only_for_new_name(tmp_path):
    nc, session, server = make_nc(tmp_path)
    compiled = iter([b"first bytecode", b"updated bytecode"])
    server.gs2_manager.compiler.compile = lambda source, name: next(compiled)

    run(nc._handle_weapon_add(session, weapon_add("wand", "a.png", "show();")))

    reply = session.player.send_raw.await_args.args[0]
    assert reply[0] == PLO.NC_WEAPONLISTGET + 32
    assert b"wand" in reply
    assert server.gs2_manager.get_weapon("wand").checksum == zlib.crc32(b"first bytecode")
    session.player.send_raw.reset_mock()

    run(nc._handle_weapon_add(session, weapon_add("WAND", "b.png", "hide();")))

    session.player.send_raw.assert_not_awaited()
    weapon = server.gs2_manager.get_weapon("wand")
    assert weapon.image == "b.png"
    assert weapon.checksum == zlib.crc32(b"updated bytecode")
    assert weapon.header_with_crc.endswith("," + str(weapon.checksum))


def test_class_nc_round_trip(tmp_path):
    nc, session, server = make_nc(tmp_path)
    source = "function onCreated() { this.x = 1; }"

    run(nc._handle_class_add(session, class_add("helpers", source)))
    run(nc._handle_class_edit(session, b"helpers"))
    reply = session.player.send_raw.await_args.args[0]
    assert reply[0] == PLO.NC_CLASSGET + 32
    assert source.encode("latin-1") in reply
    path = Path(server.gs2_manager.get_class("helpers").source_path)
    assert path.read_text(encoding="latin-1") == source

    run(nc._handle_class_delete(session, b"helpers"))
    assert server.gs2_manager.get_class("helpers") is None
    assert not path.exists()


def test_weapon_list_uses_loaded_directory_content(tmp_path):
    nc, session, server = make_nc(tmp_path)
    (tmp_path / "weapons" / "weaponstarter.txt").write_text(
        "GRAWP001\nREALNAME starter\nIMAGE start.png\nSCRIPT\nsetcharani idle,;\nSCRIPTEND\n",
        encoding="latin-1")
    server.gs2_manager.load()

    run(nc._handle_weapon_list_get(session, b""))

    assert b"starter" in session.player.send_raw.await_args.args[0]


def test_compile_error_is_sent_to_saving_nc_session(tmp_path):
    nc, session, server = make_nc(tmp_path)

    run(nc._handle_weapon_add(session, weapon_add("broken", "", "function ( {")))

    assert b"[NC weapon compile error]" in session.player.send_raw.await_args.args[0]
    assert server.gs2_manager.get_weapon("broken") is None
