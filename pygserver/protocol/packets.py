"""
pygserver.protocol.packets - Packet parsing and building utilities

Provides PacketReader for parsing incoming packets and PacketBuilder
for constructing outgoing packets using Reborn protocol encodings.

Based on GServer-v2 packet formats.
"""

from typing import Dict, Any, Optional, List, Tuple
from .constants import (
    PLO, PLI, PLPROP, NPCPROP, BDPROP, BDMODE, LevelItemType,
    PLSTATUS, PLPERM, NPCVISFLAG, NPCBLOCKFLAG
)


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
        return (b1 << 14) | (b2 << 7) | b3

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
                         PLPROP.STATUS, PLPROP.CARRYSPRITE]:
            if pos < len(data):
                props[prop_id] = data[pos] - 32
                pos += 1

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
                props[prop_id] = (b1 << 14) | (b2 << 7) | b3
                pos += 3

        # Text codepage (3 bytes gInt3)
        elif prop_id == PLPROP.TEXTCODEPAGE:
            if pos + 2 < len(data):
                b1 = data[pos] - 32
                b2 = data[pos + 1] - 32
                b3 = data[pos + 2] - 32
                props[prop_id] = (b1 << 14) | (b2 << 7) | b3
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
    """Build PLO_PLAYERPROPS packet with given properties."""
    builder = PacketBuilder().write_gchar(PLO.PLAYERPROPS)

    for prop_id, value in props.items():
        builder.write_gchar(prop_id)

        # String properties
        if prop_id in [PLPROP.NICKNAME, PLPROP.GANI, PLPROP.HEADIMAGE,
                       PLPROP.CURCHAT, PLPROP.CURLEVEL, PLPROP.BODYIMAGE,
                       PLPROP.ACCOUNTNAME]:
            builder.write_gstring(str(value))

        # GATTRIB strings
        elif PLPROP.GATTRIB1 <= prop_id <= PLPROP.GATTRIB30:
            builder.write_gstring(str(value))

        # Single byte
        elif prop_id in [PLPROP.MAXPOWER, PLPROP.CURPOWER, PLPROP.ARROWSCOUNT,
                         PLPROP.BOMBSCOUNT, PLPROP.GLOVEPOWER, PLPROP.BOMBPOWER,
                         PLPROP.SPRITE, PLPROP.DIRECTION, PLPROP.STATUS]:
            builder.write_gchar(int(value))

        # Low-precision position
        elif prop_id == PLPROP.X:
            builder.write_gchar(int(value * 2))
        elif prop_id == PLPROP.Y:
            builder.write_gchar(int(value * 2))

        # High-precision position
        elif prop_id == PLPROP.X2:
            builder.write_position2(float(value))
        elif prop_id == PLPROP.Y2:
            builder.write_position2(float(value))

        # Colors (5 bytes)
        elif prop_id == PLPROP.COLORS:
            for c in value[:5]:
                builder.write_gchar(int(c))

        # Rupees (gInt3)
        elif prop_id == PLPROP.RUPEESCOUNT:
            builder.write_gint3(int(value))

    builder.write_byte(ord('\n'))
    return builder.build()


def build_other_player_props(player_id: int, props: dict) -> bytes:
    """Build PLO_OTHERPLPROPS packet for another player."""
    builder = PacketBuilder().write_gchar(PLO.OTHERPLPROPS).write_gshort(player_id)

    for prop_id, value in props.items():
        builder.write_gchar(prop_id)

        # String properties
        if prop_id in [PLPROP.NICKNAME, PLPROP.GANI, PLPROP.HEADIMAGE,
                       PLPROP.CURCHAT, PLPROP.CURLEVEL, PLPROP.BODYIMAGE,
                       PLPROP.ACCOUNTNAME]:
            builder.write_gstring(str(value))

        # GATTRIB strings
        elif PLPROP.GATTRIB1 <= prop_id <= PLPROP.GATTRIB30:
            builder.write_gstring(str(value))

        # Single byte
        elif prop_id in [PLPROP.MAXPOWER, PLPROP.CURPOWER, PLPROP.ARROWSCOUNT,
                         PLPROP.BOMBSCOUNT, PLPROP.GLOVEPOWER, PLPROP.BOMBPOWER,
                         PLPROP.SPRITE, PLPROP.DIRECTION, PLPROP.STATUS]:
            builder.write_gchar(int(value))

        # Low-precision position
        elif prop_id == PLPROP.X:
            builder.write_gchar(int(value * 2))
        elif prop_id == PLPROP.Y:
            builder.write_gchar(int(value * 2))

        # High-precision position
        elif prop_id == PLPROP.X2:
            builder.write_position2(float(value))
        elif prop_id == PLPROP.Y2:
            builder.write_position2(float(value))

        # Colors (5 bytes)
        elif prop_id == PLPROP.COLORS:
            for c in value[:5]:
                builder.write_gchar(int(c))

    builder.write_byte(ord('\n'))
    return builder.build()


def build_npc_props(npc_id: int, props: dict) -> bytes:
    """Build PLO_NPCPROPS packet."""
    builder = PacketBuilder().write_gchar(PLO.NPCPROPS).write_gint3(npc_id)

    for prop_id, value in props.items():
        builder.write_gchar(prop_id)

        if prop_id == 0:  # Image
            builder.write_gstring(str(value))
        elif prop_id == 1:  # Script (gShort length)
            encoded = str(value).encode('latin-1', errors='replace')
            builder.write_gshort(len(encoded)).write_bytes(encoded)
        elif prop_id in [2, 3]:  # X, Y position
            builder.write_gchar(int(value * 2))
        elif prop_id == 5:  # Direction
            builder.write_gchar(int(value))
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


def build_warp2(x: float, y: float, level_name: str, gmap_x: int = 0, gmap_y: int = 0) -> bytes:
    """Build PLO_PLAYERWARP2 packet for GMAP warps."""
    builder = PacketBuilder().write_gchar(PLO.PLAYERWARP2)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gstring(level_name)
    # GMAP grid position
    builder.write_gchar(gmap_x)
    builder.write_gchar(gmap_y)
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
    """Build PLO_NEWWORLDTIME heartbeat packet."""
    import time
    # World time is seconds since midnight
    t = time.localtime()
    world_time = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    return PacketBuilder().write_gchar(PLO.NEWWORLDTIME).write_gint3(world_time).write_byte(ord('\n')).build()


def build_npc_del(npc_id: int) -> bytes:
    """Build PLO_NPCDEL packet."""
    return PacketBuilder().write_gchar(PLO.NPCDEL).write_gint3(npc_id).write_byte(ord('\n')).build()


def build_level_sign(x: float, y: float, text: str) -> bytes:
    """Build PLO_LEVELSIGN packet."""
    builder = PacketBuilder().write_gchar(PLO.LEVELSIGN)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_string(text)
    builder.write_byte(ord('\n'))
    return builder.build()


# =============================================================================
# Combat Packets
# =============================================================================

def build_bomb_add(player_id: int, x: float, y: float, power: int, time_left: int) -> bytes:
    """Build PLO_BOMBADD packet."""
    builder = PacketBuilder().write_gchar(PLO.BOMBADD)
    builder.write_gshort(player_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(power)
    builder.write_gchar(time_left)
    builder.write_newline()
    return builder.build()


def build_bomb_del(x: float, y: float) -> bytes:
    """Build PLO_BOMBDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.BOMBDEL)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_newline()
    return builder.build()


def build_arrow_add(player_id: int, x: float, y: float, direction: int) -> bytes:
    """Build PLO_ARROWADD packet."""
    builder = PacketBuilder().write_gchar(PLO.ARROWADD)
    builder.write_gshort(player_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(direction)
    builder.write_newline()
    return builder.build()


def build_explosion(x: float, y: float, radius: int, power: int) -> bytes:
    """Build PLO_EXPLOSION packet."""
    builder = PacketBuilder().write_gchar(PLO.EXPLOSION)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(radius)
    builder.write_gchar(power)
    builder.write_newline()
    return builder.build()


def build_hurt_player(player_id: int, power: int, from_x: float, from_y: float) -> bytes:
    """Build PLO_HURTPLAYER packet."""
    builder = PacketBuilder().write_gchar(PLO.HURTPLAYER)
    builder.write_gshort(player_id)
    builder.write_gchar(power)
    builder.write_gchar(int(from_x * 2))
    builder.write_gchar(int(from_y * 2))
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


def build_throw_carried(player_id: int, x: float, y: float, direction: int) -> bytes:
    """Build PLO_THROWCARRIED packet."""
    builder = PacketBuilder().write_gchar(PLO.THROWCARRIED)
    builder.write_gshort(player_id)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(direction)
    builder.write_newline()
    return builder.build()


def build_hit_objects(data: bytes) -> bytes:
    """Build PLO_HITOBJECTS packet."""
    builder = PacketBuilder().write_gchar(PLO.HITOBJECTS)
    builder.write_bytes(data)
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


def build_level_chest(x: int, y: int, item_type: int, sign_index: int) -> bytes:
    """Build PLO_LEVELCHEST packet."""
    builder = PacketBuilder().write_gchar(PLO.LEVELCHEST)
    builder.write_gchar(x)
    builder.write_gchar(y)
    builder.write_gchar(item_type)
    builder.write_gchar(sign_index)
    builder.write_newline()
    return builder.build()


# =============================================================================
# Horse Packets
# =============================================================================

def build_horse_add(x: float, y: float, direction: int, bushes: int, image: str) -> bytes:
    """Build PLO_HORSEADD packet."""
    builder = PacketBuilder().write_gchar(PLO.HORSEADD)
    builder.write_gchar(int(x * 2))
    builder.write_gchar(int(y * 2))
    builder.write_gchar(direction)
    builder.write_gchar(bushes)
    builder.write_gstring(image)
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


def build_baddy_hurt(baddy_id: int, power: int, from_x: float, from_y: float) -> bytes:
    """Build PLO_BADDYHURT packet."""
    builder = PacketBuilder().write_gchar(PLO.BADDYHURT)
    builder.write_gchar(baddy_id)
    builder.write_gchar(power)
    builder.write_gchar(int(from_x * 2))
    builder.write_gchar(int(from_y * 2))
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

def build_private_message(sender: str, message: str) -> bytes:
    """Build PLO_PRIVATEMESSAGE packet."""
    builder = PacketBuilder().write_gchar(PLO.PRIVATEMESSAGE)
    builder.write_gshort(len(sender))
    builder.write_string(sender)
    builder.write_string(message)
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


def build_trigger_action(x: float, y: float, action: str) -> bytes:
    """Build PLO_TRIGGERACTION packet."""
    builder = PacketBuilder().write_gchar(PLO.TRIGGERACTION)
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

def build_file(filename: str, data: bytes) -> bytes:
    """Build PLO_FILE packet."""
    builder = PacketBuilder().write_gchar(PLO.FILE)
    builder.write_gstring(filename)
    builder.write_bytes(data)
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


def build_is_leader(is_leader: bool) -> bytes:
    """Build PLO_ISLEADER packet (guild leader status)."""
    builder = PacketBuilder().write_gchar(PLO.ISLEADER)
    builder.write_gchar(1 if is_leader else 0)
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


def parse_trigger_action(data: bytes) -> Tuple[float, float, str, List[str]]:
    """Parse PLI_TRIGGERACTION packet.

    Returns:
        Tuple of (x, y, action, params)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    action_str = reader.remaining().decode('latin-1', errors='replace').strip()
    parts = action_str.split(',')
    action = parts[0] if parts else ''
    params = parts[1:] if len(parts) > 1 else []
    return x, y, action, params


def parse_bomb_add(data: bytes) -> Tuple[float, float, int]:
    """Parse PLI_BOMBADD packet.

    Returns:
        Tuple of (x, y, power)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    power = reader.read_gchar()
    return x, y, power


def parse_arrow_add(data: bytes) -> Tuple[float, float, int]:
    """Parse PLI_ARROWADD packet.

    Returns:
        Tuple of (x, y, direction)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    direction = reader.read_gchar()
    return x, y, direction


def parse_horse_add(data: bytes) -> Tuple[float, float, int, int, str]:
    """Parse PLI_HORSEADD packet.

    Returns:
        Tuple of (x, y, direction, bushes, image)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    direction = reader.read_gchar()
    bushes = reader.read_gchar()
    image = reader.read_gstring()
    return x, y, direction, bushes, image


def parse_item_take(data: bytes) -> Tuple[float, float]:
    """Parse PLI_ITEMTAKE packet.

    Returns:
        Tuple of (x, y)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    return x, y


def parse_hurt_player(data: bytes) -> Tuple[int, int, float, float]:
    """Parse PLI_HURTPLAYER packet.

    Returns:
        Tuple of (target_id, power, from_x, from_y)
    """
    reader = PacketReader(data)
    target_id = reader.read_gshort()
    power = reader.read_gchar()
    from_x = reader.read_gchar() / 2.0
    from_y = reader.read_gchar() / 2.0
    return target_id, power, from_x, from_y


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


def parse_open_chest(data: bytes) -> Tuple[float, float]:
    """Parse PLI_OPENCHEST packet.

    Returns:
        Tuple of (x, y)
    """
    reader = PacketReader(data)
    x = reader.read_gchar() / 2.0
    y = reader.read_gchar() / 2.0
    return x, y


def parse_private_message(data: bytes) -> Tuple[str, str]:
    """Parse PLI_PRIVATEMESSAGE packet.

    Returns:
        Tuple of (target_account, message)
    """
    reader = PacketReader(data)
    target_len = reader.read_gshort()
    target = reader.read_string(target_len)
    message = reader.remaining().decode('latin-1', errors='replace').strip()
    return target, message


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

def parse_profile(data: bytes) -> dict:
    """Parse profile data.

    Returns:
        Dict with profile fields
    """
    reader = PacketReader(data)
    profile = {}
    fields = ['age', 'gender', 'country', 'messenger', 'email', 'website', 'hangout', 'quote']
    for field in fields:
        if reader.has_data():
            profile[field] = reader.read_gstring()
    return profile


def build_profile(profile: dict) -> bytes:
    """Build PLO_PROFILE packet."""
    builder = PacketBuilder().write_gchar(PLO.PROFILE)
    fields = ['age', 'gender', 'country', 'messenger', 'email', 'website', 'hangout', 'quote']
    for field in fields:
        builder.write_gstring(profile.get(field, ''))
    builder.write_newline()
    return builder.build()


# Re-export NPCPROP_COUNT for parser
from .constants import NPCPROP_COUNT
