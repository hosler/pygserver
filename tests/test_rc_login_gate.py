"""RC/RC2/NC login gate: staff membership AND an admin-IP match, nothing else.

GServer-v2 server/src/player/Player.cpp:685-694 (gate), :836-845 (admin IP),
Server.cpp:1773 (staff, case-insensitive), StringUtils.h:1622 (wildcards),
PlayerRCPackets.cpp:913-918 (NPC control removed -> NC session dropped).
"""

import asyncio
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import Account, AccountManager, RCRight
from pygserver.admin_access import is_admin_ip, peer_ip, split_ip_range, wildcard_match
from pygserver.config import ServerConfig
from pygserver.nc import NCManager
from pygserver.player import Player
from pygserver.protocol.constants import PLI, PLO, PLTYPE
from pygserver.protocol.packet_codec import PacketBuilder
from pygserver.rc import RCManager, RCSession
from pygserver.server import GameServer

RC, NC, RC2 = 1, 3, 6
ALL_RIGHTS = sum(int(right) for right in RCRight)
LOOPBACK = ("127.0.0.1", 50000)
REFUSAL = "You do not have RC rights."


def login_bytes(account, client_type):
    builder = PacketBuilder().write_gchar(client_type)
    if client_type not in (RC, NC):
        builder.write_gchar(7)  # gen-2 types send no key byte
    body = (builder.write_string("G3D0311C").write_gstring(account)
            .write_gstring("pw").build())
    compressed = zlib.compress(body)
    return struct.pack(">H", len(compressed)) + compressed


def make_server(staff, account=None, account_manager=None):
    config = ServerConfig(staff=list(staff))
    config.name = "Gate"
    config.start_level = "start.nw"
    server = GameServer(config)
    server.rc_manager = RCManager(server)
    server.nc_manager = NCManager(server)
    server.listserver = None
    server.irc_manager = None
    server.gs2_manager = MagicMock()
    server.gs2_manager.announce_weapons = AsyncMock()
    if account_manager is None:
        account_manager = MagicMock()
        account_manager.get_account.return_value = account
    server.account_manager = account_manager
    return server


def login(server, name, client_type, peer=LOOPBACK, player_id=2):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    server.players[player_id] = player
    player.session.writer.get_extra_info.return_value = peer
    player.session.read = AsyncMock(return_value=login_bytes(name, client_type))
    player.login_service.send_login_response = AsyncMock()
    player.send_raw = AsyncMock()
    player.warp = AsyncMock()
    player.disconnect = AsyncMock()
    assert asyncio.run(player._handle_login()) is True
    return player


def admitted(server, player):
    return (server.rc_manager.get_session(player.id) is not None
            or server.nc_manager.get_session(player.id) is not None)


def persist(manager, *accounts):
    """Write edits through: login reloads idle accounts from disk."""
    for edited in accounts:
        manager.save_account(edited)
    manager._save_executor.submit(lambda: None).result()


def account(rights=0, ip_range="127.0.0.1"):
    return SimpleNamespace(admin_rights=rights, admin_ip=ip_range,
                           is_banned=False, ban_reason="")


@pytest.mark.parametrize("client_type", [RC, RC2, NC])
@pytest.mark.parametrize("staff", [True, False])
@pytest.mark.parametrize(("ip_range", "ip_ok"), [
    ("127.0.0.1", True), ("10.0.0.1", False), ("", False)])
@pytest.mark.parametrize("rights", [0, int(RCRight.WARPTO), ALL_RIGHTS, 1 << 20, 1 << 31])
def test_login_outcome_depends_only_on_staff_and_ip(client_type, staff, ip_range,
                                                    ip_ok, rights):
    server = make_server(["Admin"] if staff else ["someone"], account(rights, ip_range))
    player = login(server, "admin", client_type)

    expected = staff and ip_ok
    assert admitted(server, player) is expected
    assert player.logged_in is expected
    if expected:
        player.disconnect.assert_not_awaited()
        if client_type != NC:
            # Rights come from the account only; staff adds nothing.
            assert server.rc_manager.get_session(player.id).rights == rights
    else:
        player.disconnect.assert_awaited_once_with(REFUSAL)


def test_non_staff_with_every_right_is_refused():
    server = make_server(["owner"], account(ALL_RIGHTS, "*"))
    player = login(server, "admin", RC2)
    assert not admitted(server, player)
    player.disconnect.assert_awaited_once_with(REFUSAL)


def test_undefined_high_rights_bit_does_not_open_a_session():
    server = make_server([], account(1 << 24, "127.0.0.1"))
    player = login(server, "admin", RC2)
    assert not admitted(server, player)


def test_staff_with_zero_rights_logs_in_but_privileged_packets_are_denied(tmp_path):
    server = make_server(["admin"], account_manager=AccountManager(
        GameServer(ServerConfig()), str(tmp_path / "accounts")))
    manager = server.account_manager
    admin = manager.create_account("admin")
    admin.admin_ip = "127.0.0.1"
    target = manager.create_account("target")
    target.admin_rights = int(RCRight.WARPTO)
    persist(manager, admin, target)
    server.config.description = "unchanged"
    player = login(server, "admin", RC2)
    admin = manager.get_account("admin")
    session = server.rc_manager.get_session(player.id)

    assert session is not None and session.rights == 0
    assert not any(session.has_right(right) for right in RCRight)

    def rights_set(name):
        return (PacketBuilder().write_gstring(name).write_gint5(ALL_RIGHTS)
                .write_gstring("*").write_gstring_short("rw */*").build())

    for packet_id, data in ((PLI.RC_PLAYERRIGHTSSET, rights_set("target")),
                            (PLI.RC_PLAYERRIGHTSSET, rights_set("admin")),
                            (PLI.RC_SERVEROPTIONSSET, b"description = hijacked")):
        asyncio.run(server.rc_manager.handle_packet(player, packet_id, data))

    assert target.admin_rights == int(RCRight.WARPTO)
    assert target.admin_ip == "0.0.0.0"
    assert admin.admin_rights == 0
    assert server.config.description == "unchanged"
    replies = b"".join(call.args[0] for call in player.send_raw.await_args_list)
    assert b"not authorized" in replies


@pytest.mark.parametrize(("peer", "ip_range", "ok"), [
    (("::ffff:127.0.0.1", 50000, 0, 0), "127.0.0.1", True),
    (("::1", 50000, 0, 0), "::1", True),
    (("::1", 50000, 0, 0), "127.0.0.1", False),
    (("2001:db8::7", 50000, 0, 0), "2001:db8::*", True),
    (None, "*", False),
])
def test_peer_address_forms(peer, ip_range, ok):
    server = make_server(["admin"], account(0, ip_range))
    player = login(server, "admin", RC2, peer=peer)
    assert admitted(server, player) is ok


@pytest.mark.parametrize(("ip", "mask", "ok"), [
    ("127.0.0.1", "127.0.0.1", True),
    ("127.0.0.1", "127.0.0.*", True),
    ("127.0.0.1", "127.*", True),
    ("127.0.0.1", "*", True),
    ("127.0.0.1", "*.*.*.*", True),
    ("127.0.0.1", "127.0.0.?", True),
    ("127.0.0.1", "1?7.0.*.1", True),
    ("127.0.0.12", "127.0.0.?", False),
    ("127.0.0.1", "127.0.0.2", False),
    ("127.0.0.1", "0.0.0.0", False),
    ("127.0.0.1", "", False),
    ("127.0.0.1", "127.0.0.1*", False),   # reference quirk, kept
    ("", "*", False),
    ("10.1.2.3", "10.*.3", True),
    ("10.1.2.4", "10.*.3", False),
])
def test_wildcard_match_mirrors_reference(ip, mask, ok):
    assert wildcard_match(ip, mask) is ok


def test_ip_range_is_comma_split_without_trimming():
    assert split_ip_range("10.*,,127.0.0.1") == ["10.*", "127.0.0.1"]
    assert is_admin_ip("127.0.0.1", "10.*,127.0.0.1")
    assert not is_admin_ip("127.0.0.1", "10.*, 127.0.0.1")
    assert not is_admin_ip("127.0.0.1", "")
    assert not is_admin_ip(None, "*")
    assert peer_ip("::ffff:10.0.0.5") == "10.0.0.5"
    assert peer_ip(MagicMock()) is None


def test_new_account_ships_reference_default_range_and_cannot_log_in(tmp_path):
    manager = AccountManager(GameServer(ServerConfig()), str(tmp_path / "accounts"))
    fresh = manager.create_account("admin")
    assert Account(account_name="x").admin_ip == fresh.admin_ip == "0.0.0.0"
    server = make_server(["admin"], account_manager=manager)
    player = login(server, "admin", RC2)
    assert not admitted(server, player)


def test_nc_without_npc_control_is_admitted_and_served_like_the_reference():
    # The reference gates only the NC address push on NPC control
    # (NPCServer.cpp:120-131); NC login and NC packets never check it.
    server = make_server(["admin"], account(int(RCRight.WARPTO), "127.0.0.1"))
    server.gs2_manager.weapons = {"bow": SimpleNamespace(name="bow")}
    player = login(server, "admin", NC)
    assert server.nc_manager.get_session(player.id) is not None
    assert player.connection_type == PLTYPE.NC
    player.send_raw.reset_mock()

    asyncio.run(server.nc_manager.handle_packet(player, PLI.NC_WEAPONLISTGET, b""))

    sent = [call.args[0][0] - 32 for call in player.send_raw.await_args_list]
    assert PLO.NC_WEAPONLISTGET in sent


def _rights_edit_server(tmp_path, new_rights):
    manager = AccountManager(GameServer(ServerConfig()), str(tmp_path / "accounts"))
    server = make_server(["coder", "boss"], account_manager=manager)
    coder = manager.create_account("coder")
    coder.admin_rights = ALL_RIGHTS
    coder.admin_ip = "127.0.0.1"
    persist(manager, coder)
    nc_player = login(server, "coder", NC, player_id=3)
    assert server.nc_manager.get_session(nc_player.id) is not None

    boss = Player(server, 4, AsyncMock(), MagicMock())
    boss.account_name = "boss"
    boss.send_raw = AsyncMock()
    editor = RCSession(player=boss, rights=ALL_RIGHTS)
    payload = (PacketBuilder().write_gstring("coder").write_gint5(new_rights)
               .write_gstring("127.0.0.1").write_gstring_short("").build())
    asyncio.run(server.rc_manager._handle_player_rights_set(editor, payload))
    return server, manager.get_account("coder"), nc_player


def test_removing_npc_control_drops_the_live_nc_session(tmp_path):
    server, coder, nc_player = _rights_edit_server(
        tmp_path, ALL_RIGHTS & ~int(RCRight.NPCCONTROL))
    assert not coder.admin_rights & RCRight.NPCCONTROL
    nc_player.disconnect.assert_awaited_once_with()
    assert server.nc_manager.get_session(nc_player.id) is None


def test_keeping_npc_control_leaves_the_nc_session(tmp_path):
    server, _, nc_player = _rights_edit_server(tmp_path, ALL_RIGHTS)
    nc_player.disconnect.assert_not_awaited()
    assert server.nc_manager.get_session(nc_player.id) is not None


def test_staff_option_change_grants_no_rights(tmp_path):
    # An options upload that adds an account to `staff` yields an RC login at
    # most (and only from its own admin-IP range); rights stay as stored.
    manager = AccountManager(GameServer(ServerConfig()), str(tmp_path / "accounts"))
    server = make_server([], account_manager=manager)
    ops = manager.create_account("ops")
    ops.admin_rights = int(RCRight.SETSERVEROPTIONS)
    ops.admin_ip = "127.0.0.1"
    persist(manager, ops)
    refused = login(server, "ops", RC2, player_id=2)
    assert not admitted(server, refused)

    server.config.staff = ["ops"]
    player = login(server, "ops", RC2, player_id=3)
    assert server.rc_manager.get_session(player.id).rights == int(RCRight.SETSERVEROPTIONS)
    ops = manager.get_account("ops")
    ops.admin_ip = "0.0.0.0"
    persist(manager, ops)
    assert not admitted(server, login(server, "ops", RC2, player_id=4))
