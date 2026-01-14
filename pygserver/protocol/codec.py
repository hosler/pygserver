"""
pygserver.protocol.codec - Server-side packet codec

This module re-exports core codec classes from the shared reborn_protocol library
and provides backwards compatibility for pygserver imports.
"""

# Re-export from shared library
from reborn_protocol.codec import (
    PacketReader,
    PacketBuilder,
    PacketBuffer,
    ServerCodec,
)

__all__ = [
    "PacketReader",
    "PacketBuilder",
    "PacketBuffer",
    "ServerCodec",
]
