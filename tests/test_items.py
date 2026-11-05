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


class TestDefaultWeaponItems:
    """Taking a bow/bomb/... item grants the built-in weapon."""

    @staticmethod
    def _player():
        from unittest.mock import AsyncMock, MagicMock
        from pygserver.player import Player

        player = MagicMock()
        player.weapons = []
        player.has_weapon = lambda n: Player.has_weapon(player, n)
        player.add_weapon = lambda n: Player.add_weapon(player, n)
        player.send_raw = AsyncMock()
        player.send_props = AsyncMock()
        return player

    def test_take_bow_grants_once(self):
        import asyncio
        from unittest.mock import MagicMock
        from pygserver.items import ItemManager
        from pygserver.protocol.constants import LevelItemType
        from pygserver.protocol.packets import build_default_weapon

        manager = ItemManager(MagicMock())
        player = self._player()

        async def run():
            await manager.give_item_to_player(player, LevelItemType.BOW)
            await manager.give_item_to_player(player, LevelItemType.BOW)

        asyncio.run(run())
        assert player.weapons == ['bow']
        player.send_raw.assert_awaited_once_with(
            build_default_weapon(int(LevelItemType.BOW)))

    def test_rupee_adds_no_weapon(self):
        import asyncio
        from unittest.mock import MagicMock
        from pygserver.items import ItemManager
        from pygserver.protocol.constants import LevelItemType

        manager = ItemManager(MagicMock())
        player = self._player()
        player.rupees = 0
        asyncio.run(manager.give_item_to_player(player, LevelItemType.GREENRUPEE))
        assert player.weapons == []
        player.send_raw.assert_not_awaited()

    def test_all_seven_types(self):
        from pygserver.items import DEFAULT_WEAPON_ITEMS
        assert sorted(DEFAULT_WEAPON_ITEMS.values()) == sorted(
            ['bow', 'bomb', 'superbomb', 'fireball', 'fireblast',
             'nukeshot', 'joltbomb'])

    def test_chest_with_bomb_grants_bomb(self):
        import asyncio
        from unittest.mock import MagicMock
        from pygserver.items import ItemManager
        from pygserver.protocol.constants import LevelItemType

        manager = ItemManager(MagicMock())
        level = MagicMock()
        level.name = "test.nw"
        manager.add_chest(level, 10, 20, LevelItemType.BOMB)
        player = self._player()
        player.level = level
        player.x, player.y = 10.0, 20.0
        player.account_name = "tester"
        assert asyncio.run(manager.handle_open_chest(player, 10, 20))
        assert player.weapons == ['bomb']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
