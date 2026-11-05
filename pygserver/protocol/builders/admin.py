"""Packet construction helpers."""

import logging
from typing import Any, List, Mapping, Sequence

from ...server_flags import serialize_flag
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


def build_rc_server_flags(flags: Mapping[str, Any]) -> bytes:
    """Build PLO_RC_SERVERFLAGSGET with counted serialized flag entries."""
    entries = [serialize_flag(name, value) for name, value in flags.items()]
    entries = [entry for entry in entries if entry is not None]
    builder = PacketBuilder().write_gchar(PLO.RC_SERVERFLAGSGET)
    builder.write_gshort(len(entries))
    for entry in entries:
        builder.write_gstring(entry)
    builder.write_newline()
    return builder.build()


def build_rc_player_props(player_id: int, account: str, props: bytes,
                          flags: Mapping[str, Any] = None,
                          chests: Sequence[Any] = (),
                          weapons: Sequence[str] = ()) -> bytes:
    """Build PLO_RC_PLAYERPROPSGET in ``getPropsForRCPacket`` layout."""
    builder = PacketBuilder().write_gchar(PLO.RC_PLAYERPROPSGET)
    builder.write_gshort(player_id)
    builder.write_gstring(account)
    builder.write_gstring("main")
    builder.write_gchar(len(props)).write_bytes(props)

    flags = flags or {}
    builder.write_gshort(len(flags))
    for name, value in flags.items():
        builder.write_gstring(f"{name}={value}"[:223])

    builder.write_gshort(len(chests))
    for chest in chests:
        if isinstance(chest, Mapping):
            level, x, y = chest["level"], chest["x"], chest["y"]
        else:
            level, x, y = chest
        encoded_level = level.encode("latin-1")
        builder.write_gchar(len(encoded_level) + 2)
        builder.write_gchar(x).write_gchar(y).write_bytes(encoded_level)

    builder.write_gchar(len(weapons))
    for weapon in weapons:
        builder.write_gstring(weapon)
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


def build_rc_player_ban(account: str, banned: bool, reason: str) -> bytes:
    """Build PLO_RC_PLAYERBANGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_PLAYERBANGET)
    builder.write_gstring(account)
    builder.write_gchar(1 if banned else 0)
    builder.write_string(reason)
    builder.write_newline()
    return builder.build()


def build_rc_account_list(accounts: List[str]) -> bytes:
    """Build PLO_RC_ACCOUNTLISTGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_ACCOUNTLISTGET)
    for acc in accounts:
        builder.write_gstring(acc)
    builder.write_newline()
    return builder.build()


def build_rc_account_get(account: str, email: str = "", banned: bool = False,
                         load_only: bool = False, ban_length: str = "",
                         ban_reason: str = "") -> bytes:
    """Build PLO_RC_ACCOUNTGET packet."""
    builder = PacketBuilder().write_gchar(PLO.RC_ACCOUNTGET)
    builder.write_gstring(account)
    builder.write_gstring("")
    builder.write_gstring(email)
    builder.write_gchar(1 if banned else 0)
    builder.write_gchar(1 if load_only else 0)
    builder.write_gchar(0)
    builder.write_gstring("main")
    builder.write_gstring(ban_length)
    builder.write_gstring(ban_reason)
    builder.write_newline()
    return builder.build()


def build_rc_file_browser_dir_list(folders: Sequence[str]) -> bytes:
    """Build PLO_RC_FILEBROWSER_DIRLIST with a force-quoted CSV payload."""
    payload = ','.join(
        '"' + folder.replace('"', '""') + '"'
        for folder in folders
    )
    return (PacketBuilder().write_gchar(PLO.RC_FILEBROWSER_DIRLIST)
            .write_string(payload).write_newline().build())


def build_rc_file_browser_dir(path: str, files: Sequence[Any],
                              rights: str = "rw") -> bytes:
    """Build PLO_RC_FILEBROWSER_DIR with length-prefixed file blocks.

    Args:
        path: Current directory path
        files: FileInfo objects or (filename, size, modtime) tuples
        rights: Rights string attached to each regular file
    """
    builder = PacketBuilder().write_gchar(PLO.RC_FILEBROWSER_DIR)
    builder.write_gstring(path)
    for file_info in files:
        if hasattr(file_info, "name"):
            filename = file_info.name
            size = file_info.size
            modtime = int(file_info.modified_time)
            is_directory = file_info.is_directory
        else:
            filename, size, modtime = file_info
            is_directory = False

        # The reference wildcard listing omits directories.  Keep them usable
        # in this hierarchical browser by marking their names with a trailing
        # slash and encoding them as zero-sized, read-only file blocks.
        entry_rights = "r" if is_directory else rights
        if is_directory and not filename.endswith("/"):
            filename += "/"
        entry = PacketBuilder().write_gstring(filename)
        entry.write_gstring(entry_rights).write_gint5(size).write_gint5(modtime)
        entry_data = entry.build()
        builder.write_string(" ").write_gchar(len(entry_data))
        builder.write_string(entry_data.decode("latin-1"))
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
    """Build PLO_NC_LEVELLIST packet.

    The body is ONE gtokenized (comma-separated) list, not a sequence of
    length-prefixed strings: the reference builds `"name\\n"` per level and
    sends `ret.gtokenize()` (GServer-v2 PlayerNCPackets.cpp:723). The weapon
    list two packets over IS length-prefixed (`>> (char)len << name`,
    PlayerNCPackets.cpp:565), which is what this builder used to copy - so a
    client that decodes the reference form read the whole list as one row
    with the length bytes stuck to the front of each name.
    """
    from ...irc import gtokenize      # local: irc is above the protocol layer

    builder = PacketBuilder().write_gchar(PLO.NC_LEVELLIST)
    builder.write_string(gtokenize(levels))
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
