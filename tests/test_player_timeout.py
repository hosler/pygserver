"""Connection activity and transport closure without real sockets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import pygserver.player as player_module
import pygserver.player_session as session_module
from pygserver.player import Player


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    fake_time = SimpleNamespace(time=lambda: clock.now)
    monkeypatch.setattr(player_module, "time", fake_time)
    monkeypatch.setattr(session_module, "time", fake_time)
    return clock


def make_player():
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    player = Player(SimpleNamespace(), 1, AsyncMock(), writer)
    player._handle_login = AsyncMock(return_value=True)
    player.session.start_codec()
    return player, writer


def test_recent_sends_keep_receive_timeouts_connected(clock):
    player, writer = make_player()
    calls = 0

    async def read(*, timeout):
        nonlocal calls
        calls += 1
        assert timeout == 30.0
        assert player.connected
        writer.close.assert_not_called()
        if calls == 4:
            return b""
        clock.now += 250.0
        await player.send_raw(b"heartbeat\n")
        clock.now += 100.0
        raise asyncio.TimeoutError

    player.session.read = AsyncMock(side_effect=read)
    asyncio.run(player.run())

    assert calls == 4
    assert writer.write.call_count == 3
    assert player.last_packet_time == 0.0
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


def test_no_activity_timeout_closes_transport(clock):
    player, writer = make_player()
    calls = 0

    async def read(*, timeout):
        nonlocal calls
        calls += 1
        # Exactly 300 seconds is allowed; more than 300 ends the loop.
        clock.now = 1300.0 if calls == 1 else 1300.1
        raise asyncio.TimeoutError

    player.session.read = AsyncMock(side_effect=read)
    asyncio.run(player.run())

    assert calls == 2
    assert not player.connected
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


def test_cleanup_failure_still_closes_transport(clock):
    player, writer = make_player()
    player.session.read = AsyncMock(return_value=b"")
    player._cleanup = AsyncMock(side_effect=RuntimeError("cleanup failed"))

    with pytest.raises(RuntimeError, match="cleanup failed"):
        asyncio.run(player.run())

    player._cleanup.assert_awaited_once()
    assert not player.connected
    writer.close.assert_called_once()
    writer.wait_closed.assert_awaited_once()


def test_receive_refreshes_both_activity_times(clock):
    player, writer = make_player()
    calls = 0

    async def read(*, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.now = 1250.0
            return b"packet"
        if calls == 2:
            clock.now = 1400.0
            raise asyncio.TimeoutError
        return b""

    player.session.read = AsyncMock(side_effect=read)
    player._process_data = AsyncMock()
    asyncio.run(player.run())

    assert calls == 3
    assert player.last_data_time == player.last_packet_time == 1250.0
    player._process_data.assert_awaited_once_with(b"packet")
    writer.close.assert_called_once()


@pytest.mark.parametrize("method", ["send_raw", "send_login_response"])
def test_successful_send_refreshes_activity(clock, method):
    player, writer = make_player()
    clock.now = 1200.0
    sender = player if method == "send_raw" else player.session

    asyncio.run(getattr(sender, method)(b"heartbeat\n"))

    assert player.last_data_time == 1200.0
    assert player.last_packet_time == 0.0
    writer.drain.assert_awaited_once()


@pytest.mark.parametrize("failure", ["no_codec", "write", "drain"])
def test_unsuccessful_send_does_not_refresh_activity(clock, failure):
    player, writer = make_player()
    clock.now = 1200.0
    if failure == "no_codec":
        player.session.codec = None
    elif failure == "write":
        writer.write.side_effect = ConnectionResetError
    else:
        writer.drain.side_effect = ConnectionResetError

    asyncio.run(player.send_raw(b"heartbeat\n"))

    assert player.last_data_time == 1000.0


def test_disconnect_and_run_can_both_close_transport(clock):
    player, writer = make_player()

    async def read(*, timeout):
        await player.disconnect("Goodbye")
        return b""

    player.session.read = AsyncMock(side_effect=read)
    asyncio.run(player.run())

    assert not player.connected
    assert writer.close.call_count == 2
    assert writer.wait_closed.await_count == 2
