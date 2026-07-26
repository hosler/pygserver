"""Profile packet helpers."""

import logging

from ..packet_codec import PacketBuilder, PacketReader
from ..constants import (
    PLO
)
from ..constants import BDMODE, BDPROP, LevelItemType, NPCBLOCKFLAG, NPCPROP, NPCVISFLAG, PLI, PLPERM, PLPROP, PLSTATUS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.props import COLORS_NEWWORLD, NPC_PROPS, PLAYER_PROPS, StreamPolicy, encode_value, parse_prop_stream, preset_power_image, with_gif_fallback  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

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
