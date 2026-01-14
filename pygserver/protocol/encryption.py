"""
pygserver.protocol.encryption - ENCRYPT_GEN_5 implementation

This module re-exports from the shared reborn_protocol library.
Import from here for backwards compatibility.
"""

# Re-export from shared library
from reborn_protocol.encryption import (
    CompressionType,
    RebornEncryption,
    compress_data,
    decompress_data,
)

__all__ = [
    "CompressionType",
    "RebornEncryption",
    "compress_data",
    "decompress_data",
]
