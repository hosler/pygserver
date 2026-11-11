"""Ground items, chests and horses."""

import logging

from ..protocol.constants import PLI
from ..protocol.packets import PacketReader, build_item_del
from .registry import handles

logger = logging.getLogger(__name__)


class ItemHandlers:
    """Mixin: PLI_ITEM*/OPENCHEST and PLI_HORSEADD/HORSEDEL."""

    @handles(PLI.ITEMADD)
    async def _handle_item_add(self, data: bytes):
        """Handle PLI_ITEMADD packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        item_type = reader.read_gchar() if reader.remaining() else 0

        # PlayerClientPackets.cpp:352-354: with `disableitemdropping` the
        # sender's own item is taken back and nothing is spawned or relayed.
        # (Its NPC-server refund, :355-356, is not copied: the reference
        # re-adds an item it never removed there, and pygserver never deducts
        # the dropped item either.)
        if getattr(getattr(self.server, 'config', None),
                   'disableitemdropping', False) is True:
            await self.send_raw(build_item_del(x, y))
            return

        if hasattr(self.server, 'item_manager'):
            from ..protocol.constants import LevelItemType
            await self.server.item_manager.spawn_item(
                self.level, x, y, LevelItemType(item_type),
                exclude_player_id=self.id,
                layer=getattr(self, 'layer', 0),
            )

    @handles(PLI.ITEMDEL)
    async def _handle_item_del(self, data: bytes):
        """Handle PLI_ITEMDEL packet (PlayerClientPackets.cpp:385-403).

        The reference relays PLO_ITEMDEL to the other players of the level
        first (:389, sender excluded, whether or not the item is still on the
        ground), then removes the item at that position (:394) and stops when
        none is there (:395). A client sends this when its own timer for a
        level item runs out, so it is routinely late: the removal is silent
        and a missing item changes nothing.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        await self.server.broadcast_to_level(
            self.level.name, build_item_del(x, y), exclude={self.id})
        if hasattr(self.server, 'item_manager'):
            self.server.item_manager.remove_item_silently(self.level.name, x, y)

    @handles(PLI.ITEMTAKE)
    async def _handle_item_take(self, data: bytes):
        """Handle PLI_ITEMTAKE packet. The reference runs it through the
        PLI_ITEMDEL handler (PlayerClient.cpp:91): PLO_ITEMDEL goes to the
        level's other players first, sender excluded, whether or not the take
        then succeeds (PlayerClientPackets.cpp:385-403); only a take that
        finds the item grants it (:394-399). The taker is sent nothing: its
        client removed the item when it sent the take.

        Not in the reference: a take of an item that is still on the ground
        but that the age, layer or reach test refuses
        (ItemManager.take_relay_position) is ignored outright, with no relay,
        so a client cannot hide an item from everyone else while the server
        keeps it. The relay names the item the take resolved to."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        item_manager = getattr(self.server, 'item_manager', None)
        if item_manager is not None:
            position = item_manager.take_relay_position(self, x, y)
            if position is None:
                return
            x, y = position
        await self.server.broadcast_to_level(
            self.level.name, build_item_del(x, y), exclude={self.id})
        if item_manager is not None:
            await item_manager.handle_item_take(self, x, y)

    @handles(PLI.OPENCHEST)
    async def _handle_open_chest(self, data: bytes):
        """Handle PLI_OPENCHEST packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar()
        y = reader.read_gchar()

        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.handle_open_chest(self, x, y)

    @handles(PLI.HORSEADD)
    async def _handle_horse_add(self, data: bytes):
        """Handle PLI_HORSEADD packet.

        Wire format (GServer-v2 msgPLI_HORSEADD, PlayerClientPackets.cpp:
        256-269): {GCHAR x*2}{GCHAR y*2}{GCHAR dir_bushes}{RAW image}.
        dir_bushes packs direction in bits 0-1 and bush count in the rest of
        the byte; image is a raw trailing string with no length prefix.
        Previously this read direction/bushes as two separate gchars and a
        length-prefixed image, which doesn't match what real clients send.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        dir_bushes = reader.read_gchar() if reader.remaining() else 0x0E  # dir=2, bushes=3
        direction = dir_bushes & 0x03
        bushes = dir_bushes >> 2
        image = reader.remaining().decode('latin-1', errors='replace') if reader.remaining() else "horse.png"

        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_horse_add_packet(
                self, x, y, direction, bushes, image
            )

    @handles(PLI.HORSEDEL)
    async def _handle_horse_del(self, data: bytes):
        """Handle PLI_HORSEDEL packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_horse_del_packet(self, x, y)
