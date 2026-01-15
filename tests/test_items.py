"""Unit tests for pygserver items module."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


class TestItemManager:
    """Tests for ItemManager class."""

    def test_item_manager_creation(self):
        """Test creating an ItemManager."""
        from pygserver.items import ItemManager
        from unittest.mock import MagicMock

        mock_server = MagicMock()
        manager = ItemManager(mock_server)

        assert manager is not None

    def test_item_effects_defined(self):
        """Test ITEM_EFFECTS dictionary is defined."""
        from pygserver.items import ITEM_EFFECTS

        assert isinstance(ITEM_EFFECTS, dict)
        assert len(ITEM_EFFECTS) > 0


class TestLevelChest:
    """Tests for LevelChest dataclass."""

    def test_chest_creation(self):
        """Test creating a LevelChest."""
        from pygserver.items import LevelChest
        from pygserver.protocol.constants import LevelItemType

        chest = LevelChest(
            level_name="test.nw",
            x=10,
            y=20,
            item_type=LevelItemType.GREENRUPEE,
            sign_index=0
        )

        assert chest.level_name == "test.nw"
        assert chest.x == 10
        assert chest.y == 20


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
