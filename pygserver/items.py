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

from .attachments import absolute_position
from .config import ServerConfig

from .protocol.constants import PLO, PLPROP, LevelItemType
from .protocol.packets import (
    PacketBuilder,
    build_default_weapon,
    build_item_add,
    build_item_del,
    build_level_chest,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)

# GServer-v2 LevelItem.h:66: server expiry is 8200 milliseconds.
ITEM_LIFETIME = 8.2


@dataclass
class LevelItem:
    """Represents an item on the ground."""
    id: int
    level_name: str
    x: float
    y: float
    item_type: LevelItemType
    created_at: float = field(default_factory=time.time)
    despawn_time: float = ITEM_LIFETIME  # Seconds until despawn (0 = never)
    layer: int = 0

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


# Items that grant a built-in weapon on take (GServer-v2 LevelItem.cpp
# getItemPlayerProp -> Player::addWeapon(LevelItemType); Server.cpp default
# weapon list). The weapon is named by the item name, lowercase as in
# LevelItem.h ItemNames, and reaches the client as PLO_DEFAULTWEAPON.
DEFAULT_WEAPON_ITEMS = {
    LevelItemType.BOW: 'bow',
    LevelItemType.BOMB: 'bomb',
    LevelItemType.SUPERBOMB: 'superbomb',
    LevelItemType.FIREBALL: 'fireball',
    LevelItemType.FIREBLAST: 'fireblast',
    LevelItemType.NUKESHOT: 'nukeshot',
    LevelItemType.JOLTBOMB: 'joltbomb',
}
DEFAULT_WEAPON_IDS = {name: item for item, name in DEFAULT_WEAPON_ITEMS.items()}


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
    # Player GLOVEPOWER is 2/3 for glove1/glove2. NPCs use the distinct 1/2
    # scale; this table applies only to player level-item pickup.
    LevelItemType.GLOVE1: ('glove_power', 2),
    LevelItemType.GLOVE2: ('glove_power', 3),
    LevelItemType.FULLHEART: ('max_hearts', 1),
    LevelItemType.SUPERBOMB: ('bombs', 10),
    LevelItemType.SPINATTACK: ('spin_attack', True),
}

# Tiles whose cut drops an item, and the vase tile (GServer-v2
# PlayerClientPackets.cpp:112 dropTiles; :159 0x2ac): bushes, grass, swamp,
# snow grass, desert grass.
BUSH_TILES = frozenset({0x002, 0x1a4, 0x1ff, 0x7ff, 0x3ff, 0x5d9, 0x34f})
VASE_TILE = 0x2ac

# What a stock server rolls and may drop (config.py defaults).
DEFAULT_BUSH_DROPS = ServerConfig().bush_drops
DEFAULT_DEATH_DROPS = ServerConfig().death_drop_items()

# Level-segment size in pixels, and one half tile (Extents.h toPixelPosition).
SEGMENT_PIXELS = 1024
HALF_TILE_PIXELS = 8


def _option(config, name: str, default):
    """A typed server option, or `default` when the config does not carry it."""
    value = getattr(config, name, default)
    return value if isinstance(value, type(default)) else default


def _truncate_div(numerator: int, denominator: int) -> int:
    """C++ integer division (rounds toward zero)."""
    quotient = abs(numerator) // denominator
    return -quotient if numerator < 0 else quotient


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

        # Drop rolls. Tests replace this with a seeded random.Random.
        self.rng = random.Random()

        # Settings
        self.item_despawn_time = ITEM_LIFETIME
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
        """Expire items at the gameplay tick cadence."""
        tick_interval = 0.05

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
                # The reference expires level items silently (Level.cpp:711-716,
                # no PLO_ITEMDEL): clients run their own timer for the item and
                # answer the expiry with PLI_ITEMDEL (see remove_item_silently).
                items.pop(item_id)

    async def spawn_item(self, level: 'Level', x: float, y: float,
                         item_type: LevelItemType,
                         despawn_time: Optional[float] = None,
                         exclude_player_id: Optional[int] = None,
                         layer: int = 0) -> Optional[LevelItem]:
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
        if not self.has_room(level_name):
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
            layer=layer,
            despawn_time=despawn_time if despawn_time is not None else self.item_despawn_time
        )

        # Store item
        if level_name not in self._items:
            self._items[level_name] = {}
        self._items[level_name][item_id] = item

        # Broadcast to level
        packet = build_item_add(x, y, item_type.value)
        exclude = {exclude_player_id} if exclude_player_id is not None else None
        await self.server.broadcast_to_level(level_name, packet, exclude=exclude)

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

    def _item_near(self, level_name: str, x: float, y: float, tolerance: float):
        """(id, item) at a position: the item exactly there, else the nearest
        one closer than `tolerance` on both axes, else None."""
        best = None
        best_distance = None
        for item_id, item in self._items.get(level_name, {}).items():
            dx, dy = abs(item.x - x), abs(item.y - y)
            if dx >= tolerance or dy >= tolerance:
                continue
            distance = dx * dx + dy * dy
            if best_distance is None or distance < best_distance:
                best, best_distance = (item_id, item), distance
                if distance == 0:
                    break
        return best

    def remove_item_silently(self, level_name: str, x: float, y: float) -> bool:
        """Drop the ground item at a position without telling anyone
        (Level::removeItem(position), Level.cpp:2495-2508). False, and no
        change, when no item is there (it already expired or was taken)."""
        target = self._item_near(level_name, x, y, 0.5)
        if target is None:
            return False
        del self._items[level_name][target[0]]
        return True

    def _take_target(self, level_name: str, x: float, y: float):
        """(id, item) a take at (x, y) refers to: the item exactly there, else
        the nearest within a tile of it, or None."""
        return self._item_near(level_name, x, y, 1.0)

    @staticmethod
    def _within_reach(player: 'Player', item: LevelItem) -> bool:
        """TServerLevel.cpp:1419: player and item centres, not the packet xy."""
        px, py = absolute_position(player)
        return (abs(px + 1.5 - (item.x + 1.0)) < 2.0
                and abs(py + 2.0 - (item.y + 1.0)) < 2.0)

    def _take_allowed(self, player: 'Player', item: LevelItem) -> bool:
        """Whether `player` may take a live `item` now: same layer, old enough
        (remaining time <160, TServerLevel.cpp:1416) and within reach (:1419)."""
        ticks = int((time.time() - item.created_at) / .05 + 1e-9)
        return (164 - ticks < 160
                and item.layer == getattr(player, 'layer', 0)
                and self._within_reach(player, item))

    def take_relay_position(self, player: 'Player', x: float, y: float):
        """What the PLI_ITEMTAKE handler does before relaying: None when the
        take is ignored outright (a live item that the age, layer or reach
        test refuses: no relay, no removal, no credit), else the (x, y) the
        other players are told to drop. That is the chosen item's own position
        or, when no item is there any more, the packet's."""
        if not player.level:
            return None
        target = self._take_target(player.level.name, x, y)
        if target is None:
            return x, y
        item = target[1]
        if not item.expired and not self._take_allowed(player, item):
            return None
        return item.x, item.y

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
        target = self._take_target(level_name, x, y)
        if target is None:
            return False
        item_id, item = target
        if item.expired:
            self.remove_item_silently(level_name, item.x, item.y)
            return False
        if not self._take_allowed(player, item):
            return False
        # Claim before awaiting the grant so concurrent takes grant once.
        del self._items[level_name][item_id]
        # The other players were told by the PLI_ITEMTAKE handler before this
        # lookup (PlayerClientPackets.cpp:389); the taker is sent nothing, its
        # client removed the item when it sent the take.
        await self.give_item_to_player(player, item.item_type)
        logger.debug(f"Player {player.id} picked up {item.item_type.name}")
        return True

    async def give_item_to_player(self, player: 'Player', item_type: LevelItemType):
        """
        Give an item's effect to a player.

        Args:
            player: Player receiving item
            item_type: Type of item
        """
        weapon = DEFAULT_WEAPON_ITEMS.get(item_type)
        if weapon is not None:
            if not player.has_weapon(weapon):
                player.add_weapon(weapon)
                await player.send_raw(build_default_weapon(int(item_type)))
            return

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
            # MAXPOWER is FULL hearts on the wire (GServer-v2
            # LevelItem.cpp:148-151: `>> MAXPOWER >> heartMax`); CURPOWER
            # below is the halves one.
            props_to_send[PLPROP.MAXPOWER] = int(player.max_hearts)
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

        # Proximity check: a chest is a 2x2 object opened from right next to it.
        # Without this, a modified client can open (and loot) every chest on the
        # level from spawn — a playtester grabbed chests 16 tiles away. The
        # player pos is the sprite's top-left; allow a couple tiles of slack for
        # sprite size + movement latency, same spirit as combat's range cap.
        if abs(absolute_position(player)[0] - x) > 3.0 or abs(absolute_position(player)[1] - y) > 3.0:
            logger.debug(f"Rejecting chest open from {player.id} at "
                         f"({absolute_position(player)[0]:.1f},{absolute_position(player)[1]:.1f}) on chest ({x},{y}): "
                         f"out of range")
            return False

        # Check if player already opened this chest
        if player.account_name in chest.opened_by:
            logger.debug(f"Player {player.id} already opened chest at ({x}, {y})")
            return False

        # Mark as opened
        chest.opened_by.add(player.account_name)

        # Give item
        await self.give_item_to_player(player, chest.item_type)

        # Send chest open packet (3-byte opened form, no item/sign)
        packet = build_level_chest(True, x, y)
        await player.send_raw(packet)

        logger.info(f"Player {player.id} opened chest at ({x}, {y}), got {chest.item_type.name}")
        return True

    # -- reference item drops ---------------------------------------------------

    def has_room(self, level_name: str) -> bool:
        """False once the per-level item cap is reached (flood guard; the
        reference has no cap)."""
        return len(self._items.get(level_name, ())) < self.max_items_per_level

    async def drop_item_at(self, level: 'Level', half_x: int, half_y: int,
                           item_type: LevelItemType) -> bool:
        """Level::addItem(inform_client) at a half-tile offset from this level
        segment's origin (Level.cpp:2424-2433, Extents.h:389-395).

        The reference places by world pixel and tells only the clients in the
        segment the pixel falls in (Server.cpp:2361-2375), so a drop that
        lands past the edge goes to the neighbouring segment, or to nobody
        when there is none. Segment numbers truncate toward zero and local
        coordinates keep the sign (Extents.h:451, :562).

        Returns False only when the item cap stopped the drop.
        """
        gmap_info = self.server.world.get_gmap_for_level(level.name)
        if gmap_info:
            gmap, grid_x, grid_y = gmap_info
        else:
            gmap, grid_x, grid_y = None, 0, 0
        world_x = grid_x * SEGMENT_PIXELS + half_x * HALF_TILE_PIXELS
        world_y = grid_y * SEGMENT_PIXELS + half_y * HALF_TILE_PIXELS
        cell_x = _truncate_div(world_x, SEGMENT_PIXELS)
        cell_y = _truncate_div(world_y, SEGMENT_PIXELS)
        if not (0 <= cell_x <= 255 and 0 <= cell_y <= 255):
            return True
        if gmap is None:
            if (cell_x, cell_y) != (0, 0):
                return True
            target = level
        elif (cell_x, cell_y) == (grid_x, grid_y):
            target = level
        else:
            name = gmap.get_level_at(cell_x, cell_y)
            target = self.server.world.get_level(name) if name else None
            if target is None:
                return True
        local_x = world_x - cell_x * SEGMENT_PIXELS
        local_y = world_y - cell_y * SEGMENT_PIXELS
        item = await self.spawn_item(
            target, local_x / 16.0, local_y / 16.0, item_type)
        return item is not None

    def roll_bush_drop(self, config) -> int:
        """Server::rollBushItemDrop (Server.cpp:1438-1453): one uniform
        [1, 100] roll walked through the cumulative rates. -1 for nothing."""
        roll = self.rng.randint(1, 100)
        table = (config.bush_drops if isinstance(config, ServerConfig)
                 else DEFAULT_BUSH_DROPS)
        for item, rate in table:
            if roll <= rate:
                return item
            roll -= rate
        return -1

    async def drop_board_item(self, player: 'Player', loc_x: int, loc_y: int,
                              old_tile: int) -> None:
        """Items laid by cutting a bush or breaking a vase
        (PlayerClientPackets.cpp:143-166). `old_tile` is the tile as it stood
        before the cut."""
        level = player.level
        if level is None:
            return
        config = getattr(self.server, 'config', None)
        dropped = -1
        if old_tile in BUSH_TILES and _option(config, 'bushitems', True):
            dropped = self.roll_bush_drop(config)
        elif old_tile == VASE_TILE and _option(config, 'vasesdrop', True):
            dropped = int(LevelItemType.HEART)
        if dropped >= 0:
            await self.drop_item_at(level, loc_x * 2, loc_y * 2,
                                    LevelItemType(dropped))

    def _death_half_tile(self, tile_x: float, tile_y: float):
        """One drop position: the player's tile + (1.5, 2.0) + a uniform
        [-4, 4] tile displacement per axis, x drawn first
        (PlayerClient.cpp:1977-1978), truncated to a half tile
        (Extents.h:392-393)."""
        pos_x = tile_x + 1.5 + self.rng.randint(-4, 4)
        pos_y = tile_y + 2.0 + self.rng.randint(-4, 4)
        return int(pos_x * 2), int(pos_y * 2)

    async def drop_items_on_death(self, player: 'Player') -> None:
        """PlayerClient::dropItemsOnDeath (PlayerClient.cpp:1898-1998)."""
        config = getattr(self.server, 'config', None)
        if not _option(config, 'dropitemsdead', True):
            return
        level = player.level
        if level is None:
            return
        minimum = _option(config, 'mindeathgralats', 1)
        maximum = _option(config, 'maxdeathgralats', 50)
        # min > max is undefined behaviour in the reference's uniform
        # distribution (:1913); treat min as equal to max.
        minimum = min(minimum, maximum)
        allowed = (config.death_drop_items() if isinstance(config, ServerConfig)
                   else DEFAULT_DEATH_DROPS)

        gralats = max(0, int(player.rupees))
        drop_gralats = 0
        if maximum > 0:
            drop_gralats = min(self.rng.randint(minimum, maximum), gralats)
        drop_arrows = self.rng.randint(0, 3)
        drop_bombs = self.rng.randint(0, 3)
        if drop_arrows * 5 > player.arrows:
            drop_arrows = player.arrows // 5
        if drop_bombs * 5 > player.bombs:
            drop_bombs = player.bombs // 5
        if int(LevelItemType.DARTS) not in allowed:
            drop_arrows = 0
        if int(LevelItemType.BOMBS) not in allowed:
            drop_bombs = 0

        # Deducted before the type check below, so with no rupee type allowed
        # the gralats are simply lost (:1935-1946). Only the dying player hears
        # about the new counts (:1938).
        player.rupees = gralats - drop_gralats
        player.arrows -= drop_arrows * 5
        player.bombs -= drop_bombs * 5
        await player.send_props({
            PLPROP.RUPEESCOUNT: player.rupees,
            PLPROP.ARROWSCOUNT: player.arrows,
            PLPROP.BOMBSCOUNT: player.bombs,
        })
        gold, red, blue, green = (int(LevelItemType.GOLDRUPEE) in allowed,
                                  int(LevelItemType.REDRUPEE) in allowed,
                                  int(LevelItemType.BLUERUPEE) in allowed,
                                  int(LevelItemType.GREENRUPEE) in allowed)
        if not (gold or red or blue or green):
            drop_gralats = 0

        try:
            local_x, local_y = absolute_position(player)
            tile_x = int(float(local_x) * 16) / 16.0   # whole pixels (Character.h:55)
            tile_y = int(float(local_y) * 16) / 16.0
        except (TypeError, ValueError, OverflowError):
            tile_x = tile_y = 0.0

        # Greedy 100/30/5/1 split (:1950-1981). The reference loop runs once
        # per item; here it also stops after `max_items_per_level` attempts, so
        # an absurd maxdeathgralats cannot hold the event loop (the cap alone
        # would not stop drops that land outside any level segment).
        attempts = 0
        while drop_gralats != 0 and attempts < self.max_items_per_level:
            if gold and drop_gralats >= 100:
                drop_gralats -= 100
                item = LevelItemType.GOLDRUPEE
            elif red and drop_gralats >= 30:
                drop_gralats -= 30
                item = LevelItemType.REDRUPEE
            elif blue and drop_gralats >= 5:
                drop_gralats -= 5
                item = LevelItemType.BLUERUPEE
            else:
                if not green:
                    break
                drop_gralats -= 1
                item = LevelItemType.GREENRUPEE
            attempts += 1
            if not await self.drop_item_at(level, *self._death_half_tile(tile_x, tile_y), item):
                return

        for count, item in ((drop_arrows, LevelItemType.DARTS),
                            (drop_bombs, LevelItemType.BOMBS)):
            for _ in range(count):
                if not await self.drop_item_at(
                        level, *self._death_half_tile(tile_x, tile_y), item):
                    return

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

    def chest_packets(self, player: 'Player', level: 'Level') -> bytes:
        """The level's chests for an entry: an opened one as the 3-byte form,
        an unopened one with its item and sign (Level.cpp:142-154). Part of
        every static block, visited or not (PlayerClient.cpp:1505)."""
        out = bytearray()
        for chest in self.get_chests_on_level(level.name):
            opened = player.account_name in chest.opened_by
            out += build_level_chest(
                opened, chest.x, chest.y,
                chest.item_type.value, chest.sign_index)
        return bytes(out)

