"""Unit tests for pygserver protocol module."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


class TestConstants:
    """Tests for protocol constants."""

    def test_pli_constants_exist(self):
        """Test PLI (client to server) constants exist."""
        from pygserver.protocol.constants import PLI

        assert hasattr(PLI, 'PLAYERPROPS')
        assert hasattr(PLI, 'LEVELWARP')
        assert hasattr(PLI, 'TOALL')  # Chat message

    def test_plo_constants_exist(self):
        """Test PLO (server to client) constants exist."""
        from pygserver.protocol.constants import PLO

        assert hasattr(PLO, 'PLAYERPROPS')
        assert hasattr(PLO, 'LEVELBOARD')
        assert hasattr(PLO, 'OTHERPLPROPS')

    def test_plprop_constants_exist(self):
        """Test PLPROP (player property) constants exist."""
        from pygserver.protocol.constants import PLPROP

        assert hasattr(PLPROP, 'NICKNAME')
        assert hasattr(PLPROP, 'X2')
        assert hasattr(PLPROP, 'Y2')
        assert hasattr(PLPROP, 'KILLSCOUNT')
        assert hasattr(PLPROP, 'DEATHSCOUNT')


class TestPacketBuilders:
    """Tests for packet builder functions."""

    def test_build_player_props(self):
        """Test building player props packet."""
        from pygserver.protocol.packets import build_player_props
        from pygserver.protocol.constants import PLPROP

        props = {
            PLPROP.NICKNAME: "TestPlayer",
        }
        packet = build_player_props(props)

        assert isinstance(packet, bytes)
        assert len(packet) > 0

    def test_build_bomb_add(self):
        """Test building bomb add packet."""
        from pygserver.protocol.packets import build_bomb_add

        packet = build_bomb_add(1, 10.0, 20.0, 1, 5)  # Added time_left param (int)
        assert isinstance(packet, bytes)

    def test_build_arrow_add(self):
        """Test building arrow add packet."""
        from pygserver.protocol.packets import build_arrow_add

        packet = build_arrow_add(1, 10.0, 20.0, 2)
        assert isinstance(packet, bytes)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
