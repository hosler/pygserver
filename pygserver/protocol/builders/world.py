"""Packet construction helpers."""

import logging

from ..packet_codec import PacketBuilder
from ..constants import (
    PLO, NPCPROP, BDPROP
)
from ..constants import BDMODE, LevelItemType, NPCBLOCKFLAG, NPCVISFLAG, PLI, PLPERM, PLPROP, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..packet_codec import PacketReader  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import COLORS_NEWWORLD, NPC_PROPS, PLAYER_PROPS, StreamPolicy, encode_value, parse_prop_stream, preset_power_image, with_gif_fallback  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

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
    """Build a classic-script PLO_NPCWEAPONADD packet for v6.037."""
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONADD)
    builder.write_gstring(weapon_name)
    builder.write_gchar(0)  # NPCProp.IMAGE
    builder.write_gstring(image)
    builder.write_gchar(1)  # NPCProp.SCRIPT
    builder.write_gstring_short(script)
    builder.write_newline()
    return builder.build()


def build_npc_weapon_add_scripted(weapon_name: str, image: str,
                                  joined_classes: str = "") -> bytes:
    """Build the compiled-script PLO_NPCWEAPONADD packet for v6.037.

    Format (GServer-v2 Weapon.cpp registerWeaponWithPlayer, bytecode branch):
    the CLASS property replaces SCRIPT, and the bytecode itself follows later
    as PLO_LOADSCRIPT + the client's PLI_UPDATESCRIPT pull.
    """
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONADD)
    builder.write_gstring(weapon_name)
    builder.write_gchar(NPCPROP.IMAGE)
    builder.write_gstring(image)
    builder.write_gchar(NPCPROP.CLASS)
    builder.write_gstring_short(joined_classes)
    builder.write_newline()
    return builder.build()


def build_npc_weapon_del(weapon_name: str) -> bytes:
    """Build PLO_NPCWEAPONDEL packet."""
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONDEL)
    builder.write_gstring(weapon_name)
    builder.write_newline()
    return builder.build()


def build_npc_weapon_script(header: str, bytecode: bytes) -> bytes:
    """Build PLO_NPCWEAPONSCRIPT: [gshort header_len][header CSV][bytecode].

    Bytecode is binary and routinely contains 0x0a, so this packet must be
    announced with build_raw_data_announcement() - unframed it is truncated at
    the first newline.
    """
    builder = PacketBuilder().write_gchar(PLO.NPCWEAPONSCRIPT)
    builder.write_gstring_short(header)
    builder.write_bytes(bytecode)
    builder.write_newline()
    return builder.build()


def build_load_script_header(header: str) -> bytes:
    """Build the announcement form of PLO_LOADSCRIPT: a bare header CSV with no
    length prefix and no bytecode (Weapon.cpp registerWeaponWithPlayer)."""
    builder = PacketBuilder().write_gchar(PLO.LOADSCRIPT)
    builder.write_string(header)
    builder.write_newline()
    return builder.build()


def build_load_script_bytecode(header: str, bytecode: bytes) -> bytes:
    """Build the class-bytecode form of PLO_LOADSCRIPT:
    [gchar header_len][header CSV][bytecode] (ScriptClass.cpp getClassPacket).

    Like PLO_NPCWEAPONSCRIPT this must be sent inside PLO_RAWDATA framing.
    """
    builder = PacketBuilder().write_gchar(PLO.LOADSCRIPT)
    builder.write_gstring(header)
    builder.write_bytes(bytecode)
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


# PLO_SHOWIMGNPC is absent from the shared beta4 enum, but is packet 166 in
# the reference server's gs2lib IEnums.h. It is distinct from PLO_SHOWIMG (32),
# which carries player-originated images/legacy level chat.
PLO_SHOWIMGNPC = 166


def _write_showimg_prop(builder: PacketBuilder, prop_id: int, value) -> None:
    builder.write_gchar(prop_id)
    if prop_id == 0:  # image: GSTRING
        builder.write_gstring(str(value))
    elif prop_id in (1, 2, 3, 6, 8):  # x, y, layer, zoom, draw mode
        builder.write_gchar(int(value))
    elif prop_id == 4:  # enabled byte, then GSHORT x/y and GCHAR width/height
        x, y, width, height = value
        if width == 0 and height == 0:
            builder.write_gchar(0)
        else:
            builder.write_gchar(1).write_gshort(x).write_gshort(y)
            builder.write_gchar(width).write_gchar(height)
    elif prop_id == 5:  # RGBA, each 0..200
        for component in value:
            builder.write_gchar(component)
    elif prop_id == 7:  # z tile coordinate, biased by 50
        builder.write_gchar(max(-50, min(170, int(value))) + 50)


def build_npc_showimgs(npc_id: int, images: dict, *, reset: bool = False) -> bytes:
    """Build packet 166 with one or more indexed NPC showimg records."""
    builder = PacketBuilder().write_gchar(PLO_SHOWIMGNPC).write_gint3(npc_id)
    if reset:
        builder.write_gchar(9)
    for index, props in sorted(images.items()):
        builder.write_gchar(index + 10)
        for prop_id, value in sorted(props.items()):
            _write_showimg_prop(builder, prop_id, value)
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


def build_board_modify2(map_x: int, map_y: int, x: int, y: int,
                        width: int, height: int, tiles: bytes) -> bytes:
    """Build PLO_BOARDMODIFY2 packet for a gmap segment."""
    builder = PacketBuilder().write_gchar(PLO.BOARDMODIFY2)
    builder.write_gchar(map_x)
    builder.write_gchar(map_y)
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

