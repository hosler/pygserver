"""Ground items, chests and horses."""

import logging

from ..protocol.constants import PLI
from ..protocol.packets import PacketReader
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

        if hasattr(self.server, 'item_manager'):
            from ..protocol.constants import LevelItemType
            await self.server.item_manager.spawn_item(
                self.level, x, y, LevelItemType(item_type),
                exclude_player_id=self.id,
            )

    @handles(PLI.ITEMDEL)
    async def _handle_item_del(self, data: bytes):
        """Handle PLI_ITEMDEL packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.remove_item(self.level.name, x, y)

    @handles(PLI.ITEMTAKE)
    async def _handle_item_take(self, data: bytes):
        """Handle PLI_ITEMTAKE packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.handle_item_take(self, x, y)

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
