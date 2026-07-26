"""Packet construction helpers."""

import logging
from typing import List, Tuple

from ..packet_codec import PacketBuilder
from ..constants import (
    PLO
)
from ..constants import BDMODE, BDPROP, LevelItemType, NPCBLOCKFLAG, NPCPROP, NPCVISFLAG, PLI, PLPERM, PLPROP, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..packet_codec import PacketReader  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import COLORS_NEWWORLD, NPC_PROPS, PLAYER_PROPS, StreamPolicy, encode_value, parse_prop_stream, preset_power_image, with_gif_fallback  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, Optional  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

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


