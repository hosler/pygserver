"""Unit tests for ServerListClient dead-connection detection.

Mirrors an upstream GServer-v2 regression fix (ServerList::doTimedEvents):
SVI_PING from the list server is really a latency probe answered with
SVO_PING, so it never lets us originate keep-alive traffic, and a silently
dropped NAT/TCP session never surfaces via read() EOF either. These tests
cover the originated keep-alive ping and the "force reconnect if silent too
long" behavior.
"""

import asyncio
import sys
import os
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


def _make_client():
    """Build a ServerListClient with a real config (not a MagicMock chain)."""
    from pygserver.listserver import ServerListClient
    from pygserver.config import ServerConfig

    mock_server = MagicMock()
    mock_server.config = ServerConfig(serverip="1.2.3.4")

    client = ServerListClient(mock_server)
    client.connected = True
    return client


class TestKeepalivePing:
    """The originated keep-alive packet is well-formed SVO_SETIP."""

    def test_send_keepalive_ping_sends_svo_setip(self):
        from reborn_protocol import SVO
        from pygserver.protocol.packets import PacketReader

        client = _make_client()
        client._send_packet = AsyncMock()

        async def main():
            await client._send_keepalive_ping()
        asyncio.run(main())

        client._send_packet.assert_awaited_once()
        sent = client._send_packet.await_args.args[0]
        reader = PacketReader(sent)
        assert reader.read_gchar() == SVO.SETIP
        ip_len = reader.read_gchar()
        assert reader.read_bytes(ip_len) == b"1.2.3.4"

    def test_send_keepalive_ping_is_not_svo_ping(self):
        # SVO_PING is the reply to the listserver's own SVI_PING (used to
        # measure latency); it must not be reused as our originated ping.
        from reborn_protocol import SVO
        from pygserver.protocol.packets import PacketReader

        client = _make_client()
        client._send_packet = AsyncMock()

        async def main():
            await client._send_keepalive_ping()
        asyncio.run(main())

        sent = client._send_packet.await_args.args[0]
        reader = PacketReader(sent)
        assert reader.read_gchar() != SVO.PING


class TestCheckKeepalive:
    """_check_keepalive drives both the periodic ping and staleness detection."""

    def test_no_op_when_not_connected(self):
        client = _make_client()
        client.connected = False
        client._send_keepalive_ping = AsyncMock()
        client._disconnect = AsyncMock()
        client._schedule_reconnect = AsyncMock()

        async def main():
            await client._check_keepalive()
        asyncio.run(main())

        client._send_keepalive_ping.assert_not_awaited()
        client._disconnect.assert_not_awaited()

    def test_no_ping_before_interval_elapsed(self):
        import time
        from pygserver.listserver import PING_INTERVAL

        client = _make_client()
        client._send_keepalive_ping = AsyncMock()
        now = time.time()
        client.last_ping_time = now
        client.last_data_time = now

        async def main():
            await client._check_keepalive()
        asyncio.run(main())

        client._send_keepalive_ping.assert_not_awaited()

    def test_sends_ping_after_interval_elapsed(self):
        import time
        from pygserver.listserver import PING_INTERVAL

        client = _make_client()
        client._send_keepalive_ping = AsyncMock()
        now = time.time()
        client.last_ping_time = now - PING_INTERVAL - 1
        client.last_data_time = now

        async def main():
            await client._check_keepalive()
        asyncio.run(main())

        client._send_keepalive_ping.assert_awaited_once()
        # last_ping_time should have been refreshed so we don't spam pings
        assert client.last_ping_time > now - 1

    def test_forces_reconnect_when_data_stale(self):
        import time
        from pygserver.listserver import DEAD_CONNECTION_TIMEOUT

        client = _make_client()
        client._send_keepalive_ping = AsyncMock()
        client._disconnect = AsyncMock()
        client._schedule_reconnect = AsyncMock()

        now = time.time()
        client.last_ping_time = now
        client.last_data_time = now - DEAD_CONNECTION_TIMEOUT - 1

        async def main():
            await client._check_keepalive()
        asyncio.run(main())

        client._disconnect.assert_awaited_once()
        client._schedule_reconnect.assert_awaited_once()
        # A dead connection shouldn't also try to ping on the same pass
        client._send_keepalive_ping.assert_not_awaited()

    def test_no_reconnect_when_data_within_timeout(self):
        import time
        from pygserver.listserver import DEAD_CONNECTION_TIMEOUT

        client = _make_client()
        client._send_keepalive_ping = AsyncMock()
        client._disconnect = AsyncMock()
        client._schedule_reconnect = AsyncMock()

        now = time.time()
        client.last_ping_time = now
        client.last_data_time = now - (DEAD_CONNECTION_TIMEOUT / 2)

        async def main():
            await client._check_keepalive()
        asyncio.run(main())

        client._disconnect.assert_not_awaited()
        client._schedule_reconnect.assert_not_awaited()


class TestConnectResetsKeepaliveTimers:
    """A fresh connection shouldn't be immediately treated as stale."""

    def test_connect_resets_last_data_and_ping_time(self):
        import time
        from pygserver.listserver import ServerListClient
        from pygserver.config import ServerConfig

        mock_server = MagicMock()
        mock_server.config = ServerConfig(serverip="AUTO", localip="AUTO")

        client = ServerListClient(mock_server)
        # Simulate a long-stale client from a previous connection attempt.
        client.last_ping_time = 0.0
        client.last_data_time = 0.0

        fake_reader = AsyncMock()
        fake_writer = MagicMock()
        fake_writer.get_extra_info.return_value = None

        async def fake_open_connection(*args, **kwargs):
            return fake_reader, fake_writer

        client._send_registration = AsyncMock()

        before = time.time()

        async def main():
            import asyncio as _asyncio
            orig = _asyncio.open_connection
            _asyncio.open_connection = fake_open_connection
            try:
                await client._connect()
            finally:
                _asyncio.open_connection = orig
        asyncio.run(main())

        assert client.last_ping_time >= before
        assert client.last_data_time >= before
