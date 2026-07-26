"""Packet construction helpers."""

import logging
from typing import Optional

from ..packet_codec import PacketBuilder
from ..constants import (
    PLO
)
from ..constants import BDMODE, BDPROP, LevelItemType, NPCBLOCKFLAG, NPCPROP, NPCVISFLAG, PLI, PLPERM, PLPROP, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..packet_codec import PacketReader  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import COLORS_NEWWORLD, NPC_PROPS, PLAYER_PROPS, StreamPolicy, encode_value, parse_prop_stream, preset_power_image, with_gif_fallback  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, List, Tuple  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

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

