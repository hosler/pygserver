"""Unit tests for pygserver combat module."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


class TestCombatManager:
    """Tests for CombatManager class."""

    def test_combat_manager_creation(self):
        """Test creating a CombatManager."""
        from pygserver.combat import CombatManager
        from unittest.mock import MagicMock

        mock_server = MagicMock()
        manager = CombatManager(mock_server)

        assert manager is not None
        assert manager.server == mock_server

    def test_combat_manager_has_damage_types(self):
        """Test CombatManager has damage type enum."""
        from pygserver.combat import DamageType

        assert hasattr(DamageType, 'SWORD')
        assert hasattr(DamageType, 'BOMB')
        assert hasattr(DamageType, 'ARROW')

    def test_combat_manager_attributes(self):
        """Test CombatManager has required attributes."""
        from pygserver.combat import CombatManager
        from unittest.mock import MagicMock

        mock_server = MagicMock()
        manager = CombatManager(mock_server)

        assert hasattr(manager, 'respawn_time')


class TestDamageType:
    """Tests for DamageType enum."""

    def test_damage_types_are_ints(self):
        """Test damage types are integers."""
        from pygserver.combat import DamageType

        assert isinstance(DamageType.SWORD.value, int)
        assert isinstance(DamageType.BOMB.value, int)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
