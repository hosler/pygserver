"""Game-client packet parity with the reference (PlayerClientPackets.cpp,
PlayerProps.cpp, NPC.cpp, Level.cpp). Expected bytes are composed by hand from
the reference layouts, not by the builders under test."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from reborn_protocol.codec import Gen2Codec

from pygserver.account import Account, AccountManager
from pygserver.audience import Audience
from pygserver.combat import CombatManager
from pygserver.config import ServerConfig
from pygserver.gs2 import GS2ScriptManager
from pygserver.level import Level
from pygserver.npc import NPC, NPCManager
from pygserver.player import Player
from pygserver.protocol.constants import NPCPROP, PLO, PLPROP, PLTYPE
from pygserver.protocol.packets import (
    PacketBuilder,
    build_npc_weapon_add,
    build_player_props,
)
from pygserver.server import GameServer


def run(coro):
    return asyncio.run(coro)


def make_player(server, player_id, level, *, classic=False):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    player.account_name = f"acct{player_id}"
    player.nickname = player.account_name
    player.level = level
    player.send_raw = AsyncMock()
    player.logged_in = True
    if classic:
        player.session.codec = Gen2Codec()
    server.players[player_id] = player
    level.add_player(player)
    return player


@pytest.fixture
def world(tmp_path):
    server = GameServer.__new__(GameServer)
    server.config = ServerConfig(weapons_dir=str(tmp_path / "weapons"),
                                 serverside=False)
    level = Level("lobby.nw")
    other_level = Level("cellar.nw")
    levels = {level.name: level, other_level.name: other_level}
    server.world = SimpleNamespace(
        get_level=levels.get, get_gmap_for_level=lambda name: None,
        get_all_levels=lambda: list(levels.values()))
    server.players = {}
    server.audience = Audience(server)
    server.npc_manager = NPCManager(server)
    server.combat_manager = CombatManager(server)
    server.gs2_manager = GS2ScriptManager(server, str(tmp_path))
    sender = make_player(server, 1, level)
    return SimpleNamespace(server=server, level=level, other_level=other_level,
                           sender=sender, tmp=tmp_path)


def add_npc(world, level):
    manager = world.server.npc_manager
    npc = NPC(1000 + len(manager._npcs))
    level.add_npc(npc)
    manager._npcs[npc.id] = npc
    return npc


def sent(player):
    return [call.args[0] for call in player.send_raw.await_args_list]


# -- PLI_ADJACENTLEVEL ------------------------------------------------------

def adjacent_payload(modtime, name):
    return PacketBuilder().write_gint5(modtime).write_bytes(name.encode()).build()


@pytest.mark.parametrize('modtime', [0, 1, 1_700_000_000])
def test_adjacent_level_reads_modtime_before_the_name(world, modtime):
    world.sender.level = world.level
    run(world.sender._handle_adjacent_level(adjacent_payload(modtime, "cellar.nw")))

    board = bytes([PLO.BOARDPACKET + 32]) + bytes(8192) + b"\n"
    announcement = bytes([PLO.RAWDATA + 32]) + PacketBuilder().write_gint3(len(board)).build() + b"\n"
    name = bytes([PLO.LEVELNAME + 32]) + b"cellar.nw\n"
    assert sent(world.sender) == [name + announcement + board]


def test_adjacent_level_unknown_name_sends_nothing(world):
    run(world.sender._handle_adjacent_level(adjacent_payload(5, "missing.nw")))
    world.sender.send_raw.assert_not_awaited()


# -- PLO_BOARDLAYER ---------------------------------------------------------

BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def test_level_loads_extra_board_layers_and_sends_them_raw(world):
    row = "AB" * 64  # tile 1
    layer_row = "BA" * 64  # tile 64
    text = "GLEVNW01\n" + "".join(f"BOARD 0 {y} 64 0 {row}\n" for y in range(64))
    text += "".join(f"BOARD 0 {y} 64 2 {layer_row}\n" for y in range(64))
    level = Level("layers.nw")
    level._parse_nw_file(text.encode())

    assert sorted(level.board_layers) == [2]
    assert level.get_tile(5, 5) == 1
    assert bytes(level.board_layers[2][:2]) == (64).to_bytes(2, "little")

    packets = Player._board_layer_packets(level)
    body = bytes([PLO.BOARDLAYER + 32, 2, 0, 0, 64, 64]) + bytes(level.board_layers[2]) + b"\n"
    announcement = bytes([PLO.RAWDATA + 32]) + PacketBuilder().write_gint3(len(body)).build() + b"\n"
    assert packets == announcement + body
    assert Player._board_layer_packets(Level("plain.nw")) == b""


def test_missing_layer_cells_are_the_reference_empty_tile():
    level = Level("sparse.nw")
    text = "GLEVNW01\nBOARD 3 4 2 1 ABAB\n"
    level._parse_nw_file(text.encode())

    layer = level.board_layers[1]
    assert layer[:2] == b"\xff\xff"
    cells = [int.from_bytes(layer[i:i + 2], "little") for i in range(0, len(layer), 2)]
    assert cells.count(1) == 2
    assert cells.count(0xFFFF) == 4094


def test_adjacent_level_sends_layers_after_the_board(world):
    world.other_level.board_layers[3] = bytearray(b"\x07\x00" * 4096)
    run(world.sender._handle_adjacent_level(adjacent_payload(0, "cellar.nw")))

    [packet] = sent(world.sender)
    layer = bytes([PLO.BOARDLAYER + 32, 3, 0, 0, 64, 64])
    assert packet.endswith(layer + b"\x07\x00" * 4096 + b"\n")


# -- PLI_SHOOT / PLI_SHOOT2 -------------------------------------------------

def shoot2_payload():
    return (PacketBuilder().write_gshort(800).write_gshort(400).write_gshort(32)
            .write_byte(32).write_byte(32)
            .write_gchar(10).write_gchar(20).write_gchar(30).write_gchar(2)
            .write_gshort(5).write_bytes(b"arrow").write_gchar(3)
            .write_bytes(b"1,2").build())


def relayed_v2():
    return (PacketBuilder().write_gchar(PLO.SHOOT2).write_gshort(1)
            .write_gshort(800).write_gshort(400).write_gshort(32)
            .write_byte(32).write_byte(32)
            .write_gchar(10).write_gchar(20).write_gchar(30).write_gchar(2)
            .write_gshort(5).write_bytes(b"arrow").write_gchar(3)
            .write_bytes(b"1,2").write_newline().build())


def relayed_v1():
    # Player.cpp:239-254: pixels % 1024 / 8, z / 16 + 50, one-byte lengths.
    return (PacketBuilder().write_gchar(PLO.SHOOT).write_gshort(1)
            .write_gint3(0).write_gchar(100).write_gchar(50).write_gchar(52)
            .write_gchar(10).write_gchar(20).write_gchar(30)
            .write_gchar(5).write_bytes(b"arrow").write_gchar(3)
            .write_bytes(b"1,2").write_newline().build())


def test_shoot2_is_relayed_as_shoot2_to_modern_and_as_shoot_to_old_clients(world):
    modern = make_player(world.server, 2, world.level)
    old = make_player(world.server, 3, world.level, classic=True)
    elsewhere = make_player(world.server, 4, world.other_level)

    run(world.sender._dispatch_packet(48, shoot2_payload()))

    assert PLO.SHOOT2 == 191
    assert sent(modern) == [relayed_v2()]
    assert sent(old) == [relayed_v1()]
    world.sender.send_raw.assert_not_awaited()
    elsewhere.send_raw.assert_not_awaited()


def test_shoot_v1_input_is_normalised_for_both_audiences(world):
    modern = make_player(world.server, 2, world.level)
    old = make_player(world.server, 3, world.level, classic=True)
    payload = (PacketBuilder().write_gint3(0).write_gchar(100).write_gchar(50)
               .write_gchar(52).write_gchar(10).write_gchar(20).write_gchar(30)
               .write_gchar(5).write_bytes(b"arrow").write_gchar(3)
               .write_bytes(b"1,2").build())

    run(world.sender._dispatch_packet(40, payload))

    # PlayerClientPackets.cpp:1329-1370: pixels are 8 * wire, z is 16 * (wire - 50)
    # and gravity comes from the server variable (default 2).
    expected_v2 = (PacketBuilder().write_gchar(PLO.SHOOT2).write_gshort(1)
                   .write_gshort(800).write_gshort(400).write_gshort(32)
                   .write_byte(32).write_byte(32)
                   .write_gchar(10).write_gchar(20).write_gchar(30).write_gchar(2)
                   .write_gshort(5).write_bytes(b"arrow").write_gchar(3)
                   .write_bytes(b"1,2").write_newline().build())
    assert sent(modern) == [expected_v2]
    assert sent(old) == [relayed_v1()]


# -- PLI_FIRESPY ------------------------------------------------------------

def test_firespy_is_one_packed_byte_relayed_to_the_level(world):
    other = make_player(world.server, 2, world.level)
    elsewhere = make_player(world.server, 3, world.other_level)
    packed = (3 << 3) | 2  # length 3, power 2

    run(world.sender._dispatch_packet(10, bytes([packed + 32])))

    expected = (PacketBuilder().write_gchar(PLO.FIRESPY).write_gshort(1)
                .write_gchar(packed).write_newline().build())
    assert sent(other) == [expected]
    world.sender.send_raw.assert_not_awaited()
    elsewhere.send_raw.assert_not_awaited()


# -- PLI_WEAPONADD ----------------------------------------------------------

def default_weapon_request(item_id):
    return PacketBuilder().write_gchar(0).write_gchar(item_id).build()


def test_weapon_add_default_item_is_binary_and_sends_the_default_weapon(world):
    run(world.sender._dispatch_packet(33, default_weapon_request(7)))  # bow

    assert world.sender.weapons == ["bow"]
    assert sent(world.sender) == [
        PacketBuilder().write_gchar(PLO.DEFAULTWEAPON).write_gchar(7)
        .write_newline().build()]


def test_weapon_add_is_refused_with_an_npc_server_or_without_default_weapons(world):
    world.server.config.serverside = True
    run(world.sender._dispatch_packet(33, default_weapon_request(7)))
    world.server.config.serverside = False
    world.server.config.defaultweapons = False
    run(world.sender._dispatch_packet(33, default_weapon_request(7)))
    run(world.sender._dispatch_packet(33, default_weapon_request(99)))

    assert world.sender.weapons == []
    world.sender.send_raw.assert_not_awaited()


def test_weapon_add_npc_weapon_is_named_from_toweapons(world):
    npc = add_npc(world, world.level)
    npc.image = "wand.png"
    npc.gs1_source = "if (playertouchsme) { toweapons Wand; }"
    payload = PacketBuilder().write_gchar(1).write_gint3(npc.id).build()

    run(world.sender._dispatch_packet(33, payload))

    assert world.sender.weapons == ["Wand"]
    assert world.server.gs2_manager.get_weapon("Wand").image == "wand.png"
    assert sent(world.sender)[0] == build_npc_weapon_add(
        "Wand", "wand.png", npc.gs1_source)


def test_weapon_add_unknown_npc_or_unnamed_weapon_is_ignored(world):
    npc = add_npc(world, world.level)
    npc.gs1_source = "message hello;"
    for npc_id in (npc.id, npc.id + 50):
        payload = PacketBuilder().write_gchar(1).write_gint3(npc_id).build()
        run(world.sender._dispatch_packet(33, payload))
    assert world.sender.weapons == []
    world.sender.send_raw.assert_not_awaited()


# -- nickname / Z -----------------------------------------------------------

def props_payload(props):
    return build_player_props(props)[1:-1]


@pytest.mark.parametrize('requested,stored', [
    ("Bob", "Bob"),
    ("  Bob  ", "Bob"),
    ("Bob (Guild)", "Bob"),
    ("*Bob", "Bob"),
    ("", "unknown"),
    ("*", "unknown"),
    ("acct1", "*acct1"),
    ("*acct1", "*acct1"),
])
def test_nickname_is_normalised_forwarded_and_corrected_to_the_sender(
        world, requested, stored):
    other = make_player(world.server, 2, world.level)
    world.sender.nickname = "old"

    run(world.sender._handle_player_props(props_payload({PLPROP.NICKNAME: requested})))

    assert world.sender.nickname == stored
    relayed = (PacketBuilder().write_gchar(PLO.OTHERPLPROPS).write_gshort(1)
               .write_gchar(PLPROP.NICKNAME).write_gstring(stored)
               .write_newline().build())
    assert sent(other) == [relayed]
    if requested == stored:
        world.sender.send_raw.assert_not_awaited()
    else:
        assert sent(world.sender) == [
            PacketBuilder().write_gchar(PLO.PLAYERPROPS)
            .write_gchar(PLPROP.NICKNAME).write_gstring(stored)
            .write_newline().build()]


def test_unchanged_nickname_is_ignored(world):
    other = make_player(world.server, 2, world.level)
    world.sender.nickname = "Same"
    run(world.sender._handle_player_props(props_payload({PLPROP.NICKNAME: "Same"})))
    other.send_raw.assert_not_awaited()
    world.sender.send_raw.assert_not_awaited()


def test_z_updates_state_and_is_forwarded_as_z_and_z2(world):
    other = make_player(world.server, 2, world.level)

    run(world.sender._handle_player_props(props_payload({PLPROP.Z: 3})))

    assert world.sender.z == 3
    relayed = (PacketBuilder().write_gchar(PLO.OTHERPLPROPS).write_gshort(1)
               .write_gchar(PLPROP.Z).write_gchar(53)
               .write_gchar(PLPROP.Z2).write_gshort(96)  # 48 pixels << 1
               .write_newline().build())
    assert sent(other) == [relayed]
    world.sender.send_raw.assert_not_awaited()


def test_z2_sets_a_fractional_height(world):
    run(world.sender._handle_player_props(props_payload({PLPROP.Z2: 1.5})))
    assert world.sender.z == 1.5


def test_lockplayerz_reverts_z_and_tells_only_the_sender(world):
    other = make_player(world.server, 2, world.level)
    world.server.config.lockplayerz = True
    world.sender.z = 2.0

    run(world.sender._handle_player_props(props_payload({PLPROP.Z: 9})))

    assert world.sender.z == 2.0
    other.send_raw.assert_not_awaited()
    assert sent(world.sender) == [
        PacketBuilder().write_gchar(PLO.PLAYERPROPS)
        .write_gchar(PLPROP.Z).write_gchar(52)
        .write_gchar(PLPROP.Z2).write_gshort(64)
        .write_newline().build()]


def test_nickname_and_z_persist_through_the_account(tmp_path):
    server = SimpleNamespace(config=ServerConfig())
    manager = AccountManager(server, str(tmp_path))
    player = SimpleNamespace(
        head_image="h", body_image="b", sword_image="s", shield_image="d",
        colors=[0] * 5, max_hearts=3, hearts=3, rupees=0, bombs=0, arrows=0,
        glove_power=1, sword_power=1, shield_power=1, mp=0, ap=50, ap_counter=0,
        kills=0, deaths=0, elo_rating=1500.0, elo_deviation=350.0,
        last_spar_time=0, flags={}, weapons=[], gattribs={}, level=None,
        nickname="Nick", z=2.5)
    account = Account("acct")
    try:
        manager.save_player_to_account(player, account)
    finally:
        manager._save_executor.shutdown(wait=True)
    restored = Account.from_dict(account.to_dict())

    target = SimpleNamespace(nickname="acct")
    for key, value in vars(player).items():
        target.__dict__.setdefault(key, value)
    target.z = 0.0
    manager.load_player_from_account(target, restored)
    assert (target.nickname, target.z) == ("Nick", 2.5)


def test_lockplayerz_and_defaultweapons_are_read_from_serveroptions(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("lockplayerz = true\ndefaultweapons = false\n")
    config = ServerConfig.from_file(str(path))
    assert config.lockplayerz is True
    assert config.defaultweapons is False
    assert ServerConfig().lockplayerz is False
    assert ServerConfig().defaultweapons is True


# -- PLI_NPCPROPS / PLI_NPCDEL ----------------------------------------------

def npc_props_payload(npc_id, props):
    return PacketBuilder().write_gint3(npc_id).write_bytes(
        _npc_prop_stream(props)).build()


def _npc_prop_stream(props):
    from pygserver.protocol.packets import build_npc_props
    return build_npc_props(0, props)[4:-1]


def test_npc_props_are_refused_with_an_npc_server(world):
    world.server.config.serverside = True
    other = make_player(world.server, 2, world.level)
    npc = add_npc(world, world.level)

    run(world.sender._handle_npc_props(npc_props_payload(npc.id, {NPCPROP.IMAGE: "x.png"})))

    assert npc.image == ""
    other.send_raw.assert_not_awaited()


def test_npc_props_apply_and_relay_to_everyone_but_the_sender(world):
    other = make_player(world.server, 2, world.level)
    elsewhere = make_player(world.server, 3, world.other_level)
    npc = add_npc(world, world.level)

    run(world.sender._handle_npc_props(npc_props_payload(
        npc.id, {NPCPROP.IMAGE: "block.png", NPCPROP.X: 12.5})))

    assert (npc.image, npc.x) == ("block.png", 12.5)
    [packet] = sent(other)
    assert packet[0] == PLO.NPCPROPS + 32
    assert b"block.png" in packet
    world.sender.send_raw.assert_not_awaited()
    elsewhere.send_raw.assert_not_awaited()


def test_npc_props_from_a_player_on_another_level_are_ignored(world):
    npc = add_npc(world, world.other_level)
    run(world.sender._handle_npc_props(npc_props_payload(npc.id, {NPCPROP.IMAGE: "x.png"})))
    assert npc.image == ""


def test_npc_props_never_let_a_client_replace_the_script(world):
    other = make_player(world.server, 2, world.level)
    npc = add_npc(world, world.level)
    npc.client_script = "original"

    run(world.sender._handle_npc_props(npc_props_payload(
        npc.id, {NPCPROP.SCRIPT: "evil();"})))

    assert npc.client_script == "original"
    other.send_raw.assert_not_awaited()


def test_npc_props_with_an_unknown_prop_id_disconnect_the_sender(world):
    world.sender.disconnect = AsyncMock()
    npc = add_npc(world, world.level)
    payload = PacketBuilder().write_gint3(npc.id).write_gchar(120).build()

    run(world.sender._handle_npc_props(payload))

    world.sender.disconnect.assert_awaited_once_with('Corrupted packet received.')


def test_npc_del_is_refused_with_an_npc_server(world):
    world.server.config.serverside = True
    npc = add_npc(world, world.level)
    run(world.sender._handle_npc_del(PacketBuilder().write_gint3(npc.id).build()))
    assert world.server.npc_manager.get_npc(npc.id) is npc


def test_npc_del_removes_the_npc_for_every_game_client(world):
    same_level = make_player(world.server, 2, world.level)
    visitor = make_player(world.server, 3, world.other_level)
    rc = make_player(world.server, 4, world.level)
    rc.connection_type = PLTYPE.RC
    npc = add_npc(world, world.level)

    # Any player may delete any NPC (PlayerClientPackets.cpp:783-794).
    run(world.sender._handle_npc_del(PacketBuilder().write_gint3(npc.id).build()))

    assert world.server.npc_manager.get_npc(npc.id) is None
    plain = PacketBuilder().write_gchar(PLO.NPCDEL).write_gint3(npc.id).write_newline().build()
    scoped = (PacketBuilder().write_gchar(PLO.NPCDEL2).write_gchar(len("lobby.nw"))
              .write_bytes(b"lobby.nw").write_gint3(npc.id).write_newline().build())
    assert sent(same_level) == [plain]
    assert sent(world.sender) == [plain]
    assert sent(visitor) == [scoped]
    rc.send_raw.assert_not_awaited()
