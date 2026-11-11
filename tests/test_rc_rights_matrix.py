"""Reference RC authorization, denial packets, and filesystem containment."""

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import Account, AccountManager, RCRight
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLO, PLPERM, PLTYPE
from pygserver.protocol.packets import PacketBuilder, PacketReader, build_rc_chat
from pygserver.rc import IMPORTANT_FILES, PROTECTED_DOWNLOADS, RCManager, RCSession
from .test_rc_reference_packets import _upload_manager


def packet():
    return PacketBuilder()


def account_payload(name="target", banned=True):
    return (packet().write_gstring(name).write_gstring("password")
            .write_gstring("new@example.test").write_gchar(int(banned))
            .write_gchar(1).write_gchar(0).write_gstring("main")
            .write_gstring("reason").build())


def empty_prop_stream():
    # [world][props][flag count][chest count][weapon count] (PlayerProps.cpp:1246-1307)
    return (packet().write_gstring("main").write_gchar(0).write_gshort(0)
            .write_gshort(0).write_gchar(0).build())


def rights_payload(name="target", rights=RCRight.BAN, folders="rw levels/*"):
    return (packet().write_gstring(name).write_gint5(rights)
            .write_gstring("127.0.0.*,10.*").write_gstring_short(folders).build())


@pytest.fixture
def rc(tmp_path):
    server, manager, player, session = _upload_manager(tmp_path)
    server.config = SimpleNamespace(description="old", name="old", start_level="old")
    server.account_manager = AccountManager(server, str(tmp_path / "accounts"))
    admin = server.account_manager.create_account("admin")
    admin.folders = ["rw levels/*", "rw config/*", "rw accounts/*", "rw images/*"]
    target = server.account_manager.create_account("target")
    server.account_manager.save_account(admin)
    target_player = SimpleNamespace(
        id=2, account_name="target", nickname="Target", hearts=1, max_hearts=3,
        rupees=9, bombs=4, arrows=2, disconnect=AsyncMock(), warp=AsyncMock(),
        send_raw=AsyncMock(), flags={}, weapons=[], chests_opened=[],
    )
    server.get_player = MagicMock(return_value=target_player)
    server.get_player_by_name = MagicMock(return_value=target_player)
    server.broadcast_to_all = AsyncMock()
    server.world = SimpleNamespace(reload_levels=AsyncMock())
    server.server_flags = {}
    manager._build_player_props = MagicMock(return_value=b"")
    manager._build_rc_player_props_packet = MagicMock(return_value=b"props\n")

    def flush():
        # Account writes run on a single worker thread; wait for the queue.
        server.account_manager._save_executor.submit(lambda: None).result()

    flush()
    return SimpleNamespace(flush=flush, server=server, manager=manager, player=player,
                           session=session, admin=admin, target=target,
                           target_player=target_player, root=tmp_path)


# Packet, required wire bit, payload, exact denial text, observable success.
GATES = [
    (PLI.RC_SERVEROPTIONSSET, 0x10000, b"startlevel=new", "admin is not authorized to change the server options.", "options"),
    (PLI.RC_FOLDERCONFIGSET, 0x20000, b"levels/*", "admin is not authorized to change the folder config.", "folders"),
    (PLI.RC_PLAYERPROPSGET2, 0x20, packet().write_gshort(2).build(), "You are not authorized to view player props.", "props_read"),
    (PLI.RC_PLAYERPROPSGET3, 0x20, packet().write_gstring("target").build(), "You are not authorized to view player props.", "props_read"),
    (PLI.RC_PLAYERPROPSSET, 0x40, packet().write_gshort(2).build() + empty_prop_stream(), "admin is not authorized to set the properties of target", "props_write"),
    (PLI.RC_PLAYERPROPSSET2, 0x40, packet().write_gstring("target").build() + empty_prop_stream(), "admin is not authorized to set the properties of target", "props_write"),
    (PLI.RC_PLAYERPROPSRESET, 0x100, b"target", "You are not authorized to reset accounts.", "reset"),
    (PLI.RC_DISCONNECTPLAYER, 0x10, packet().write_gshort(2).write_string("reason").build(), "admin is not authorized to disconnect players.", "disconnect"),
    (PLI.RC_UPDATELEVELS, 0x08, b"", "admin is not authorized to update levels.", "levels"),
    (PLI.RC_ADMINMESSAGE, 0x200, b"message", "You are not authorized to send an admin message.", "message"),
    (PLI.RC_PRIVADMINMESSAGE, 0x200, packet().write_gshort(2).write_string("message").build(), "You are not authorized to send an admin message.", "private_message"),
    (PLI.RC_SERVERFLAGSSET, 0x8000, packet().write_gshort(1).write_gstring("test=on").build(), "You are not authorized to set the server flags.", "flags"),
    (PLI.RC_ACCOUNTADD, 0x4000, account_payload("created"), "You are not authorized to create new accounts.", "create"),
    (PLI.RC_ACCOUNTDEL, 0x4000, b"target", "You are not authorized to delete accounts.", "delete"),
    (PLI.RC_ACCOUNTSET, 0x4000, account_payload(), "You are not authorized to edit accounts.", "account"),
    (PLI.RC_WARPPLAYER, 0x02, packet().write_gshort(2).write_gchar_signed(10).write_gchar_signed(20).write_string("new").build(), "You are not authorized to warp players.\n", "warp"),
    (PLI.RC_PLAYERRIGHTSGET, 0x400, b"target", "You are not authorized to view that player's rights.", "rights_read"),
    (PLI.RC_PLAYERRIGHTSSET, 0x400, rights_payload(rights=RCRight.SETRIGHTS), "You are not authorized to set player rights.", "rights"),
    (PLI.RC_PLAYERCOMMENTSSET, 0x1000, packet().write_gstring("target").write_string("comment").build(), "You are not authorized to set player comments.", "comments"),
    (PLI.RC_PLAYERBANSET, 0x800, packet().write_gstring("target").write_gchar(1).write_string("reason").build(), "You are not authorized to set player bans.", "ban"),
]


def effect(rc, name, caplog):
    rc.flush()
    return {
        "options": lambda: rc.server.config.start_level == "new",
        "folders": lambda: ((rc.root / "config/foldersconfig.txt").is_file()
                            and (rc.root / "config/foldersconfig.txt").read_text() == "levels/*\n"),
        "props_read": lambda: rc.manager._build_player_props.called,
        "props_write": lambda: any(b"set the attributes of player" in c.args[0] for c in rc.player.send_raw.await_args_list),
        "reset": lambda: rc.target_player.rupees == 0,
        "disconnect": lambda: bool(rc.target_player.disconnect.await_count),
        "levels": lambda: bool(rc.server.world.reload_levels.await_count),
        "message": lambda: bool(rc.server.broadcast_to_all.await_count),
        "private_message": lambda: bool(rc.target_player.send_raw.await_count),
        "flags": lambda: rc.server.server_flags.get("test") == "on",
        "create": lambda: (rc.root / "accounts/created.json").exists(),
        "delete": lambda: not (rc.root / "accounts/target.json").exists(),
        "account": lambda: rc.target.profile_email == "new@example.test",
        "warp": lambda: bool(rc.target_player.warp.await_count),
        "rights_read": lambda: any(c.args[0][0] == PLO.RC_PLAYERRIGHTSGET + 32 for c in rc.player.send_raw.await_args_list),
        "rights": lambda: rc.target.admin_rights == RCRight.SETRIGHTS,
        "comments": lambda: rc.target.comments == "comment",
        "ban": lambda: rc.target.is_banned,
    }[name]()


@pytest.mark.parametrize("packet_id,right,payload,denial,name", GATES)
@pytest.mark.parametrize("allowed", [False, True])
def test_privileged_packet_matrix(rc, caplog, packet_id, right, payload, denial, name, allowed):
    caplog.set_level("INFO", logger="pygserver.rc")
    # Keep every unrelated bit: this catches generic attribute and mislabeled enum gates.
    rc.session.rights = right if allowed else 0xfffff & ~right
    if name == "props_write":
        rc.session.rights &= ~RCRight.SETSELFATTRIBUTES
    rc.manager._sessions[rc.player.id] = rc.session
    before = rc.target.to_dict()
    asyncio.run(rc.manager.handle_packet(rc.player, packet_id, payload))
    assert effect(rc, name, caplog) == allowed
    assert "RC handler error" not in caplog.text
    if not allowed:
        assert rc.target.to_dict() == before
        assert rc.player.send_raw.await_args.args[0] == build_rc_chat("Server: " + denial)


@pytest.mark.parametrize("staff_admin", [False, True])
def test_grants_mask_bits_but_not_ip_or_valid_folders(rc, staff_admin):
    rc.session.rights = int(RCRight.SETRIGHTS | RCRight.BAN)
    if staff_admin:
        rc.session.rights |= RCRight.MODIFYSTAFFACCOUNT
    grants = int(RCRight.BAN | RCRight.SETCOMMENTS) | (1 << 22)
    folders = '"rw images/*","rw accounts/*","rw ../outside/*","rw C:/bad/*","rw /*"'
    asyncio.run(rc.manager._handle_player_rights_set(
        rc.session, rights_payload(rights=grants, folders=folders)))
    # PlayerRCPackets.cpp:864-872 masks bits 0-19 only and lets higher bits through;
    # we also mask bits 20+ unless the caller holds modify-staff-account.
    assert rc.target.admin_rights == (
        grants if staff_admin else int(RCRight.BAN))
    assert rc.target.folders == ["rw images/*", "rw accounts/*"]
    assert rc.target.admin_ip == "127.0.0.*,10.*"
    rc.flush()
    loaded = rc.server.account_manager.get_account("target", reload=True)
    assert loaded.admin_rights == rc.target.admin_rights
    assert loaded.folders == rc.target.folders
    assert loaded.admin_ip == rc.target.admin_ip


@pytest.mark.parametrize("staff_admin", [False, True])
def test_self_rights_preserve_only_bits_the_caller_holds(rc, staff_admin):
    # Reference ORs both bits in unconditionally (PlayerRCPackets.cpp:874-879);
    # we keep only what the caller already holds.
    rc.session.rights = int(RCRight.SETRIGHTS)
    if staff_admin:
        rc.session.rights |= RCRight.MODIFYSTAFFACCOUNT
    asyncio.run(rc.manager._handle_player_rights_set(
        rc.session, rights_payload("AdMiN", 0)))
    expected = RCRight.SETRIGHTS | (RCRight.MODIFYSTAFFACCOUNT if staff_admin else 0)
    assert rc.admin.admin_rights == expected
    assert rc.session.rights == rc.admin.admin_rights


def test_set_rights_only_session_cannot_escalate_in_two_steps(rc):
    rc.session.rights = int(RCRight.SETRIGHTS)
    rc.admin.admin_rights = int(RCRight.SETRIGHTS)
    asyncio.run(rc.manager._handle_player_rights_set(
        rc.session, rights_payload("admin", 0)))
    assert rc.admin.admin_rights == RCRight.SETRIGHTS
    asyncio.run(rc.manager._handle_player_rights_set(
        rc.session, rights_payload("admin", 0xfffff)))
    assert rc.admin.admin_rights == RCRight.SETRIGHTS
    assert rc.session.rights == RCRight.SETRIGHTS


@pytest.mark.parametrize("staff_admin", [False, True])
def test_bits_at_20_and_above_need_modify_staff_account(rc, staff_admin):
    rc.session.rights = RCRight.SETRIGHTS | (
        RCRight.MODIFYSTAFFACCOUNT if staff_admin else 0)
    asyncio.run(rc.manager._handle_player_rights_set(
        rc.session, rights_payload("target", 1 << 20 | 1 << 31 | RCRight.BAN)))
    assert rc.target.admin_rights == ((1 << 20 | 1 << 31 | RCRight.BAN)
                                      if staff_admin else 0)


@pytest.mark.parametrize("own,right,allowed", [
    (True, RCRight.SETATTRIBUTES, False),
    (True, RCRight.SETSELFATTRIBUTES, True),
    (False, RCRight.SETSELFATTRIBUTES, False),
    (False, RCRight.SETATTRIBUTES, True),
])
@pytest.mark.parametrize("by_name", [False, True])
def test_attributes_self_other_gate(rc, own, right, allowed, by_name):
    name = "admin" if own else "target"
    rc.target_player.account_name = name
    rc.session.rights = right
    payload = packet().write_gstring(name).build() if by_name else packet().write_gshort(2).build()
    handler = rc.manager._handle_player_props_set2 if by_name else rc.manager._handle_player_props_set
    asyncio.run(handler(rc.session, payload + empty_prop_stream()))
    replies = [c.args[0] for c in rc.player.send_raw.await_args_list]
    assert any(b"set the attributes of player" in reply for reply in replies) == allowed


def test_default_account_edit_and_delete_protection(rc):
    rc.server.account_manager.create_account("(defaultaccount)")
    rc.session.rights = RCRight.SETATTRIBUTES
    asyncio.run(rc.manager._handle_player_props_set2(
        rc.session, packet().write_gstring("(defaultaccount)").build()))
    assert rc.player.send_raw.await_args.args[0] == build_rc_chat(
        "Server: You are not authorized to modify the default account.")
    rc.session.rights = RCRight.MODIFYSTAFFACCOUNT
    asyncio.run(rc.manager._handle_account_del(rc.session, b"(defaultaccount)"))
    rc.flush()
    assert (rc.root / "accounts/(defaultaccount).json").exists()
    assert rc.player.send_raw.await_args.args[0] == build_rc_chat(
        "Server: You are not allowed to delete the default account.")


@pytest.mark.parametrize("handler", ["_handle_account_add", "_handle_account_set"])
@pytest.mark.parametrize("can_ban", [False, True])
def test_account_metadata_requires_separate_ban_bit(rc, handler, can_ban):
    rc.session.rights = RCRight.MODIFYSTAFFACCOUNT | (RCRight.BAN if can_ban else 0)
    name = "new" if handler.endswith("add") else "target"
    asyncio.run(getattr(rc.manager, handler)(rc.session, account_payload(name)))
    rc.flush()
    account = rc.server.account_manager.get_account(name, reload=True)
    assert account.is_banned == can_ban
    assert account.ban_reason == ("reason" if can_ban else "")
    assert account.profile_email == "new@example.test"
    assert account.load_only


@pytest.mark.parametrize("staff_admin", [False, True])
def test_server_admin_options_preserved(rc, staff_admin):
    rc.session.rights = RCRight.SETSERVEROPTIONS | (RCRight.MODIFYSTAFFACCOUNT if staff_admin else 0)
    asyncio.run(rc.manager._handle_server_options_set(
        rc.session, b'"name=changed","description=changed","startlevel=new"'))
    assert rc.server.config.name == ("changed" if staff_admin else "old")
    assert rc.server.config.description == ("changed" if staff_admin else "old")
    assert rc.server.config.start_level == "new"


@pytest.mark.parametrize("handler,payload", [
    ("_handle_server_options_get", b""), ("_handle_folder_config_get", b""),
    ("_handle_server_flags_get", b""), ("_handle_account_list_get", b""),
    ("_handle_account_get", b"target"), ("_handle_player_comments_get", b"target"),
    ("_handle_player_ban_get", b"target"), ("_handle_player_rights_get", b"admin"),
])
def test_control_only_reads_need_no_permission_bits(rc, handler, payload):
    # PlayerRCPackets.cpp:142: the folder config reply needs the file to exist.
    (rc.root / "config").mkdir(exist_ok=True)
    (rc.root / "config/foldersconfig.txt").write_text("file *.png\n")
    rc.session.rights = 0
    asyncio.run(getattr(rc.manager, handler)(rc.session, payload))
    assert rc.player.send_raw.await_count == 1
    assert rc.player.send_raw.await_args.args[0][0] != PLO.RC_CHAT + 32


@pytest.mark.parametrize("handler", [
    "_handle_respawn_set", "_handle_horse_life_set", "_handle_ap_increment_set",
    "_handle_baddy_respawn_set", "_handle_player_props_get", "_handle_list_rcs",
    "_handle_disconnect_rc", "_handle_apply_reason",
])
def test_deprecated_packets_have_no_effect(rc, handler):
    rc.session.rights = 0xfffff
    asyncio.run(getattr(rc.manager, handler)(rc.session, b"malformed"))
    rc.player.send_raw.assert_not_awaited()
    rc.target_player.disconnect.assert_not_awaited()


def file_payload(operation, name="file.txt", destination="images"):
    if operation == "up":
        return packet().write_gstring(name).write_bytes(b"new").build()
    if operation == "move":
        return packet().write_gstring(destination).write_string(name).build()
    if operation == "rename":
        return packet().write_gstring(name).write_gstring("renamed.txt").build()
    if operation == "folder_delete":
        return b"levels/empty"
    return name.encode("latin-1")


def run_file(rc, operation, payload=None):
    handler = ("_handle_" + operation if operation.startswith("large_file_")
               or operation == "folder_delete" else "_handle_file_browser_" + operation)
    asyncio.run(getattr(rc.manager, handler)(rc.session, payload or file_payload(operation)))


@pytest.mark.parametrize("operation", ["down", "up", "move", "delete", "rename", "large_file_start", "large_file_end", "folder_delete"])
@pytest.mark.parametrize("allowed", [False, True])
def test_file_operation_folder_rights_matrix(rc, operation, allowed):
    rc.session.rights = 0
    flag = "r" if operation == "down" else "w"
    rc.admin.folders = [f"{flag} levels/*", f"{flag} images/*", f"{flag} levels/empty/*"] if allowed else ["r config/*"]
    source = rc.root / "levels/file.txt"
    source.write_bytes(b"ORIGINAL")
    (rc.root / "levels/empty").mkdir()
    if operation == "large_file_end":
        rc.session.large_file_uploads["file.txt"] = bytearray(b"new")
    run_file(rc, operation, file_payload(operation, "file.txt"))
    if operation in ("up", "large_file_end"):
        assert source.read_bytes() == (b"new" if allowed else b"ORIGINAL")
    elif operation in ("move", "delete", "rename"):
        assert source.exists() != allowed
        if operation == "move":
            assert (rc.root / "images/file.txt").exists() == allowed
        if operation == "rename":
            assert (rc.root / "levels/renamed.txt").exists() == allowed
    elif operation == "folder_delete":
        assert (rc.root / "levels/empty").exists() != allowed
    elif operation == "large_file_start":
        assert ("file.txt" in rc.session.large_file_uploads) == allowed
    else:
        assert any(b"ORIGINAL" in c.args[0] for c in rc.player.send_raw.await_args_list) == allowed


@pytest.mark.parametrize("operation", ["start", "cd", "down", "up", "move", "delete", "rename", "large_file_start", "large_file_end", "folder_delete"])
def test_empty_folder_list_denies_every_file_operation(rc, operation):
    rc.admin.folders = []
    rc.session.rights = 0xfffff
    source = rc.root / "levels/file.txt"
    source.write_bytes(b"ORIGINAL")
    (rc.root / "levels/empty").mkdir()
    if operation == "large_file_end":
        rc.session.large_file_uploads["file.txt"] = bytearray(b"new")
    payload = b"levels" if operation == "cd" else file_payload(operation)
    run_file(rc, operation, payload)
    assert source.read_bytes() == b"ORIGINAL"
    assert not (rc.root / "images/file.txt").exists()
    assert not (rc.root / "levels/renamed.txt").exists()
    assert (rc.root / "levels/empty").exists()
    assert not any(b"ORIGINAL" in c.args[0] for c in rc.player.send_raw.await_args_list)


@pytest.mark.parametrize("path", sorted(PROTECTED_DOWNLOADS))
@pytest.mark.parametrize("allowed", [False, True])
def test_protected_downloads(rc, path, allowed):
    target = rc.root / path
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"secret")
    rc.session.file_browser_path = target.parent.relative_to(rc.root).as_posix()
    rc.session.rights = RCRight.MODIFYSTAFFACCOUNT if allowed else 0
    run_file(rc, "down", target.name.encode())
    replies = [c.args[0] for c in rc.player.send_raw.await_args_list]
    assert any(b"secret" in reply for reply in replies) == allowed
    if not allowed:
        assert replies[-1] == (packet().write_gchar(PLO.RC_FILEBROWSER_MESSAGE)
                              .write_string("Insufficient rights to download/view " + path)
                              .write_newline().build())


@pytest.mark.parametrize("path,right", list(IMPORTANT_FILES.items()))
@pytest.mark.parametrize("grant", ["none", "specific", "staff"])
def test_protected_uploads(rc, path, right, grant):
    target = rc.root / path
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"ORIGINAL")
    rc.session.file_browser_path = target.parent.relative_to(rc.root).as_posix()
    rc.session.rights = {"none": 0, "specific": right, "staff": RCRight.MODIFYSTAFFACCOUNT}[grant]
    run_file(rc, "up", file_payload("up", target.name))
    assert target.read_bytes() == (b"ORIGINAL" if grant == "none" else b"new")
    if grant == "none":
        assert rc.player.send_raw.await_args.args[0] == (
            packet().write_gchar(PLO.RC_FILEBROWSER_MESSAGE)
            .write_string("Insufficent rights to upload " + path).write_newline().build())


@pytest.mark.parametrize("operation", ["move", "delete", "rename"])
@pytest.mark.parametrize("path", list(IMPORTANT_FILES))
def test_important_files_cannot_be_removed_even_by_staff(rc, operation, path):
    target = rc.root / path
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"ORIGINAL")
    rc.session.file_browser_path = target.parent.relative_to(rc.root).as_posix()
    rc.session.rights = 0xfffff
    run_file(rc, operation, file_payload(operation, target.name))
    assert target.read_bytes() == b"ORIGINAL"
    assert b"Not allowed to" in rc.player.send_raw.await_args.args[0]


@pytest.mark.parametrize("operation", ["down", "up", "move", "delete", "rename", "large_file_start", "large_file_end", "folder_delete", "cd"])
@pytest.mark.parametrize("escape", ["../outside", "/tmp/outside", "C:\\outside", "link"])
def test_path_escape_refused_regardless_of_rights(rc, operation, escape):
    outside = rc.root.parent / (rc.root.name + "-outside")
    outside.mkdir()
    secret = outside / "file.txt"
    secret.write_bytes(b"secret")
    (rc.root / "levels/link").symlink_to(outside, target_is_directory=True)
    (rc.root / "link").symlink_to(outside, target_is_directory=True)
    (rc.root / "levels/file.txt").write_bytes(b"ORIGINAL")
    rc.admin.folders = ["rw levels/*", "rw levels/link/*", "rw *", "rw link/*"]
    rc.session.rights = 0xfffff
    if operation in ("down", "up", "delete", "large_file_start", "large_file_end"):
        rc.session.file_browser_path = escape
        rc.session.large_file_uploads["file.txt"] = bytearray(b"new")
        payload = file_payload(operation)
    elif operation == "move":
        payload = file_payload(operation, destination=escape)
    elif operation == "rename":
        payload = packet().write_gstring("file.txt").write_gstring(escape + "/file.txt").build()
    else:
        payload = escape.encode()
    run_file(rc, operation, payload)
    assert secret.read_bytes() == b"secret"
    assert (rc.root / "levels/file.txt").read_bytes() == b"ORIGINAL"
    assert outside.is_dir()
    assert not any(b"secret" in c.args[0] for c in rc.player.send_raw.await_args_list)


def test_folder_map_exact_selection_and_wildcard_listing(rc):
    rc.admin.folders = ["r levels/*.txt", "w levels/*.nw"]
    (rc.root / "levels/readme.txt").write_bytes(b"text")
    (rc.root / "levels/map.nw").write_bytes(b"map")
    (rc.root / "levels/hidden.bin").write_bytes(b"hidden")
    (rc.root / "levels/sub").mkdir()
    run_file(rc, "cd", b"levels/sub")
    rc.player.send_raw.assert_not_awaited()
    run_file(rc, "cd", b"levels/")
    reply = rc.player.send_raw.await_args.args[0]
    assert b"readme.txt!r" in reply
    assert b"map.nw!w" in reply
    assert b"hidden.bin" not in reply
    assert b"sub" not in reply
    run_file(rc, "cd", b"levels/")
    assert rc.session.file_browser_path == "levels"


def test_negative_folder_rules_override_positive_entries(rc):
    rc.admin.folders = ["rw levels/*", "-w levels/file.txt"]
    source = rc.root / "levels/file.txt"
    source.write_bytes(b"ORIGINAL")
    run_file(rc, "up")
    assert source.read_bytes() == b"ORIGINAL"
    run_file(rc, "down")
    assert b"ORIGINAL" in rc.player.send_raw.await_args.args[0]


def test_download_uses_exact_path_instead_of_general_filename_search(rc):
    (rc.root / "images/file.txt").write_bytes(b"wrong")
    (rc.root / "levels/file.txt").write_bytes(b"correct")
    run_file(rc, "down")
    assert b"correct" in rc.player.send_raw.await_args.args[0]
    assert b"wrong" not in rc.player.send_raw.await_args.args[0]


def test_rights_reply_includes_five_byte_mask_ip_and_counted_folders(rc):
    rc.session.rights = RCRight.SETRIGHTS
    rc.target.admin_rights = 0x80000
    rc.target.admin_ip = "127.*"
    rc.target.folders = ["rw levels/*", "r images/*"]
    asyncio.run(rc.manager._handle_player_rights_get(rc.session, b"target"))
    reader = PacketReader(rc.player.send_raw.await_args.args[0])
    assert reader.read_gchar() == PLO.RC_PLAYERRIGHTSGET
    assert reader.read_gstring() == "target"
    assert reader.read_gint5() == 0x80000
    assert reader.read_gstring() == "127.*"
    assert reader.read_gstring_short() == "rw levels/*,r images/*"
    assert reader.remaining() == b"\n"


def test_nc_chat_is_ignored(rc):
    rc.player.connection_type = PLTYPE.NC
    asyncio.run(rc.manager._handle_rc_chat(rc.session, b"hello"))
    rc.player.send_raw.assert_not_awaited()


def test_rights_enum_matches_reference_bits():
    # GServer-v2/server/include/Account.h:29-50
    assert (PLPERM.MODIFYSTAFFACCOUNT, PLPERM.SETSERVERFLAGS, PLPERM.SETSERVEROPTIONS,
            PLPERM.SETFOLDEROPTIONS, PLPERM.SETFOLDERRIGHTS, PLPERM.NPCCONTROL) == (
            0x4000, 0x8000, 0x10000, 0x20000, 0x40000, 0x80000)
    assert RCRight is PLPERM


def test_move_onto_important_file_is_refused(rc):
    (rc.root / "config").mkdir()
    (rc.root / "config/rules.txt").write_bytes(b"ORIGINAL")
    (rc.root / "levels/rules.txt").write_bytes(b"replacement")
    rc.session.rights = 0xfffff
    run_file(rc, "move", file_payload("move", "rules.txt", "config"))
    assert (rc.root / "config/rules.txt").read_bytes() == b"ORIGINAL"
    assert (rc.root / "levels/rules.txt").exists()


def test_non_staff_options_cannot_reach_internal_config_names(rc):
    rc.server.config.staff = ["admin"]
    rc.server.config.accounts_dir = "accounts"
    rc.session.rights = RCRight.SETSERVEROPTIONS
    asyncio.run(rc.manager._handle_server_options_set(
        rc.session, b'"staff=attacker","accounts_dir=/etc","startlevel=new"'))
    assert rc.server.config.staff == ["admin"]
    assert rc.server.config.accounts_dir == "accounts"
    assert rc.server.config.start_level == "new"


def test_rights_edit_gives_staff_live_session_only_the_stored_rights(rc):
    # Player.cpp:685-694: staff status gates login only; rights are the stored ones.
    rc.server.is_staff = lambda name: name.lower() == "target"
    live = RCSession(player=rc.target_player, rights=0xfffff)
    rc.manager._sessions[rc.target_player.id] = live
    rc.session.rights = RCRight.SETRIGHTS | RCRight.MODIFYSTAFFACCOUNT
    asyncio.run(rc.manager._handle_player_rights_set(
        rc.session, rights_payload(rights=RCRight.BAN)))
    assert rc.target.admin_rights == RCRight.BAN
    assert live.rights == rc.target.admin_rights


@pytest.mark.parametrize("packet_id", [PLI.RC_LARGEFILESTART, PLI.RC_LARGEFILEEND, PLI.RC_FOLDERDELETE])
def test_large_file_and_folder_delete_ids_reach_the_rc_manager(rc, packet_id):
    rc.manager.handle_packet = AsyncMock()
    connection = SimpleNamespace(
        id=rc.player.id, server=rc.server, _handlers={}, _unhandled_packet_ids=set())
    rc.server.rc_manager = rc.manager
    dispatch = MethodType(Player._dispatch_packet, connection)
    asyncio.run(dispatch(packet_id, b"levels/empty"))
    rc.manager.handle_packet.assert_awaited_once_with(connection, packet_id, b"levels/empty")


def _set_options(rc, text, rights=RCRight.SETSERVEROPTIONS):
    from pygserver.config import ServerConfig
    rc.server.config = ServerConfig()
    rc.session.rights = rights
    rc.player.send_raw.reset_mock()
    asyncio.run(rc.manager._handle_server_options_set(rc.session, text.encode("latin-1")))
    return rc.server.config


def test_non_numeric_option_value_is_rejected_and_keeps_typed_value(rc):
    config = _set_options(
        rc, "startx=abc\nstarty=30\nsword_limit=99\nswordlimit=x\nheartlimit=nan")
    assert config.start_x == 30.0 and isinstance(config.start_x, float)
    assert config.start_y == 30.0
    assert config.sword_limit == 3
    assert config.heart_limit == 3
    sent = b"".join(c.args[0] for c in rc.player.send_raw.await_args_list)
    assert b"Invalid value for server option startx" in sent
    assert b"swordlimit" in sent


def test_non_finite_float_option_is_rejected(rc):
    config = _set_options(rc, "startx=nan\nstarty=inf")
    assert (config.start_x, config.start_y) == (30.0, 30.5)


def test_unknown_option_keys_never_become_config_attributes(rc):
    config = _set_options(
        rc, "heartbeat_interval=x\nbase_dir=/etc\n__class__=1\nmystery = hello")
    assert config.heartbeat_interval == 5.0
    assert config.base_dir == "."
    assert not hasattr(config, "mystery")
    assert rc.manager.extra_server_options == {
        "heartbeat_interval": "x", "__class__": "1",
        "mystery": "hello"}


def test_extra_option_store_is_bounded(rc):
    from pygserver.rc import MAX_EXTRA_OPTIONS
    _set_options(rc, "\n".join(f"k{i}=v" for i in range(MAX_EXTRA_OPTIONS + 10)))
    assert len(rc.manager.extra_server_options) == MAX_EXTRA_OPTIONS


def test_legitimate_option_changes_apply_with_loader_types(rc):
    config = _set_options(
        rc, "startx=12.5\nstartlevel=new.nw\nswordlimit=5\nhealswords=true\naptime2=77",
        RCRight.SETSERVEROPTIONS)
    assert config.start_x == 12.5 and isinstance(config.start_x, float)
    assert config.start_level == "new.nw"
    assert config.sword_limit == 5 and isinstance(config.sword_limit, int)
    assert config.heal_swords is True
    assert config.aptime[2] == 77


@pytest.mark.parametrize("staff_admin", [False, True])
def test_login_verification_option_is_admin_only(rc, staff_admin):
    rights = RCRight.SETSERVEROPTIONS | (RCRight.MODIFYSTAFFACCOUNT if staff_admin else 0)
    config = _set_options(rc, "noverifylogin=false\nstaff=mallory", rights)
    assert config.verify_login is staff_admin
    assert config.staff == (["mallory"] if staff_admin else [])


@pytest.mark.parametrize("pattern,path,expected", [
    ("rw levels/a.nw", "levels/a.nw", "rw"),
    ("rw levels/a.nw", "levels/aXnw", ""),
    ("rw levels/b?.nw", "levels/b1.nw", "rw"),
    ("rw levels/b?.nw", "levels/bb.nw", "rw"),
    ("rw levels/b?.nw", "levels/b.nw", ""),
    ("rw levels/b?.nw", "levels/B1.nw", ""),
    ("rw levels/*.nw", "levels/x.nw", "rw"),
    ("rw levels/*.nw", "levels/sub/x.nw", ""),
    ("rw levels/*", "levels/sub/x.nw", ""),
    ("rw levels/[ab].nw", "levels/a.nw", ""),
    ("rw levels/[ab].nw", "levels/[ab].nw", "rw"),
    ("rw levels/(x).nw", "levels/(x).nw", "rw"),
])
def test_folder_rights_use_wildcards_not_regex(rc, pattern, path, expected):
    rc.session.rights = 0
    rc.admin.folders = [pattern]
    assert rc.manager._file_rights(rc.session, path) == expected


def test_hostile_folder_pattern_does_not_backtrack(rc):
    import time
    rc.session.rights = 0
    rc.admin.folders = ["rw levels/" + "*a" * 40 + "b", "rw levels/(a+)+$"]
    started = time.monotonic()
    assert rc.manager._file_rights(rc.session, "levels/" + "a" * 5000) == ""
    assert time.monotonic() - started < 2
