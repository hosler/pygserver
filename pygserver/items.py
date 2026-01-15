"""
pygserver.items - Item system management

Handles ground items, chests, item spawning, and player inventory.
Based on GServer-v2 item implementation.
"""

import asyncio
import logging
import time
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, Set

from .protocol.constants import PLO, PLPROP, LevelItemType
from .protocol.packets import (
    PacketBuilder,
    build_item_add,
    build_item_del,
    build_level_chest,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)


@dataclass
class LevelItem:
    """Represents an item on the ground."""
    id: int
    level_name: str
    x: float
    y: float
    item_type: LevelItemType
    created_at: float = field(default_factory=time.time)
    despawn_time: float = 60.0  # Seconds until despawn (0 = never)

    @property
    def expired(self) -> bool:
        """Check if item should despawn."""
        if self.despawn_time <= 0:
            return False
        return time.time() - self.created_at >= self.despawn_time


@dataclass
class LevelChest:
    """Represents a chest in a level."""
    level_name: str
    x: int
    y: int
    item_type: LevelItemType
    sign_index: int  # Index into level's sign list for unique ID
    opened_by: Set[str] = field(default_factory=set)  # Account names who opened it

    @property
    def chest_id(self) -> str:
        """Unique chest identifier for save/load."""
        return f"{self.level_name}:{self.x},{self.y}"


# Item type to player stat mapping
# Note: LevelItemType.DARTS is arrows, LevelItemType.BOMBS is the bombs pickup
ITEM_EFFECTS = {
    LevelItemType.GREENRUPEE: ('rupees', 1),
    LevelItemType.BLUERUPEE: ('rupees', 5),
    LevelItemType.REDRUPEE: ('rupees', 30),
    LevelItemType.GOLDRUPEE: ('rupees', 100),
    LevelItemType.HEART: ('hearts', 1),
    LevelItemType.DARTS: ('arrows', 5),  # DARTS = arrows in protocol
    LevelItemType.BOMBS: ('bombs', 5),
    LevelItemType.GLOVE1: ('glove_power', 1),
    LevelItemType.GLOVE2: ('glove_power', 2),
    LevelItemType.FULLHEART: ('max_hearts', 1),
    LevelItemType.SUPERBOMB: ('bombs', 10),
    LevelItemType.SPINATTACK: ('spin_attack', True),
}

# Drop tables for various sources
# Note: DARTS = arrows in the protocol
BUSH_DROPS = [
    (LevelItemType.GREENRUPEE, 0.4),
    (LevelItemType.BLUERUPEE, 0.1),
    (LevelItemType.HEART, 0.2),
    (LevelItemType.DARTS, 0.1),  # DARTS = arrows
    (LevelItemType.BOMBS, 0.1),
    (None, 0.1),  # Nothing
]

POT_DROPS = [
    (LevelItemType.GREENRUPEE, 0.3),
    (LevelItemType.BLUERUPEE, 0.15),
    (LevelItemType.HEART, 0.25),
    (LevelItemType.DARTS, 0.15),  # DARTS = arrows
    (LevelItemType.BOMBS, 0.15),
]


class ItemManager:
    """
    Manages ground items and chests.

    Handles:
    - Item spawning and despawning
    - Player item pickup
    - Chest opening
    - Random drops from bushes/pots
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Active items by level
        self._items: Dict[str, Dict[int, LevelItem]] = {}  # level_name -> {item_id: Item}

        # Chests by level
        self._chests: Dict[str, List[LevelChest]] = {}  # level_name -> [Chest]

        # ID counter
        self._next_item_id = 1

        # Tick task
        self._tick_task: Optional[asyncio.Task] = None
        self._running = False

        # Settings
        self.item_despawn_time = 60.0  # Default despawn time
        self.max_items_per_level = 100  # Prevent item spam

    async def start(self):
        """Start the item tick loop."""
        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("Item manager started")

    async def stop(self):
        """Stop the item tick loop."""
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        logger.info("Item manager stopped")

    async def _tick_loop(self):
        """Main item tick loop (runs every 1 second)."""
        tick_interval = 1.0

        while self._running:
            try:
                await self._tick()
                await asyncio.sleep(tick_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Item tick error: {e}")
                await asyncio.sleep(tick_interval)

    async def _tick(self):
        """Process one item tick - remove expired items."""
        for level_name, items in list(self._items.items()):
            expired = [item_id for item_id, item in items.items() if item.expired]

            for item_id in expired:
                item = items.pop(item_id)
                # Broadcast item removal
                packet = build_item_del(item.x, item.y)
                await self.server.broadcast_to_level(level_name, packet)

    async def spawn_item(self, level: 'Level', x: float, y: float,
                         item_type: LevelItemType,
                         despawn_time: Optional[float] = None) -> Optional[LevelItem]:
        """
        Spawn an item on a level.

        Args:
            level: Level to spawn on
            x: X position
            y: Y position
            item_type: Type of item
            despawn_time: Custom despawn time (None = use default)

        Returns:
            The spawned item, or None if failed
        """
        level_name = level.name

        # Check item limit
        if level_name in self._items:
            if len(self._items[level_name]) >= self.max_items_per_level:
                logger.warning(f"Item limit reached on {level_name}")
                return None

        # Create item
        item_id = self._next_item_id
        self._next_item_id += 1

        item = LevelItem(
            id=item_id,
            level_name=level_name,
            x=x,
            y=y,
            item_type=item_type,
            despawn_time=despawn_time if despawn_time is not None else self.item_despawn_time
        )

        # Store item
        if level_name not in self._items:
            self._items[level_name] = {}
        self._items[level_name][item_id] = item

        # Broadcast to level
        packet = build_item_add(x, y, item_type.value)
        await self.server.broadcast_to_level(level_name, packet)

        logger.debug(f"Spawned {item_type.name} at ({x}, {y}) on {level_name}")
        return item

    async def remove_item(self, level_name: str, x: float, y: float) -> bool:
        """
        Remove an item from a level by position.

        Args:
            level_name: Level name
            x: X position
            y: Y position

        Returns:
            True if item was removed
        """
        if level_name not in self._items:
            return False

        # Find item at position
        for item_id, item in list(self._items[level_name].items()):
            if abs(item.x - x) < 0.5 and abs(item.y - y) < 0.5:
                del self._items[level_name][item_id]

                # Broadcast removal
                packet = build_item_del(x, y)
                await self.server.broadcast_to_level(level_name, packet)
                return True

        return False

    async def handle_item_take(self, player: 'Player', x: float, y: float) -> bool:
        """
        Handle player picking up an item.

        Args:
            player: Player picking up
            x: Item X position
            y: Item Y position

        Returns:
            True if item was picked up
        """
        if not player.level:
            return False

        level_name = player.level.name
        if level_name not in self._items:
            return False

        # Find item at position
        for item_id, item in list(self._items[level_name].items()):
            if abs(item.x - x) < 1.0 and abs(item.y - y) < 1.0:
                # Apply item effect
                await self.give_item_to_player(player, item.item_type)

                # Remove item
                del self._items[level_name][item_id]

                # Broadcast removal
                packet = build_item_del(x, y)
                await self.server.broadcast_to_level(level_name, packet)

                logger.debug(f"Player {player.id} picked up {item.item_type.name}")
                return True

        return False

    async def give_item_to_player(self, player: 'Player', item_type: LevelItemType):
        """
        Give an item's effect to a player.

        Args:
            player: Player receiving item
            item_type: Type of item
        """
        if item_type not in ITEM_EFFECTS:
            logger.debug(f"Item {item_type.name} has no effect defined")
            return

        attr, value = ITEM_EFFECTS[item_type]

        props_to_send = {}

        if attr == 'rupees':
            player.rupees = min(player.rupees + value, 9999)
            props_to_send[PLPROP.RUPEESCOUNT] = player.rupees
        elif attr == 'hearts':
            player.hearts = min(player.hearts + value, player.max_hearts)
            props_to_send[PLPROP.CURPOWER] = int(player.hearts * 2)
        elif attr == 'arrows':
            player.arrows = min(player.arrows + value, 99)
            props_to_send[PLPROP.ARROWSCOUNT] = player.arrows
        elif attr == 'bombs':
            player.bombs = min(player.bombs + value, 99)
            props_to_send[PLPROP.BOMBSCOUNT] = player.bombs
        elif attr == 'darts':
            if hasattr(player, 'darts'):
                player.darts = min(player.darts + value, 99)
                # Note: DARTS prop not commonly used
        elif attr == 'glove_power':
            player.glove_power = max(player.glove_power, value)
            props_to_send[PLPROP.GLOVEPOWER] = player.glove_power
        elif attr == 'max_hearts':
            player.max_hearts = min(player.max_hearts + value, 20)
            player.hearts = player.max_hearts  # Full heal on heart container
            props_to_send[PLPROP.MAXPOWER] = int(player.max_hearts * 2)
            props_to_send[PLPROP.CURPOWER] = int(player.hearts * 2)
        elif attr == 'spin_attack':
            if hasattr(player, 'has_spin_attack'):
                player.has_spin_attack = True

        # Send stat update to player
        if props_to_send:
            asyncio.create_task(player.send_props(props_to_send))

    def add_chest(self, level: 'Level', x: int, y: int,
                  item_type: LevelItemType, sign_index: int = 0) -> LevelChest:
        """
        Add a chest to a level.

        Args:
            level: Level to add chest to
            x: Chest X position
            y: Chest Y position
            item_type: Item in chest
            sign_index: Sign index for unique ID

        Returns:
            The created chest
        """
        chest = LevelChest(
            level_name=level.name,
            x=x,
            y=y,
            item_type=item_type,
            sign_index=sign_index
        )

        if level.name not in self._chests:
            self._chests[level.name] = []
        self._chests[level.name].append(chest)

        return chest

    def remove_chest(self, level_name: str, x: int, y: int) -> bool:
        """
        Remove a chest from a level.

        Args:
            level_name: Level name
            x: Chest X position
            y: Chest Y position

        Returns:
            True if chest was removed
        """
        if level_name not in self._chests:
            return False

        for i, chest in enumerate(self._chests[level_name]):
            if chest.x == x and chest.y == y:
                self._chests[level_name].pop(i)
                return True

        return False

    def get_chest_at(self, level_name: str, x: int, y: int) -> Optional[LevelChest]:
        """
        Get chest at position.

        Args:
            level_name: Level name
            x: X position
            y: Y position

        Returns:
            Chest at position, or None
        """
        if level_name not in self._chests:
            return None

        for chest in self._chests[level_name]:
            if chest.x == x and chest.y == y:
                return chest

        return None

    async def handle_open_chest(self, player: 'Player', x: int, y: int) -> bool:
        """
        Handle player opening a chest.

        Args:
            player: Player opening chest
            x: Chest X position
            y: Chest Y position

        Returns:
            True if chest was opened
        """
        if not player.level:
            return False

        chest = self.get_chest_at(player.level.name, x, y)
        if not chest:
            return False

        # Check if player already opened this chest
        if player.account_name in chest.opened_by:
            logger.debug(f"Player {player.id} already opened chest at ({x}, {y})")
            return False

        # Mark as opened
        chest.opened_by.add(player.account_name)

        # Give item
        await self.give_item_to_player(player, chest.item_type)

        # Send chest open packet
        packet = build_level_chest(x, y, chest.item_type.value, chest.sign_index)
        await player.send_raw(packet)

        logger.info(f"Player {player.id} opened chest at ({x}, {y}), got {chest.item_type.name}")
        return True

    async def spawn_random_drop(self, level: 'Level', x: float, y: float,
                                 drop_table: List[Tuple[Optional[LevelItemType], float]]
                                 ) -> Optional[LevelItem]:
        """
        Spawn a random item from a drop table.

        Args:
            level: Level to spawn on
            x: X position
            y: Y position
            drop_table: List of (item_type, probability) tuples

        Returns:
            Spawned item, or None
        """
        roll = random.random()
        cumulative = 0.0

        for item_type, probability in drop_table:
            cumulative += probability
            if roll < cumulative:
                if item_type is not None:
                    return await self.spawn_item(level, x, y, item_type)
                return None

        return None

    async def spawn_bush_drop(self, level: 'Level', x: float, y: float) -> Optional[LevelItem]:
        """Spawn a random drop from a bush."""
        return await self.spawn_random_drop(level, x, y, BUSH_DROPS)

    async def spawn_pot_drop(self, level: 'Level', x: float, y: float) -> Optional[LevelItem]:
        """Spawn a random drop from a pot."""
        return await self.spawn_random_drop(level, x, y, POT_DROPS)

    def get_items_on_level(self, level_name: str) -> List[LevelItem]:
        """Get all items on a level."""
        return list(self._items.get(level_name, {}).values())

    def get_chests_on_level(self, level_name: str) -> List[LevelChest]:
        """Get all chests on a level."""
        return self._chests.get(level_name, []).copy()

    def clear_level(self, level_name: str):
        """Clear all items from a level."""
        self._items.pop(level_name, None)

    def load_player_chests(self, player: 'Player', opened_chests: List[str]):
        """
        Load which chests a player has opened.

        Args:
            player: Player to load for
            opened_chests: List of chest IDs the player has opened
        """
        for chest_id in opened_chests:
            # Parse chest ID (format: "level_name:x,y")
            if ':' not in chest_id:
                continue

            level_name, coords = chest_id.rsplit(':', 1)
            try:
                x, y = coords.split(',')
                x, y = int(x), int(y)
            except ValueError:
                continue

            chest = self.get_chest_at(level_name, x, y)
            if chest:
                chest.opened_by.add(player.account_name)

    def get_player_opened_chests(self, player: 'Player') -> List[str]:
        """
        Get list of chest IDs a player has opened.

        Args:
            player: Player to check

        Returns:
            List of chest IDs
        """
        opened = []
        for level_name, chests in self._chests.items():
            for chest in chests:
                if player.account_name in chest.opened_by:
                    opened.append(chest.chest_id)
        return opened

    async def send_level_items(self, player: 'Player', level: 'Level'):
        """
        Send all items on a level to a player.

        Args:
            player: Player to send to
            level: Level to send items from
        """
        # Send ground items
        for item in self.get_items_on_level(level.name):
            packet = build_item_add(item.x, item.y, item.item_type.value)
            await player.send_raw(packet)

        # Send chests (that player hasn't opened)
        for chest in self.get_chests_on_level(level.name):
            if player.account_name not in chest.opened_by:
                packet = build_level_chest(
                    chest.x, chest.y, chest.item_type.value, chest.sign_index
                )
                await player.send_raw(packet)
