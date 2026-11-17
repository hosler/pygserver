"""PLO_SIGNATURE, PLO_STAFFGUILDS, PLO_PROFILE and PLI_HITOBJECTS damage,
checked against GServer-v2 origin/beta4 (Player.cpp:751-753, PlayerClient.cpp:
526-529, PlayerRC.cpp:256-259, ServerList.cpp:568-761,
PlayerClientPackets.cpp:1023-1053)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from pygserver.account import ProfileManager
from pygserver.config import DEFAULT_PROFILEVARS, ServerConfig
from pygserver.protocol.builders.core import build_signature, build_staff_guilds, to_csv_words
from pygserver.protocol.constants import PLO, PLSTATUS
from pygserver.protocol.packet_codec import PacketReader

from .test_control_login import (
    flat_ids, framed_login, make_login_player, make_server, make_wire_player)


# -- PLO_SIGNATURE ---------------------------------------------------------------

def test_build_signature_is_id_25_then_gchar_73():
    assert build_signature() == bytes([25 + 32, 73 + 32, 0x0A])


@pytest.mark.parametrize("client_type, account, staff", [
    (5, "player", ()), (1, "hosler", ("hosler",)), (3, "hosler", ("hosler",))])
def test_signature_is_the_first_packet_of_every_login_type(client_type, account, staff):
    from pygserver.nc import NCManager
    server = make_server(staff=staff)
    server.nc_manager = NCManager(server)
    player, trace = make_wire_player(server, client_type, False, account)
    if client_type != 5:
        player.session.read.return_value = framed_login(client_type, include_key=False)
    assert asyncio.run(player._handle_login()) is True
    assert trace[0][0] == "login-frame"
    assert trace[0][1][0] == PLO.SIGNATURE
    assert flat_ids(trace).count(PLO.SIGNATURE) == 1


def test_signature_precedes_the_kingdoms_mode_packet():
    server = make_server()
    server.server_mode = "kingdoms"
    player, trace = make_wire_player(server, 5, False)
    assert asyncio.run(player._handle_login()) is True
    assert trace[0][1][:2] == [PLO.SIGNATURE, PLO.KINGDOMSMODE]


def test_refused_login_sends_no_signature():
    server = make_server()
    server.config.onlystaff = True
    player, trace = make_wire_player(server, 5, False, "visitor")
    asyncio.run(player._handle_login())
    assert PLO.SIGNATURE not in flat_ids(trace)


# -- PLO_STAFFGUILDS ---------------------------------------------------------------

@pytest.mark.parametrize("words, body", [
    (["Server", "GP Chief"], "Server,GP Chief"),
    (['A"B'], '"A""B"'),
    (["a\\b", "x"], '"a\\\\b",x'),
    (["Caf\xe9", "ok"], '"Caf\xe9",ok'),
    ([], ""),
])
def test_staff_guilds_csv_quoting(words, body):
    assert to_csv_words(words) == body
    packet = build_staff_guilds(words)
    assert packet == bytes([PLO.STAFFGUILDS + 32]) + body.encode("latin-1") + b"\n"


def _guilds_body(server):
    from pygserver.player_login import LoginService
    packet = LoginService(make_login_player(server, 2, "p", 5))._staff_guilds_packet()
    return packet[1:-1].decode("latin-1")


def test_staff_guilds_come_from_the_option_only_never_from_staff():
    server = make_server(staff=("alice", "bob"))
    assert _guilds_body(server) == ""            # unset: empty, not the staff list
    server.config.staffguilds = " Server , GP Chief,,Admin"
    assert _guilds_body(server) == "Server,GP Chief,Admin"


def test_staffguilds_option_is_read_from_the_options_file(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("staffguilds = Server,GP Chief\nstaff = alice\n")
    assert ServerConfig.from_file(str(path)).staffguilds == "Server,GP Chief"


def test_rc_gets_staff_guilds_and_nc_does_not():
    from pygserver.nc import NCManager
    server = make_server(staff=("hosler",))
    server.nc_manager = NCManager(server)
    rc, rc_trace = make_wire_player(server, 1, False, "hosler")
    rc.session.read.return_value = framed_login(1, include_key=False)
    assert asyncio.run(rc._handle_login()) is True
    assert PLO.STAFFGUILDS in flat_ids(rc_trace)
    server = make_server(staff=("hosler",))
    server.nc_manager = NCManager(server)
    nc, nc_trace = make_wire_player(server, 3, False, "hosler")
    nc.session.read.return_value = framed_login(3, include_key=False)
    assert asyncio.run(nc._handle_login()) is True
    assert PLO.STAFFGUILDS not in flat_ids(nc_trace)


# -- PLO_PROFILE ---------------------------------------------------------------------

def _profile_lines(packet):
    """The gstring lines after the packet id."""
    assert packet.endswith(b"\n")
    reader = PacketReader(packet[:-1])
    assert reader.read_gchar() == PLO.PROFILE
    lines = []
    while reader.has_data():
        lines.append(reader.read_gstring())
    return lines


def _profile_server(tmp_path):
    from pygserver.account import AccountManager
    server = make_server()
    server.account_manager = AccountManager(server, accounts_dir=str(tmp_path))
    server.account_manager.create_account("target", "pw")
    server.profile_manager = ProfileManager(server)
    return server


def _online(server, player_id, account, version):
    player = make_login_player(server, player_id, account, 5)
    player.send_raw = AsyncMock()
    player.account_name = account
    player.logged_in = True
    player.client_version = version
    return player


def _get(requester, name):
    asyncio.run(requester._handle_profile_get(name.encode()))
    if not requester.send_raw.await_args_list:
        return None
    return _profile_lines(requester.send_raw.await_args.args[0])


def test_profile_modern_requester_gets_profilevars_lines(tmp_path):
    server = _profile_server(tmp_path)
    target = _online(server, 3, "target", 2.22)
    target.kills, target.deaths, target.max_hearts, target.rupees = 4, 2, 5.0, 77
    requester = _online(server, 4, "asker", 2.22)
    lines = _get(requester, "target")
    assert lines[0] == "target"
    assert lines[10] == "0 hrs 0 mins 0 secs"
    # ServerList.cpp:636-755: "Kills:=playerkills" etc.
    assert lines[11:] == ["Kills:=4", "Deaths:=2", "Maxpower:=5",
                          "Rating:=1500/350", "Alignment:=50", "Gralat:=77",
                          "Swordpower:=1", "Spin Attack:=false"]


def test_profile_pre_2_1_requester_gets_the_eight_fixed_lines(tmp_path):
    server = _profile_server(tmp_path)
    target = _online(server, 3, "target", 2.22)
    target.client_status = PLSTATUS.HASSPIN
    requester = _online(server, 4, "asker", 1.411)
    lines = _get(requester, "target")
    assert lines[11:] == ["0", "0", "3", "1500/350", "50", "0", "1", "true"]


def test_profile_of_an_offline_account_sends_nothing(tmp_path):
    server = _profile_server(tmp_path)
    requester = _online(server, 4, "asker", 2.22)
    assert _get(requester, "target") is None          # on disk, not online
    assert _get(requester, "nobody") is None


def test_profilevars_are_capped_at_26_and_support_builtins_and_flags(tmp_path):
    server = _profile_server(tmp_path)
    target = _online(server, 3, "target", 2.22)
    target.hearts = 3.5
    target.flags["mood"] = "happy"
    target.flags["list"] = 'a,"b,c",d'
    server.config.profilevars = (
        ["Foo=playerhearts", "mood", "Pick=list{1}", "=playermp"]
        + [f"V{n}=playerbombs" for n in range(30)])
    requester = _online(server, 4, "asker", 2.22)
    lines = _get(requester, "target")[11:]
    assert len(lines) == 26
    assert lines[:4] == ["Foo=3.5", "mood=happy", "Pick=b,c", " =0"]
    assert lines[4] == "V0=5"


def test_bare_default_variable_captions(tmp_path):
    server = _profile_server(tmp_path)
    _online(server, 3, "target", 2.22)
    # In its own position the bare name has no caption; elsewhere the stock one.
    server.config.profilevars = ["playerkills", "playerkills", "unknown"]
    lines = _get(_online(server, 4, "asker", 2.22), "target")[11:]
    assert lines == ["0", "kills=0", "unknown="]


def test_profilevars_default_and_option(tmp_path):
    assert ServerConfig().profilevars == list(DEFAULT_PROFILEVARS)
    path = tmp_path / "serveroptions.txt"
    path.write_text("profilevars = A=playerkills,playerdeaths\n")
    assert ServerConfig.from_file(str(path)).profilevars == [
        "A=playerkills", "playerdeaths"]


# -- PLI_HITOBJECTS damage -----------------------------------------------------------

def _hit_world(setup):
    """Run `setup(server, level, npc_manager, attacker)` inside a loop (NPC
    creation schedules a task)."""
    from .test_gs1_hit_events import FakePlayer, make_server_and_level

    async def main():
        server, level = make_server_and_level()
        attacker = FakePlayer(1)
        attacker.level = level
        await setup(server, level, attacker)

    asyncio.run(main())


async def _swing(server, attacker, power_hearts):
    await server.combat_manager.handle_hit_objects(
        attacker, 11.5, 12.5, power_hearts)


def test_hit_objects_takes_wire_byte_hearts_from_a_character_npc():
    async def setup(server, level, attacker):
        npc = server.npc_manager.create_npc(name="t", level=level, x=10, y=10)
        npc.image = "#c#"
        npc.hearts = 5.0
        await _swing(server, attacker, 1.0)   # wire byte 2 (half hearts)
        assert npc.hearts == 3.0               # :1046 removes byte * 2 halves
        await _swing(server, attacker, 0.5)   # wire byte 1
        assert npc.hearts == 2.0
        await _swing(server, attacker, 5.0)
        assert npc.hearts == 0.0               # floored at zero

    _hit_world(setup)


def test_hit_objects_leaves_other_npcs_alone():
    async def setup(server, level, attacker):
        plain = server.npc_manager.create_npc(name="plain", level=level, x=10, y=10)
        hidden = server.npc_manager.create_npc(name="hidden", level=level, x=10, y=10)
        hidden.image = "#c#"
        hidden.visible = False
        await _swing(server, attacker, 1.0)
        assert plain.hearts == 3.0 and hidden.hearts == 3.0

    _hit_world(setup)


def test_hit_objects_without_an_npc_server_does_no_damage():
    async def setup(server, level, attacker):
        npc = server.npc_manager.create_npc(name="t", level=level, x=10, y=10)
        npc.image = "#c#"
        server.config = ServerConfig(serverside=False)
        await _swing(server, attacker, 1.0)
        assert npc.hearts == 3.0

    _hit_world(setup)


# -- PLI_HITOBJECTS hit area (follow-up) ----------------------------------------------
# findIntersectingNPCsForCollision(point) (Level.cpp:3521-3535) tests the probe
# static_cast<int16_t>(loc * 16) against NPC::getCollisionBoundingBox
# (NPC.h:573-584): a character is (x + 8, y + 16, 32, 32) px, edges inclusive;
# an NPC with the NOBLOCK flag has none.

@pytest.mark.parametrize("probe_x, probe_y, hit", [
    (11.5, 12.5, True),    # the sprite's body (184, 200 px)
    (10.5, 12.0, True),    # left edge x + 8 = 168 px, top edge y + 16 = 176 px
    (12.5, 13.0, True),    # right/bottom edge 200, 208 px (inclusive)
    (10.0, 10.0, False),   # the old test: the NPC's top-left corner (160, 160)
    (10.0, 12.5, False),   # left of the box
    (12.5, 14.5, False),   # below the box
])
def test_hit_objects_probe_is_tested_against_the_collision_box(probe_x, probe_y, hit):
    async def setup(server, level, attacker):
        npc = server.npc_manager.create_npc(name="t", level=level, x=10, y=10)
        npc.image = "#c#"
        await server.combat_manager.handle_hit_objects(attacker, probe_x, probe_y, 1.0)
        assert npc.hearts == (1.0 if hit else 3.0)

    _hit_world(setup)


def test_hit_objects_noblock_character_has_no_box():
    from pygserver.protocol.constants import NPCBLOCKFLAG

    async def setup(server, level, attacker):
        npc = server.npc_manager.create_npc(name="t", level=level, x=10, y=10)
        npc.image = "#c#"
        npc.block_flags = NPCBLOCKFLAG.NOBLOCK
        await _swing(server, attacker, 1.0)
        assert npc.hearts == 3.0

    _hit_world(setup)


def test_the_collision_box_of_a_character_is_the_reference_one():
    from pygserver import collision
    npc = type("N", (), {"x": 10.0, "y": 10.0, "image": "#c#", "visible": True,
                         "block_flags": 0})()
    assert collision.npc_collision_rect(npc) == (168, 176, 32, 32)


# -- fromCSV for profilevars flag values (follow-up) ----------------------------------
# StringUtils.h:1046-1135: inside quotes `\\` is one backslash and `""` one
# quote; after a closing quote everything up to the next comma is dropped.

@pytest.mark.parametrize("flag, index, field", [
    ('a,"b\\\\c",d', 1, 'b\\c'),         # python csv keeps both backslashes
    ('a,"b""c",d', 1, 'b"c'),
    ('"a"junk,b', 0, 'a'),               # python csv would give 'ajunk'
    ('"a"junk,b', 1, 'b'),
    ('a,b,', 2, ''),                     # an empty last field is not returned
    ('a,,c', 1, ''),
    ('a,b', 5, ''),
])
def test_profile_flag_index_uses_from_csv(flag, index, field):
    assert ProfileManager._csv_field(flag, index) == field


# -- client version recorded from the login token (follow-up) -------------------------
# getVersionID (gs2lib IUtil.cpp:271) maps the token through clientVersions[].

@pytest.mark.parametrize("token, version", [
    ("GNW24011", 2.0), ("GNW31101", 2.1), ("GNW03014", 2.22),
    ("G3D22067", 5.07), ("G3D3007A", 6.015), ("G3D0311C", 6.037),
    ("G3D0511C", 6.037),
])
def test_login_records_the_client_version_from_the_token(token, version):
    from pygserver.handlers.versions import client_version
    from .test_player_facade import login_frame
    server = make_server()
    player = make_login_player(server, 2, "someone", 5)
    player.session.read = AsyncMock(return_value=login_frame(
        username="someone", protocol=token))
    assert asyncio.run(player._handle_login()) is True
    assert player.client_version == version
    assert client_version(player) == version


def test_an_unknown_token_leaves_the_version_to_the_codec_fallback():
    from pygserver.handlers.versions import version_for_token
    assert version_for_token("XXXXXXXX") is None


def test_a_version_below_2_1_takes_the_old_profile_branch_via_the_token(tmp_path):
    server = _profile_server(tmp_path)
    from pygserver.handlers.versions import version_for_token
    _online(server, 3, "target", 6.037)
    requester = _online(server, 4, "asker", version_for_token("GNW24011"))
    assert len(_get(requester, "target")[11:]) == 8


# -- PLO_PLAYERWARP / PLO_PLAYERWARP2 (follow-up) ---------------------------------------
# PlayerClient.cpp:1262 and :1273 write the props' serialize() output: X and Y
# are PropertyTileCoordinate bytes (negative tiles survive); Z is
# PropertyTileCoordinateZ, tiles + 50 (PropertySerializers.cpp:530-535).

def test_build_warp_writes_the_shared_tile_coordinate():
    from pygserver.protocol.builders.core import build_warp
    assert build_warp(30.0, 31.5, "a.nw") == (
        bytes([PLO.PLAYERWARP + 32, 60 + 32, 63 + 32]) + b"a.nw\n")
    # -2.5 tiles is half -5: the reference's one-byte value is -5 + 32, not the
    # 0 the old clamping writer gave.
    assert build_warp(-2.5, -16.0, "a.nw")[1:3] == bytes([-5 + 32, -32 + 32])


def test_build_warp2_writes_tile_coordinates_and_z_plus_50():
    from pygserver.protocol.builders.core import build_warp2
    assert build_warp2(10.0, 12.0, "m.gmap", 1, 0) == (
        bytes([PLO.PLAYERWARP2 + 32, 20 + 32, 24 + 32, 50 + 32, 1 + 32, 0 + 32])
        + b"m.gmap\n")
    assert build_warp2(-2.5, 111.5, "m.gmap", 2, 3, z=2)[1:6] == bytes(
        [-5 + 32, 255, 52 + 32, 2 + 32, 3 + 32])


# -- PLI_LEVELWARP reader (follow-up) -------------------------------------------------
# PlayerClientPackets.cpp:59: static_cast<int16_t>(readGChar() * 8) pixels with
# readGChar a signed char of (byte - 32) (CString.cpp:1824-1829).

@pytest.mark.parametrize("byte, tiles", [
    (32, 0.0), (33, 0.5), (32 + 20, 10.0), (32 + 127, 63.5),   # 0..127
    (32 + 128, -64.0),                                           # wraps negative
    (255, -16.5),                                                # 223 -> -33 half tiles
    (31, -0.5), (0, -16.0),                                      # below 0x20
])
def test_level_warp_axis_is_a_signed_half_tile_count(byte, tiles):
    from pygserver.protocol.parsers import parse_level_warp, read_level_warp_axis
    assert read_level_warp_axis(PacketReader(bytes([byte]))) == tiles
    assert parse_level_warp(bytes([byte, 32 + 4]) + b"a.nw")[:3] == (tiles, 2.0, "a.nw")


def test_level_warp_handler_uses_the_signed_reader():
    from pygserver.handlers.movement import MovementHandlers
    reader = PacketReader(bytes([32 + 128, 32 + 6]) + b"c1.nw")
    assert MovementHandlers._read_warp(reader) == (-64.0, 3.0, "c1.nw")


# -- list-server player position (follow-up) -------------------------------------------
# ServerList.cpp:344-345 sends the player's X/Y props: level-local, the
# attached NPC's position is not added.

def test_listserver_reports_level_local_x_y():
    from types import SimpleNamespace
    from pygserver.listserver import ServerListClient
    from reborn_protocol.props import PLAYER_PROPS, encode_value
    from pygserver.protocol.constants import PLPROP
    server = make_server()
    client = ServerListClient(server)
    client.connected = True
    client._send_packet = AsyncMock()
    npc_stub = SimpleNamespace(x=50.0, y=40.0)
    level = SimpleNamespace(name="a.nw", get_npc=lambda i: npc_stub)
    player = SimpleNamespace(
        id=2, connection_type=1, account_name="a", nickname="a", level=level,
        x=12.5, y=7.0, ap=50, attach_npc=(0, 9), server=server)
    asyncio.run(client.add_player(player))
    body = client._send_packet.await_args.args[0]
    for prop, value in ((PLPROP.X, 12.5), (PLPROP.Y, 7.0)):
        expected = bytes([prop + 32]) + encode_value(PLAYER_PROPS[prop], value)
        assert expected in body
    # The attached-NPC-adjusted position would have been (62.5, 47.0).
    wrong = bytes([PLPROP.X + 32]) + encode_value(PLAYER_PROPS[PLPROP.X], 62.5)
    assert wrong not in body
