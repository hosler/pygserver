"""
pygserver.protocol.packets - Packet parsing and building utilities

Provides PacketReader for parsing incoming packets and PacketBuilder
for constructing outgoing packets using Reborn protocol encodings.

Based on GServer-v2 packet formats.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple
from .constants import (
    PLO, PLI, PLPROP, NPCPROP, BDPROP, BDMODE, LevelItemType,
    PLSTATUS, PLPERM, NPCVISFLAG, NPCBLOCKFLAG
)

logger = logging.getLogger(__name__)


class PacketReader:
    """
    Utility for reading packet data with Reborn protocol encodings.

    Reborn uses special encodings:
    - GCHAR: value + 32 (1 byte, values 0-223)
    - GSHORT: 2 bytes, (v >> 7) + 32, (v & 0x7F) + 32
    - GINT3: 3 bytes, similar encoding
    """

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_byte(self) -> int:
        """Read a raw byte."""
        if self.pos >= len(self.data):
            return 0
        value = self.data[self.pos]
        self.pos += 1
        return value

    def read_gchar(self) -> int:
        """Read a GCHAR (byte - 32)."""
        return max(0, self.read_byte() - 32)

    def read_gchar_signed(self) -> int:
        """Read a signed GCHAR (byte - 32, not clamped to >=0).

        GServer-v2's CString::readGChar() (dependencies/gs2lib/src/CString.cpp)
        is just `byte - 32` with no floor at zero; unlike read_gchar() above,
        this is for fields that carry a genuine signed value, e.g. hurt
        knockback dx/dy (msgPLI_HURTPLAYER, PlayerClientPackets.cpp:811-815).
        Using read_gchar() there clamps all left/up knockback to 0.
        """
        return self.read_byte() - 32

    def read_gshort(self) -> int:
        """Read a 2-byte GSHORT."""
        if self.pos + 1 >= len(self.data):
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        self.pos += 2
        return (b1 << 7) + b2

    def read_gint3(self) -> int:
        """Read a 3-byte GINT3."""
        if self.pos + 2 >= len(self.data):
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        b3 = self.data[self.pos + 2] - 32
        self.pos += 3
        return (b1 << 14) + (b2 << 7) + b3  # + not |: carry crosses bit 14 (see codec.read_gint3)

    def read_string(self, length: int) -> str:
        """Read a fixed-length string."""
        if self.pos + length > len(self.data):
            length = len(self.data) - self.pos
        data = self.data[self.pos:self.pos + length]
        self.pos += length
        return data.decode('latin-1', errors='replace')

    def read_gstring(self) -> str:
        """Read a length-prefixed string (GCHAR length)."""
        length = self.read_gchar()
        return self.read_string(length)

    def read_bytes(self, length: int) -> bytes:
        """Read raw bytes."""
        if self.pos + length > len(self.data):
            length = len(self.data) - self.pos
        data = self.data[self.pos:self.pos + length]
        self.pos += length
        return data

    def remaining(self) -> bytes:
        """Get remaining data."""
        return self.data[self.pos:]

    def has_data(self) -> bool:
        """Check if more data available."""
        return self.pos < len(self.data)

    def skip(self, count: int):
        """Skip bytes."""
        self.pos += count

    def read_gint5(self) -> int:
        """Read a 5-byte GINT5 (for CRC32, file sizes, etc.)."""
        if self.pos + 4 >= len(self.data):
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        b3 = self.data[self.pos + 2] - 32
        b4 = self.data[self.pos + 3] - 32
        b5 = self.data[self.pos + 4] - 32
        self.pos += 5
        return (b1 << 28) | (b2 << 21) | (b3 << 14) | (b4 << 7) | b5

    def peek_byte(self) -> int:
        """Peek at next byte without advancing position."""
        if self.pos >= len(self.data):
            return 0
        return self.data[self.pos]

    def read_gstring_short(self) -> str:
        """Read a GSHORT-length-prefixed string."""
        length = self.read_gshort()
        return self.read_string(length)


class PacketBuilder:
    """
    Utility for building packet data with Reborn protocol encodings.
    """

    def __init__(self):
        self.data = bytearray()

    def write_byte(self, value: int) -> 'PacketBuilder':
        """Write a raw byte."""
        self.data.append(value & 0xFF)
        return self

    def write_gchar(self, value: int) -> 'PacketBuilder':
        """Write a GCHAR (value + 32)."""
        self.data.append((value + 32) & 0xFF)
        return self

    def write_gchar_signed(self, value: int) -> 'PacketBuilder':
        """Write a signed GCHAR (value + 32).

        Same wire encoding as write_gchar() (which already round-trips
        negative values correctly since it never clamps) - this alias exists
        so signed-value call sites like hurt knockback dx/dy read as
        intentional, matching read_gchar_signed().
        """
        return self.write_gchar(value)

    def write_gshort(self, value: int) -> 'PacketBuilder':
        """Write a 2-byte GSHORT."""
        self.data.append(((value >> 7) + 32) & 0xFF)
        self.data.append(((value & 0x7F) + 32) & 0xFF)
        return self

    def write_gint3(self, value: int) -> 'PacketBuilder':
        """Write a 3-byte GINT3."""
        self.data.append(((value >> 14) + 32) & 0xFF)
        self.data.append((((value >> 7) & 0x7F) + 32) & 0xFF)
        self.data.append(((value & 0x7F) + 32) & 0xFF)
        return self

    def write_gint4(self, value: int) -> 'PacketBuilder':
        """Write a 4-byte GINT4. Max 471347295, lanes clamp at 223 with
        carry, matching gs2lib CString::writeGInt4 (and reborn-protocol's
        writer) — an unclamped top lane would silently wrap mod 256."""
        t = max(0, min(int(value), 471347295))
        b0 = min(t >> 21, 223)
        t -= b0 << 21
        b1 = min(t >> 14, 223)
        t -= b1 << 14
        b2 = min(t >> 7, 223)
        b3 = t - (b2 << 7)
        for b in (b0, b1, b2, b3):
            self.data.append((b + 32) & 0xFF)
        return self

    def write_string(self, value: str) -> 'PacketBuilder':
        """Write raw string bytes."""
        self.data.extend(value.encode('latin-1', errors='replace'))
        return self

    def write_gstring(self, value: str) -> 'PacketBuilder':
        """Write a length-prefixed string (GCHAR length)."""
        encoded = value.encode('latin-1', errors='replace')
        self.write_gchar(len(encoded))
        self.data.extend(encoded)
        return self

    def write_bytes(self, value: bytes) -> 'PacketBuilder':
        """Write raw bytes."""
        self.data.extend(value)
        return self

    def write_gint5(self, value: int) -> 'PacketBuilder':
        """Write a 5-byte GINT5 (for CRC32, file sizes, etc.)."""
        self.data.append(((value >> 28) + 32) & 0xFF)
        self.data.append((((value >> 21) & 0x7F) + 32) & 0xFF)
        self.data.append((((value >> 14) & 0x7F) + 32) & 0xFF)
        self.data.append((((value >> 7) & 0x7F) + 32) & 0xFF)
        self.data.append(((value & 0x7F) + 32) & 0xFF)
        return self

    def write_position2(self, tiles: float) -> 'PacketBuilder':
        """
        Write high-precision position (PLPROP_X2/Y2 format).

        The position is encoded as 2 bytes:
        - Pixels = tiles * 16
        - Value = (pixels << 1) | sign_bit
        - Encoded as GSHORT
        """
        pixels = int(tiles * 16)
        if pixels < 0:
            value = ((-pixels) << 1) | 1
        else:
            value = pixels << 1
        return self.write_gshort(value)

    def write_newline(self) -> 'PacketBuilder':
        """Write packet terminator (newline)."""
        self.data.append(0x0A)
        return self

    def write_gstring_short(self, value: str) -> 'PacketBuilder':
        """Write a GSHORT-length-prefixed string."""
        encoded = value.encode('latin-1', errors='replace')
        self.write_gshort(len(encoded))
        self.data.extend(encoded)
        return self

    def build(self) -> bytes:
        """Build the final packet bytes."""
        return bytes(self.data)


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


def parse_player_props(data: bytes, start_pos: int = 0) -> dict:
    """
    Parse player properties from packet data.

    Returns dict of property values.
    """
    props = {}
    pos = start_pos

    while pos < len(data):
        prop_id = data[pos] - 32
        pos += 1

        if prop_id < 0 or prop_id > 100:
            break

        # String properties
        if prop_id in [PLPROP.NICKNAME, PLPROP.GANI, PLPROP.HEADIMAGE,
                       PLPROP.CURCHAT, PLPROP.CURLEVEL, PLPROP.BODYIMAGE,
                       PLPROP.ACCOUNTNAME, PLPROP.OSTYPE]:
            if pos < len(data):
                str_len = data[pos] - 32
                pos += 1
                if str_len > 0 and pos + str_len <= len(data):
                    props[prop_id] = data[pos:pos + str_len].decode('latin-1', errors='replace')
                    pos += str_len

        # GATTRIB properties (strings)
        elif PLPROP.GATTRIB1 <= prop_id <= PLPROP.GATTRIB30:
            if pos < len(data):
                str_len = data[pos] - 32
                pos += 1
                if str_len > 0 and pos + str_len <= len(data):
                    props[prop_id] = data[pos:pos + str_len].decode('latin-1', errors='replace')
                    pos += str_len

        # Single byte properties
        elif prop_id in [PLPROP.MAXPOWER, PLPROP.CURPOWER, PLPROP.ARROWSCOUNT,
                         PLPROP.BOMBSCOUNT, PLPROP.GLOVEPOWER, PLPROP.BOMBPOWER,
                         PLPROP.SPRITE, PLPROP.X, PLPROP.Y, PLPROP.DIRECTION,
                         PLPROP.STATUS, PLPROP.CARRYSPRITE,
                         PLPROP.MAGICPOINTS, PLPROP.ALIGNMENT]:
            if pos < len(data):
                props[prop_id] = data[pos] - 32
                pos += 1

        # ID of the NPC currently carried by the player (3-byte GInt).
        elif prop_id == PLPROP.CARRYNPC:
            if pos + 2 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                b3 = data[pos + 2] - 32
                props[prop_id] = (b1 << 14) + (b2 << 7) + b3  # + not |: carry crosses bit 14 (see codec.read_gint3)
                pos += 3

        # Sword/Shield power (1 byte, or 1 + string if > threshold)
        elif prop_id == PLPROP.SWORDPOWER:
            if pos < len(data):
                power = data[pos] - 32
                pos += 1
                if power > 4:
                    str_len = data[pos] - 32 if pos < len(data) else 0
                    pos += 1
                    if str_len > 0 and pos + str_len <= len(data):
                        props['sword_image'] = data[pos:pos + str_len].decode('latin-1', errors='replace')
                        pos += str_len
                props[prop_id] = power

        elif prop_id == PLPROP.SHIELDPOWER:
            if pos < len(data):
                power = data[pos] - 32
                pos += 1
                if power > 3:
                    str_len = data[pos] - 32 if pos < len(data) else 0
                    pos += 1
                    if str_len > 0 and pos + str_len <= len(data):
                        props['shield_image'] = data[pos:pos + str_len].decode('latin-1', errors='replace')
                        pos += str_len
                props[prop_id] = power

        # Colors (5 bytes)
        elif prop_id == PLPROP.COLORS:
            if pos + 4 < len(data):
                props[prop_id] = [data[pos + i] - 32 for i in range(5)]
                pos += 5

        # Rupees (3 bytes gInt3)
        elif prop_id == PLPROP.RUPEESCOUNT:
            if pos + 2 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                b3 = data[pos + 2] - 32
                props[prop_id] = (b1 << 14) + (b2 << 7) + b3  # + not |: carry crosses bit 14 (see codec.read_gint3)
                pos += 3

        # Text codepage (3 bytes gInt3)
        elif prop_id == PLPROP.TEXTCODEPAGE:
            if pos + 2 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                b3 = data[pos + 2] - 32
                props[prop_id] = (b1 << 14) + (b2 << 7) + b3  # + not |: carry crosses bit 14 (see codec.read_gint3)
                pos += 3

        # High-precision position (2 bytes each)
        elif prop_id == PLPROP.X2:
            if pos + 1 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                pos += 2
                raw = (b1 << 7) | b2
                pixels = raw >> 1
                if raw & 1:
                    pixels = -pixels
                props[prop_id] = pixels / 16.0

        elif prop_id == PLPROP.Y2:
            if pos + 1 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                pos += 2
                raw = (b1 << 7) | b2
                pixels = raw >> 1
                if raw & 1:
                    pixels = -pixels
                props[prop_id] = pixels / 16.0

        elif prop_id == PLPROP.Z2:
            if pos + 1 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                pos += 2
                raw = (b1 << 7) | b2
                props[prop_id] = raw

        # Default: skip 1 byte
        else:
            pos += 1

    return props


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


# Sword/shield power: a raw value below the threshold is a bare preset power; at
# or above it the power is (raw - threshold) followed by an image string. Match
# the v6.037 (new-world) client reader in pyReborn (_read_sword).
_SWORD_THRESHOLD = 30
_SHIELD_THRESHOLD = 10


def _write_player_prop(builder: 'PacketBuilder', prop_id: int, value) -> None:
    """Write a single player property (id + correctly-sized payload).

    Unknown props are skipped WITHOUT writing the prop id, since a bare id with
    no payload desyncs every following prop in the packet.
    """
    # String properties. HORSEGIF (21) is a plain PropertyString (GServer-v2
    # server/include/object/Player.h PLAYERPROP_LIST + PropertySerializers.h
    # PropertyString::serialize - length-prefixed, unlike the HEADGIF
    # 100-offset form), so it belongs here rather than needing special-casing.
    if prop_id in (PLPROP.NICKNAME, PLPROP.GANI,
                   PLPROP.CURCHAT, PLPROP.CURLEVEL, PLPROP.BODYIMAGE,
                   PLPROP.ACCOUNTNAME, PLPROP.HORSEGIF):
        builder.write_gchar(prop_id)
        builder.write_gstring(str(value))

    # Head image (HEADGIF, prop 11): a preset id 0-99 is a bare gchar; a custom
    # image string is gchar(100 + len) followed by the raw chars. Matches
    # GServer-v2 PropertyHeadGif::serialize and the client _read_headgif.
    elif prop_id == PLPROP.HEADIMAGE:
        builder.write_gchar(prop_id)
        if isinstance(value, int):
            builder.write_gchar(min(99, value))
        else:
            name = str(value).encode('latin-1')
            builder.write_gchar(100 + len(name))
            builder.write_bytes(name)

    # GATTRIB strings
    elif PLPROP.GATTRIB1 <= prop_id <= PLPROP.GATTRIB30:
        builder.write_gchar(prop_id)
        builder.write_gstring(str(value))

    # Single byte. HORSEBUSHES (22) is GServer-v2's PropertyNumeric<GBYTE1>
    # (server/include/object/Player.h) - a plain gchar, independent of the
    # direction-packed byte used on the wire in the PLI/PLO_HORSEADD packet.
    elif prop_id in (PLPROP.MAXPOWER, PLPROP.CURPOWER, PLPROP.ARROWSCOUNT,
                     PLPROP.BOMBSCOUNT, PLPROP.GLOVEPOWER, PLPROP.BOMBPOWER,
                     PLPROP.SPRITE, PLPROP.DIRECTION, PLPROP.STATUS,
                     PLPROP.MAGICPOINTS, PLPROP.ALIGNMENT, PLPROP.HORSEBUSHES):
        builder.write_gchar(prop_id)
        builder.write_gchar(int(value))

    # Sword/shield power (bare gchar for preset powers; image form above threshold)
    elif prop_id == PLPROP.SWORDPOWER:
        builder.write_gchar(prop_id)
        _write_sword_prop(builder, value, _SWORD_THRESHOLD)
    elif prop_id == PLPROP.SHIELDPOWER:
        builder.write_gchar(prop_id)
        _write_sword_prop(builder, value, _SHIELD_THRESHOLD)

    # Low-precision position (half-tiles)
    elif prop_id == PLPROP.X:
        builder.write_gchar(prop_id)
        builder.write_gchar(int(value * 2))
    elif prop_id == PLPROP.Y:
        builder.write_gchar(prop_id)
        builder.write_gchar(int(value * 2))

    # High-precision position
    elif prop_id == PLPROP.X2:
        builder.write_gchar(prop_id)
        builder.write_position2(float(value))
    elif prop_id == PLPROP.Y2:
        builder.write_gchar(prop_id)
        builder.write_position2(float(value))

    # Colors: v6.037 new-world expects 8 bytes (pad shorter lists with 0).
    elif prop_id == PLPROP.COLORS:
        builder.write_gchar(prop_id)
        colors = list(value)[:8]
        colors += [0] * (8 - len(colors))
        for c in colors:
            builder.write_gchar(int(c))

    # Rupees (gInt3)
    elif prop_id == PLPROP.RUPEESCOUNT:
        builder.write_gchar(prop_id)
        builder.write_gint3(int(value))

    # Kills/deaths (GBYTE3 = gInt3, GServer-v2 server/include/object/Player.h
    # PLAYERPROP_LIST); previously unhandled, so death/kill counters were
    # silently dropped instead of reaching the client.
    elif prop_id in (PLPROP.KILLSCOUNT, PLPROP.DEATHSCOUNT):
        builder.write_gchar(prop_id)
        builder.write_gint3(int(value))

    else:
        logger.warning("build_player_props: unhandled prop %s (skipped)", prop_id)


def _write_sword_prop(builder: 'PacketBuilder', value, threshold: int) -> None:
    """Write a SWORDPOWER/SHIELDPOWER payload.

    `value` may be an int power (no image) or a (power, image) tuple.
    """
    if isinstance(value, (tuple, list)):
        power, image = int(value[0]), str(value[1])
        builder.write_gchar(threshold + power)
        builder.write_gstring(image)
    else:
        builder.write_gchar(int(value))


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


# NPCProp string ids (length-prefixed strings), per GServer-v2 NPC.h + the
# client parse_npc_props. IMAGE, SWORD/SHIELD image, GANI, MESSAGE, NICKNAME,
# HORSEIMAGE, BODYIMAGE and all GATTRIBs.
_NPC_STRING_PROPS = frozenset(
    {0, 10, 11, 12, 15, 20, 21, 35} | set(range(36, 48)) | set(range(53, 74))
)


def build_npc_props(npc_id: int, props: dict) -> bytes:
    """Build PLO_NPCPROPS packet.

    Encodings mirror GServer-v2 NPC props / the client parse_npc_props:
    IMAGE/GANI/etc are length-prefixed strings, SCRIPT is a gshort-length string,
    X/Y are half-tiles, HEADIMAGE uses the HEADGIF 100-offset form, everything
    else is a single byte.
    """
    builder = PacketBuilder().write_gchar(PLO.NPCPROPS).write_gint3(npc_id)

    for prop_id, value in props.items():
        builder.write_gchar(prop_id)

        if prop_id in _NPC_STRING_PROPS:
            builder.write_gstring(str(value))
        elif prop_id == NPCPROP.SCRIPT:  # gShort length + raw
            encoded = str(value).encode('latin-1', errors='replace')
            builder.write_gshort(len(encoded)).write_bytes(encoded)
        elif prop_id in (NPCPROP.X, NPCPROP.Y):
            builder.write_gchar(int(value * 2))
        elif prop_id in (NPCPROP.X2, NPCPROP.Y2):  # high-precision position (gshort, pixels/16)
            builder.write_position2(float(value))
        elif prop_id == NPCPROP.HEADIMAGE:  # HEADGIF 100-offset string
            name = str(value).encode('latin-1')
            builder.write_gchar(100 + len(name)).write_bytes(name)
        elif prop_id == NPCPROP.COLORS:  # 8 colors, each written as a gchar (client reads byte-32)
            colors = list(value)[:8]
            colors += [0] * (8 - len(colors))
            for c in colors:
                builder.write_gchar(int(c) & 0xFF)
        elif prop_id == NPCPROP.RUPEES:
            builder.write_gint3(int(value))
        elif prop_id == NPCPROP.IMAGEPART:
            # PropertyImagePart: gushort x, gushort y, gchar w, gchar h -
            # sub-rect of the NPC image sheet (GS1 setimgpart)
            px, py, pw, ph = (int(v) for v in value)
            builder.write_gshort(px).write_gshort(py)
            builder.write_gchar(pw).write_gchar(ph)
        else:
            builder.write_gchar(int(value) if isinstance(value, (int, float)) else 0)

    builder.write_byte(ord('\n'))
    return builder.build()


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
            idx = _SIGN_ALPHABET.find(ch)
            if idx == -1 and ch == '#':
                idx = 86
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

def build_bomb_add(player_id: int, x: float, y: float, power: int, time_left: float) -> bytes:
    """Build PLO_BOMBADD packet.

    Format: {GSHORT owner_id}{GCHAR x*2}{GCHAR y*2}{GCHAR power}{GCHAR timer}
    timer is 50ms increments (+50ms base); time_left is seconds.
    """
    builder = PacketBuilder().write_gchar(PLO.BOMBADD)
    builder.write_gshort(player_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(int(power) & 0x03)
    builder.write_gchar(max(0, int(time_left / 0.05) - 1))
    builder.write_newline()
    return builder.build()


def build_bomb_del(x: float, y: float) -> bytes:
    """Build PLO_BOMBDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.BOMBDEL)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_newline()
    return builder.build()


def build_arrow_add(player_id: int, x: float, y: float, flags: int,
                     sprite: int = 0, power: int = 1) -> bytes:
    """Build PLO_ARROWADD packet.

    Wire format (GServer-v2 PlayerClientPackets.cpp msgPLI_ARROWADD, which
    rebroadcasts the client's own payload verbatim after prefixing the
    sender's id):
        {GSHORT owner_id}{GCHAR x*2}{GCHAR y*2}{GCHAR flags}{GCHAR sprite}{GCHAR power}
    flags: bit0-1 direction, bit2 reflect, bit3 fromPlayer (see the same
    function's read side: dir = flags & 0b11, reflect = flags & 0b100,
    fromPlayer = flags & 0b1000). Previously this only wrote a bare
    direction and dropped sprite/power, corrupting the relay payload the
    client's parse_arrow_add() expects.
    """
    builder = PacketBuilder().write_gchar(PLO.ARROWADD)
    builder.write_gshort(player_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(flags & 0xFF)
    builder.write_gchar(sprite)
    builder.write_gchar(power)
    builder.write_newline()
    return builder.build()


def build_explosion(x: float, y: float, radius: int, power: int) -> bytes:
    """Build PLO_EXPLOSION packet."""
    builder = PacketBuilder().write_gchar(PLO.EXPLOSION)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(int(radius))
    builder.write_gchar(int(power))
    builder.write_newline()
    return builder.build()


def build_hurt_player(attacker_id: int, hurt_dx: int, hurt_dy: int,
                      power: int, npc_id: int = 0) -> bytes:
    """Build PLO_HURTPLAYER packet.

    Format (GServer-v2 msgPLI_HURTPLAYER relay, PlayerClientPackets.cpp:811-829):
        [gshort attacker_id][gchar hurtdx][gchar hurtdy][gchar power][gint3 npc]
    `power` is the damage in half-hearts. attacker_id is the player dealing the
    damage (0 = environment). hurtdx/hurtdy are signed (readGChar() on the
    client), so left/up knockback must round-trip through write_gchar_signed.
    """
    builder = PacketBuilder().write_gchar(PLO.HURTPLAYER)
    builder.write_gshort(attacker_id)
    builder.write_gchar_signed(int(hurt_dx))
    builder.write_gchar_signed(int(hurt_dy))
    builder.write_gchar(int(power))
    builder.write_gint3(npc_id)
    builder.write_newline()
    return builder.build()


def build_hit_objects(source_id: int, power: int, x: float, y: float,
                       npc_id: Optional[int] = None) -> bytes:
    """Build PLO_HITOBJECTS packet (client hit-effect notification).

    Wire format (GServer-v2 msgPLI_HITOBJECTS relay / Server::hitObjectsAtPoint,
    PlayerClientPackets.cpp:1017-1044, Server.cpp:2247-2257):
        {GSHORT source_id}{GCHAR power}{GCHAR x*2}{GCHAR y*2}[{GINT3 npc_id}]
    source_id is the hitting player's id, or 0 when the hit was NPC-sourced
    (in which case npc_id is appended instead). `power` is already
    half-heart scaled (callers pass power*2, matching the C++ side which
    pre-scales before this call - see gs1_host._c_hitobjects).
    """
    builder = PacketBuilder().write_gchar(PLO.HITOBJECTS)
    builder.write_gshort(source_id)
    builder.write_gchar(int(power) & 0xFF)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    if npc_id is not None:
        builder.write_gint3(npc_id)
    builder.write_newline()
    return builder.build()


def build_fire_spy(player_id: int, x: float, y: float) -> bytes:
    """Build PLO_FIRESPY packet."""
    builder = PacketBuilder().write_gchar(PLO.FIRESPY)
    builder.write_gshort(player_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_newline()
    return builder.build()


def build_throw_carried(player_id: int) -> bytes:
    """Build PLO_THROWCARRIED packet.

    Format (GServer-v2 msgPLI_THROWCARRIED, PlayerClientPackets.cpp:332-336):
        [gshort player_id] - no other payload; the client already knows what
    it was carrying and infers the throw. Confirmed against pyReborn's
    parse_throwcarried (pyReborn/pyreborn/packets.py), which reads only the
    owner id. Previously this also wrote x/y/direction, which would have
    desynced the client's parser had this ever been called.
    """
    builder = PacketBuilder().write_gchar(PLO.THROWCARRIED)
    builder.write_gshort(player_id)
    builder.write_newline()
    return builder.build()


def build_push_away(dx: float, dy: float) -> bytes:
    """Build PLO_PUSHAWAY packet (knockback)."""
    builder = PacketBuilder().write_gchar(PLO.PUSHAWAY)
    builder.write_gchar(int(dx * 2))
    builder.write_gchar(int(dy * 2))
    builder.write_newline()
    return builder.build()


# =============================================================================
# Item Packets
# =============================================================================

def build_item_add(x: float, y: float, item_type: int) -> bytes:
    """Build PLO_ITEMADD packet."""
    builder = PacketBuilder().write_gchar(PLO.ITEMADD)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(item_type)
    builder.write_newline()
    return builder.build()


def build_item_del(x: float, y: float) -> bytes:
    """Build PLO_ITEMDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.ITEMDEL)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_newline()
    return builder.build()


def build_level_chest(opened: bool, x: int, y: int,
                      item_type: int = 0, sign_index: int = 0) -> bytes:
    """Build PLO_LEVELCHEST packet.

    Format (GServer-v2 sendChestsToPlayer): [gchar opened][gchar x][gchar y],
    plus [gchar item][gchar sign] only for *unopened* chests announced on entry.
    """
    builder = PacketBuilder().write_gchar(PLO.LEVELCHEST)
    builder.write_gchar(1 if opened else 0)
    builder.write_gchar(x)
    builder.write_gchar(y)
    if not opened:
        builder.write_gchar(item_type)
        builder.write_gchar(sign_index)
    builder.write_newline()
    return builder.build()


# =============================================================================
# Horse Packets
# =============================================================================

def build_horse_add(x: float, y: float, direction: int, bushes: int, image: str) -> bytes:
    """Build PLO_HORSEADD packet.

    Wire format (GServer-v2 msgPLI_HORSEADD relay, PlayerClientPackets.cpp:
    256-269): {GCHAR x*2}{GCHAR y*2}{GCHAR dir_bushes}{RAW image}. dir_bushes
    packs direction in bits 0-1 and bush count in the rest of the byte
    (dir | bushes << 2); image is a raw trailing string with NO length
    prefix (pPacket.readString("")), so this packet must be last in its
    frame. Previously this wrote direction/bushes as two separate gchars and
    length-prefixed the image, desyncing every client that parses the real
    wire format.
    """
    builder = PacketBuilder().write_gchar(PLO.HORSEADD)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar((int(direction) & 0x03) | ((int(bushes) & 0x3F) << 2))
    builder.write_string(image)
    builder.write_newline()
    return builder.build()


def build_horse_del(x: float, y: float) -> bytes:
    """Build PLO_HORSEDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.HORSEDEL)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_newline()
    return builder.build()


# =============================================================================
# Baddy Packets
# =============================================================================

def build_baddy_props(baddy_id: int, props: dict) -> bytes:
    """Build PLO_BADDYPROPS packet."""
    builder = PacketBuilder().write_gchar(PLO.BADDYPROPS)
    builder.write_gchar(baddy_id)

    for prop_id, value in props.items():
        builder.write_gchar(prop_id)

        if prop_id == BDPROP.ID:
            builder.write_gchar(value)
        elif prop_id in [BDPROP.X, BDPROP.Y]:
            builder.write_gchar(int(value * 2))
        elif prop_id == BDPROP.TYPE:
            builder.write_gchar(value)
        elif prop_id == BDPROP.POWERIMAGE:
            power, image = value
            builder.write_gchar(power)
            builder.write_gstring(image)
        elif prop_id == BDPROP.MODE:
            builder.write_gchar(value)
        elif prop_id in [BDPROP.ANI, BDPROP.DIR]:
            builder.write_gchar(value)
        elif prop_id in [BDPROP.VERSESIGHT, BDPROP.VERSEHURT, BDPROP.VERSEATTACK]:
            builder.write_gstring(value)

    builder.write_newline()
    return builder.build()


def build_baddy_hurt(baddy_id: int, hurt_dx: float, hurt_dy: float, damage: int) -> bytes:
    """Build PLO_BADDYHURT packet.

    Wire format (GServer-v2 msgPLI_BADDYHURT, PlayerClientPackets.cpp:523-539,
    commit e0cd07af9bb4be09c54c0335f222dd0eacb71c1): [GUChar baddyId]
    [GChar hurtDX][GChar hurtDY][GUChar damage in half-hearts]. GServer-v2
    itself never parses these server-side - it just forwards the raw inbound
    PLI_BADDYHURT payload to the baddy's leader verbatim. pygserver is
    authoritative for baddy damage/knockback (see BaddyManager.handle_baddy_hurt),
    so this builds the relay from scratch instead of echoing client input -
    hurt_dx/hurt_dy are the server-computed knockback direction, normalized to
    -1.0..1.0 per axis.

    hurtDX/hurtDY use the "midpoint: 64" gchar idiom that packet handler notes:
    a value of 0 encodes as byte 64+32, +1.0 as 128+32, -1.0 as 0+32 - the
    write-side mirror of PacketReader.read_gchar_signed() minus 64 on read.
    """
    builder = PacketBuilder().write_gchar(PLO.BADDYHURT)
    builder.write_gchar(baddy_id)
    builder.write_gchar_signed(int(max(-1.0, min(1.0, hurt_dx)) * 64) + 64)
    builder.write_gchar_signed(int(max(-1.0, min(1.0, hurt_dy)) * 64) + 64)
    builder.write_gchar(damage)
    builder.write_newline()
    return builder.build()


# =============================================================================
# NPC Packets (Extended)
# =============================================================================

def build_npc_moved(npc_id: int) -> bytes:
    """Build PLO_NPCMOVED packet (hides NPC for warping)."""
    builder = PacketBuilder().write_gchar(PLO.NPCMOVED)
    builder.write_gint3(npc_id)
    builder.write_newline()
    return builder.build()


def build_npc_del2(level_name: str, npc_id: int) -> bytes:
    """Build PLO_NPCDEL2 packet (NPC deleted with level name)."""
    builder = PacketBuilder().write_gchar(PLO.NPCDEL2)
    builder.write_gstring(level_name)
    builder.write_gint3(npc_id)
    builder.write_newline()
    return builder.build()


def build_npc_weapon_add(weapon_name: str, image: str, script: str) -> bytes:
    """Build PLO_NPCWEAPONADD packet."""
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONADD)
    builder.write_gstring(weapon_name)
    builder.write_gstring(image)
    builder.write_gstring_short(script)
    builder.write_newline()
    return builder.build()


def build_npc_weapon_del(weapon_name: str) -> bytes:
    """Build PLO_NPCWEAPONDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONDEL)
    builder.write_gstring(weapon_name)
    builder.write_newline()
    return builder.build()


def build_npc_weapon_script(info_length: int, script: str) -> bytes:
    """Build PLO_NPCWEAPONSCRIPT packet."""
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONSCRIPT)
    builder.write_gshort(info_length)
    builder.write_string(script)
    builder.write_newline()
    return builder.build()


def build_npc_bytecode(npc_id: int, bytecode: bytes) -> bytes:
    """Build PLO_NPCBYTECODE packet."""
    builder = PacketBuilder().write_gchar(PLO.NPCBYTECODE)
    builder.write_gint3(npc_id)
    builder.write_bytes(bytecode)
    builder.write_newline()
    return builder.build()


def build_hide_npcs(hide: bool) -> bytes:
    """Build PLO_HIDENPCS packet."""
    builder = PacketBuilder().write_gchar(PLO.HIDENPCS)
    builder.write_gchar(1 if hide else 0)
    builder.write_newline()
    return builder.build()


# =============================================================================
# Communication Packets
# =============================================================================

def build_private_message(from_id: int, sender_name: str, message: str,
                          is_mass: bool = False) -> bytes:
    """Build PLO_PRIVATEMESSAGE packet.

    Format (GServer-v2 Player.cpp sendPrivateMessage): [gshort from_id][body],
    where the body is the constructed message "#b{label}:#b{msg}" (newlines
    and literal "#b" both act as line breaks) split into lines and re-joined
    with toCSV(force_quoted=True) - every line becomes a comma-separated
    quoted field with '"' and '\\' doubled. The client strips the first line,
    so the body always leads with an empty quoted field:

        '"","Private message:","line1","line2"'

    The sender is identified by from_id only; sender_name is not on the wire.
    """
    label = "Mass message:" if is_mass else "Private message:"
    lines = ['', label] + message.replace('\n', '#b').split('#b')
    body = ','.join(
        '"' + line.replace('\\', '\\\\').replace('"', '""') + '"'
        for line in lines
    )
    builder = PacketBuilder().write_gchar(PLO.PRIVATEMESSAGE)
    builder.write_gshort(from_id)
    builder.write_string(body)
    builder.write_newline()
    return builder.build()


def build_show_img(code: int, x: float, y: float, image: str) -> bytes:
    """Build PLO_SHOWIMG packet."""
    builder = PacketBuilder().write_gchar(PLO.SHOWIMG)
    builder.write_gchar(code)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_string(image)
    builder.write_newline()
    return builder.build()


def build_admin_message(message: str) -> bytes:
    """Build PLO_RC_ADMINMESSAGE packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_ADMINMESSAGE)
    builder.write_string(message)
    builder.write_newline()
    return builder.build()


def build_say2(text: str) -> bytes:
    """Build PLO_SAY2 packet (also used for signs)."""
    builder = PacketBuilder().write_gchar(PLO.SAY2)
    builder.write_string(text)
    builder.write_newline()
    return builder.build()


def build_trigger_action(player_id: int, npc_id: int, x: float, y: float,
                          action: str) -> bytes:
    """Build PLO_TRIGGERACTION packet.

    Wire format (GServer-v2 PlayerClientPackets.cpp msgPLI_TRIGGERACTION,
    both the player->player relay - which prepends the sender's gshort id to
    its own raw payload starting with the gint3 npc id - and the
    server/NPC-originated variants in Server.cpp/TriggerCommandHandlers.cpp):
        {GSHORT player_id}{GINT3 npc_id}{GCHAR x*2}{GCHAR y*2}{action CSV}
    player_id/npc_id are mutually exclusive in practice (0 for whichever
    didn't originate the trigger).
    """
    builder = PacketBuilder().write_gchar(PLO.TRIGGERACTION)
    builder.write_gshort(player_id)
    builder.write_gint3(npc_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_string(action)
    builder.write_newline()
    return builder.build()


def build_ghost_text(text: str) -> bytes:
    """Build PLO_GHOSTTEXT packet (shows in lower-right during ghost mode)."""
    builder = PacketBuilder().write_gchar(PLO.GHOSTTEXT)
    builder.write_string(text)
    builder.write_newline()
    return builder.build()


def build_rpg_window(text: str) -> bytes:
    """Build PLO_RPGWINDOW packet."""
    builder = PacketBuilder().write_gchar(PLO.RPGWINDOW)
    builder.write_string(text)
    builder.write_newline()
    return builder.build()


# =============================================================================
# Level Packets (Extended)
# =============================================================================

def build_level_board(tiles: bytes) -> bytes:
    """Build PLO_LEVELBOARD packet."""
    builder = PacketBuilder().write_gchar(PLO.LEVELBOARD)
    builder.write_bytes(tiles)
    builder.write_newline()
    return builder.build()


def build_level_modtime(modtime: int) -> bytes:
    """Build PLO_LEVELMODTIME packet."""
    builder = PacketBuilder().write_gchar(PLO.LEVELMODTIME)
    builder.write_gint5(modtime)
    builder.write_newline()
    return builder.build()


def build_board_modify(x: int, y: int, width: int, height: int, tiles: bytes) -> bytes:
    """Build PLO_BOARDMODIFY packet."""
    builder = PacketBuilder().write_gchar(PLO.BOARDMODIFY)
    builder.write_gchar(x)
    builder.write_gchar(y)
    builder.write_gchar(width)
    builder.write_gchar(height)
    builder.write_bytes(tiles)
    builder.write_newline()
    return builder.build()


def build_board_layer(layer: int, tiles: bytes) -> bytes:
    """Build PLO_BOARDLAYER packet."""
    builder = PacketBuilder().write_gchar(PLO.BOARDLAYER)
    builder.write_gchar(layer)
    builder.write_bytes(tiles)
    builder.write_newline()
    return builder.build()


def build_set_active_level(level_name: str) -> bytes:
    """Build PLO_SETACTIVELEVEL packet."""
    builder = PacketBuilder().write_gchar(PLO.SETACTIVELEVEL)
    builder.write_string(level_name)
    builder.write_newline()
    return builder.build()


def build_minimap(text: str) -> bytes:
    """Build PLO_MINIMAP packet."""
    builder = PacketBuilder().write_gchar(PLO.MINIMAP)
    builder.write_string(text)
    builder.write_newline()
    return builder.build()


# =============================================================================
# File Packets
# =============================================================================

def build_file(filename: str, data: bytes, mod_time: int = 0) -> bytes:
    """Build PLO_FILE packet.

    Format (GServer-v2 sendFile, client >= 2.1):
        [gchar PLO_FILE][gint5 modTime][gchar len(filename)][filename][data][\\n]

    This packet contains arbitrary bytes (incl. newlines) so it must be preceded
    by a PLO_RAWDATA announcement of its total length.
    """
    name = filename.encode('latin-1')
    builder = PacketBuilder().write_gchar(PLO.FILE)
    builder.write_gint5(mod_time)
    builder.write_gchar(len(name))
    builder.write_bytes(name)
    builder.write_bytes(data)
    builder.write_byte(ord('\n'))
    return builder.build()


def build_file_send_failed(filename: str) -> bytes:
    """Build PLO_FILESENDFAILED packet."""
    builder = PacketBuilder().write_gchar(PLO.FILESENDFAILED)
    builder.write_string(filename)
    builder.write_newline()
    return builder.build()


def build_file_uptodate(filename: str) -> bytes:
    """Build PLO_FILEUPTODATE packet."""
    builder = PacketBuilder().write_gchar(PLO.FILEUPTODATE)
    builder.write_string(filename)
    builder.write_newline()
    return builder.build()


def build_large_file_start(filename: str) -> bytes:
    """Build PLO_LARGEFILESTART packet."""
    builder = PacketBuilder().write_gchar(PLO.LARGEFILESTART)
    builder.write_string(filename)
    builder.write_newline()
    return builder.build()


def build_large_file_end() -> bytes:
    """Build PLO_LARGEFILEEND packet."""
    builder = PacketBuilder().write_gchar(PLO.LARGEFILEEND)
    builder.write_newline()
    return builder.build()


def build_large_file_size(size: int) -> bytes:
    """Build PLO_LARGEFILESIZE packet."""
    builder = PacketBuilder().write_gchar(PLO.LARGEFILESIZE)
    builder.write_gint5(size)
    builder.write_newline()
    return builder.build()


def build_gani_script(gani_name: str, script: str) -> bytes:
    """Build PLO_GANISCRIPT packet."""
    builder = PacketBuilder().write_gchar(PLO.GANISCRIPT)
    builder.write_gstring(gani_name)
    builder.write_string(script)
    builder.write_newline()
    return builder.build()


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


# =============================================================================
# RC (Remote Control) Packets
# =============================================================================

def build_rc_chat(message: str) -> bytes:
    """Build PLO_RC_CHAT packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_CHAT)
    builder.write_string(message)
    builder.write_newline()
    return builder.build()


def build_rc_server_options(options: str) -> bytes:
    """Build PLO_RC_SERVEROPTIONSGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_SERVEROPTIONSGET)
    builder.write_string(options)
    builder.write_newline()
    return builder.build()


def build_rc_folder_config(config: str) -> bytes:
    """Build PLO_RC_FOLDERCONFIGGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_FOLDERCONFIGGET)
    builder.write_string(config)
    builder.write_newline()
    return builder.build()


def build_rc_server_flags(flags: str) -> bytes:
    """Build PLO_RC_SERVERFLAGSGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_SERVERFLAGSGET)
    builder.write_string(flags)
    builder.write_newline()
    return builder.build()


def build_rc_player_props(account: str, props: str) -> bytes:
    """Build PLO_RC_PLAYERPROPSGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_PLAYERPROPSGET)
    builder.write_gstring(account)
    builder.write_string(props)
    builder.write_newline()
    return builder.build()


def build_rc_player_rights(account: str, rights: int) -> bytes:
    """Build PLO_RC_PLAYERRIGHTSGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_PLAYERRIGHTSGET)
    builder.write_gstring(account)
    builder.write_gint3(rights)
    builder.write_newline()
    return builder.build()


def build_rc_player_comments(account: str, comments: str) -> bytes:
    """Build PLO_RC_PLAYERCOMMENTSGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_PLAYERCOMMENTSGET)
    builder.write_gstring(account)
    builder.write_string(comments)
    builder.write_newline()
    return builder.build()


def build_rc_player_ban(account: str, banned: bool, reason: str, length: str) -> bytes:
    """Build PLO_RC_PLAYERBANGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_PLAYERBANGET)
    builder.write_gstring(account)
    builder.write_gchar(1 if banned else 0)
    builder.write_gstring(reason)
    builder.write_gstring(length)
    builder.write_newline()
    return builder.build()


def build_rc_account_list(accounts: List[str]) -> bytes:
    """Build PLO_RC_ACCOUNTLISTGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_ACCOUNTLISTGET)
    for acc in accounts:
        builder.write_gstring(acc)
    builder.write_newline()
    return builder.build()


def build_rc_account_get(account: str, props: str) -> bytes:
    """Build PLO_RC_ACCOUNTGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_ACCOUNTGET)
    builder.write_gstring(account)
    builder.write_string(props)
    builder.write_newline()
    return builder.build()


def build_rc_file_browser_dir(path: str, files: List[Tuple[str, int, int]]) -> bytes:
    """Build PLO_RC_FILEBROWSER_DIRLIST packet.

    Args:
        path: Current directory path
        files: List of (filename, size, modtime) tuples
    """
    builder = PacketBuilder().write_gchar(PLO.RC_FILEBROWSER_DIRLIST)
    builder.write_gstring(path)
    for filename, size, modtime in files:
        builder.write_gstring(filename)
        builder.write_gint5(size)
        builder.write_gint5(modtime)
    builder.write_newline()
    return builder.build()


def build_rc_file_browser_message(message: str) -> bytes:
    """Build PLO_RC_FILEBROWSER_MESSAGE packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_FILEBROWSER_MESSAGE)
    builder.write_string(message)
    builder.write_newline()
    return builder.build()


def build_rc_max_upload_filesize(size: int) -> bytes:
    """Build PLO_RC_MAXUPLOADFILESIZE packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_MAXUPLOADFILESIZE)
    builder.write_gint5(size)
    builder.write_newline()
    return builder.build()


# =============================================================================
# NC (NPC Control) Packets
# =============================================================================

def build_nc_level_list(levels: List[str]) -> bytes:
    """Build PLO_NC_LEVELLIST packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_LEVELLIST)
    for level in levels:
        builder.write_gstring(level)
    builder.write_newline()
    return builder.build()


def build_nc_npc_attributes(npc_id: int, attributes: str) -> bytes:
    """Build PLO_NC_NPCATTRIBUTES packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_NPCATTRIBUTES)
    builder.write_gint3(npc_id)
    builder.write_string(attributes)
    builder.write_newline()
    return builder.build()


def build_nc_npc_add(npc_id: int, name: str, npc_type: str, level: str) -> bytes:
    """Build PLO_NC_NPCADD packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_NPCADD)
    builder.write_gint3(npc_id)
    builder.write_gchar(50)  # Name tag
    builder.write_gstring(name)
    builder.write_gchar(51)  # Type tag
    builder.write_gstring(npc_type)
    builder.write_gchar(52)  # Level tag
    builder.write_gstring(level)
    builder.write_newline()
    return builder.build()


def build_nc_npc_delete(npc_id: int) -> bytes:
    """Build PLO_NC_NPCDELETE packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_NPCDELETE)
    builder.write_gint3(npc_id)
    builder.write_newline()
    return builder.build()


def build_nc_npc_script(npc_id: int, script: str) -> bytes:
    """Build PLO_NC_NPCSCRIPT packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_NPCSCRIPT)
    builder.write_gint3(npc_id)
    builder.write_gstring_short(script)
    builder.write_newline()
    return builder.build()


def build_nc_npc_flags(npc_id: int, flags: str) -> bytes:
    """Build PLO_NC_NPCFLAGS packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_NPCFLAGS)
    builder.write_gint3(npc_id)
    builder.write_string(flags)
    builder.write_newline()
    return builder.build()


def build_nc_class_get(name: str, script: str) -> bytes:
    """Build PLO_NC_CLASSGET packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_CLASSGET)
    builder.write_gstring(name)
    builder.write_gstring_short(script)
    builder.write_newline()
    return builder.build()


def build_nc_class_add(name: str) -> bytes:
    """Build PLO_NC_CLASSADD packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_CLASSADD)
    builder.write_string(name)
    builder.write_newline()
    return builder.build()


def build_nc_class_delete(name: str) -> bytes:
    """Build PLO_NC_CLASSDELETE packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_CLASSDELETE)
    builder.write_string(name)
    builder.write_newline()
    return builder.build()


def build_nc_weapon_list(weapons: List[str]) -> bytes:
    """Build PLO_NC_WEAPONLISTGET packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_WEAPONLISTGET)
    for weapon in weapons:
        builder.write_gstring(weapon)
    builder.write_newline()
    return builder.build()


def build_nc_weapon_get(name: str, image: str, script: str) -> bytes:
    """Build PLO_NC_WEAPONGET packet."""
    builder = PacketBuilder().write_gchar(PLO.NC_WEAPONGET)
    builder.write_gstring(name)
    builder.write_gstring(image)
    builder.write_string(script)
    builder.write_newline()
    return builder.build()


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


def parse_npc_props(data: bytes) -> Tuple[int, dict]:
    """Parse PLI_NPCPROPS packet.

    Returns:
        Tuple of (npc_id, props_dict)
    """
    reader = PacketReader(data)
    npc_id = reader.read_gint3()
    props = {}

    while reader.has_data():
        prop_id = reader.read_gchar()
        if prop_id >= NPCPROP_COUNT:
            break

        if prop_id == NPCPROP.IMAGE:
            props[prop_id] = reader.read_gstring()
        elif prop_id == NPCPROP.SCRIPT:
            props[prop_id] = reader.read_gstring_short()
        elif prop_id in [NPCPROP.X, NPCPROP.Y]:
            props[prop_id] = reader.read_gchar() / 2.0
        elif prop_id in [NPCPROP.X2, NPCPROP.Y2]:
            raw = reader.read_gshort()
            pixels = raw >> 1
            if raw & 1:
                pixels = -pixels
            props[prop_id] = pixels / 16.0
        elif prop_id == NPCPROP.COLORS:
            props[prop_id] = [reader.read_byte() for _ in range(5)]
        elif prop_id == NPCPROP.ID:
            props[prop_id] = reader.read_gint3()
        elif prop_id == NPCPROP.RUPEES:
            props[prop_id] = reader.read_gint3()
        elif prop_id in [NPCPROP.MESSAGE, NPCPROP.NICKNAME, NPCPROP.GANI,
                         NPCPROP.SWORDIMAGE, NPCPROP.SHIELDIMAGE, NPCPROP.BODYIMAGE,
                         NPCPROP.HEADIMAGE, NPCPROP.HORSEIMAGE]:
            props[prop_id] = reader.read_gstring()
        else:
            props[prop_id] = reader.read_gchar()

    return npc_id, props


# =============================================================================
# Profile Parser
# =============================================================================

# The 9 free-text webpage-profile fields, in wire order (Player.cpp
# msgPLI_PROFILESET / ServerList.cpp msgSVI_PROFILE). Kept in sync with
# account.PROFILE_FIELDS.
PROFILE_FIELDS = ('name', 'age', 'gender', 'country', 'messenger',
                  'email', 'website', 'hangout', 'quote')


def parse_profile(data: bytes) -> dict:
    """Parse PLI_PROFILESET (81) payload.

    Format (Player.cpp msgPLI_PROFILESET): {GCHAR len}{account} then
    9 x {GCHAR len}{field}: name, age, gender, country, messenger, email,
    website, hangout, quote. The account name is a self-check - GServer
    rejects the whole packet if it doesn't match the sender's own account.

    Returns:
        Dict with 'account' plus any of PROFILE_FIELDS present in the packet.
    """
    reader = PacketReader(data)
    profile = {'account': reader.read_gstring()}
    for field in PROFILE_FIELDS:
        if reader.has_data():
            profile[field] = reader.read_gstring()
    return profile


def build_profile(account: str, profile: dict, online_time: str = '') -> bytes:
    """Build PLO_PROFILE (75) packet - reply to PLI_PROFILEGET.

    Format (ServerList.cpp msgSVI_PROFILE, modern client >= 2.1):
        {GSTRING account}{9 x GSTRING fields: name/age/gender/country/
        messenger/email/website/hangout/quote}{GSTRING online_time}
    The pre-2.1 kills/deaths/rating/alignment/rupees fallback format isn't
    implemented - this server targets modern (6.037) clients.
    """
    builder = PacketBuilder().write_gchar(PLO.PROFILE)
    builder.write_gstring(account)
    for field in PROFILE_FIELDS:
        builder.write_gstring(profile.get(field, ''))
    builder.write_gstring(online_time)
    builder.write_newline()
    return builder.build()


# Re-export NPCPROP_COUNT for parser
from .constants import NPCPROP_COUNT
