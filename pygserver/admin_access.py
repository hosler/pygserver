"""
pygserver.admin_access - who may open an RC or NC session

GServer-v2 server/src/player/Player.cpp:685-694 admits an RC, RC2 or NC login
only when the account is listed in the `staff` option AND the connecting
address matches one entry of the account's admin-IP range. Rights are not
consulted at login, and staff membership grants none: every privileged packet
checks the account's stored rights alone.
"""

import ipaddress
from typing import List, Optional


def peer_ip(raw: object) -> Optional[str]:
    """The socket peer address as text, IPv4-mapped IPv6 unwrapped to IPv4.

    Only the real socket peer is ever passed in; nothing the client sends.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def split_ip_range(text: object) -> List[str]:
    """FlatFileAccountLoader.cpp:506 and PlayerRCPackets.cpp:886: split on
    commas, drop empty entries, no trimming."""
    if not isinstance(text, str):
        return []
    return [entry for entry in text.split(",") if entry]


def wildcard_match(text: str, mask: str) -> bool:
    """Port of the reference `string::match` (server/include/utilities/StringUtils.h:1622).

    `*` matches any run, `?` one character, case-sensitive. Kept verbatim,
    quirks included: an empty text never matches, and a trailing `*` after a
    complete match fails (`127.0.0.1*` does not match `127.0.0.1`).
    """
    s = text + "\0"
    m = mask + "\0"
    i = j = 0
    last_star = None
    while s[i] != "\0":
        if m[j] == "*":
            j += 1
            if m[j] == "\0":
                return True
            last_star = j - 1
            while s[i] != "\0" and s[i] != m[j]:
                i += 1
        # The C++ would step past the terminator on `?` here; treat as a miss.
        if m[j] == s[i] or (m[j] == "?" and s[i] != "\0"):
            i += 1
            j += 1
        else:
            if last_star is None:
                return False
            j = last_star
            continue
        if s[i] == "\0" and m[j] == "\0":
            return True
        if s[i] == "\0" and m[j] != "\0" and m[j] != "*":
            return False
    return False


def is_admin_ip(ip: Optional[str], ip_range: object) -> bool:
    """Player.cpp:836-845: any range entry matching the peer; empty denies."""
    if not ip:
        return False
    return any(wildcard_match(ip, mask) for mask in split_ip_range(ip_range))
