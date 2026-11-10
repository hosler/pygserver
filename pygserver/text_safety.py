"""Control-character filter for strings a game client supplies.

Packets are newline-terminated on the wire and several values (nicknames,
chat, image names) are relayed to other clients and written to account and
weapon files, so a client-supplied string must never carry a control
character: an embedded newline would be parsed as the start of another
packet or another file line. Property decoders are the single entry point
for those strings (movement.py for player props, entities.py for NPC props),
and both run their decoded values through here.
"""

import re

_CONTROL_CHARS = re.compile('[\x00-\x1f\x7f]')


def strip_control_chars(value: str) -> str:
    """Drop C0 control characters and DEL from `value`."""
    return _CONTROL_CHARS.sub('', value)


def has_control_chars(value: str) -> bool:
    return _CONTROL_CHARS.search(value) is not None


def sanitize_value(value):
    """Strip control characters from a decoded prop value.

    Strings are cleaned directly; tuples and lists (power/image pairs, colour
    arrays) are cleaned element-wise. Anything else passes through unchanged.
    """
    if isinstance(value, str):
        return strip_control_chars(value)
    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    return value
