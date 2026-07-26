"""File, gani, script and class requests."""

import logging

from ..protocol.constants import PLI
from ..protocol.packets import PacketReader
from .registry import handles

logger = logging.getLogger(__name__)


class FileHandlers:
    """Mixin: PLI_WANTFILE/UPDATEFILE/VERIFYWANTSEND/UPDATE{GANI,SCRIPT,CLASS}."""

    @handles(PLI.WANTFILE)
    async def _handle_want_file(self, data: bytes):
        """Handle PLI_WANTFILE packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_want_file(self, filename)

    @handles(PLI.UPDATEFILE)
    async def _handle_update_file(self, data: bytes):
        """Handle PLI_UPDATEFILE packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_want_file(self, filename)

    @handles(PLI.VERIFYWANTSEND)
    async def _handle_verify_want_send(self, data: bytes):
        """Handle PLI_VERIFYWANTSEND packet."""
        reader = PacketReader(data)
        checksum = reader.read_gint5()
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_verify_want_send(self, checksum, filename)

    @handles(PLI.UPDATEGANI)
    async def _handle_update_gani(self, data: bytes):
        """Handle PLI_UPDATEGANI packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_update_gani(self, filename)

    @handles(PLI.UPDATESCRIPT)
    async def _handle_update_script(self, data: bytes):
        """Handle PLI_UPDATESCRIPT packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_update_script(self, filename)

    @handles(PLI.UPDATECLASS)
    async def _handle_update_class(self, data: bytes):
        """Handle PLI_UPDATECLASS packet."""
        reader = PacketReader(data)
        classname = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_update_class(self, classname)
