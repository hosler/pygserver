"""Server options `onlystaff`, `dontaddserverflags` and `protectedweapons`.

Reference: GServer-v2 origin/beta4 server/src/player/Player.cpp:697-706
(onlystaff), ServerList.cpp:276-279,479-482 (HQ level 0),
player/packets/PlayerClientPackets.cpp:659-669,702-711 (server flags),
player/PlayerClient.cpp:605-610 and PlayerClientPackets.cpp:878-888
(protected weapons).
"""

import asyncio
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.config import ServerConfig
from pygserver.listserver import ServerListClient
from pygserver.nc import NCManager
from pygserver.player import Player
from reborn_protocol import SVO
from pygserver.protocol.packet_codec import PacketBuilder
from pygserver.protocol.packets import PacketReader
from pygserver.rc import RCManager, _probe_option
from pygserver.server import GameServer

STAFF_ONLY = "This server is currently restricted to staff only."
RC_REFUSAL = "You do not have RC rights."
LOOPBACK = ("127.0.0.1", 50000)


def login_bytes(account, client_type):
    builder = PacketBuilder().write_gchar(client_type)
    if client_type not in (1, 3):
        builder.write_gchar(7)  # gen-2 types send no key byte
    body = (builder.write_string("G3D0311C").write_gstring(account)
            .write_gstring("pw").build())
    compressed = zlib.compress(body)
    return struct.pack(">H", len(compressed)) + compressed


def make_server(*, staff=("admin",), onlystaff=False, ip_range="127.0.0.1", **options):
    config = ServerConfig(staff=list(staff), onlystaff=onlystaff, **options)
    config.start_level = "start.nw"
    server = GameServer(config)
    server.rc_manager = RCManager(server)
    server.nc_manager = NCManager(server)
    server.listserver = None
    server.irc_manager = None
    server.gs2_manager = MagicMock()
    server.gs2_manager.announce_weapons = AsyncMock()
    manager = MagicMock()
    manager.get_account.return_value = SimpleNamespace(
        admin_rights=0, admin_ip=ip_range, is_banned=False, ban_reason="")
    server.account_manager = manager
    return server


def login(server, name, client_type, player_id=2):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    server.players[player_id] = player
    player.session.writer.get_extra_info.return_value = LOOPBACK
    player.session.read = AsyncMock(return_value=login_bytes(name, client_type))
    player.login_service.send_login_response = AsyncMock()
    player.send_raw = AsyncMock()
    player.warp = AsyncMock()
    player.disconnect = AsyncMock()
    # A login refused before the account is touched (onlystaff) returns False.
    result = asyncio.run(player._handle_login())
    assert result or not player.logged_in
    return player


# --- onlystaff ---------------------------------------------------------------

def test_onlystaff_refuses_a_non_staff_client_with_the_reference_message():
    server = make_server(onlystaff=True)
    player = login(server, "mallory", 0)
    assert not player.logged_in
    player.disconnect.assert_awaited_once_with(STAFF_ONLY)
    player.warp.assert_not_awaited()
    player.login_service.send_login_response.assert_not_awaited()


@pytest.mark.parametrize("client_type", [0, 5])  # CLIENT, CLIENT3
def test_onlystaff_admits_staff_clients_case_insensitively(client_type):
    server = make_server(onlystaff=True)
    player = login(server, "ADMIN", client_type)
    assert player.logged_in
    player.disconnect.assert_not_awaited()


def test_onlystaff_off_admits_everyone():
    server = make_server(onlystaff=False)
    player = login(server, "mallory", 0)
    assert player.logged_in
    player.disconnect.assert_not_awaited()


def test_onlystaff_judges_the_account_name_not_the_nickname():
    server = make_server(onlystaff=True)
    player = Player(server, 2, AsyncMock(), MagicMock())
    player.account_name = "mallory"
    player.nickname = "admin"
    player.send_raw = AsyncMock()
    player.disconnect = AsyncMock()
    player.login_service.send_login_response = AsyncMock()
    asyncio.run(player.login_service.complete_login())
    assert not player.logged_in
    player.disconnect.assert_awaited_once_with(STAFF_ONLY)


@pytest.mark.parametrize("account", ["admin ", " admin", "admin\t", "xadmin", "admin2"])
def test_onlystaff_staff_lookalike_account_names_are_refused(account):
    server = make_server(onlystaff=True)
    player = Player(server, 2, AsyncMock(), MagicMock())
    player.account_name = account
    player.send_raw = AsyncMock()
    player.disconnect = AsyncMock()
    player.login_service.send_login_response = AsyncMock()
    asyncio.run(player.login_service.complete_login())
    assert not player.logged_in
    player.disconnect.assert_awaited_once_with(STAFF_ONLY)


@pytest.mark.parametrize("client_type", [2, 8, 7])  # slots for NPCSERVER, EXTERNAL, WEB... any
def test_onlystaff_refuses_non_staff_on_every_connection_type(client_type):
    # The reference tests client types only; here no type is exempt, so a
    # login type that is neither client, RC nor NC cannot walk past the gate.
    server = make_server(onlystaff=True)
    player = login(server, "mallory", client_type)
    assert not player.logged_in
    assert player.disconnect.await_args.args[0] in (STAFF_ONLY, RC_REFUSAL)


@pytest.mark.parametrize("client_type", [1, 3, 6])  # RC, NC, RC2
def test_onlystaff_leaves_staff_control_logins_alone(client_type):
    server = make_server(onlystaff=True)
    player = login(server, "admin", client_type)
    assert player.logged_in
    player.disconnect.assert_not_awaited()


@pytest.mark.parametrize("client_type", [1, 3, 6])
def test_onlystaff_non_staff_control_login_still_gets_the_rc_message(client_type):
    # Player.cpp:685-694 runs before the onlystaff test, so the RC text wins.
    server = make_server(onlystaff=True)
    player = login(server, "mallory", client_type)
    assert not player.logged_in
    player.disconnect.assert_awaited_once_with(RC_REFUSAL)


def test_onlystaff_staff_without_admin_ip_can_still_play_as_client():
    # Only RC and NC need the admin-IP match (Player.cpp:689); a staff client
    # does not, as long as the account has no IP range of its own.
    server = make_server(onlystaff=True, ip_range="")
    player = login(server, "admin", 0)
    assert player.logged_in


def test_onlystaff_parses_and_round_trips():
    path_text = "onlystaff = true\n"
    config = ServerConfig()
    assert config.onlystaff is False
    fields, _ = _probe_option("onlystaff", "true")
    assert fields == {"onlystaff": True}
    fields, _ = _probe_option("onlystaff", "false")
    assert fields == {"onlystaff": False}
    assert "onlystaff = false" in config.option_lines()
    config.onlystaff = True
    assert "onlystaff = true" in config.option_lines()
    assert path_text  # the text form is what the loader reads (probe above)


# --- onlystaff and the list server ------------------------------------------

def _hq_packets(client):
    seen = []
    for call in client._send_packet.await_args_list:
        reader = PacketReader(call.args[0])
        packet_id = reader.read_gchar()
        if packet_id == SVO.SERVERHQLEVEL:
            seen.append(("level", reader.read_gchar()))
        elif packet_id == SVO.SERVERHQPASS:
            seen.append(("pass", reader.remaining().decode().strip()))
    return seen


def _list_client(**options):
    server = MagicMock()
    server.config = ServerConfig(hq_level=2, hq_password="pw", **options)
    client = ServerListClient(server)
    client.connected = True
    client._send_packet = AsyncMock()
    return client


def test_listserver_reports_hq_level_zero_only_when_onlystaff():
    for onlystaff, expected in ((False, 2), (True, 0)):
        client = _list_client(onlystaff=onlystaff)
        asyncio.run(client._send_hq_level())
        assert _hq_packets(client) == [("level", expected)]


def test_listserver_resends_the_hq_pair_when_onlystaff_flips():
    client = _list_client()
    asyncio.run(client._send_hq_level())
    client._send_packet.reset_mock()

    asyncio.run(client._refresh_server_hq())          # nothing changed
    client._send_packet.assert_not_awaited()

    client.config.onlystaff = True                    # e.g. an RC option edit
    asyncio.run(client._refresh_server_hq())
    assert _hq_packets(client) == [("pass", "pw"), ("level", 0)]

    client._send_packet.reset_mock()
    asyncio.run(client._refresh_server_hq())          # already told
    client._send_packet.assert_not_awaited()

    client.config.onlystaff = False
    asyncio.run(client._refresh_server_hq())
    assert _hq_packets(client) == [("pass", "pw"), ("level", 2)]


# --- dontaddserverflags ------------------------------------------------------

def flag_server(tmp_path, *, serverside=False, dontadd=False):
    config = ServerConfig.from_server_dir(str(tmp_path))
    config.serverside = serverside
    config.dontaddserverflags = dontadd
    return GameServer(config)


def flag_player(server):
    player = Player(server, 3, AsyncMock(), SimpleNamespace())
    player.send_raw = AsyncMock()
    return player


@pytest.mark.parametrize("packet", [
    b"server.x=1", b"server.x", b"myserver.x=1", b"a.server.b=1",
])
async def test_dontaddserverflags_blocks_every_server_flag_set(tmp_path, packet):
    server = flag_server(tmp_path, dontadd=True)
    player = flag_player(server)
    await player._handle_flag_set(packet)
    assert dict(server.server_flags) == {}
    # PlayerClientPackets.cpp:667: swallowed, never stored as a client flag.
    assert player.flags == {}


async def test_dontaddserverflags_blocks_server_flag_delete(tmp_path):
    server = flag_server(tmp_path, dontadd=True)
    server.server_flags.put("server.keep", "v", notify=False)
    player = flag_player(server)
    await player._handle_flag_del(b"server.keep")
    await player._handle_flag_set(b"server.keep=")
    assert server.get_flag("server.keep") == "v"


async def test_server_flags_work_when_the_option_is_off(tmp_path):
    server = flag_server(tmp_path, dontadd=False)
    player = flag_player(server)
    await player._handle_flag_set(b"server.x=1")
    assert server.get_flag("server.x") == "1"
    await player._handle_flag_del(b"server.x")
    assert server.get_flag("server.x") == ""


async def test_dontaddserverflags_leaves_client_flags_alone(tmp_path):
    server = flag_server(tmp_path, dontadd=True)
    player = flag_player(server)
    await player._handle_flag_set(b"quest.done=yes")
    await player._handle_flag_set(b"serverr")  # not `server.`
    assert player.flags == {"quest.done": "yes", "serverr": True}
    await player._handle_flag_del(b"quest.done")
    assert "quest.done" not in player.flags


async def test_dontaddserverflags_is_moot_with_an_npc_server(tmp_path):
    # PlayerClientPackets.cpp:661: with an NPC server clients never reach the
    # server flag store, option or not.
    for dontadd in (False, True):
        server = flag_server(tmp_path, serverside=True, dontadd=dontadd)
        player = flag_player(server)
        await player._handle_flag_set(b"server.x=1")
        assert dict(server.server_flags) == {}


def test_dontaddserverflags_parses_and_round_trips():
    assert ServerConfig().dontaddserverflags is False
    assert _probe_option("dontaddserverflags", "true")[0] == {"dontaddserverflags": True}
    config = ServerConfig(dontaddserverflags=True)
    assert "dontaddserverflags = true" in config.option_lines()


# --- protectedweapons --------------------------------------------------------

def weapon_server(protected, weapons):
    """`weapons` maps weapon name to None (plain script) for gs2.get_weapon."""
    server = make_server(onlystaff=False, protectedweapons=list(protected))
    scripts = {name.lower(): SimpleNamespace(name=name) for name in weapons}
    server.gs2_manager.get_weapon = lambda name: scripts.get(name.lower())
    server.gs2_manager.announce_weapon = AsyncMock(return_value=True)
    return server


def test_protected_weapons_are_granted_at_login_when_missing():
    server = weapon_server(["Beer", "bomb", "Missing"], ["Beer"])
    player = login(server, "mallory", 0)
    assert player.logged_in
    # `Missing` names no weapon on the server, so nothing is granted for it
    # (Player.cpp:1103-1107); `bomb` is a built-in weapon.
    assert player.weapons == ["Beer", "bomb"]
    announced = [call.args[1] for call in
                 server.gs2_manager.announce_weapon.await_args_list]
    assert announced == ["Beer", "bomb"]


def test_protected_weapon_already_owned_is_not_granted_twice():
    server = weapon_server(["Beer"], ["Beer"])
    player = Player(server, 2, AsyncMock(), MagicMock())
    player.account_name = "mallory"
    player.weapons = ["beer"]  # same weapon, different case
    player.send_raw = AsyncMock()
    player.warp = AsyncMock()
    player.login_service.send_login_response = AsyncMock()
    asyncio.run(player.login_service.complete_login())
    assert player.weapons == ["beer"]
    server.gs2_manager.announce_weapon.assert_not_awaited()


def test_nothing_is_granted_without_the_option():
    server = weapon_server([], ["Beer"])
    player = login(server, "mallory", 0)
    assert player.weapons == []


@pytest.mark.parametrize("sent", [
    b"Beer", b"beer", b"BEER", b"Beer ", b" Beer", b"Beer\t", b"bomb",
])
async def test_protected_weapon_cannot_be_deleted_by_the_client(tmp_path, sent):
    server = weapon_server(["Beer", "bomb"], ["Beer"])
    player = flag_player(server)
    player.weapons = ["Beer", "bomb", "Plain"]
    await player._handle_npc_weapon_del(sent)
    assert player.weapons == ["Beer", "bomb", "Plain"]


async def test_unprotected_weapon_is_still_deleted(tmp_path):
    server = weapon_server(["Beer"], ["Beer"])
    player = flag_player(server)
    player.weapons = ["Beer", "Plain"]
    await player._handle_npc_weapon_del(b"Plain")
    assert player.weapons == ["Beer"]


def test_protectedweapons_parses_trims_and_round_trips():
    assert ServerConfig().protectedweapons == []
    fields, _ = _probe_option("protectedweapons", " Beer , ,bomb,")
    assert fields == {"protectedweapons": ["Beer", "bomb"]}
    config = ServerConfig(protectedweapons=["Beer", "bomb"])
    assert "protectedweapons = Beer,bomb" in config.option_lines()
    assert not any(line.startswith("protectedweapons")
                   for line in ServerConfig().option_lines())


# --- disableitemdropping / noexplosions / sendplayertriggers / baddyrespawntime ---

def _level_player(server, level_name="room.nw"):
    from pygserver.level import Level
    player = Player(server, 2, AsyncMock(), MagicMock())
    player.account_name = "mallory"
    player.level = Level(level_name)
    player.send_raw = AsyncMock()
    server.players[2] = player
    server.broadcast_to_level = AsyncMock()
    return player


async def test_disableitemdropping_takes_the_item_back_and_spawns_nothing(tmp_path):
    from pygserver.protocol.packets import build_item_del
    server = flag_server(tmp_path)
    server.item_manager = MagicMock(spawn_item=AsyncMock())
    player = _level_player(server)
    server.config.disableitemdropping = True
    # x=10.0 tiles half-step 20, y=12.0 -> raw bytes 20, 24, item 0
    await player._handle_item_add(bytes([20 + 32, 24 + 32, 0 + 32]))
    server.item_manager.spawn_item.assert_not_awaited()
    player.send_raw.assert_awaited_once_with(build_item_del(10.0, 12.0))


async def test_item_drop_still_works_by_default(tmp_path):
    server = flag_server(tmp_path)
    server.item_manager = MagicMock(spawn_item=AsyncMock())
    player = _level_player(server)
    await player._handle_item_add(bytes([20 + 32, 24 + 32, 0 + 32]))
    server.item_manager.spawn_item.assert_awaited_once()
    player.send_raw.assert_not_awaited()


@pytest.mark.parametrize("disabled", [True, False])
async def test_noexplosions_drops_the_client_explosion(tmp_path, disabled):
    server = flag_server(tmp_path)
    server.config.noexplosions = disabled
    server.combat_manager = MagicMock(
        handle_held_bomb_explosion=AsyncMock(return_value=False),
        blast_level_objects=AsyncMock())
    player = _level_player(server)
    await player._handle_explosion(bytes([4 + 32, 20 + 32, 20 + 32, 2 + 32]))
    relayed = server.broadcast_to_level.await_count
    assert (relayed, server.combat_manager.blast_level_objects.await_count) == (
        (0, 0) if disabled else (1, 1))


@pytest.mark.parametrize("relay", [True, False])
async def test_sendplayertriggers_gates_only_the_relay(tmp_path, relay):
    server = flag_server(tmp_path)
    server.config.sendplayertriggers = relay
    server.npc_manager.on_trigger_action = AsyncMock()
    player = _level_player(server)
    packet = bytes([32, 32, 32, 20 + 32, 20 + 32]) + b"ping,hello"
    await player._handle_trigger_action(packet)
    assert server.broadcast_to_level.await_count == (1 if relay else 0)
    server.npc_manager.on_trigger_action.assert_awaited_once()


def test_new_option_defaults_match_the_reference():
    config = ServerConfig()
    assert (config.disableitemdropping, config.noexplosions,
            config.sendplayertriggers, config.baddyrespawntime) == (False, False, True, 60)


def test_new_option_parsing_and_round_trip(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("disableitemdropping = true\nnoexplosions = TRUE # x\n"
                    "sendplayertriggers = false\nbaddyrespawntime = 5\n")
    config = ServerConfig.from_file(str(path))
    assert (config.disableitemdropping, config.noexplosions,
            config.sendplayertriggers, config.baddyrespawntime) == (True, True, False, 5)
    lines = config.option_lines()
    for line in ("disableitemdropping = true", "noexplosions = true",
                 "sendplayertriggers = false", "baddyrespawntime = 5"):
        assert line in lines
    # A value that is neither word keeps the default (Settings.h:208-216).
    path.write_text("sendplayertriggers = maybe\n")
    assert ServerConfig.from_file(str(path)).sendplayertriggers is True
    # RC option-set goes through the same loader.
    assert _probe_option("noexplosions", "true")[0] == {"noexplosions": True}
    assert _probe_option("baddyrespawntime", "7")[0] == {"baddyrespawntime": 7}


@pytest.mark.parametrize(("seconds", "waited", "respawns"), [
    (3, 5.0, True), (3, 1.0, False), (60, 5.0, False), (0, 0.1, True)])
async def test_baddyrespawntime_sets_the_dead_wait(seconds, waited, respawns):
    import time
    from pygserver.baddy import BDMODE, Baddy, BaddyManager, BaddyType
    server = MagicMock()
    server.config = ServerConfig(baddyrespawntime=seconds)
    manager = BaddyManager(server)
    manager._respawn_baddy = AsyncMock()
    baddy = Baddy(id=1, level_name="t.nw", baddy_type=BaddyType.GRAYBALL, x=1, y=1)
    baddy.dead, baddy.mode = True, BDMODE.DEAD
    baddy.death_time = time.time() - waited
    manager._baddies = {"t.nw": {1: baddy}}
    await manager._tick(0.05)
    assert manager._respawn_baddy.await_count == (1 if respawns else 0)
    # An RC edit applies to a baddy already waiting.
    if not respawns:
        server.config.baddyrespawntime = 0
        await manager._tick(0.05)
        assert manager._respawn_baddy.await_count == 1


# --- fix round: blank staff entries, clamped numbers, no files for refused logins ---

@pytest.mark.parametrize("text", [
    "staff = admin,\n", "staff = \n", "staff =\n", "staff = ,\n", "staff = admin, ,bob\n"])
def test_staff_option_never_holds_a_blank_entry(tmp_path, text):
    path = tmp_path / "serveroptions.txt"
    path.write_text(text)
    config = ServerConfig.from_file(str(path))
    assert all(entry.strip() for entry in config.staff)
    assert _probe_option("staff", text.split("=", 1)[1].strip())[0]["staff"] == [
        e for e in (x.strip() for x in text.split("=", 1)[1].split(",")) if e]


@pytest.mark.parametrize("name", ["", " ", "\t", "  "])
@pytest.mark.parametrize("staff", [("admin", ""), ("",), (" ",)])
def test_blank_account_name_is_never_staff(staff, name):
    server = make_server(staff=staff)
    assert server.is_staff(name) is False


@pytest.mark.parametrize("name", ["", " ", "\0"])
@pytest.mark.parametrize("staff", [("admin", ""), ("",)])
def test_onlystaff_blank_name_is_refused_even_with_a_blank_staff_entry(staff, name):
    server = make_server(onlystaff=True, staff=staff)
    player = login(server, name, 0)
    assert not player.logged_in
    player.disconnect.assert_awaited_once_with(STAFF_ONLY)


@pytest.mark.parametrize("client_type", [1, 3, 6])
def test_blank_staff_entry_does_not_admit_a_blank_named_control_login(client_type):
    server = make_server(staff=("",), ip_range="*")
    player = login(server, "", client_type)
    assert not player.logged_in
    assert (server.rc_manager.get_session(player.id) is None
            and server.nc_manager.get_session(player.id) is None)


def test_account_manager_blank_staff_check_is_false(tmp_path):
    from pygserver.account import AccountManager
    manager = AccountManager(MagicMock(), str(tmp_path))
    manager.set_staff_list(["", "admin"])
    assert manager.is_staff("") is False
    assert manager.is_staff("admin") is True


def test_refused_onlystaff_login_writes_no_account_file(tmp_path):
    from pygserver.account import AccountManager
    server = make_server(onlystaff=True)
    server.account_manager = AccountManager(server, str(tmp_path / "accounts"))
    for name, client_type in (("mallory", 0), ("", 0), ("rcmallory", 1), ("ncmallory", 3)):
        player = login(server, name, client_type)
        assert not player.logged_in
    server.account_manager._save_executor.submit(lambda: None).result()
    accounts = tmp_path / "accounts"
    assert not accounts.exists() or list(accounts.iterdir()) == []
    # Staff still get their account.
    player = login(server, "admin", 0, player_id=3)
    assert player.logged_in
    server.account_manager._save_executor.submit(lambda: None).result()
    assert [p.name for p in accounts.iterdir()] == ["admin.json"]


def test_baddyrespawntime_is_clamped_to_unsigned_32_bit():
    config = ServerConfig()
    for text, expected in (("9" * 400, 0xFFFFFFFF), ("-5", 0), ("4294967296", 0xFFFFFFFF),
                           ("4294967295", 0xFFFFFFFF), ("7", 7)):
        assert _probe_option("baddyrespawntime", text)[0] == {"baddyrespawntime": expected}
    from pygserver.baddy import BaddyManager
    server = MagicMock()
    server.config = ServerConfig(baddyrespawntime=10 ** 400)
    assert BaddyManager(server).respawn_seconds() == float(0xFFFFFFFF)
    server.config.baddyrespawntime = float("inf")
    assert BaddyManager(server).respawn_seconds() == 60.0
    assert config.baddyrespawntime == 60


async def test_one_baddy_raising_does_not_stop_the_others():
    import time
    from pygserver.baddy import BDMODE, Baddy, BaddyManager, BaddyType
    server = MagicMock()
    server.config = ServerConfig(baddyrespawntime=0)
    manager = BaddyManager(server)
    calls = []

    async def respawn(baddy):
        calls.append(baddy.id)
        if baddy.id == 1:
            raise RuntimeError("boom")
    manager._respawn_baddy = respawn
    baddies = {}
    for ident in (1, 2):
        baddy = Baddy(id=ident, level_name="t.nw", baddy_type=BaddyType.GRAYBALL, x=1, y=1)
        baddy.dead, baddy.mode, baddy.death_time = True, BDMODE.DEAD, time.time() - 5
        baddies[ident] = baddy
    manager._baddies = {"t.nw": baddies}
    await manager._tick(0.05)
    await manager._tick(0.05)
    assert calls == [1, 2, 1, 2]
