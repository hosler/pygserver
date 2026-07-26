"""PLO_NPCPROPS wire-format regressions for build_npc_props.

The stream is decoded here with reborn_protocol.props.parse_prop_stream over
NPC_PROPS under the SAME StreamPolicy the client uses (pyReborn's
pyreborn/packets.py `_NPC_STREAM`: require_ascending, check_alignment,
require_full_consume), rather than importing pyReborn - pygserver's CI only
checks out reborn-protocol. `clean` False is exactly what the client turns into a
dropped tail of props.
"""

from reborn_protocol.props import (
    COLORS_CLASSIC,
    COLORS_NEWWORLD,
    NPC_PROPS,
    StreamPolicy,
    decode_value,
    parse_prop_stream,
)

from pygserver.level import Level
from pygserver.npc import NPC
from pygserver.protocol.constants import NPCPROP, PLO
from pygserver.protocol.packets import build_npc_props

_CLIENT_STREAM = StreamPolicy(
    table=NPC_PROPS, max_prop_id=77, require_ascending=True,
    check_alignment=True, require_full_consume=True)

# Surface every prop the packet carries, so a desync shows up as a missing key.
_HANDLERS = {pid: (lambda props, value, pid=pid: props.__setitem__(pid, value))
             for pid in NPC_PROPS}


def parse_as_client(packet: bytes, colors_len: int = COLORS_NEWWORLD):
    """Decode a PLO_NPCPROPS packet -> (npc_id, props, clean)."""
    assert packet[0] == PLO.NPCPROPS + 32
    assert packet[-1:] == b"\n"
    body = packet[1:-1]              # strip the id and the frame delimiter
    npc_id = ((body[0] - 32) << 14) + ((body[1] - 32) << 7) + (body[2] - 32)
    props, clean, _pos = parse_prop_stream(
        body, 3, _CLIENT_STREAM.with_colors_len(colors_len), _HANDLERS)
    return npc_id, props, clean


def prop_id_sequence(packet: bytes):
    """The prop ids a PLO_NPCPROPS packet carries, in wire order."""
    body = packet[1:-1]
    ids = []
    pos = 3
    while pos < len(body):
        prop_id = body[pos] - 32
        ids.append(prop_id)
        _value, pos = decode_value(NPC_PROPS[prop_id], body, pos + 1,
                                   COLORS_NEWWORLD)
    return ids


def make_npc():
    npc = NPC(5, "guard")
    npc.level = Level("t.nw")
    npc.image = "guard.png"
    npc.x, npc.y = 10.0, 12.0
    npc.gani = "walk"
    npc.nickname = "Guard"
    npc.head_image = "head5.png"
    npc.body_image = "body2.png"
    return npc


def test_sword_image_keeps_the_rest_of_the_stream_aligned():
    """SWORDIMAGE/SHIELDIMAGE are power+image, not gstrings.

    Written as a gstring, id 10's length byte was read as the next prop id and
    everything after it was lost.
    """
    npc = make_npc()
    npc.sword_image = "sword3.png"
    npc.sword_power = 3
    npc.shield_image = "shield2.png"
    npc.shield_power = 2

    npc_id, props, clean = parse_as_client(npc.build_props_packet())

    assert (npc_id, clean) == (5, True)
    assert props[NPCPROP.SWORDIMAGE] == (3, "sword3.png")
    assert props[NPCPROP.SHIELDIMAGE] == (2, "shield2.png")
    # Props after id 11 still line up.
    assert props[NPCPROP.GANI] == "walk"
    assert props[NPCPROP.NICKNAME] == "Guard"
    assert props[NPCPROP.BODYIMAGE] == "body2.png"
    assert props[NPCPROP.X2] == 10.0 and props[NPCPROP.Y2] == 12.0


def test_props_are_emitted_in_ascending_id_order():
    """The client ends the parse at the first descending id.

    NPC.build_props_packet groups its dict by meaning (image, x, y, x2, y2,
    sprite, ...), which put SPRITE (18) after Y2 (76) and stopped every client
    at the NPC's position - no gani/nickname/colors/gear reached it.
    """
    npc = make_npc()
    npc.colors = [1, 2, 3, 4, 5]

    packet = npc.build_props_packet()
    _npc_id, props, clean = parse_as_client(packet)

    assert prop_id_sequence(packet) == sorted(prop_id_sequence(packet))
    assert clean is True
    assert props[NPCPROP.SPRITE] == 2
    assert props[NPCPROP.COLORS] == [1, 2, 3, 4, 5, 0, 0, 0]


def test_gattribs_survive_the_stream():
    npc = make_npc()
    npc.gattribs = {NPCPROP.GATTRIB1: "one", NPCPROP.GATTRIB10: "ten"}

    _npc_id, props, clean = parse_as_client(npc.build_props_packet())

    assert clean is True
    assert props[NPCPROP.GATTRIB1] == "one"
    assert props[NPCPROP.GATTRIB10] == "ten"


def test_unknown_prop_id_is_skipped_without_writing_its_id():
    """A prop with no descriptor must not leave a bare id on the wire."""
    packet = build_npc_props(5, {NPCPROP.IMAGE: "a.png", 200: 1,
                                 NPCPROP.GANI: "idle"})

    _npc_id, props, clean = parse_as_client(packet)

    assert clean is True
    assert props[NPCPROP.IMAGE] == "a.png"
    assert props[NPCPROP.GANI] == "idle"


def test_colors_are_written_at_the_new_world_width():
    """Outbound COLORS is 8 wide (see build_player_props' _OUTBOUND_COLORS).

    Recorded rather than asserted as correct: reading the same stream at the
    classic width of 5 desyncs it, which is the open PLPROP/NPCPROP COLORS
    width question, not something this test settles.
    """
    npc = make_npc()
    npc.colors = [1, 2, 3, 4, 5]
    packet = npc.build_props_packet()

    assert parse_as_client(packet, COLORS_NEWWORLD)[2] is True
    assert parse_as_client(packet, COLORS_CLASSIC)[2] is False
