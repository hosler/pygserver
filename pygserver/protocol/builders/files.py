"""Packet construction helpers."""

import logging

from ..packet_codec import PacketBuilder
from ..constants import (
    PLO
)
from ..constants import BDMODE, BDPROP, LevelItemType, NPCBLOCKFLAG, NPCPROP, NPCVISFLAG, PLI, PLPERM, PLPROP, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..packet_codec import PacketReader  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import COLORS_NEWWORLD, NPC_PROPS, PLAYER_PROPS, StreamPolicy, encode_value, parse_prop_stream, preset_power_image, with_gif_fallback  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

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


def build_gani_script(gani_name: str, bytecode: bytes) -> bytes:
    """Build PLO_GANISCRIPT: [gchar name_len][gani name][bytecode]
    (GameAni.cpp getBytecodePacket; the name carries no .gani suffix).

    The payload is compiled GS2, not gani text, and like PLO_NPCWEAPONSCRIPT
    it must be announced with build_raw_data_announcement().
    """
    builder = PacketBuilder().write_gchar(PLO.GANISCRIPT)
    builder.write_gstring(gani_name)
    builder.write_bytes(bytecode)
    builder.write_newline()
    return builder.build()


def build_load_gani(gani_name: str, setbackto: str = "") -> bytes:
    """Build PLO_LOADGANI: [gchar name_len][gani name]["SETBACKTO <ani>"]
    (PlayerClientPackets.cpp msgPLI_UPDATEGANI)."""
    builder = PacketBuilder().write_gchar(PLO.LOADGANI)
    builder.write_gstring(gani_name)
    builder.write_string(f'"SETBACKTO {setbackto}"')
    builder.write_newline()
    return builder.build()


