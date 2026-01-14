"""
pygserver.protocol - Protocol layer for Reborn v6.037

Handles encryption, compression, and packet framing.
Uses the shared reborn_protocol library for core protocol components.
"""

# Re-export from shared library via local modules
from .encryption import RebornEncryption, CompressionType
from .codec import ServerCodec, PacketBuffer, PacketReader, PacketBuilder
from .constants import PLI, PLO, PLPROP, NPCPROP, PLTYPE, PLSTATUS, PLFLAG, PLPERM

# Import packet building/parsing functions (pygserver-specific implementations)
from .packets import (
    # Parsing functions
    parse_login_packet,
    parse_player_props,
    parse_level_warp,
    parse_trigger_action,
    parse_hurt_player,
    parse_npc_props,
    # Building functions
    build_player_props,
    build_other_player_props,
    build_npc_props,
    build_level_name,
    build_level_link,
    build_board_packet,
    build_raw_data_announcement,
    build_chat,
    build_warp,
    build_warp2,
    build_private_message,
    build_player_left,
    build_disc_message,
    build_flag_set,
    build_flag_del,
    build_explosion,
    build_bomb_add,
    build_bomb_del,
    build_arrow_add,
    build_item_add,
    build_item_del,
    build_horse_add,
    build_horse_del,
    build_baddy_props,
    build_baddy_hurt,
    build_hurt_player,
    build_level_chest,
    build_level_sign,
    build_trigger_action,
    build_npc_weapon_add,
    build_npc_del,
    build_npc_moved,
    build_world_time,
    build_show_img,
    build_hit_objects,
)

__all__ = [
    # Encryption
    "RebornEncryption",
    "CompressionType",
    # Codec
    "ServerCodec",
    "PacketBuffer",
    "PacketReader",
    "PacketBuilder",
    # Constants
    "PLI",
    "PLO",
    "PLPROP",
    "NPCPROP",
    "PLTYPE",
    "PLSTATUS",
    "PLFLAG",
    "PLPERM",
    # Packet parsing
    "parse_login_packet",
    "parse_player_props",
    "parse_level_warp",
    "parse_trigger_action",
    "parse_hurt_player",
    "parse_npc_props",
    # Packet building
    "build_player_props",
    "build_other_player_props",
    "build_npc_props",
    "build_level_name",
    "build_level_link",
    "build_board_packet",
    "build_raw_data_announcement",
    "build_chat",
    "build_warp",
    "build_warp2",
    "build_private_message",
    "build_player_left",
    "build_disc_message",
    "build_flag_set",
    "build_flag_del",
    "build_explosion",
    "build_bomb_add",
    "build_bomb_del",
    "build_arrow_add",
    "build_item_add",
    "build_item_del",
    "build_horse_add",
    "build_horse_del",
    "build_baddy_props",
    "build_baddy_hurt",
    "build_hurt_player",
    "build_level_chest",
    "build_level_sign",
    "build_trigger_action",
    "build_npc_weapon_add",
    "build_npc_del",
    "build_npc_moved",
    "build_world_time",
    "build_show_img",
    "build_hit_objects",
]
