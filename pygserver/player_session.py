"""
pygserver.player_session - the socket/codec half of a player connection.

Everything that knows about bytes on the wire lives here: the asyncio stream
pair, the ServerCodec (whose key is set from the login packet), the inbound
packet-framing buffer and the connected flag. Player delegates its send_raw /
send_packet / disconnect to this and keeps no transport state of its own.
"""

import asyncio
import logging
from typing import Optional

from .protocol.codec import ServerCodec, PacketBuffer

logger = logging.getLogger(__name__)


class PlayerSession:
    """Transport for one connected client."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.connected = True
        self.codec: Optional[ServerCodec] = None
        self.buffer = PacketBuffer()

    async def read(self, limit: int = 65536, timeout: Optional[float] = None) -> bytes:
        """Read raw bytes from the socket. Raises asyncio.TimeoutError."""
        if timeout is None:
            return await self.reader.read(limit)
        return await asyncio.wait_for(self.reader.read(limit), timeout=timeout)

    def start_codec(self, key: int = 0) -> ServerCodec:
        """Create the session codec (the login packet arrives with key 0)."""
        self.codec = ServerCodec(key)
        return self.codec

    async def send(self, data: bytes):
        """Encode and send one packet. Marks the session dead on a send error."""
        if not self.connected or not self.codec:
            return
        try:
            encoded = self.codec.encode_packet(data)
            self.writer.write(encoded)
            await self.writer.drain()
        except Exception as e:
            logger.error(f"Send error: {e}")
            self.connected = False

    async def send_login_response(self, data: bytes):
        """Send the one packet that is encoded as a login response.

        Unlike send(), a failure here propagates: the login flow treats it as a
        failed login rather than continuing with a half-initialised player.
        """
        encoded = self.codec.encode_packet(data, is_login_response=True)
        self.writer.write(encoded)
        await self.writer.drain()

    async def close(self):
        """Close the socket, ignoring an already-broken connection."""
        self.connected = False
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass
