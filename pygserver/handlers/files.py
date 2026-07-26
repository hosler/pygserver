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

    # The three script requests below all answer with compiled bytecode, so the
    # GS2 manager owns them outright; there is no text fallback to drop back to
    # (PLO_GANISCRIPT/PLO_NPCWEAPONSCRIPT/PLO_LOADSCRIPT payloads are bytecode,
    # and script text posted into one of them is parsed as a container header).

    @handles(PLI.UPDATEGANI)
    async def _handle_update_gani(self, data: bytes):
        """Handle PLI_UPDATEGANI packet: [gint5 crc32][gani name]."""
        reader = PacketReader(data)
        checksum = reader.read_gint5()
        name = reader.remaining().decode('latin-1', errors='replace')

        gs2 = getattr(self.server, 'gs2_manager', None)
        if gs2 is not None:
            await gs2.send_gani(self, name, checksum)

    @handles(PLI.UPDATESCRIPT)
    async def _handle_update_script(self, data: bytes):
        """Handle PLI_UPDATESCRIPT packet (payload is a weapon name)."""
        reader = PacketReader(data)
        name = reader.remaining().decode('latin-1', errors='replace')

        gs2 = getattr(self.server, 'gs2_manager', None)
        if gs2 is not None:
            await gs2.send_weapon_bytecode(self, name)

    @handles(PLI.UPDATECLASS)
    async def _handle_update_class(self, data: bytes):
        """Handle PLI_UPDATECLASS packet: [gint5 crc32][class name]."""
        reader = PacketReader(data)
        checksum = reader.read_gint5()
        classname = reader.remaining().decode('latin-1', errors='replace')

        gs2 = getattr(self.server, 'gs2_manager', None)
        if gs2 is not None:
            await gs2.send_class_bytecode(self, classname, checksum)
