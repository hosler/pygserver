"""Dispatch table behind Player._handle_packets.

Every inbound PLI packet id is handled by exactly one method in one of the
mixins in this package, declared with `@handles(<PLI id>)`. The set of ids the
server understands is therefore exactly the set in those decorators:

    grep -rn '@handles' pygserver/handlers/

The methods stay methods (rather than module-level functions taking a player,
as in pyReborn's client-side handler package) because they are also the tested
surface: the suite calls `player._handle_baddy_hurt(payload)` directly.
"""

from typing import Callable, Dict

# Attribute the decorator stamps on a handler method.
_PLI_IDS_ATTR = '_pli_packet_ids'


def handles(*packet_ids) -> Callable:
    """Register the decorated method as the handler for `packet_ids`."""

    def decorate(fn: Callable) -> Callable:
        setattr(fn, _PLI_IDS_ATTR, tuple(int(pid) for pid in packet_ids))
        return fn

    return decorate


def collect_handler_names(cls: type) -> Dict[int, str]:
    """packet id -> method name, for every @handles method on `cls`'s MRO.

    Returns names, not bound methods, so the table can be built once per class
    and resolved per instance.
    """
    table: Dict[int, str] = {}
    for klass in reversed(cls.__mro__):
        for name, attr in vars(klass).items():
            ids = getattr(attr, _PLI_IDS_ATTR, None)
            if not ids:
                continue
            for packet_id in ids:
                existing = table.get(packet_id)
                if existing is not None and existing != name:
                    raise RuntimeError(
                        "PLI packet id %d is handled by both %s and %s"
                        % (packet_id, existing, name))
                table[packet_id] = name
    return table
