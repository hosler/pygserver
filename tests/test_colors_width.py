"""PLPROP_COLORS / NPCPROP_COLORS wire width follows `servermode`.

Reference (GServer-v2 origin/beta4): PropertyColors::getColorCount
(PropertySerializers.h:793-796) is 8 only under `servermode = newworld`,
otherwise 5 (normal and kingdoms), with no client-version test. The player prop
uses it for both serialize (:739-748) and deserialize (:750-759); the NPC prop
(PropertyColors<true>, NPC.h:646) writes it but always READS 5. Storage is
always 8 slots, and a read replaces the whole stored array with the
zero-filled one it built (PlayerProps.cpp:453-460, 1191-1192).

Expected bytes are composed by hand, not with the builders under test.
"""

import asyncio
from types import SimpleNamespace

import pytest
from reborn_protocol.props import COLORS_CLASSIC, COLORS_NEWWORLD

from pygserver.account import Account
from pygserver.config import ServerConfig
from pygserver.player_state import Character
from pygserver.protocol.colors import (
    COLORS_STORED, bind_server, clamp_colors, colors_max_value, colors_wire_width,
    normalize_colors)
from pygserver.protocol.constants import NPCPROP, PLI, PLO, PLPROP
from pygserver.protocol.packets import (
    PacketBuilder, build_npc_props, build_other_player_props,
    build_player_props, parse_player_props)
from tests.test_game_packet_parity import add_npc, make_player as make_world_player, sent, world  # noqa: F401
from tests.test_player_props_inbound import make_player as make_props_player
from tests.test_rcnc_1110_rc import by_id, prop_stream, rc  # noqa: F401

MODES = [("normal", 5), ("kingdoms", 5), ("newworld", 8), ("NewWorld", 8), ("", 5)]
ALL8 = [3, 4, 5, 6, 7, 8, 9, 10]
DEFAULTS = [2, 0, 10, 4, 18, 18, 18, 18]     # Character.h:43


def run(coro):
    return asyncio.run(coro)


def g(value):
    return bytes([value + 32])


def colors_bytes(values, width):
    return b"".join(g(v) for v in values[:width])


@pytest.fixture
def server_mode():
    """Bind a server stand-in; the returned callable sets its start-up mode."""
    holder = SimpleNamespace(config=ServerConfig(), server_mode="normal")
    bind_server(holder)

    def set_mode(mode):
        holder.server_mode = mode

    set_mode.holder = holder
    return set_mode


# -- the helper -------------------------------------------------------------

@pytest.mark.parametrize("mode,width", MODES)
def test_width_helper_follows_servermode(server_mode, mode, width):
    server_mode(mode)
    assert colors_wire_width() == width


def test_width_helper_defaults_to_five_without_a_server():
    assert colors_wire_width() == COLORS_CLASSIC
    assert colors_wire_width(ServerConfig(servermode="newworld")) == COLORS_NEWWORLD


def test_servermode_is_read_once_at_start_up(tmp_path):
    """Server.cpp:194-195 reads `servermode` once ("requires a full server
    restart to change"). An RC option write persists the new value but the
    running server's COLORS width (and login announcement) keep the start-up
    mode; a server built afterwards picks the new mode up."""
    from pygserver.rc import RCManager
    from pygserver.server import GameServer

    config = ServerConfig(servermode="normal")
    config.options_path = str(tmp_path / "serveroptions.txt")
    server = GameServer(config)
    manager = RCManager(server)
    props = {PLPROP.COLORS: ALL8, PLPROP.CURLEVEL: "a.nw"}
    tail = g(PLPROP.CURLEVEL) + g(4) + b"a.nw\n"
    head = bytes([PLO.PLAYERPROPS + 32]) + g(PLPROP.COLORS)
    five = head + colors_bytes(ALL8, 5) + tail
    stream = five[1:-1]

    assert server.server_mode == "normal"
    assert build_player_props(props) == five

    assert manager._apply_server_options("servermode = newworld") == []
    assert server.config.servermode == "newworld"      # the option store follows
    assert server.server_mode == "normal"              # the running mode does not
    assert colors_wire_width() == COLORS_CLASSIC
    assert build_player_props(props) == five
    assert parse_player_props(stream)[PLPROP.CURLEVEL] == "a.nw"

    restarted = GameServer(ServerConfig(servermode="newworld"))
    assert restarted.server_mode == "newworld"
    assert colors_wire_width() == COLORS_NEWWORLD
    assert build_player_props(props) == head + colors_bytes(ALL8, 8) + tail


def test_server_mode_normalises_like_the_reference():
    from pygserver.server import GameServer
    for raw, mode in (("NewWorld", "newworld"), (" kingdoms ", "kingdoms"),
                      ("normal", "normal"), ("", "normal"), ("bogus", "normal")):
        assert GameServer(ServerConfig(servermode=raw)).server_mode == mode


# -- writing ----------------------------------------------------------------

@pytest.mark.parametrize("mode,width", MODES)
def test_player_props_to_self_and_to_others_use_the_mode_width(
        server_mode, mode, width):
    server_mode(mode)
    props = {PLPROP.COLORS: ALL8, PLPROP.CURLEVEL: "a.nw"}
    body = g(PLPROP.COLORS) + colors_bytes(ALL8, width) + g(PLPROP.CURLEVEL) + g(4) + b"a.nw\n"
    assert build_player_props(props) == bytes([PLO.PLAYERPROPS + 32]) + body
    assert build_other_player_props(2, props) == (
        bytes([PLO.OTHERPLPROPS + 32]) + PacketBuilder().write_gshort(2).build() + body)


@pytest.mark.parametrize("mode,width", MODES)
def test_short_stored_colors_are_zero_padded_to_the_width(server_mode, mode, width):
    server_mode(mode)
    packet = build_player_props({PLPROP.COLORS: [1, 2, 3]})
    assert packet[2:-1] == colors_bytes([1, 2, 3, 0, 0, 0, 0, 0], width)


@pytest.mark.parametrize("mode,width", MODES)
def test_npc_props_use_the_mode_width(server_mode, mode, width):
    server_mode(mode)
    packet = build_npc_props(5, {NPCPROP.COLORS: ALL8, NPCPROP.GANI: "a"})
    assert packet[4:] == (g(NPCPROP.GANI) + g(1) + b"a"          # ids ascend
                          + g(NPCPROP.COLORS) + colors_bytes(ALL8, width) + b"\n")


@pytest.mark.parametrize("mode,width", MODES)
def test_rc_player_props_packet_uses_the_mode_width(rc, mode, width):
    bind_server(rc.server)
    rc.server.server_mode = mode
    packet = rc.manager._build_rc_player_props_packet(
        rc.player, {PLPROP.COLORS: ALL8, PLPROP.ALIGNMENT: 7})
    expected = (g(PLPROP.COLORS) + colors_bytes(ALL8, width)
                + g(PLPROP.ALIGNMENT) + g(7))
    assert expected in packet


# -- reading (PLI_PLAYERPROPS) ---------------------------------------------

@pytest.mark.parametrize("mode,width", MODES)
def test_colors_followed_by_another_prop_parses_in_every_mode(
        server_mode, mode, width):
    server_mode(mode)
    data = (g(PLPROP.COLORS) + colors_bytes(ALL8, width)
            + g(PLPROP.CURLEVEL) + g(4) + b"a.nw")
    props = parse_player_props(data)
    assert props[PLPROP.COLORS] == ALL8[:width]
    assert props[PLPROP.CURLEVEL] == "a.nw"


@pytest.mark.parametrize("mode,width", MODES)
def test_client_colors_replace_all_eight_stored_slots(server_mode, mode, width):
    server_mode(mode)
    server, player = make_props_player()
    player.colors = [9] * 8
    payload = (g(PLPROP.COLORS) + colors_bytes(ALL8, width)
               + g(PLPROP.BODYIMAGE) + g(5) + b"b.png")
    run(player._handle_player_props(payload))
    # Slots the wire did not carry become 0 (zero-filled array, :730 / PlayerProps.cpp:459).
    assert player.colors == ALL8[:width] + [0] * (8 - width)
    assert player.body_image == "b.png"          # the prop AFTER colors stayed aligned


# -- reading (RC prop set) --------------------------------------------------

@pytest.mark.parametrize("mode,width", MODES)
def test_rc_prop_set_reads_colors_at_the_mode_width(rc, mode, width):
    bind_server(rc.server)
    rc.server.server_mode = mode
    blob = (g(PLPROP.COLORS) + colors_bytes(ALL8, width)
            + g(PLPROP.ALIGNMENT) + g(40))
    stream = (PacketBuilder().write_gstring("main").write_gchar(len(blob))
              .write_bytes(blob).write_gshort(0).write_gshort(0)
              .write_gchar(0).build())
    run(rc.manager._handle_player_props_set(rc.session, by_id(2, stream)))
    assert rc.player.colors == ALL8[:width] + [0] * (8 - width)
    assert rc.player.ap == 40             # the prop after colors stayed aligned


# -- reading (PLI_NPCPROPS): always 5 ---------------------------------------

@pytest.mark.parametrize("mode", ["normal", "kingdoms", "newworld"])
def test_client_npc_colors_are_always_read_five_wide(world, mode):
    world.server.server_mode = mode
    bind_server(world.server)
    npc = add_npc(world, world.level)
    payload = (PacketBuilder().write_gint3(npc.id)
               .write_bytes(g(NPCPROP.COLORS) + colors_bytes(ALL8, 5)
                            + g(NPCPROP.IMAGE) + g(5) + b"a.png")
               .build())
    run(world.sender._handle_npc_props(payload))
    assert npc.colors == ALL8[:5] + [0, 0, 0]
    assert npc.image == "a.png"


# -- storage ----------------------------------------------------------------

def test_storage_is_eight_slots_everywhere():
    assert COLORS_STORED == 8
    assert normalize_colors([1, 2]) == [1, 2, 0, 0, 0, 0, 0, 0]
    assert normalize_colors(list(range(12))) == list(range(8))
    assert normalize_colors(None) == [0] * 8
    assert normalize_colors(["x", 3]) == [0, 3, 0, 0, 0, 0, 0, 0]
    assert len(Account(account_name="a").colors) == 8
    assert len(Character().colors) == 8


def test_account_load_pads_old_five_slot_files():
    # The account loader copies the file's tokens over a Character that already
    # holds the defaults (FlatFileAccountLoader.cpp:361-369), so a short list
    # keeps the default tail (18) rather than zeros.
    assert Account.from_dict({"account_name": "a", "colors": [1, 2, 3, 4, 5]}).colors == [
        1, 2, 3, 4, 5, 18, 18, 18]
    assert Account.from_dict({"account_name": "a", "colors": ALL8}).colors == ALL8
    assert Account.from_dict({"account_name": "a"}).colors == DEFAULTS
    assert Account.from_dict({"account_name": "a", "colors": "junk"}).colors == DEFAULTS


def test_account_save_keeps_all_eight_slots_whatever_the_mode(server_mode):
    account = Account(account_name="a")
    account.colors = list(ALL8)
    server_mode("normal")
    assert account.to_dict()["colors"] == ALL8
    assert Account.from_dict(account.to_dict()).colors == ALL8


# -- defaults (Character.h:43) ---------------------------------------------------

def test_new_characters_start_from_the_reference_colours():
    assert Character().colors == DEFAULTS
    assert Account(account_name="a").colors == DEFAULTS
    assert Account(account_name="a").colors is not Account(account_name="b").colors


def test_default_account_template_matches_the_stored_defaults():
    """The shipped (defaultaccount) files say COLORS 2,0,10,4,18; the other
    three slots come from the Character defaults (FlatFileAccountLoader.cpp:363-367)."""
    from pathlib import Path
    template = (Path(__file__).resolve().parents[2] / "funtimes-pygserver" / "accounts"
                / "defaultaccount.txt")
    if not template.exists():
        pytest.skip("funtimes-pygserver checkout not present")
    line = next(l for l in template.read_text().splitlines() if l.startswith("COLORS "))
    given = [int(token) for token in line.split(None, 1)[1].split(",")]
    assert given == DEFAULTS[:5]
    assert Account.from_dict({"account_name": "a", "colors": given}).colors == DEFAULTS


def test_create_account_carries_the_defaults(tmp_path):
    from pygserver.account import AccountManager
    manager = AccountManager(SimpleNamespace(), str(tmp_path))
    created = manager.create_account("fresh")
    assert created.colors == DEFAULTS
    assert Account.from_dict(created.to_dict()).colors == DEFAULTS
    assert build_player_props({PLPROP.COLORS: created.colors})[2:-1] == colors_bytes(DEFAULTS, 5)


def test_a_saved_account_keeps_its_own_values():
    saved = Account.from_dict({"account_name": "a", "colors": [0] * 8})
    assert saved.colors == [0] * 8


# -- clamp to the colour table (PropertySerializers.h:744,758,798-804) ---------------

def test_max_value_is_the_classic_table_unless_extended_colours_are_on():
    assert colors_max_value(ServerConfig()) == 20
    assert colors_max_value(ServerConfig(enableexbodycolors=True)) == 170
    assert colors_max_value(ServerConfig(enableexbodycolors=True, generation="modern")) == 170
    for generation in ("classic", "newmain"):
        assert colors_max_value(
            ServerConfig(enableexbodycolors=True, generation=generation)) == 20


def test_clamp_is_inclusive_of_the_table_size_and_floors_at_zero():
    assert clamp_colors([0, 19, 20, 21, 200, -3], ServerConfig()) == [0, 19, 20, 20, 20, 0]
    assert clamp_colors([169, 170, 171, 223], ServerConfig(enableexbodycolors=True)) == [
        169, 170, 170, 170]


@pytest.mark.parametrize("mode,width", MODES)
def test_write_clamps_every_player_and_npc_colour(server_mode, mode, width):
    server_mode(mode)
    wild = [0, 21, 50, 223, 7, 19, 20, 99]
    clamped = [0, 20, 20, 20, 7, 19, 20, 20]
    assert build_player_props({PLPROP.COLORS: wild})[2:-1] == colors_bytes(clamped, width)
    assert build_other_player_props(2, {PLPROP.COLORS: wild})[3:-1] == (
        g(PLPROP.COLORS) + colors_bytes(clamped, width))
    assert build_npc_props(5, {NPCPROP.COLORS: wild})[4:-1] == (
        g(NPCPROP.COLORS) + colors_bytes(clamped, width))


def test_write_clamp_follows_the_live_extended_colour_option(server_mode):
    server_mode("normal")
    server_mode.holder.config.enableexbodycolors = True
    wild = [150, 170, 171, 223, 0]
    assert build_player_props({PLPROP.COLORS: wild})[2:-1] == colors_bytes(
        [150, 170, 170, 170, 0], 5)
    server_mode.holder.config.generation = "classic"
    assert build_player_props({PLPROP.COLORS: wild})[2:-1] == colors_bytes([20] * 4 + [0], 5)


@pytest.mark.parametrize("mode,width", MODES)
def test_read_clamps_a_client_prop_stream(server_mode, mode, width):
    server_mode(mode)
    wild = [0, 21, 50, 223, 7, 19, 20, 99]
    data = g(PLPROP.COLORS) + colors_bytes(wild, width) + g(PLPROP.CURLEVEL) + g(4) + b"a.nw"
    props = parse_player_props(data)
    assert props[PLPROP.COLORS] == clamp_colors(wild[:width])
    assert props[PLPROP.CURLEVEL] == "a.nw"
    _server, player = make_props_player()
    run(player._handle_player_props(data))
    assert player.colors == clamp_colors(wild[:width]) + [0] * (8 - width)


def test_rc_prop_set_clamps_colours(rc):
    bind_server(rc.server)
    rc.server.server_mode = "normal"
    wild = [99, 21, 3, 223, 20]
    blob = g(PLPROP.COLORS) + colors_bytes(wild, 5) + g(PLPROP.ALIGNMENT) + g(40)
    stream = (PacketBuilder().write_gstring("main").write_gchar(len(blob))
              .write_bytes(blob).write_gshort(0).write_gshort(0)
              .write_gchar(0).build())
    run(rc.manager._handle_player_props_set(rc.session, by_id(2, stream)))
    assert rc.player.colors == [20, 20, 3, 20, 20, 0, 0, 0]
    assert rc.player.ap == 40


@pytest.mark.parametrize("mode", ["normal", "newworld"])
def test_client_npc_colours_are_clamped_on_read(world, mode):
    world.server.server_mode = mode
    bind_server(world.server)
    npc = add_npc(world, world.level)
    payload = (PacketBuilder().write_gint3(npc.id)
               .write_bytes(g(NPCPROP.COLORS) + colors_bytes([1, 99, 20, 21, 5], 5)
                            + g(NPCPROP.IMAGE) + g(5) + b"a.png")
               .build())
    run(world.sender._handle_npc_props(payload))
    assert npc.colors == [1, 20, 20, 20, 5, 0, 0, 0]
    assert npc.image == "a.png"
