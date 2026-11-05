"""
pygserver.player_session - the socket/codec half of a player connection.

Everything that knows about bytes on the wire lives here: the asyncio stream
pair, the ServerCodec (whose key is set from the login packet), the inbound
packet-framing buffer and the connected flag. Player delegates its send_raw /
send_packet / disconnect to this and keeps no transport state of its own.
"""

import asyncio
import logging
import time
from typing import Optional, Union

from reborn_protocol.codec import Gen2Codec

from .protocol.codec import ServerCodec, PacketBuffer

logger = logging.getLogger(__name__)


class PlayerSession:
    """Transport for one connected client."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.connected = True
        self.last_data_time = time.time()
        self.codec: Optional[Union[ServerCodec, Gen2Codec]] = None
        self.buffer = PacketBuffer()

    async def read(self, limit: int = 65536, timeout: Optional[float] = None) -> bytes:
        """Read raw bytes from the socket. Raises asyncio.TimeoutError."""
        if timeout is None:
            return await self.reader.read(limit)
        return await asyncio.wait_for(self.reader.read(limit), timeout=timeout)

    def start_codec(self, key: int = 0, generation: int = 5):
        """Create the codec selected by the connection's login generation."""
        if generation == 2:
            self.codec = Gen2Codec()
        elif generation == 5:
            self.codec = ServerCodec(key)
        else:
            raise ValueError(f"Unsupported encryption generation: {generation}")
        return self.codec

    def decode_packet(self, data: bytes) -> Optional[bytes]:
        """Decode one framed payload with the selected session codec."""
        if not self.codec:
            return None
        if isinstance(self.codec, Gen2Codec):
            return self.codec.recv_packet(data)
        return self.codec.decode_packet(data)

    async def send(self, data: bytes):
        """Encode and send one packet. Marks the session dead on a send error."""
        if not self.connected or not self.codec:
            return
        try:
            encoded = (self.codec.send_packet(data)
                       if isinstance(self.codec, Gen2Codec)
                       else self.codec.encode_packet(data))
            await self._write(encoded)
        except Exception as e:
            logger.error(f"Send error: {e}")
            self.connected = False

    async def send_login_response(self, data: bytes):
        """Send the one packet that is encoded as a login response.

        Unlike send(), a failure here propagates: the login flow treats it as a
        failed login rather than continuing with a half-initialised player.
        """
        encoded = (self.codec.send_packet(data)
                   if isinstance(self.codec, Gen2Codec)
                   else self.codec.encode_packet(data, is_login_response=True))
        await self._write(encoded)

    async def _write(self, encoded: bytes):
        """Record activity only after the encoded data is sent successfully."""
        self.writer.write(encoded)
        await self.writer.drain()
        self.last_data_time = time.time()

    async def close(self):
        """Close the socket, ignoring an already-broken connection."""
        self.connected = False
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass
