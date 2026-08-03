"""Byte-level regression tests for NC response layouts.

The two list packets an NC client asks for first look alike and are NOT
encoded alike, which is exactly how they drifted:

  * PLO_NC_WEAPONLISTGET is a run of length-prefixed names,
    `>> (char)weaponName.length() << weaponName`
    (GServer-v2 PlayerNCPackets.cpp:565).
  * PLO_NC_LEVELLIST is ONE gtokenized (comma-separated) list, built from
    `"name\\n"` per level and sent as `ret.gtokenize()`
    (GServer-v2 PlayerNCPackets.cpp:723).

Sending the level list in the weapon list's shape parses as a single row
with a stray length character welded to the front of every name, and
nothing errors - the F12 level tab just shows one unusable entry.
"""

from pygserver.protocol.builders.admin import (
    build_nc_level_list,
    build_nc_weapon_list,
)
from pygserver.protocol.constants import PLO


def _gchar(value):
    return bytes([value + 32])


def test_level_list_is_gtokenized_comma_text():
    packet = build_nc_level_list(["onlinestartlocal.nw", "chicken1.nw"])

    assert packet == (_gchar(PLO.NC_LEVELLIST)
                      + b"onlinestartlocal.nw,chicken1.nw" + b"\n")


def test_level_list_quotes_a_name_a_comma_would_split():
    """A '/' or ',' in a name has to be quoted, or the row count changes."""
    packet = build_nc_level_list(["world/chicken1.nw"])

    assert packet == (_gchar(PLO.NC_LEVELLIST)
                      + b'"world/chicken1.nw"' + b"\n")


def test_weapon_list_stays_length_prefixed():
    """The neighbouring packet keeps the OTHER encoding - do not unify them."""
    packet = build_nc_weapon_list(["-gr_movement", "Beer"])

    assert packet.startswith(_gchar(PLO.NC_WEAPONLISTGET))
    assert _gchar(len("-gr_movement")) + b"-gr_movement" in packet
    assert _gchar(len("Beer")) + b"Beer" in packet
