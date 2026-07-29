"""Packet parsing helpers."""

import logging
from typing import List, Tuple
from reborn_protocol.props import (
    COLORS_NEWWORLD,
    PLAYER_PROPS,
    StreamPolicy,
    parse_prop_stream,
    preset_power_image,
    with_gif_fallback,
)

from .packet_codec import PacketReader
from .constants import (
    PLPROP
)
from .constants import BDMODE, BDPROP, LevelItemType, NPCBLOCKFLAG, NPCPROP, NPCVISFLAG, PLI, PLO, PLPERM, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from .packet_codec import PacketBuilder  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import NPC_PROPS, encode_value  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, Optional  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

# GATTRIB prop ids, derived from the descriptor table rather than assumed to be
# the contiguous 37..74 range: ATTACHNPC/GMAPLEVELX/GMAPLEVELY/Z (42-45) and
# JOINLEAVELVL/DISCONNECT/LANGUAGE/PLAYERLISTSTATUS (50-53) sit inside it, and
# reading those as length-prefixed strings desynced the rest of the packet.
_GATTRIB_IDS = frozenset(
    pid for pid, desc in PLAYER_PROPS.items() if desc.name.startswith('GATTRIB'))

def parse_login_packet(data: bytes) -> dict:
    """
    Parse login packet from client.

    Login packet format (after zlib decompression):
    [client_type+32][encryption_key+32][protocol_string:8bytes]
    [username_len+32][username][password_len+32][password]
    [build_len+32][build]?[client_info]

    Returns:
        Dict with: client_type, encryption_key, protocol, username, password, client_info
    """
    reader = PacketReader(data)

    result = {
        'client_type': reader.read_gchar(),
        'encryption_key': reader.read_gchar(),
        'protocol': reader.read_string(8),
        'username': reader.read_gstring(),
        'password': reader.read_gstring(),
    }

    # Check for build string (older clients)
    if reader.has_data():
        # Could be build string or client info
        remaining = reader.remaining().decode('latin-1', errors='replace')
        if remaining.startswith(chr(32)):  # Looks like gstring
            result['build'] = reader.read_gstring()
            result['client_info'] = reader.remaining().decode('latin-1', errors='replace')
        else:
            result['client_info'] = remaining

    return result


def _store_power_image(prop_id: int, image_key: str, prefix: str,
                       gif_fallback: bool):
    """SWORDPOWER/SHIELDPOWER handler mirroring GServer-v2's account-side reader
    (PropertySwordPower::deserialize, PropertySerializers.cpp:89, and
    PropertyShieldPower::deserialize, :147): the power is already de-biased by
    the shared decoder, and a bare power gets the reference server's synthesised
    default image name. pyReborn deliberately does NOT synthesise one - a client
    wants to know whether an image was really on the wire.

    The 1.41-client quirk PropertyShieldPower::deserialize:171 tolerates (a
    biased power with no image bytes at all) decodes as image None here, which
    lands on the same empty-image path.
    """
    def handler(props, value):
        power, image = value
        props[prop_id] = power
        if image is None:
            props[image_key] = preset_power_image(prefix, power)
        else:
            props[image_key] = with_gif_fallback(image) if gif_fallback else image
    return handler


def _store(prop_id: int):
    return lambda props, value: props.__setitem__(prop_id, value)


def _store_half_tiles(prop_id: int):
    """X/Y stay in the raw half-tile units callers already divide by 2."""
    return lambda props, value: props.__setitem__(prop_id, int(value * 2))


def _store_chat(props, value):
    # An empty CURCHAT decodes as None (a length-prefixed string can't tell
    # "empty" from "absent"), but for chat the difference matters: the client
    # clears its bubble by sending an empty one, so it is surfaced as '' via the
    # stream policy's handle_empty. Dropping it left Player.chat stale and never
    # relayed the clear.
    props[PLPROP.CURCHAT] = value or ''


def _store_head_image(props, value):
    # Only a custom image name is surfaced; a preset id would change the type
    # callers (Player.head_image) expect. Routing it through the descriptor is
    # still what fixes the width - HEADGIF is not a plain length-prefixed
    # string, so reading it as one desynced every following prop.
    if isinstance(value, str):
        props[PLPROP.HEADIMAGE] = value


# Inbound client props (PLI_PLAYERPROPS). Keys are the numeric prop ids, plus
# 'sword_image'/'shield_image'. Only what Player._handle_player_props and the
# GS1 host consume is surfaced; everything else is still stepped over at its
# real width from the descriptor table.
_INBOUND_PROP_HANDLERS = {
    PLPROP.NICKNAME: _store(PLPROP.NICKNAME),
    PLPROP.MAXPOWER: _store(PLPROP.MAXPOWER),
    PLPROP.CURPOWER: _store(PLPROP.CURPOWER),
    PLPROP.RUPEESCOUNT: _store(PLPROP.RUPEESCOUNT),
    PLPROP.ARROWSCOUNT: _store(PLPROP.ARROWSCOUNT),
    PLPROP.BOMBSCOUNT: _store(PLPROP.BOMBSCOUNT),
    PLPROP.GLOVEPOWER: _store(PLPROP.GLOVEPOWER),
    PLPROP.BOMBPOWER: _store(PLPROP.BOMBPOWER),
    PLPROP.SWORDPOWER: _store_power_image(
        PLPROP.SWORDPOWER, 'sword_image', 'sword', gif_fallback=True),
    PLPROP.SHIELDPOWER: _store_power_image(
        PLPROP.SHIELDPOWER, 'shield_image', 'shield', gif_fallback=False),
    PLPROP.GANI: _store(PLPROP.GANI),
    PLPROP.HEADIMAGE: _store_head_image,
    PLPROP.CURCHAT: _store_chat,
    PLPROP.COLORS: _store(PLPROP.COLORS),
    PLPROP.X: _store_half_tiles(PLPROP.X),
    PLPROP.Y: _store_half_tiles(PLPROP.Y),
    # SPRITE and DIRECTION are the same prop id (17).
    PLPROP.SPRITE: _store(PLPROP.SPRITE),
    PLPROP.STATUS: _store(PLPROP.STATUS),
    PLPROP.CARRYSPRITE: _store(PLPROP.CARRYSPRITE),
    PLPROP.CURLEVEL: _store(PLPROP.CURLEVEL),
    PLPROP.CARRYNPC: _store(PLPROP.CARRYNPC),
    PLPROP.GMAPLEVELX: _store(PLPROP.GMAPLEVELX),
    PLPROP.GMAPLEVELY: _store(PLPROP.GMAPLEVELY),
    PLPROP.MAGICPOINTS: _store(PLPROP.MAGICPOINTS),
    PLPROP.ALIGNMENT: _store(PLPROP.ALIGNMENT),
    PLPROP.ACCOUNTNAME: _store(PLPROP.ACCOUNTNAME),
    PLPROP.BODYIMAGE: _store(PLPROP.BODYIMAGE),
    PLPROP.OSTYPE: _store(PLPROP.OSTYPE),
    PLPROP.TEXTCODEPAGE: _store(PLPROP.TEXTCODEPAGE),
    PLPROP.X2: _store(PLPROP.X2),
    PLPROP.Y2: _store(PLPROP.Y2),
    PLPROP.Z2: _store(PLPROP.Z2),
    **{pid: _store(pid) for pid in _GATTRIB_IDS},
}

# Client->server prop streams carry no ordering promise, so unlike the client's
# reader this does not use ascending ids to detect a desync; it just stops at an
# id outside the enum, as it always has.
_INBOUND_STREAM = StreamPolicy(table=PLAYER_PROPS, max_prop_id=100,
                               handle_empty=frozenset({PLPROP.CURCHAT}))


def parse_player_props(data: bytes, start_pos: int = 0,
                       colors_len: int = COLORS_NEWWORLD) -> dict:
    """
    Parse player properties from packet data.

    Returns dict of property values, keyed by numeric prop id.

    colors_len is PLPROP_COLORS' wire width, a server-wide mode switch on the
    reference server (PropertyColors::getColorCount -> isNewWorldMode,
    PropertySerializers.cpp:628-632) rather than anything derivable from the
    client's version. It defaults to the new-world width so the reader matches
    build_player_props' 8-byte writes; the classic width stays reachable by
    passing reborn_protocol.props.COLORS_CLASSIC.
    """
    props, _clean, _pos = parse_prop_stream(
        data, start_pos, _INBOUND_STREAM.with_colors_len(colors_len),
        _INBOUND_PROP_HANDLERS)
    return props



# =============================================================================
# Packet Parsers (Additional)
# =============================================================================

def parse_level_warp(data: bytes) -> Tuple[float, float, str]:
    """Parse PLI_LEVELWARP packet.

    Returns:
        Tuple of (x, y, level_name)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    level_name = reader.remaining().decode('latin-1', errors='replace').strip()
    return x, y, level_name


def parse_board_modify(data: bytes) -> Tuple[int, int, int, int, bytes]:
    """Parse PLI_BOARDMODIFY packet.

    Returns:
        Tuple of (x, y, width, height, tiles)
    """
    reader = PacketReader(data)
    x = reader.read_gchar()
    y = reader.read_gchar()
    width = reader.read_gchar()
    height = reader.read_gchar()
    tiles = reader.remaining()
    return x, y, width, height, tiles


def parse_trigger_action(data: bytes) -> Tuple[int, float, float, str, List[str]]:
    """Parse PLI_TRIGGERACTION packet.

    Wire format (GServer-v2 msgPLI_TRIGGERACTION, PlayerClientPackets.cpp):
        {GUINT3 npc_id}{GCHAR x*2}{GCHAR y*2}{action CSV}
    npc_id is a 3-byte GInt (readGUInt() == readGInt(), NOT a 4-byte GInt4) -
    reading only 4 bytes here (or skipping it) shifts x/y/action by one and
    silently corrupts every triggeraction.

    Returns:
        Tuple of (npc_id, x, y, action, params)
    """
    reader = PacketReader(data)
    npc_id = reader.read_gint3()
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    action_str = reader.remaining().decode('latin-1', errors='replace').strip()
    parts = action_str.split(',')
    action = parts[0] if parts else ''
    params = parts[1:] if len(parts) > 1 else []
    return npc_id, x, y, action, params


def parse_item_take(data: bytes) -> Tuple[float, float]:
    """Parse PLI_ITEMTAKE packet.

    Returns:
        Tuple of (x, y)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    return x, y


def parse_baddy_hurt(data: bytes) -> Tuple[int, int, float, float]:
    """Parse PLI_BADDYHURT packet.

    Returns:
        Tuple of (baddy_id, power, from_x, from_y)
    """
    reader = PacketReader(data)
    baddy_id = reader.read_gchar()
    power = reader.read_gchar()
    from_x = reader.read_gchar() / 2.0
    from_y = reader.read_gchar() / 2.0
    return baddy_id, power, from_x, from_y


def parse_flag_set(data: bytes) -> Tuple[str, str]:
    """Parse PLI_FLAGSET packet.

    Returns:
        Tuple of (flag_name, flag_value)
    """
    text = data.decode('latin-1', errors='replace').strip()
    if '=' in text:
        name, value = text.split('=', 1)
        return name, value
    return text, ''


def parse_want_file(data: bytes) -> str:
    """Parse PLI_WANTFILE packet.

    Returns:
        Filename requested
    """
    return data.decode('latin-1', errors='replace').strip()


def parse_verify_want_send(data: bytes) -> Tuple[int, str]:
    """Parse PLI_VERIFYWANTSEND packet.

    Returns:
        Tuple of (checksum, filename)
    """
    reader = PacketReader(data)
    checksum = reader.read_gint5()
    filename = reader.remaining().decode('latin-1', errors='replace').strip()
    return checksum, filename


# There is deliberately no inbound PLI_NPCPROPS parser here: the reference server
# refuses that packet outright when it owns the NPCs and pygserver always does,
# so the packet id is left unregistered (see handlers/entities.py's module
# docstring for the oracle). The parser this replaces was unreachable code that
# also still read NPCPROP ids 10/11 as plain gstrings instead of the
# PropertySwordPower/ShieldPower form build_npc_props writes.
