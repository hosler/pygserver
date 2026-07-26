"""Table-driven player-prop reader/writer (reborn_protocol.props) as used by
pygserver: build_player_props, parse_player_props and the list-server PLYRADD
prop subset.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from reborn_protocol.props import (
    COLORS_CLASSIC, PLAYER_PROPS, StreamPolicy, parse_prop_stream)

from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import (
    build_other_player_props,
    build_player_props,
    parse_player_props,
)


def _gchar(value: int) -> bytes:
    return bytes(((value + 32) & 0xFF,))


def _gstring(text: str) -> bytes:
    return _gchar(len(text)) + text.encode('latin-1')


def _body(packet: bytes, expected_id: int, header: int = 0) -> bytes:
    """Strip the packet id, any header, and the trailing newline."""
    assert packet[0] - 32 == expected_id
    assert packet[-1:] == b"\n"
    return packet[1 + header:-1]


# =============================================================================
# Writer
# =============================================================================

def test_build_player_props_emits_ascending_ids():
    packet = build_player_props({
        PLPROP.CURLEVEL: "start.nw",
        PLPROP.X2: 30.5,
        PLPROP.MAXPOWER: 6,
        PLPROP.NICKNAME: "bob",
    })
    body = _body(packet, PLO.PLAYERPROPS)
    ids = []
    pos = 0
    while pos < len(body):
        prop_id = body[pos] - 32
        ids.append(prop_id)
        pos += 1
        from reborn_protocol.props import payload_len
        pos += payload_len(PLAYER_PROPS[prop_id], body, pos, 8)
    assert ids == sorted(ids)
    assert ids == [PLPROP.NICKNAME, PLPROP.MAXPOWER, PLPROP.CURLEVEL, PLPROP.X2]


def test_build_player_props_round_trips_through_the_reader():
    written = {
        PLPROP.NICKNAME: "bob",
        PLPROP.MAXPOWER: 6,
        PLPROP.CURPOWER: 12,
        PLPROP.RUPEESCOUNT: 4096,
        PLPROP.SWORDPOWER: (3, "blade.png"),
        PLPROP.SHIELDPOWER: 2,
        PLPROP.GANI: "walk",
        PLPROP.CURLEVEL: "start.nw",
        PLPROP.TEXTCODEPAGE: 1252,
    }
    body = _body(build_player_props(written), PLO.PLAYERPROPS)
    # The reader defaults to the classic COLORS width; nothing here writes COLORS.
    read = parse_player_props(body)
    assert read[PLPROP.NICKNAME] == "bob"
    assert read[PLPROP.MAXPOWER] == 6
    assert read[PLPROP.CURPOWER] == 12
    assert read[PLPROP.RUPEESCOUNT] == 4096
    assert (read[PLPROP.SWORDPOWER], read['sword_image']) == (3, "blade.png")
    assert (read[PLPROP.SHIELDPOWER], read['shield_image']) == (2, "shield2.png")
    assert read[PLPROP.GANI] == "walk"
    assert read[PLPROP.CURLEVEL] == "start.nw"
    assert read[PLPROP.TEXTCODEPAGE] == 1252


def test_build_player_props_writes_colors_at_the_newworld_width():
    body = _body(build_player_props({PLPROP.COLORS: [1, 2, 3]}), PLO.PLAYERPROPS)
    assert len(body) == 1 + 8
    assert list(body[1:]) == [1 + 32, 2 + 32, 3 + 32] + [32] * 5


def test_unencodable_prop_does_not_leave_a_bare_id_behind():
    """A prop id with no payload would desync every following prop."""
    packet = build_player_props({
        PLPROP.COLORS: object(),        # not a sequence
        PLPROP.CURLEVEL: "start.nw",
    })
    body = _body(packet, PLO.PLAYERPROPS)
    assert body == _gchar(PLPROP.CURLEVEL) + _gstring("start.nw")


def test_unknown_prop_id_is_skipped_entirely():
    packet = build_player_props({200: 1, PLPROP.NICKNAME: "bob"})
    assert _body(packet, PLO.PLAYERPROPS) == _gchar(PLPROP.NICKNAME) + _gstring("bob")


def test_other_player_props_keeps_the_id_header_and_ascending_body():
    packet = build_other_player_props(7, {PLPROP.X2: 1.0, PLPROP.NICKNAME: "bob"})
    body = _body(packet, PLO.OTHERPLPROPS, header=2)
    assert body[0] - 32 == PLPROP.NICKNAME


# =============================================================================
# Reader alignment
# =============================================================================

def test_undecoded_multibyte_prop_no_longer_desyncs_the_stream():
    """PLPROP_ID is a gshort. Skipping one byte for it (the old default) turned
    its second byte into the next prop id."""
    data = (_gchar(PLPROP.ID) + _gchar(1) + _gchar(2)
            + _gchar(PLPROP.CURLEVEL) + _gstring("start.nw"))
    assert parse_player_props(data)[PLPROP.CURLEVEL] == "start.nw"


def test_props_inside_the_gattrib_range_are_not_read_as_strings():
    """ATTACHNPC(42)/GMAPLEVELX(43)/GMAPLEVELY(44)/Z(45) and
    JOINLEAVELVL(50)..PLAYERLISTSTATUS(53) sit inside 37..74 but are numerics."""
    data = (_gchar(PLPROP.ATTACHNPC) + _gchar(1) + _gchar(0) + _gchar(0) + _gchar(9)
            + _gchar(PLPROP.GMAPLEVELX) + _gchar(2)
            + _gchar(PLPROP.JOINLEAVELVL) + _gchar(1)
            + _gchar(PLPROP.CURLEVEL) + _gstring("start.nw"))
    props = parse_player_props(data)
    assert props[PLPROP.CURLEVEL] == "start.nw"
    assert PLPROP.ATTACHNPC not in props


def test_headimage_uses_the_headgif_form_not_a_plain_string():
    """A preset head id is a bare gchar, so reading it as a length-prefixed
    string ate the following prop's bytes."""
    data = (_gchar(PLPROP.HEADIMAGE) + _gchar(5)
            + _gchar(PLPROP.CURLEVEL) + _gstring("start.nw"))
    props = parse_player_props(data)
    assert props[PLPROP.CURLEVEL] == "start.nw"
    # Only custom names are surfaced, so callers keep getting a str or nothing.
    assert PLPROP.HEADIMAGE not in props

    name = "myhead.png"
    custom = _gchar(PLPROP.HEADIMAGE) + _gchar(100 + len(name)) + name.encode()
    assert parse_player_props(custom)[PLPROP.HEADIMAGE] == name


def test_x_and_y_stay_in_half_tiles_for_callers():
    data = _gchar(PLPROP.X) + _gchar(61) + _gchar(PLPROP.Y) + _gchar(20)
    props = parse_player_props(data)
    assert (props[PLPROP.X] / 2.0, props[PLPROP.Y] / 2.0) == (30.5, 10.0)


def test_colors_width_is_a_reader_argument():
    """The default is the 8-wide new-world form, matching what this server
    writes. GServer picks the width from a server-wide new-world mode rather
    than the client version (PropertyColors::getColorCount), so it cannot be
    derived from the handshake -- hence a reader argument, not a constant."""
    data = (_gchar(PLPROP.COLORS) + b"".join(_gchar(i) for i in range(8))
            + _gchar(PLPROP.CURLEVEL) + _gstring("start.nw"))

    props = parse_player_props(data)
    assert props[PLPROP.COLORS] == list(range(8))
    assert props[PLPROP.CURLEVEL] == "start.nw"

    # The classic 5-wide reading is still reachable, and misreads this stream's
    # tail -- which is exactly why the width has to be chosen, not guessed.
    assert parse_player_props(
        data, colors_len=COLORS_CLASSIC).get(PLPROP.CURLEVEL) != "start.nw"


# =============================================================================
# List server PLYRADD prop subset
# =============================================================================

def _plyradd_packet():
    from pygserver.config import ServerConfig
    from pygserver.listserver import ServerListClient

    server = MagicMock()
    server.config = ServerConfig(serverip="1.2.3.4")
    client = ServerListClient(server)
    client.connected = True
    client._send_packet = AsyncMock()

    player = MagicMock()
    player.send_packet = AsyncMock()
    player.id = 7
    player.connection_type = 0
    player.account_name = "bob"
    player.nickname = "Bob"
    player.level.name = "start.nw"
    player.x = 30.5
    player.y = 10.0
    player.ap = 50

    asyncio.run(client.add_player(player))
    return client._send_packet.await_args.args[0]


def test_plyradd_prop_subset_parses_back_prop_for_prop():
    from reborn_protocol import SVO

    packet = _plyradd_packet()
    assert packet[0] - 32 == SVO.PLYRADD
    props, clean, pos = parse_prop_stream(
        packet, 4,  # id byte + gshort player id + gchar client type
        StreamPolicy(table=PLAYER_PROPS, max_prop_id=83),
        {pid: (lambda out, v, pid=pid: out.__setitem__(pid, v))
         for pid in PLAYER_PROPS},
    )
    assert clean and pos == len(packet)
    assert props[PLPROP.ACCOUNTNAME] == "bob"
    assert props[PLPROP.NICKNAME] == "Bob"
    assert props[PLPROP.CURLEVEL] == "start.nw"
    assert (props[PLPROP.X], props[PLPROP.Y]) == (30.5, 10.0)
    assert props[PLPROP.ALIGNMENT] == 50
    assert props[PLPROP.IPADDR] == 0


def test_plyradd_ipaddr_is_five_bytes():
    """The list server reads PLPROP_IPADDR with readGInt5 (graal-serverlist
    ServerPlayer.cpp:60); a single byte left it reading past the packet."""
    packet = _plyradd_packet()
    ip_at = packet.index(bytes(((int(PLPROP.IPADDR) + 32) & 0xFF,)))
    assert len(packet) - (ip_at + 1) == 5
