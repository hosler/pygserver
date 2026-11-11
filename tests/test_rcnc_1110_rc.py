"""RC player-prop edits, server options and folder config (2026-11-10 batch).

Reference: GServer-v2 server/src/player/packets/PlayerRCPackets.cpp and
server/src/player/PlayerProps.cpp (setPropsFromRCPacket).
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import AccountManager, RCRight
from pygserver.config import ServerConfig
from pygserver.filesystem import FileSystem
from pygserver.protocol.builders.admin import (
    from_csv, guntokenize_text, to_csv)
from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import (
    PacketBuilder, PacketReader, build_flag_del, build_flag_set,
    build_npc_weapon_del, build_player_props, build_rc_chat)
from pygserver.rc import RCManager, RCSession


def run(coro):
    return asyncio.run(coro)


def props_blob(props):
    return build_player_props(props)[1:-1]


def prop_stream(props=None, flags=(), chests=(), weapons=(), world="main"):
    builder = PacketBuilder().write_gstring(world)
    blob = props_blob(props or {})
    builder.write_gchar(len(blob)).write_bytes(blob)
    builder.write_gshort(len(flags))
    for flag in flags:
        builder.write_gstring(flag)
    builder.write_gshort(len(chests))
    for level, x, y in chests:
        builder.write_gchar(len(level) + 2).write_gchar(x).write_gchar(y)
        builder.write_string(level)
    builder.write_gchar(len(weapons))
    for weapon in weapons:
        builder.write_gstring(weapon)
    return builder.build()


def by_id(player_id, stream):
    return PacketBuilder().write_gshort(player_id).build() + stream


def by_name(name, stream):
    return PacketBuilder().write_gstring(name).build() + stream


@pytest.fixture
def rc(tmp_path):
    server = SimpleNamespace()
    server.config = ServerConfig()
    server.filesystem = FileSystem(server, str(tmp_path))
    server.account_manager = AccountManager(server, str(tmp_path / "accounts"))
    server.account_manager.create_account("admin")
    target = server.account_manager.create_account("target")
    target.flags = {"old": "1", "keep": "x"}
    target.weapons = ["Sword"]
    server.gs2_manager = MagicMock()
    server.gs2_manager.get_weapon = lambda name: (
        object() if name in ("Sword", "Wand") else None)
    server.gs2_manager.announce_weapon = AsyncMock()
    server.broadcast_to_world = AsyncMock()
    server.is_staff = lambda name: False
    level = SimpleNamespace(name="start.nw")
    player = SimpleNamespace(
        id=2, account_name="target", nickname="Target", hearts=2.0,
        max_hearts=3.0, rupees=9, bombs=4, arrows=2, glove_power=1,
        sword_power=1, sword_image="sword1.png", shield_power=1,
        shield_image="shield1.png", head_image="head0.png",
        body_image="body.png", colors=[0] * 5, mp=0, ap=50, kills=0,
        deaths=0, elo_rating=1500.0, elo_deviation=350.0, gani="idle",
        client_status=0, x=30.0, y=30.0, level=level,
        flags={"old": "1", "keep": "x"}, weapons=["Sword"], gattribs={},
        send_raw=AsyncMock(), send_props=AsyncMock(), warp=AsyncMock())
    server.get_player = lambda pid: player if pid == 2 else None
    server.get_player_by_name = lambda name: (
        player if name.lower() == "target" else None)
    manager = RCManager(server)
    admin = SimpleNamespace(id=7, account_name="admin", send_raw=AsyncMock())
    session = RCSession(player=admin, rights=RCRight.SETATTRIBUTES)
    manager._sessions[admin.id] = session

    def flush():
        server.account_manager._save_executor.submit(lambda: None).result()

    return SimpleNamespace(server=server, manager=manager, session=session,
                           admin=admin, player=player, target=target,
                           root=tmp_path, flush=flush)


def sent(mock):
    return [call.args[0] for call in mock.await_args_list]


# -- RC_PLAYERPROPSSET / RC_PLAYERPROPSSET2 ------------------------------------

def test_props_set_applies_clamps_pushes_and_persists(rc):
    stream = prop_stream(
        {PLPROP.NICKNAME: "Hero", PLPROP.MAXPOWER: 15, PLPROP.ARROWSCOUNT: 150,
         PLPROP.RUPEESCOUNT: 500, PLPROP.GLOVEPOWER: 9,
         PLPROP.SWORDPOWER: (9, "blade.png"), PLPROP.ALIGNMENT: 120,
         PLPROP.CURLEVEL: "cave.nw", PLPROP.X2: 12.5, PLPROP.Y2: 7.0},
        flags=["new=2", "keep=x", "boolflag"],
        weapons=["Sword", "Wand", "Unknown"])
    run(rc.manager._handle_player_props_set(rc.session, by_id(2, stream)))

    player, config = rc.player, rc.server.config
    assert player.nickname == "Hero"
    assert player.max_hearts == config.apply_max_hearts(15)
    assert player.hearts == player.max_hearts
    assert player.arrows == 99                      # Limits::MaxArrows
    assert player.rupees == 500
    assert player.glove_power == 3                  # MaxPlayerGlovePower
    assert player.sword_power == config.sword_limit
    assert player.sword_image == "blade.png"
    assert player.ap == 100
    assert (player.x, player.y) == (12.5, 7.0)
    player.warp.assert_awaited_once_with("cave.nw", 12.5, 7.0)
    # Flags are replaced wholesale and the client is told the difference.
    assert player.flags == {"new": "2", "keep": "x", "boolflag": ""}
    raw = sent(player.send_raw)
    assert build_flag_del("old") in raw
    assert build_flag_set("new", "2") in raw
    assert build_flag_set("boolflag", True) in raw
    assert build_flag_set("keep", "x") not in raw
    # Only weapons the server has are added; built-in bomb/bow are removed.
    assert player.weapons == ["Sword", "Wand"]
    rc.server.gs2_manager.announce_weapon.assert_awaited_once_with(player, "Wand")
    assert build_npc_weapon_del("Bomb") in raw and build_npc_weapon_del("Bow") in raw
    pushed = player.send_props.await_args.args[0]
    assert pushed[PLPROP.NICKNAME] == "Hero" and pushed[PLPROP.ARROWSCOUNT] == 99

    rc.flush()
    stored = rc.server.account_manager.get_account("target", reload=True)
    assert stored.nickname == "Hero" and stored.arrows == 99
    assert stored.flags == {"new": "2", "keep": "x", "boolflag": ""}
    assert stored.weapons == ["Sword", "Wand"]
    assert stored.level_name == "cave.nw" and (stored.x, stored.y) == (12.5, 7.0)
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat(
        "admin set the attributes of player target")


def test_props_set2_edits_an_offline_account(rc):
    rc.server.get_player_by_name = lambda name: None
    rc.server.account_manager.create_account("sleeper")
    stream = prop_stream({PLPROP.BOMBSCOUNT: 120, PLPROP.CURLEVEL: "inn.nw",
                          PLPROP.X: 10.5, PLPROP.KILLSCOUNT: 4},
                         flags=["q=done"], chests=[("inn.nw", 3, 4)],
                         weapons=["Wand", "Nope"])
    run(rc.manager._handle_player_props_set2(rc.session, by_name("sleeper", stream)))
    rc.flush()
    stored = rc.server.account_manager.get_account("sleeper", reload=True)
    assert stored.bombs == 99
    assert stored.kills == 4
    assert stored.level_name == "inn.nw" and stored.x == 10.5
    assert stored.flags == {"q": "done"}
    assert stored.weapons == ["Wand"]
    assert stored.chests_opened == ["inn.nw:3,4"]


def test_props_set2_unknown_account_is_silent(rc):
    rc.server.get_player_by_name = lambda name: None
    run(rc.manager._handle_player_props_set2(
        rc.session, by_name("ghost", prop_stream())))
    rc.admin.send_raw.assert_not_awaited()
    assert not (rc.root / "accounts/ghost.json").exists()


@pytest.mark.parametrize("cut", [1, 3, 6])
def test_truncated_prop_stream_changes_nothing(rc, cut):
    stream = prop_stream({PLPROP.NICKNAME: "Hero"}, flags=["a=1"], weapons=["Wand"])
    run(rc.manager._handle_player_props_set(rc.session, by_id(2, stream[:-cut])))
    assert rc.player.nickname == "Target"
    assert rc.player.flags == {"old": "1", "keep": "x"}
    assert rc.player.weapons == ["Sword"]
    assert rc.target.flags == {"old": "1", "keep": "x"}
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat(
        "Server: Invalid player properties packet.")


def test_props_set_without_right_changes_nothing(rc):
    rc.session.rights = RCRight.SETSELFATTRIBUTES
    stream = prop_stream({PLPROP.NICKNAME: "Hero"}, weapons=[])
    run(rc.manager._handle_player_props_set(rc.session, by_id(2, stream)))
    assert rc.player.nickname == "Target" and rc.player.weapons == ["Sword"]
    assert sent(rc.admin.send_raw) == [build_rc_chat(
        "Server: admin is not authorized to set the properties of target")]


def test_control_characters_are_stripped(rc):
    stream = prop_stream({PLPROP.NICKNAME: "Ev\x01il\x1b"},
                         flags=["fl\x00ag=v\x07al"], weapons=["Sword"])
    run(rc.manager._handle_player_props_set(rc.session, by_id(2, stream)))
    assert rc.player.nickname == "Evil"
    assert rc.player.flags == {"flag": "val"}


@pytest.mark.parametrize("staff,rights,changes", [
    (False, RCRight.SETATTRIBUTES, False),
    (True, RCRight.SETATTRIBUTES, False),
    (True, RCRight.SETATTRIBUTES | RCRight.SETRIGHTS, True),
])
def test_gralats_gate_when_normal_admins_may_not(rc, staff, rights, changes):
    # PlayerProps.cpp:1205-1210 behind `normaladminscanchangegralats`.
    rc.server.config.normaladminscanchangegralats = False
    rc.server.is_staff = lambda name: staff
    rc.session.rights = rights
    run(rc.manager._handle_player_props_set(
        rc.session, by_id(2, prop_stream({PLPROP.RUPEESCOUNT: 777}, weapons=["Sword"]))))
    assert (rc.player.rupees == 777) is changes


def test_heal_refused_under_forty_ap(rc):
    # PlayerProps.cpp:234-235 applies to server-set hearts too.
    rc.player.ap = 10
    run(rc.manager._handle_player_props_set(
        rc.session, by_id(2, prop_stream({PLPROP.CURPOWER: 6}, weapons=["Sword"]))))
    assert rc.player.hearts == 2.0
    rc.player.ap = 50
    run(rc.manager._handle_player_props_set(
        rc.session, by_id(2, prop_stream({PLPROP.CURPOWER: 6}, weapons=["Sword"]))))
    assert rc.player.hearts == 3.0


def test_long_flag_cut_by_the_reply_keeps_its_stored_value(rc):
    long_value = "v" * 400
    rc.player.flags = {"big": long_value}
    reply_entry = ("big=" + long_value)[:220] + "[!]"
    run(rc.manager._handle_player_props_set(
        rc.session, by_id(2, prop_stream(flags=[reply_entry], weapons=["Sword"]))))
    assert rc.player.flags == {"big": long_value}


def test_props_reply_includes_opened_chests_for_a_lossless_round_trip(rc):
    chest = SimpleNamespace(x=5, y=6, opened_by={"target"})
    rc.server.item_manager = SimpleNamespace(_chests={"town.nw": [chest]})
    packet = rc.manager._build_rc_player_props_packet(rc.player, {})
    reader = PacketReader(packet)
    assert reader.read_gchar() == PLO.RC_PLAYERPROPSGET
    reader.read_gshort()
    reader.read_gstring()
    reader.read_gstring()
    reader.read_bytes(reader.read_gchar())
    for _ in range(reader.read_gshort()):
        reader.read_gstring()
    assert reader.read_gshort() == 1
    assert reader.read_gchar() == len("town.nw") + 2
    assert (reader.read_gchar(), reader.read_gchar()) == (5, 6)
    assert reader.read_string(len("town.nw")) == "town.nw"


def test_chest_list_replaces_opened_chests(rc):
    kept = SimpleNamespace(x=1, y=1, opened_by={"target"})
    dropped = SimpleNamespace(x=2, y=2, opened_by={"target", "other"})
    rc.server.item_manager = SimpleNamespace(_chests={"a.nw": [kept, dropped]})
    run(rc.manager._handle_player_props_set(
        rc.session, by_id(2, prop_stream(chests=[("a.nw", 1, 1)], weapons=["Sword"]))))
    assert "target" in kept.opened_by
    assert dropped.opened_by == {"other"}


# -- comma-text codecs ---------------------------------------------------------

def test_reference_csv_codecs():
    assert to_csv(["a", "b,c", 'q"t', "back\\slash", ""]) == \
        'a,"b,c","q""t","back\\\\slash",'
    assert from_csv('a,"b,c","q""t","back\\\\slash",') == \
        ["a", "b,c", 'q"t', "back\\slash"]
    assert from_csv("a,,b") == ["a", "", "b"]
    # StringUtils.h:984-986: any byte outside 32..126 also forces quotes.
    lines = ["function onCreated() {", "\tthis.x = 1;", "}", "name = caf\xe9",
             "del\x7f", "tilde~ok", "\xa0lead"]
    encoded = to_csv(lines)
    assert encoded == ('function onCreated() {,"\tthis.x = 1;",},'
                       '"name = caf\xe9","del\x7f",tilde~ok,"\xa0lead"')
    assert from_csv(encoded) == lines
    assert from_csv('"\ta ""b"" \\\\ c",x') == ['\ta "b" \\ c', "x"]
    assert guntokenize_text('"name=x, y",desc=z,') == "name=x, y\ndesc=z\n"
    assert guntokenize_text("a\\b,c") == "ab\nc"


# -- RC_SERVEROPTIONSGET / SET ---------------------------------------------------

def options_rc(rc, text):
    path = rc.root / "config" / "serveroptions.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    rc.server.config = ServerConfig.from_file(str(path))
    return path


def test_options_get_sends_the_file_as_one_csv_row(rc):
    options_rc(rc, "# Server\nname = My, Server\nstartlevel = a.nw\n")
    run(rc.manager._handle_server_options_get(rc.session, b""))
    assert sent(rc.admin.send_raw) == [
        PacketBuilder().write_gchar(PLO.RC_SERVEROPTIONSGET)
        .write_string('# Server,"name = My, Server",startlevel = a.nw')
        .write_newline().build()]


def test_options_set_merges_into_the_loaded_file(rc):
    path = options_rc(rc, "# keep me\nname = Old\nstartlevel = a.nw\nheartlimit = 3\n")
    rc.session.rights = RCRight.SETSERVEROPTIONS
    run(rc.manager._handle_server_options_set(
        rc.session, b'startlevel = b.nw,heartlimit = 5,newkey = hello'))
    assert path.read_text() == (
        "# keep me\nname = Old\nstartlevel = b.nw\nheartlimit = 5\nnewkey = hello\n")
    assert rc.server.config.start_level == "b.nw"
    reloaded = ServerConfig.from_file(str(path))
    assert (reloaded.start_level, reloaded.heart_limit) == ("b.nw", 5)
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat(
        "admin has updated the server options.")


@pytest.mark.parametrize("staff", [False, True])
def test_options_set_admin_keys_need_staff_right_on_disk_too(rc, staff):
    path = options_rc(rc, "name = Old\nserverport = 14900\n")
    rc.session.rights = RCRight.SETSERVEROPTIONS | (
        RCRight.MODIFYSTAFFACCOUNT if staff else 0)
    run(rc.manager._handle_server_options_set(
        rc.session, b"name = New\nserverport = 1\nstartlevel = z.nw"))
    text = path.read_text()
    assert ("name = New" in text) is staff
    assert ("serverport = 1\n" in text) is staff
    assert "startlevel = z.nw" in text


@pytest.mark.parametrize("payload", [
    b"options_path = /tmp/elsewhere.txt", b"options_path=../../x",
    b"base_dir = /", b'"startlevel = a\nserverport = 1"'])
def test_options_set_cannot_redirect_or_inject_lines(rc, tmp_path, payload):
    path = options_rc(rc, "name = Old\nserverport = 14900\n")
    rc.session.rights = RCRight.SETSERVEROPTIONS | RCRight.MODIFYSTAFFACCOUNT
    run(rc.manager._handle_server_options_set(rc.session, payload))
    assert rc.server.config.options_path == str(path)
    assert rc.server.config.base_dir == "."
    assert not (tmp_path / "elsewhere.txt").exists()
    assert all(line.count("=") <= 1 for line in path.read_text().splitlines())


def test_options_set_rejects_control_characters(rc):
    path = options_rc(rc, "name = Old\n")
    rc.session.rights = RCRight.SETSERVEROPTIONS | RCRight.MODIFYSTAFFACCOUNT
    run(rc.manager._handle_server_options_set(rc.session, b"name = Bad\x1bName"))
    assert path.read_text() == "name = Old\n"
    assert rc.server.config.name == "Old"


def test_options_set_on_an_in_memory_config_writes_no_file(rc, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc.session.rights = RCRight.SETSERVEROPTIONS
    run(rc.manager._handle_server_options_set(rc.session, b"startlevel = q.nw"))
    assert rc.server.config.start_level == "q.nw"
    assert not list(tmp_path.rglob("serveroptions.txt"))


def test_update_options_file_refuses_line_breaks(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("name = x\n")
    for key, value in (("name", "a\nb"), ("na=me", "1"), ("#name", "1"), ("a b", "1")):
        with pytest.raises(ValueError):
            ServerConfig.update_options_file(str(path), [(key, value)])
    assert path.read_text() == "name = x\n"


def test_server_dir_config_records_its_options_file(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "serveroptions.txt").write_text("name = a\n")
    config = ServerConfig.from_server_dir(str(tmp_path))
    assert config.options_path == str(tmp_path / "config" / "serveroptions.txt")


# -- RC_FOLDERCONFIGGET / SET ----------------------------------------------------

def test_folder_config_get_needs_the_file(rc):
    run(rc.manager._handle_folder_config_get(rc.session, b""))
    rc.admin.send_raw.assert_not_awaited()
    (rc.root / "config").mkdir()
    (rc.root / "config/foldersconfig.txt").write_text("# Folders\nfile    *.png\nlevel *.nw\n")
    run(rc.manager._handle_folder_config_get(rc.session, b""))
    assert sent(rc.admin.send_raw) == [
        PacketBuilder().write_gchar(PLO.RC_FOLDERCONFIGGET)
        .write_string("# Folders,file    *.png,level *.nw,").write_newline().build()]


def test_folder_config_set_replaces_the_file_and_drops_escaping_globs(rc):
    rc.session.rights = RCRight.SETFOLDEROPTIONS
    payload = to_csv(["# mine", "file images/*.png", "file ../*", "file /etc/*",
                      "file C:/x/*", "head heads/../../accounts/*", "level\x07 *.nw"])
    run(rc.manager._handle_folder_config_set(rc.session, payload.encode()))
    # A line holding a control character is dropped whole, not cleaned.
    assert (rc.root / "config/foldersconfig.txt").read_text() == (
        "# mine\nfile images/*.png\n")
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat("admin updated the folder config.")


def test_folder_config_set_without_right_leaves_the_file(rc):
    (rc.root / "config").mkdir()
    (rc.root / "config/foldersconfig.txt").write_text("file *.png\n")
    rc.session.rights = RCRight.SETSERVEROPTIONS
    run(rc.manager._handle_folder_config_set(rc.session, b"file *"))
    assert (rc.root / "config/foldersconfig.txt").read_text() == "file *.png\n"
    assert sent(rc.admin.send_raw) == [build_rc_chat(
        "Server: admin is not authorized to change the folder config.")]


# -- foldersconfig.txt containment (review 2026-11-10) -----------------------------

def _escape_tree(root):
    """Folders beside world/ that a folder config must never make public."""
    for rel in (".git/config", "pygserver/secret.py", "tests/t_secret.py",
                "world.bak-1/old.nw", "accounts/acct.txt"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("SECRET")
    (root / "world/npcs").mkdir(parents=True, exist_ok=True)
    (root / "world/accounts").mkdir(parents=True, exist_ok=True)
    (root / "levels").mkdir(exist_ok=True)
    (root / "levels/legit.nw").write_text("GLEVNW01")
    (root / "world/npcs/guard.txt").write_text("guard")


BAD_FOLDER_LINES = [
    "file .git/*", "file pygserver/*", "file tests/*", "file world.bak-1/*",
    "file levels/.hidden/*", "file levels/../pygserver/*", "file ./tests/*",
    "file /etc/*", "file C:/x/*",
    # Every character str.splitlines() breaks on, plus other controls.
    "file levels/*\x85file .git/*", "file levels/*\x0bfile .git/*",
    "file levels/*\x0cfile .git/*", "file levels/*\x1cfile .git/*",
    "file levels/*\x1dfile .git/*", "file levels/*\x1efile .git/*",
    "file levels/*\rfile .git/*", "file levels/*\x00", "file levels/*\x9f",
]


@pytest.mark.parametrize("line", BAD_FOLDER_LINES)
def test_folder_config_set_refuses_lines_leaving_the_world(rc, line):
    _escape_tree(rc.root)
    rc.session.rights = RCRight.SETFOLDEROPTIONS
    payload = to_csv(["file levels/*.nw", line, "level *.nw"])
    run(rc.manager._handle_folder_config_set(rc.session, payload.encode("latin-1")))
    assert (rc.root / "config/foldersconfig.txt").read_bytes() == (
        b"file levels/*.nw\nlevel *.nw\n")


def test_folder_config_set_keeps_legit_lines(rc):
    _escape_tree(rc.root)
    rc.session.rights = RCRight.SETFOLDEROPTIONS
    lines = ["# cfg", "file\tlevels/*.nw", "file npcs/*.txt", "level *.nw",
             "head heads/*", "file */deep.png", "sound caf\xe9/*.wav # no folder"]
    run(rc.manager._handle_folder_config_set(
        rc.session, to_csv(lines).encode("latin-1")))
    # caf\xe9/ does not exist under world/, so that line goes.
    assert (rc.root / "config/foldersconfig.txt").read_bytes() == "".join(
        line + "\n" for line in lines[:-1]).encode("latin-1")


def test_bad_lines_already_on_disk_publish_nothing(rc):
    from pygserver.handlers.entities import find_putnpc_script
    _escape_tree(rc.root)
    (rc.root / "config").mkdir(exist_ok=True)
    (rc.root / "config/foldersconfig.txt").write_bytes(
        ("\n".join(BAD_FOLDER_LINES + ["file accounts/*", "file npcs/*.txt",
                                       "file levels/*.nw"]) + "\n").encode("latin-1"))
    fs = FileSystem(SimpleNamespace(), str(rc.root))
    assert fs._folders_config_dirs() == ["levels"]
    for name in ("config", "secret.py", "t_secret.py", "old.nw", "acct.txt"):
        assert fs._find_file(name) is None, name
        assert find_putnpc_script(fs, name) is None, name
    assert fs._find_file("legit.nw") == rc.root / "levels/legit.nw"
    # `npcs/*` still sorts world/npcs; the private sibling never becomes a root.
    assert find_putnpc_script(fs, "guard.txt") == rc.root / "world/npcs/guard.txt"
    assert rc.root / "npcs" not in fs.public_dirs()
    assert rc.root / "accounts" not in fs.public_dirs()


def test_folder_root_symlinked_out_of_the_server_is_refused(rc, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "leak.png").write_text("SECRET")
    (rc.root / "world/evil").mkdir(parents=True)
    (rc.root / "evil").symlink_to(outside, target_is_directory=True)
    (rc.root / "world/inner").mkdir()
    (rc.root / "world/linked").symlink_to(outside, target_is_directory=True)
    (rc.root / "linked").mkdir()
    (rc.root / "linked/also.png").write_text("SECRET")
    (rc.root / "config").mkdir(exist_ok=True)
    (rc.root / "config/foldersconfig.txt").write_text("file evil/*\nfile linked/*\n")
    fs = FileSystem(SimpleNamespace(), str(rc.root))
    assert fs._find_file("leak.png") is None
    assert fs._find_file("also.png") is None
    assert fs._folders_config_root("evil") is None
    assert not fs.folders_config_glob_allowed("linked/*")


def test_world_declared_sibling_folder_is_public(rc):
    (rc.root / "world/extras").mkdir(parents=True)
    (rc.root / "extras").mkdir()
    (rc.root / "extras/extra.png").write_text("png")
    rc.session.rights = RCRight.SETFOLDEROPTIONS
    fs = rc.server.filesystem
    assert fs._find_file("extra.png") is None
    run(rc.manager._handle_folder_config_set(rc.session, b"file extras/*.png"))
    assert fs._find_file("extra.png") == rc.root / "extras/extra.png"
    # Narrowing the config withdraws the root at once, not after a rescan.
    run(rc.manager._handle_folder_config_set(rc.session, b"level *.nw"))
    assert fs._find_file("extra.png") is None


def test_folders_config_is_parsed_once_per_version(rc, monkeypatch):
    (rc.root / "config").mkdir()
    path = rc.root / "config/foldersconfig.txt"
    path.write_text("file images/*\n")
    fs = rc.server.filesystem
    first = fs.folders_config_entries()
    assert first == [("file", "images/*")]
    opened = []
    real_open = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: (
        opened.append(a[0]), real_open(*a, **k))[1])
    assert fs.folders_config_entries() is first
    assert opened == []
    path.write_text("file images/*\nlevel *.nw\n")
    assert fs.folders_config_entries() == [("file", "images/*"), ("level", "*.nw")]
    assert opened == [path]


# -- account-name case (review 2026-11-10) --------------------------------------

def test_wrong_case_own_account_needs_set_self_attributes(rc):
    rc.server.get_player_by_name = lambda name: None
    rc.session.rights = RCRight.SETATTRIBUTES
    run(rc.manager._handle_player_props_set2(
        rc.session, by_name("ADMIN", prop_stream({PLPROP.BOMBSCOUNT: 50}))))
    assert sent(rc.admin.send_raw) == [build_rc_chat(
        "Server: admin is not authorized to set the properties of admin")]
    assert rc.server.account_manager.get_account("admin").bombs != 50


def test_wrong_case_own_account_edits_under_the_stored_name(rc):
    rc.server.get_player_by_name = lambda name: None
    chest = SimpleNamespace(x=3, y=4, opened_by=set())
    rc.server.item_manager = SimpleNamespace(_chests={"inn.nw": [chest]})
    rc.session.rights = RCRight.SETSELFATTRIBUTES
    run(rc.manager._handle_player_props_set2(
        rc.session, by_name("AdMiN", prop_stream({PLPROP.BOMBSCOUNT: 50},
                                                 chests=[("inn.nw", 3, 4)]))))
    assert chest.opened_by == {"admin"}
    assert rc.server.account_manager.get_account("admin").bombs == 50
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat(
        "admin set the attributes of player admin")


def test_wrong_case_default_account_is_protected(rc):
    rc.server.get_player_by_name = lambda name: None
    rc.server.account_manager.create_account("(defaultaccount)")
    rc.session.rights = RCRight.SETATTRIBUTES
    run(rc.manager._handle_player_props_set2(
        rc.session, by_name("(DefaultAccount)", prop_stream())))
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat(
        "Server: You are not authorized to modify the default account.")
    rc.session.rights = RCRight.MODIFYSTAFFACCOUNT
    run(rc.manager._handle_account_del(rc.session, b"(DEFAULTACCOUNT)"))
    rc.flush()
    assert (rc.root / "accounts/(defaultaccount).json").exists()
    assert sent(rc.admin.send_raw)[-1] == build_rc_chat(
        "Server: You are not allowed to delete the default account.")


def test_wrong_case_own_rights_are_viewable_without_set_rights(rc):
    rc.session.rights = RCRight.SETATTRIBUTES
    run(rc.manager._handle_player_rights_get(rc.session, b"ADMIN"))
    assert sent(rc.admin.send_raw)[-1][0] == PLO.RC_PLAYERRIGHTSGET + 32


# -- atomic option/folder writes (review 2026-11-10) -------------------------------

def test_options_write_keeps_mode_and_symlink(tmp_path):
    from pygserver.config import atomic_write_text
    real = tmp_path / "shared" / "serveroptions.txt"
    real.parent.mkdir()
    real.write_text("name = x\n")
    real.chmod(0o640)
    link = tmp_path / "serveroptions.txt"
    link.symlink_to(real)
    ServerConfig.update_options_file(str(link), [("name", "y")])
    assert link.is_symlink() and link.resolve() == real
    assert real.read_text() == "name = y\n"
    assert real.stat().st_mode & 0o7777 == 0o640
    assert sorted(p.name for p in real.parent.iterdir()) == ["serveroptions.txt"]
    fresh = tmp_path / "new" / "foldersconfig.txt"
    atomic_write_text(fresh, "file *\n")
    umask = os.umask(0)
    os.umask(umask)
    assert fresh.stat().st_mode & 0o7777 == 0o666 & ~umask


def test_options_write_failure_leaves_no_temporary(tmp_path, monkeypatch):
    path = tmp_path / "serveroptions.txt"
    path.write_text("name = x\n")

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        ServerConfig.update_options_file(str(path), [("name", "y")])
    assert [p.name for p in tmp_path.iterdir()] == ["serveroptions.txt"]
    assert path.read_text() == "name = x\n"


def test_options_round_trip_a_latin1_value(rc):
    path = options_rc(rc, "name = Old\n")
    rc.session.rights = RCRight.SETSERVEROPTIONS
    run(rc.manager._handle_server_options_set(rc.session, b"startlevel = caf\xe9\xa7.nw"))
    assert path.read_bytes() == b"name = Old\nstartlevel = caf\xe9\xa7.nw\n"
    assert ServerConfig.from_file(str(path)).start_level == "caf\xe9\xa7.nw"
    rc.admin.send_raw.reset_mock()
    run(rc.manager._handle_server_options_get(rc.session, b""))
    assert sent(rc.admin.send_raw) == [
        PacketBuilder().write_gchar(PLO.RC_SERVEROPTIONSGET)
        .write_string('name = Old,"startlevel = caf\xe9\xa7.nw"')
        .write_newline().build()]


def test_folder_config_write_keeps_mode_and_symlink(rc, tmp_path_factory):
    shared = tmp_path_factory.mktemp("shared") / "foldersconfig.txt"
    shared.write_text("file *.png\n")
    shared.chmod(0o604)
    (rc.root / "config").mkdir()
    (rc.root / "config/foldersconfig.txt").symlink_to(shared)
    rc.session.rights = RCRight.SETFOLDEROPTIONS
    run(rc.manager._handle_folder_config_set(rc.session, b"level *.nw"))
    assert (rc.root / "config/foldersconfig.txt").is_symlink()
    assert shared.read_text() == "level *.nw\n"
    assert shared.stat().st_mode & 0o7777 == 0o604
