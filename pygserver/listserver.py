"""
pygserver.listserver - List server client

Manages connection to the Graal list server for:
- Server registration and discovery
- Account verification
- Player list synchronization
- Cross-server messaging
"""

import asyncio
import logging
import time
import socket as sock
from typing import Optional, TYPE_CHECKING
from reborn_protocol import (
    SVI, SVO, PLO, PLPROP,
    PacketBuilder, PacketReader, Gen1Codec, Gen2Codec,
    CompressionType
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player

logger = logging.getLogger(__name__)

APP_VERSION = "pygserver-0.1.0"


class ServerListClient:
    """
    List server client for server registration and account verification.

    Handles connection to listserver.graal.in for server discovery,
    account verification, and cross-server features.
    """

    def __init__(self, server: 'GameServer'):
        self.server = server
        self.config = server.config

        # Connection state
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False

        # Reconnection backoff
        self.connection_attempts = 0
        self.next_connection_attempt = 0.0
        self.max_backoff = 300  # 5 minutes max

        # Codec for list server packets (Gen2: zlib compression, no encryption)
        self.codec = Gen2Codec()

        # Read buffer
        self.read_buffer = bytearray()

        # Task for main loop
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the list server client."""
        if not self.config.enable_listserver:
            logger.info("List server disabled in configuration")
            return

        logger.info("Starting list server client")
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """Stop the list server client."""
        logger.info("Stopping list server client")
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self._disconnect()

    async def _run(self):
        """Main loop for list server client."""
        while self._running:
            try:
                # Try to connect if not connected
                if not self.connected:
                    current_time = time.time()
                    if current_time >= self.next_connection_attempt:
                        await self._connect()

                # Process incoming packets if connected
                if self.connected:
                    await self._receive_packets()

                # Small delay to prevent busy loop
                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in list server loop: {e}", exc_info=True)
                await self._disconnect()
                await asyncio.sleep(1)

    async def _connect(self):
        """Connect to the list server."""
        try:
            logger.info(f"Connecting to list server {self.config.listip}:{self.config.listport}")

            # Open TCP connection
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.config.listip, self.config.listport),
                timeout=10.0
            )

            self.connected = True
            self.connection_attempts = 0

            # Get local and remote IPs
            sock_info = self.writer.get_extra_info('socket')
            local_ip = self.config.localip
            if local_ip == "AUTO" and sock_info:
                local_ip = sock_info.getsockname()[0]

            remote_ip = self.config.serverip
            if remote_ip == "AUTO" and sock_info:
                # Use the local IP we're connecting from
                local_ip = sock_info.getsockname()[0]
                # For external IP, use local IP as placeholder
                # (list server may update this via SetRemoteIp)
                remote_ip = local_ip

            logger.info(f"Connected to list server")

            # Send registration sequence
            await self._send_registration(local_ip, remote_ip)

        except Exception as e:
            logger.error(f"Failed to connect to list server: {e}")
            await self._schedule_reconnect()

    async def _disconnect(self):
        """Disconnect from list server."""
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except:
                pass

        self.reader = None
        self.writer = None
        self.connected = False
        self.read_buffer.clear()

    async def _schedule_reconnect(self):
        """Schedule reconnection with exponential backoff."""
        if self.connection_attempts < 8:
            self.connection_attempts += 1

        # Exponential backoff with max of 300s
        wait_time = min(2 ** self.connection_attempts, self.max_backoff)
        # Add jitter
        import random
        wait_time += random.randint(0, 5)

        self.next_connection_attempt = time.time() + wait_time
        logger.info(f"Will retry connection in {wait_time} seconds")

    async def _send_registration(self, local_ip: str, remote_ip: str):
        """Send initial registration packets to list server."""
        # Send REGISTERV3 with version using Gen1 codec (no compression, no encryption)
        gen1_codec = Gen1Codec()

        packet = PacketBuilder()
        packet.write_gchar(SVO.REGISTERV3)
        packet.write_string(APP_VERSION)
        await self._send_packet_raw(packet.build(), gen1_codec)

        # Switch to Gen2 codec for all subsequent packets (zlib compression, no encryption)
        self.codec = Gen2Codec()

        # Send HQ password
        packet = PacketBuilder()
        packet.write_gchar(SVO.SERVERHQPASS)
        packet.write_string(self.config.hq_password)
        await self._send_packet(packet.build())

        # Send NEWSERVER with full server info
        packet = PacketBuilder()
        packet.write_gchar(SVO.NEWSERVER)
        packet.write_gchar(len(self.config.name))
        packet.write_bytes(self.config.name.encode('latin1'))
        packet.write_gchar(len(self.config.description))
        packet.write_bytes(self.config.description.encode('latin1'))
        packet.write_gchar(len(self.config.language))
        packet.write_bytes(self.config.language.encode('latin1'))
        packet.write_gchar(len(APP_VERSION))
        packet.write_bytes(APP_VERSION.encode('latin1'))
        packet.write_gchar(len(self.config.url))
        packet.write_bytes(self.config.url.encode('latin1'))
        packet.write_gchar(len(remote_ip))
        packet.write_bytes(remote_ip.encode('latin1'))
        packet.write_gchar(len(str(self.config.port)))
        packet.write_bytes(str(self.config.port).encode('latin1'))
        packet.write_gchar(len(local_ip))
        packet.write_bytes(local_ip.encode('latin1'))
        await self._send_packet(packet.build())

        # Send HQ level
        packet = PacketBuilder()
        packet.write_gchar(SVO.SERVERHQLEVEL)
        packet.write_gchar(self.config.hq_level)
        await self._send_packet(packet.build())

        # Send version config (allowed versions) - must match C++ server format
        packet = PacketBuilder()
        packet.write_gchar(SVO.SENDTEXT)
        # Send comprehensive version list like C++ server
        versions = "GNW22122,GNW01940,GNW01113,GNW03014,GNW14015,GNW28015,G3D16053,G3D22067,G3D14097,G3D3007A,G3D2505C,G3D0311C,G3D0511C"
        packet.write_string(f"Listserver,settings,allowedversions,{versions}")
        await self._send_packet(packet.build())

        # Send player list
        await self.send_players()

    async def _send_packet(self, data: bytes):
        """Send a packet to the list server."""
        await self._send_packet_raw(data, self.codec)

    async def _send_packet_raw(self, data: bytes, codec):
        """Send a raw packet with specified codec (Gen1Codec or Gen2Codec)."""
        if not self.connected or not self.writer:
            return

        try:
            # Ensure packet ends with newline
            if not data.endswith(b'\n'):
                data += b'\n'

            # Debug: Log packet before encoding
            logger.debug(f"Sending packet (raw): {data.hex()} | ASCII: {data!r}")

            # Encode packet (send_packet includes length prefix)
            encoded = codec.send_packet(data)

            # Debug: Log packet after encoding
            logger.debug(f"Sending packet (encoded): {encoded.hex()} | Length: {len(encoded)} bytes")

            # Send the encoded packet (already has length prefix)
            self.writer.write(encoded)
            await self.writer.drain()

        except Exception as e:
            logger.error(f"Failed to send packet to list server: {e}")
            await self._disconnect()
            await self._schedule_reconnect()

    async def _receive_packets(self):
        """Receive and process packets from list server."""
        if not self.connected or not self.reader:
            return

        try:
            # Read data with longer timeout to allow for slow responses
            data = await asyncio.wait_for(self.reader.read(4096), timeout=0.5)

            if not data:
                # Connection closed
                logger.warning("List server closed connection")
                logger.debug(f"Read buffer at disconnect: {self.read_buffer.hex() if self.read_buffer else '(empty)'}")
                await self._disconnect()
                await self._schedule_reconnect()
                return

            # Debug: Log received data
            logger.debug(f"Received data: {data.hex()} | Length: {len(data)} bytes | ASCII: {data!r}")

            self.read_buffer.extend(data)

            # Process complete packets
            while len(self.read_buffer) >= 2:
                # Read packet length
                packet_len = int.from_bytes(self.read_buffer[:2], 'big')

                logger.debug(f"Packet length from header: {packet_len}, buffer size: {len(self.read_buffer)}")

                # Check if we have the full packet
                if len(self.read_buffer) < packet_len + 2:
                    break

                # Extract packet data (without length prefix)
                packet_data = bytes(self.read_buffer[2:packet_len + 2])
                self.read_buffer = self.read_buffer[packet_len + 2:]

                logger.debug(f"Processing packet (encoded): {packet_data.hex()}")

                # Decode packet (recv_packet expects data without length prefix)
                decoded = self.codec.recv_packet(packet_data)

                decoded_hex = decoded.hex() if decoded else '(failed)'
                decoded_repr = repr(decoded) if decoded else '(failed)'
                logger.debug(f"Decoded packet: {decoded_hex} | ASCII: {decoded_repr}")

                # Process packet
                if decoded:
                    await self._handle_packet(decoded)

        except asyncio.TimeoutError:
            # No data available, that's fine
            pass
        except Exception as e:
            logger.error(f"Error receiving packets: {e}", exc_info=True)
            await self._disconnect()
            await self._schedule_reconnect()

    async def _handle_packet(self, data: bytes):
        """Handle an incoming packet from list server."""
        reader = PacketReader(data)

        while reader.has_data():
            # Read packet ID
            packet_id = reader.read_gchar()
            logger.debug(f"Processing packet ID: {packet_id} (SVI enum)")

            try:
                if packet_id == SVI.PING:
                    await self._handle_ping()
                elif packet_id == SVI.ERRMSG:
                    await self._handle_error(reader)
                elif packet_id == SVI.VERIACC2:
                    await self._handle_verify_account(reader)
                elif packet_id == SVI.SENDTEXT:
                    await self._handle_send_text(reader)
                elif packet_id == SVI.VERSIONOLD:
                    logger.warning("List server reports this server version is old")
                elif packet_id == SVI.VERSIONCURRENT:
                    # Server version is current, no message needed
                    pass
                else:
                    logger.debug(f"Unhandled list server packet: {packet_id}")

            except Exception as e:
                logger.error(f"Error handling packet {packet_id}: {e}", exc_info=True)

    async def _handle_ping(self):
        """Respond to list server ping."""
        packet = PacketBuilder()
        packet.write_gchar(SVO.PING)
        await self._send_packet(packet.build())

    async def _handle_error(self, reader: PacketReader):
        """Handle error message from list server."""
        remaining = reader.remaining()
        message = remaining.decode('latin1', errors='ignore').strip()
        logger.error(f"List server error: {message}")

    async def _handle_verify_account(self, reader: PacketReader):
        """Handle account verification response."""
        account_len = reader.read_gchar()
        account = reader.read_bytes(account_len).decode('latin1')
        player_id = reader.read_gshort()
        player_type = reader.read_gchar()
        message = reader.read_string(reader.bytes_left()).decode('latin1')

        # Find the player
        player = self.server.players.get(player_id)
        if not player:
            return

        # Set account name from list server
        player.account_name = account

        if message != "SUCCESS":
            # Login failed
            logger.info(f"Login failed for {account}: {message}")
            # Send disconnect message
            packet = PacketBuilder()
            packet.write_gchar(PLO.DISCMESSAGE)
            packet.write_string(message)
            await player.send_packet(packet.build())
            player.disconnect()
        else:
            # Login successful
            logger.info(f"Login verified for {account}")
            # Send login data to player
            await player.send_login()

    async def _handle_send_text(self, reader: PacketReader):
        """Handle text/command from list server."""
        remaining = reader.remaining()
        text = remaining.decode('latin1', errors='ignore').strip()
        logger.debug(f"List server text: {text}")

        # Parse comma-separated tokens
        parts = text.split(',')
        if len(parts) < 2:
            return

        if parts[0] == "Listserver" and len(parts) >= 3:
            if parts[1] == "SetRemoteIp":
                remote_ip = parts[2]
                logger.info(f"List server identified remote IP as: {remote_ip}")
                # Store for future use
                self.server.remote_ip = remote_ip

    async def send_players(self):
        """Send current player list to list server."""
        if not self.connected:
            return

        # Clear player list
        packet = PacketBuilder()
        packet.write_gchar(SVO.SETPLYR)
        await self._send_packet(packet.build())

        # Add each player
        for player in self.server.players.values():
            if player.loaded:
                await self.add_player(player)

    async def add_player(self, player: 'Player'):
        """Add a player to the list server."""
        if not self.connected:
            return

        packet = PacketBuilder()
        packet.write_gchar(SVO.PLYRADD)
        packet.write_gshort(player.id)
        packet.write_gchar(player.type)

        # Add player properties
        packet.write_gchar(PLPROP.ACCOUNTNAME)
        packet.write_gchar(len(player.account_name))
        packet.write_bytes(player.account_name.encode('latin1'))

        packet.write_gchar(PLPROP.NICKNAME)
        packet.write_gchar(len(player.nickname))
        packet.write_bytes(player.nickname.encode('latin1'))

        packet.write_gchar(PLPROP.CURLEVEL)
        level_name = player.level.name if player.level else ""
        packet.write_gchar(len(level_name))
        packet.write_bytes(level_name.encode('latin1'))

        packet.write_gchar(PLPROP.X)
        packet.write_gchar(int(player.x * 2))

        packet.write_gchar(PLPROP.Y)
        packet.write_gchar(int(player.y * 2))

        packet.write_gchar(PLPROP.ALIGNMENT)
        packet.write_gchar(player.ap)

        # IP address (don't send actual IP for privacy)
        packet.write_gchar(PLPROP.IPADDR)
        packet.write_gchar(0)  # Empty IP

        await self._send_packet(packet.build())

        # Notify player that list server is connected
        notify_packet = PacketBuilder()
        notify_packet.write_gchar(PLO.SERVERLISTCONNECTED)
        await player.send_packet(notify_packet.build())

    async def remove_player(self, player: 'Player'):
        """Remove a player from the list server."""
        if not self.connected:
            return

        packet = PacketBuilder()
        packet.write_gchar(SVO.PLYRREM)
        packet.write_gshort(player.id)
        await self._send_packet(packet.build())

    async def verify_account(self, player: 'Player', password: str, identity: str = ""):
        """Request account verification from list server."""
        if not self.connected:
            # If not connected to list server, just send login directly
            await player.send_login()
            return

        packet = PacketBuilder()
        packet.write_gchar(SVO.VERIACC2)
        packet.write_gchar(len(player.account_name))
        packet.write_bytes(player.account_name.encode('latin1'))
        packet.write_gchar(len(password))
        packet.write_bytes(password.encode('latin1'))
        packet.write_gshort(player.id)
        packet.write_gchar(player.type)
        packet.write_gshort(len(identity))
        packet.write_bytes(identity.encode('latin1'))
        await self._send_packet(packet.build())
