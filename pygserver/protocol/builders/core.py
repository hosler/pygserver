"""Packet construction helpers."""

import logging
from typing import List
from reborn_protocol.props import (
    COLORS_NEWWORLD,
    NPC_PROPS,
    PLAYER_PROPS,
    encode_value,
)

from ..packet_codec import PacketBuilder
from ..constants import (
    PLO, PLPROP
)
from ..constants import BDMODE, BDPROP, LevelItemType, NPCBLOCKFLAG, NPCPROP, NPCVISFLAG, PLI, PLPERM, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..packet_codec import PacketReader  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import StreamPolicy, parse_prop_stream, preset_power_image, with_gif_fallback  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, Optional, Tuple  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

# =============================================================================
# Packet Builders
# =============================================================================

def build_level_name(level_name: str) -> bytes:
    """Build PLO_LEVELNAME packet."""
    return PacketBuilder().write_gchar(PLO.LEVELNAME).write_string(level_name).write_byte(ord('\n')).build()


def build_level_link(dest_level: str, x: int, y: int, width: int, height: int,
                     dest_x: str, dest_y: str) -> bytes:
    """Build PLO_LEVELLINK packet."""
    link_str = f"{dest_level} {x} {y} {width} {height} {dest_x} {dest_y}"
    return PacketBuilder().write_gchar(PLO.LEVELLINK).write_string(link_str).write_byte(ord('\n')).build()


def build_board_packet(tiles: bytes) -> bytes:
    """
    Build PLO_BOARDPACKET packet (raw 8192 bytes of tile data).

    This is sent as raw data after a PLO_RAWDATA announcement.
    """
    return tiles


def build_raw_data_announcement(size: int) -> bytes:
    """Build PLO_RAWDATA packet to announce raw data size."""
    return PacketBuilder().write_gchar(PLO.RAWDATA).write_gint3(size).write_byte(ord('\n')).build()


def build_player_props(props: dict) -> bytes:
    """Build PLO_PLAYERPROPS packet with given properties.

    GServer-v2 always emits PlayerProp ids in strictly ascending numeric
    order (server/include/object/Player.h's PLAYERPROP_LIST X-macro is walked
    in enum-value order by getPropsPacketFromList()/getModifiedPropsPacket(),
    server/src/player/PlayerProps.cpp). pyReborn's parser relies on that
    invariant as a self-correcting signal for PLPROP_COLORS' ambiguous wire
    width (see reborn-protocol-docs "PLPROP_COLORS Width" /
    _parse_with_colors_retry): the first out-of-order id it sees ends the
    parse early. Sorting here means callers can build the `props` dict in
    whatever order is convenient without silently corrupting every later
    prop on the wire.
    """
    builder = PacketBuilder().write_gchar(PLO.PLAYERPROPS)

    for prop_id, value in sorted(props.items()):
        _write_player_prop(builder, prop_id, value)

    builder.write_byte(ord('\n'))
    return builder.build()


# Outbound props are for v6.037 (new-world) clients, so PLPROP_COLORS is 8 wide.
_OUTBOUND_COLORS = COLORS_NEWWORLD


def _write_player_prop(builder: 'PacketBuilder', prop_id: int, value) -> None:
    """Write a single player property (id + correctly-sized payload).

    Widths come from reborn_protocol.props.PLAYER_PROPS, so the writer and the
    reader above cannot disagree. A prop that cannot be encoded is skipped
    WITHOUT writing its id, since a bare id with no payload desyncs every
    following prop in the packet.

    `value` is the natural Python form for the prop: tiles for X/Y/X2/Y2, an int
    power or a (power, image) pair for SWORDPOWER/SHIELDPOWER, a preset int or a
    name for HEADIMAGE, a sequence for COLORS.
    """
    desc = PLAYER_PROPS.get(int(prop_id))
    if desc is None:
        logger.warning("build_player_props: unhandled prop %s (skipped)", prop_id)
        return
    try:
        payload = encode_value(desc, value, _OUTBOUND_COLORS)
    except (TypeError, ValueError) as exc:
        logger.warning("build_player_props: prop %s (%s) value %r not encodable: %s",
                       prop_id, desc.name, value, exc)
        return
    builder.write_gchar(int(prop_id)).write_bytes(payload)


def build_other_player_props(player_id: int, props: dict) -> bytes:
    """Build PLO_OTHERPLPROPS packet for another player.

    See build_player_props() for why props must be emitted in ascending
    PlayerProp-id order (GServer-v2 convention the client's parser relies on).
    """
    builder = PacketBuilder().write_gchar(PLO.OTHERPLPROPS).write_gshort(player_id)

    for prop_id, value in sorted(props.items()):
        _write_player_prop(builder, prop_id, value)

    builder.write_byte(ord('\n'))
    return builder.build()


def build_npc_props(npc_id: int, props: dict) -> bytes:
    """Build PLO_NPCPROPS packet.

    Widths and encodings come from reborn_protocol.props.NPC_PROPS, so this and
    the client's parse_npc_props (which reads the same table) cannot disagree.
    `value` is the natural Python form for the prop: tiles for X/Y/X2/Y2, an int
    power or a (power, image) pair for SWORDIMAGE/SHIELDIMAGE, a preset int or a
    name for HEADIMAGE, a sequence for COLORS.

    The hand-rolled version this replaces wrote SWORDIMAGE/SHIELDIMAGE (ids
    10/11) as plain gstrings, but GServer-v2 maps them to PropertySwordPower /
    PropertyShieldPower (server/include/object/NPC.h:591-592) - a biased power
    with the image name only after it. A gstring there decoded as a bare power
    of 10 and left the length byte to be read as the next prop id, desyncing the
    rest of that NPC's prop stream.

    Ids are sorted for the same reason build_player_props sorts them: the
    reference server emits NPCProp ids in ascending order (NPC.h's
    FOR_LIST_OF_NPC_PROPS walked in enum order) and the client's parser ENDS THE
    PARSE at the first descending id. Emitting in dict-insertion order meant
    NPC.build_props_packet's natural grouping (image, x, y, x2, y2, sprite, ...)
    stopped the client dead at `sprite`, so every NPC arrived with only its
    image and position - no gani, nickname, colors, gear or gattribs.
    """
    builder = PacketBuilder().write_gchar(PLO.NPCPROPS).write_gint3(npc_id)

    for prop_id, value in sorted(props.items()):
        _write_npc_prop(builder, prop_id, value)

    builder.write_byte(ord('\n'))
    return builder.build()


def _write_npc_prop(builder: 'PacketBuilder', prop_id: int, value) -> None:
    """Write a single NPC property (id + correctly-sized payload).

    A prop that cannot be encoded is skipped WITHOUT writing its id, since a
    bare id with no payload desyncs every following prop in the packet.
    """
    desc = NPC_PROPS.get(int(prop_id))
    if desc is None:
        logger.warning("build_npc_props: unhandled prop %s (skipped)", prop_id)
        return
    try:
        payload = encode_value(desc, value, _OUTBOUND_COLORS)
    except (TypeError, ValueError) as exc:
        logger.warning("build_npc_props: prop %s (%s) value %r not encodable: %s",
                       prop_id, desc.name, value, exc)
        return
    builder.write_gchar(int(prop_id)).write_bytes(payload)


def build_chat(player_id: int, message: str) -> bytes:
    """Build PLO_TOALL chat packet."""
    builder = PacketBuilder().write_gchar(PLO.TOALL).write_gshort(player_id)
    builder.write_gchar(len(message))
    builder.write_string(message)
    builder.write_byte(ord('\n'))
    return builder.build()


def build_warp(x: float, y: float, level_name: str = "") -> bytes:
    """Build PLO_PLAYERWARP packet."""
    builder = PacketBuilder().write_gchar(PLO.PLAYERWARP)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    if level_name:
        builder.write_string(level_name)
    builder.write_byte(ord('\n'))
    return builder.build()


def build_warp2(x: float, y: float, level_name: str, gmap_x: int = 0,
                gmap_y: int = 0, z: int = 0) -> bytes:
    """Build PLO_PLAYERWARP2 packet for GMAP warps.

    Format (GServer-v2 PlayerClient + client parse_playerwarp2):
        [gchar x*2][gchar y*2][gchar z][gchar gmap_x][gchar gmap_y][raw level name]
    The level name is a raw trailing string (no length prefix), so this packet
    is self-terminating and must come last in its frame.
    """
    builder = PacketBuilder().write_gchar(PLO.PLAYERWARP2)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(int(z))
    builder.write_gchar(gmap_x)
    builder.write_gchar(gmap_y)
    builder.write_string(level_name)
    builder.write_byte(ord('\n'))
    return builder.build()


def build_player_left(player_id: int) -> bytes:
    """Build player left packet using PLO_OTHERPLPROPS with JOINLEAVELVL=0."""
    # Send PLPROP_JOINLEAVELVL = 0 (leave) via PLO_OTHERPLPROPS
    return (PacketBuilder()
        .write_gchar(PLO.OTHERPLPROPS)
        .write_gshort(player_id)
        .write_gchar(PLPROP.JOINLEAVELVL)
        .write_gchar(0)  # 0 = leave
        .write_byte(ord('\n'))
        .build())


def build_world_time() -> bytes:
    """Build PLO_NEWWORLDTIME heartbeat packet.

    Wire format per GServer-v2 Server.cpp calculateNWTime(): GINT4 of
    (unixtime - 981048814) / 5, i.e. 5-second units since the timevar
    epoch 2001-02-01T17:33:34Z. Clients read exactly 4 G-bytes; the old
    3-byte seconds-of-day encoding parsed as time=0 on their side.
    """
    import time
    world_time = int(time.time() - 981048814) // 5
    return PacketBuilder().write_gchar(PLO.NEWWORLDTIME).write_gint4(world_time).write_byte(ord('\n')).build()


def build_npc_del(npc_id: int) -> bytes:
    """Build PLO_NPCDEL packet."""
    return PacketBuilder().write_gchar(PLO.NPCDEL).write_gint3(npc_id).write_byte(ord('\n')).build()


# Reborn sign-text alphabet (GServer-v2 LevelSign.cpp `signText`). Each plain
# character maps to its index in this string, written as a GChar (index + 32).
_SIGN_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789!?-.,#>()#####\"####':/~&### <####;\n"
)


def encode_sign_text(text: str) -> bytes:
    """Encode plain sign text into the Reborn sign-code byte stream.

    Mirrors GServer-v2 encodeSign/encodeSignCode for the common (non-symbol)
    case: each line is encoded char-by-char against the sign alphabet and
    terminated with an encoded newline. Characters absent from the alphabet are
    dropped (button-symbol '#' escapes are not emitted by the server fixtures).
    """
    out = bytearray()
    for line in text.split('\n'):
        for ch in line:
            # LevelSign::encodeSignCode always uses code 86 for a literal '#'.
            # Its earlier alphabet position (67) is reserved by the escape
            # table and decodes as '#.'.
            idx = 86 if ch == '#' else _SIGN_ALPHABET.find(ch)
            if idx != -1:
                out.append((idx + 32) & 0xFF)
        # Encoded newline (alphabet index of '\n').
        out.append((_SIGN_ALPHABET.find('\n') + 32) & 0xFF)
    return bytes(out)


def build_level_sign(x: int, y: int, text: str) -> bytes:
    """Build PLO_LEVELSIGN packet: [gchar x][gchar y][encoded text]."""
    builder = PacketBuilder().write_gchar(PLO.LEVELSIGN)
    builder.write_gchar(int(x))
    builder.write_gchar(int(y))
    builder.write_bytes(encode_sign_text(text))
    builder.write_byte(ord('\n'))
    return builder.build()


# =============================================================================
# Combat Packets
# =============================================================================


# =============================================================================
# System Packets
# =============================================================================

def build_signature() -> bytes:
    """Build PLO_SIGNATURE packet."""
    builder = PacketBuilder().write_gchar(PLO.SIGNATURE)
    builder.write_newline()
    return builder.build()


def build_server_text(key: str, value: str) -> bytes:
    """Build PLO_SERVERTEXT packet."""
    builder = PacketBuilder().write_gchar(PLO.SERVERTEXT)
    builder.write_string(key)
    builder.write_byte(0x00)  # Separator
    builder.write_string(value)
    builder.write_newline()
    return builder.build()


def build_default_weapon(weapon_name: str) -> bytes:
    """Build PLO_DEFAULTWEAPON packet."""
    builder = PacketBuilder().write_gchar(PLO.DEFAULTWEAPON)
    builder.write_gstring(weapon_name)
    builder.write_newline()
    return builder.build()


def build_has_npc_server(has: bool) -> bytes:
    """Build PLO_HASNPCSERVER packet."""
    builder = PacketBuilder().write_gchar(PLO.HASNPCSERVER)
    builder.write_gchar(1 if has else 0)
    builder.write_newline()
    return builder.build()


def build_staff_guilds(guilds: List[str]) -> bytes:
    """Build PLO_STAFFGUILDS packet."""
    builder = PacketBuilder().write_gchar(PLO.STAFFGUILDS)
    builder.write_string(','.join(guilds))
    builder.write_newline()
    return builder.build()


def build_status_list(statuses: List[str]) -> bytes:
    """Build PLO_STATUSLIST packet."""
    builder = PacketBuilder().write_gchar(PLO.STATUSLIST)
    for status in statuses:
        builder.write_gstring(status)
    builder.write_newline()
    return builder.build()


def build_clear_weapons() -> bytes:
    """Build PLO_CLEARWEAPONS packet."""
    builder = PacketBuilder().write_gchar(PLO.CLEARWEAPONS)
    builder.write_newline()
    return builder.build()


def build_list_processes(processes: List[str]) -> bytes:
    """Build PLO_LISTPROCESSES packet."""
    builder = PacketBuilder().write_gchar(PLO.LISTPROCESSES)
    for proc in processes:
        builder.write_gstring(proc)
    builder.write_newline()
    return builder.build()


# =============================================================================
# Player State Packets
# =============================================================================

def build_warp_failed() -> bytes:
    """Build PLO_WARPFAILED packet."""
    builder = PacketBuilder().write_gchar(PLO.WARPFAILED)
    builder.write_newline()
    return builder.build()


def build_disc_message(message: str) -> bytes:
    """Build PLO_DISCMESSAGE packet (disconnect message)."""
    builder = PacketBuilder().write_gchar(PLO.DISCMESSAGE)
    builder.write_string(message)
    builder.write_newline()
    return builder.build()


def build_freeze_player() -> bytes:
    """Build PLO_FREEZEPLAYER2 packet."""
    builder = PacketBuilder().write_gchar(PLO.FREEZEPLAYER2)
    builder.write_newline()
    return builder.build()


def build_unfreeze_player() -> bytes:
    """Build PLO_UNFREEZEPLAYER packet."""
    builder = PacketBuilder().write_gchar(PLO.UNFREEZEPLAYER)
    builder.write_newline()
    return builder.build()


def build_ghost_mode(enabled: bool) -> bytes:
    """Build PLO_GHOSTMODE packet."""
    builder = PacketBuilder().write_gchar(PLO.GHOSTMODE)
    builder.write_gchar(1 if enabled else 0)
    builder.write_newline()
    return builder.build()


def build_ghost_icon(enabled: bool) -> bytes:
    """Build PLO_GHOSTICON packet."""
    builder = PacketBuilder().write_gchar(PLO.GHOSTICON)
    builder.write_gchar(1 if enabled else 0)
    builder.write_newline()
    return builder.build()


def build_fullstop() -> bytes:
    """Build PLO_FULLSTOP packet (hides HUD, stops input)."""
    builder = PacketBuilder().write_gchar(PLO.FULLSTOP)
    builder.write_newline()
    return builder.build()


def build_is_leader() -> bytes:
    """Build the valueless PLO_ISLEADER packet."""
    builder = PacketBuilder().write_gchar(PLO.ISLEADER)
    builder.write_newline()
    return builder.build()


def build_server_warp(server: str, level: str, x: float, y: float) -> bytes:
    """Build PLO_SERVERWARP packet."""
    builder = PacketBuilder().write_gchar(PLO.SERVERWARP)
    builder.write_gstring(server)
    builder.write_gstring(level)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_newline()
    return builder.build()


def build_flag_set(flag_name: str, flag_value: str) -> bytes:
    """Build PLO_FLAGSET packet."""
    builder = PacketBuilder().write_gchar(PLO.FLAGSET)
    builder.write_string(f"{flag_name}={flag_value}")
    builder.write_newline()
    return builder.build()


def build_flag_del(flag_name: str) -> bytes:
    """Build PLO_FLAGDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.FLAGDEL)
    builder.write_string(flag_name)
    builder.write_newline()
    return builder.build()


