"""Unit tests for pygserver Player class."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


class TestPlayerAttributes:
    """Tests for Player attribute initialization."""

    def test_player_has_stats(self):
        """Test Player has combat stats."""
        from pygserver.player import Player
        from unittest.mock import MagicMock, AsyncMock

        # Create mock server and streams
        mock_server = MagicMock()
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        player = Player(mock_server, 1, mock_reader, mock_writer)

        assert hasattr(player, 'hearts')
        assert hasattr(player, 'max_hearts')
        assert hasattr(player, 'rupees')
        assert hasattr(player, 'arrows')
        assert hasattr(player, 'bombs')
        assert hasattr(player, 'kills')
        assert hasattr(player, 'deaths')

    def test_player_initial_stats(self):
        """Test Player has correct initial stat values."""
        from pygserver.player import Player
        from unittest.mock import MagicMock, AsyncMock

        mock_server = MagicMock()
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        player = Player(mock_server, 1, mock_reader, mock_writer)

        assert player.hearts == 3.0
        assert player.max_hearts == 3.0
        assert player.kills == 0
        assert player.deaths == 0

    def test_player_has_appearance(self):
        """Test Player has appearance attributes."""
        from pygserver.player import Player
        from unittest.mock import MagicMock, AsyncMock

        mock_server = MagicMock()
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        player = Player(mock_server, 1, mock_reader, mock_writer)

        assert hasattr(player, 'head_image')
        assert hasattr(player, 'body_image')
        assert hasattr(player, 'nickname')

    def test_player_has_position(self):
        """Test Player has position attributes."""
        from pygserver.player import Player
        from unittest.mock import MagicMock, AsyncMock

        mock_server = MagicMock()
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        player = Player(mock_server, 1, mock_reader, mock_writer)

        assert hasattr(player, 'x')
        assert hasattr(player, 'y')
        assert hasattr(player, 'direction')
        assert player.x == 0.0
        assert player.y == 0.0


class TestPlayerMethods:
    """Tests for Player methods."""

    def test_player_has_send_methods(self):
        """Test Player has send methods."""
        from pygserver.player import Player
        from unittest.mock import MagicMock, AsyncMock

        mock_server = MagicMock()
        mock_reader = AsyncMock()
        mock_writer = MagicMock()

        player = Player(mock_server, 1, mock_reader, mock_writer)

        assert hasattr(player, 'send_raw')
        assert hasattr(player, 'send_packet')
        assert hasattr(player, 'send_props')
        assert callable(player.send_props)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
