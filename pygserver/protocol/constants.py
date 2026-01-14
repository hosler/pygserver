"""
pygserver.protocol.constants - Packet IDs and property constants

This module re-exports from the shared reborn_protocol library.
Import from here for backwards compatibility.
"""

# Re-export all constants from shared library
from reborn_protocol.constants import (
    PLI,
    PLO,
    PLPROP,
    NPCPROP,
    BDPROP,
    BDMODE,
    LevelItemType,
    PLTYPE,
    PLSTATUS,
    PLFLAG,
    PLPERM,
    NPCVISFLAG,
    NPCBLOCKFLAG,
    PLPROP_COUNT,
    NPCPROP_COUNT,
    BDPROP_COUNT,
    BDMODE_COUNT,
)

__all__ = [
    "PLI",
    "PLO",
    "PLPROP",
    "NPCPROP",
    "BDPROP",
    "BDMODE",
    "LevelItemType",
    "PLTYPE",
    "PLSTATUS",
    "PLFLAG",
    "PLPERM",
    "NPCVISFLAG",
    "NPCBLOCKFLAG",
    "PLPROP_COUNT",
    "NPCPROP_COUNT",
    "BDPROP_COUNT",
    "BDMODE_COUNT",
]


# Note: PLPROP.HEADIMAGE = 11 (HEADGIF in GServer), PLPROP.BODYIMAGE = 35
# PLPROP.SPRITE (17) contains direction in lower 2 bits
