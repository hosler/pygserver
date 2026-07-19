"""Unit tests for RC (Remote Control) admin protocol handlers.

Tests wire-format parsing for RC packets, verifying compliance with
GServer-v2's TPlayerRC.cpp implementation.
"""

import asyncio
import sys
import os
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


class TestWarpPlayerWireFormat:
    """PLI_RC_WARPPLAYER wire format: [GUSHORT playerId][SIGNED GCHAR x*2][SIGNED GCHAR y*2][level=rest]"""

    def test_warp_player_positive_coordinates(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        player.disconnect = AsyncMock()
        player.warp = AsyncMock()
        
        session = RCSession(player=player, rights=PLPERM.WARPTOPLAYER)
        mock_server.get_player.return_value = player

        # Build packet: player_id=2, x=10 (5 tiles), y=20 (10 tiles), level="test"
        payload = PacketBuilder().write_gshort(2).write_gchar_signed(10).write_gchar_signed(20).write_string("test").build()

        async def main():
            await rc._handle_warp_player(session, payload)
        asyncio.run(main())

        # Verify warp was called with correct values
        player.warp.assert_awaited_once()
        args = player.warp.call_args.args
        assert args[0] == "test"  # level_name
        assert args[1] == 5.0  # x / 2.0
        assert args[2] == 10.0  # y / 2.0

    def test_warp_player_negative_coordinates(self):
        """Test negative gmap coordinates (west/north of origin)."""
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        player.warp = AsyncMock()
        
        session = RCSession(player=player, rights=PLPERM.WARPTOPLAYER)
        mock_server.get_player.return_value = player

        # Build packet: player_id=2, x=-10 (signed), y=-20 (signed)
        payload = PacketBuilder().write_gshort(2).write_gchar_signed(-10).write_gchar_signed(-20).write_string("origin").build()

        async def main():
            await rc._handle_warp_player(session, payload)
        asyncio.run(main())

        args = player.warp.call_args.args
        assert args[1] == -5.0  # x / 2.0 with negative value
        assert args[2] == -10.0  # y / 2.0 with negative value

    def test_warp_player_requires_right(self):
        """WARPTOPLAYER should check rights."""
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        player.warp = AsyncMock()
        
        session = RCSession(player=player, rights=0)  # No rights

        payload = PacketBuilder().write_gshort(2).write_gchar_signed(10).write_gchar_signed(20).write_string("test").build()

        async def main():
            await rc._handle_warp_player(session, payload)
        asyncio.run(main())

        player.warp.assert_not_awaited()


class TestDisconnectPlayerWireFormat:
    """PLI_RC_DISCONNECTPLAYER: [GUSHORT id][reason=rest]"""

    def test_disconnect_player_with_reason(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        admin_player = MagicMock()
        admin_player.id = 1
        admin_player.account_name = "admin"
        target_player = MagicMock()
        target_player.id = 2
        target_player.account_name = "player2"
        target_player.disconnect = AsyncMock()
        
        session = RCSession(player=admin_player, rights=PLPERM.DISCONNECT)
        mock_server.get_player.return_value = target_player

        payload = PacketBuilder().write_gshort(2).write_string("Spamming").build()

        async def main():
            await rc._handle_disconnect_player(session, payload)
        asyncio.run(main())

        target_player.disconnect.assert_awaited_once_with("Spamming")

    def test_disconnect_player_requires_right(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        admin_player = MagicMock()
        admin_player.disconnect = AsyncMock()
        
        session = RCSession(player=admin_player, rights=0)

        payload = PacketBuilder().write_gshort(2).write_string("Reason").build()

        async def main():
            await rc._handle_disconnect_player(session, payload)
        asyncio.run(main())

        admin_player.disconnect.assert_not_awaited()


class TestServerFlagsSetWireFormat:
    """PLI_RC_SERVERFLAGSSET: [GUSHORT count] then count x [GUCHAR-len string]"""

    def test_server_flags_set_multiple_flags(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        mock_server.server_flags = {}
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        
        session = RCSession(player=player, rights=PLPERM.SETATTRIBUTES)

        # Build packet: count=2, then "flag1=value1", "flag2=value2"
        payload = PacketBuilder().write_gshort(2).write_gstring("flag1=value1").write_gstring("flag2=value2").build()

        async def main():
            await rc._handle_server_flags_set(session, payload)
        asyncio.run(main())

        assert mock_server.server_flags["flag1"] == "value1"
        assert mock_server.server_flags["flag2"] == "value2"

    def test_server_flags_set_requires_right(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder

        mock_server = MagicMock()
        mock_server.server_flags = {}
        rc = RCManager(mock_server)
        player = MagicMock()
        
        session = RCSession(player=player, rights=0)

        payload = PacketBuilder().write_gshort(1).write_gstring("flag=val").build()

        async def main():
            await rc._handle_server_flags_set(session, payload)
        asyncio.run(main())

        # Flags should not be set
        assert len(mock_server.server_flags) == 0


class TestPlayerBanSetWireFormat:
    """PLI_RC_PLAYERBANSET: [GUCHAR-len acct][GUCHAR banned][reason=rest]"""

    def test_player_ban_set_ban_with_reason(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        mock_server.account_manager = MagicMock()
        account = MagicMock()
        account.account_name = "player1"
        mock_server.account_manager.get_account.return_value = account
        mock_server.account_manager.save_account = MagicMock()
        
        rc = RCManager(mock_server)
        admin_player = MagicMock()
        admin_player.id = 1
        admin_player.account_name = "admin"
        
        session = RCSession(player=admin_player, rights=PLPERM.BAN)

        payload = PacketBuilder().write_gstring("player1").write_gchar(1).write_string("Cheating").build()

        async def main():
            await rc._handle_player_ban_set(session, payload)
        asyncio.run(main())

        assert account.is_banned is True
        assert account.ban_reason == "Cheating"
        mock_server.account_manager.save_account.assert_called_once_with(account)

    def test_player_ban_set_unban(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        mock_server.account_manager = MagicMock()
        account = MagicMock()
        account.account_name = "player1"
        mock_server.account_manager.get_account.return_value = account
        mock_server.account_manager.save_account = MagicMock()
        
        rc = RCManager(mock_server)
        admin_player = MagicMock()
        admin_player.id = 1
        admin_player.account_name = "admin"
        
        session = RCSession(player=admin_player, rights=PLPERM.BAN)

        payload = PacketBuilder().write_gstring("player1").write_gchar(0).write_string("Unban appeal accepted").build()

        async def main():
            await rc._handle_player_ban_set(session, payload)
        asyncio.run(main())

        assert account.is_banned is False
        assert account.ban_reason == "Unban appeal accepted"

    def test_player_ban_set_requires_right(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.packets import PacketBuilder

        mock_server = MagicMock()
        mock_server.account_manager = MagicMock()
        
        rc = RCManager(mock_server)
        player = MagicMock()
        
        session = RCSession(player=player, rights=0)

        payload = PacketBuilder().write_gstring("player1").write_gchar(1).write_string("Banned").build()

        async def main():
            await rc._handle_player_ban_set(session, payload)
        asyncio.run(main())

        mock_server.account_manager.get_account.assert_not_called()


class TestLargeFileTransferWireFormat:
    """PLI_RC_LARGEFILESTART/END: [filename=rest]"""

    def test_large_file_start_creates_buffer(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        
        session = RCSession(player=player, rights=PLPERM.SETATTRIBUTES)

        payload = b"myfile.txt"

        async def main():
            await rc._handle_large_file_start(session, payload)
        asyncio.run(main())

        assert "myfile.txt" in session.large_file_uploads
        assert session.large_file_uploads["myfile.txt"] == bytearray()

    def test_large_file_end_without_start_logs_warning(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        
        session = RCSession(player=player, rights=PLPERM.SETATTRIBUTES)

        payload = b"nonexistent.txt"

        async def main():
            await rc._handle_large_file_end(session, payload)
        asyncio.run(main())

        # Should not raise, just log warning
        assert "nonexistent.txt" not in session.large_file_uploads

    def test_large_file_roundtrip_start_end(self):
        """Test that LARGEFILESTART+LARGEFILEEND properly key the buffered upload."""
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        mock_server.filesystem = MagicMock()
        mock_server.filesystem.write_file = MagicMock()
        
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        
        session = RCSession(player=player, rights=PLPERM.SETATTRIBUTES)
        session.file_browser_path = ""

        # Start file upload
        async def main():
            await rc._handle_large_file_start(session, b"test.dat")
            
            # Simulate chunks being added to the buffer
            session.large_file_uploads["test.dat"].extend(b"Hello ")
            session.large_file_uploads["test.dat"].extend(b"World!")
            
            # End file upload (this should trigger write_file)
            await rc._handle_large_file_end(session, b"test.dat")
        
        asyncio.run(main())

        # Verify the file was written with the correct content
        mock_server.filesystem.write_file.assert_called_once_with("test.dat", b"Hello World!")

    def test_large_file_filename_with_path(self):
        """Test LARGEFILEEND with file_browser_path prepended."""
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.constants import PLPERM

        mock_server = MagicMock()
        mock_server.filesystem = MagicMock()
        mock_server.filesystem.write_file = MagicMock()
        
        rc = RCManager(mock_server)
        player = MagicMock()
        player.id = 1
        player.account_name = "admin"
        
        session = RCSession(player=player, rights=PLPERM.SETATTRIBUTES)
        session.file_browser_path = "uploads"

        async def main():
            await rc._handle_large_file_start(session, b"file.dat")
            session.large_file_uploads["file.dat"].extend(b"data")
            await rc._handle_large_file_end(session, b"file.dat")
        
        asyncio.run(main())

        # Verify path was prepended
        mock_server.filesystem.write_file.assert_called_once_with("uploads/file.dat", b"data")

    def test_large_file_start_requires_right(self):
        from pygserver.rc import RCManager, RCSession

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        
        session = RCSession(player=player, rights=0)

        payload = b"test.txt"

        async def main():
            await rc._handle_large_file_start(session, payload)
        asyncio.run(main())

        assert "test.txt" not in session.large_file_uploads


class TestNoArgumentlessReadString:
    """Verify there are no argument-less read_string() calls in rc.py."""

    def test_no_argumentless_read_string_calls(self):
        """Verify all read_string() calls (not in comments) have a length argument."""
        import os
        import ast
        
        rc_path = os.path.join(os.path.dirname(__file__), '..', 'pygserver', 'rc.py')
        with open(rc_path, 'r') as f:
            content = f.read()
        
        # Parse the AST to find all method calls
        tree = ast.parse(content)
        
        # Walk through and find read_string calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr == 'read_string':
                        # Verify it has at least one argument
                        assert len(node.args) > 0, f"Found read_string() call without arguments at line {node.lineno}"


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
