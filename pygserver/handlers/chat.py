"""Chat, private messages, player flags and triggeractions."""

import logging

from ..protocol.constants import PLI
from ..protocol.packets import (
    PacketReader,
    build_chat,
    build_private_message,
    build_trigger_action,
)
from .registry import handles

logger = logging.getLogger(__name__)


class CommunicationHandlers:
    """Mixin: PLI_TOALL/PRIVATEMESSAGE/SHOWIMG, PLI_FLAG*, PLI_TRIGGERACTION."""

    @handles(PLI.TOALL)
    async def _handle_chat(self, data: bytes):
        """Handle PLI_TOALL chat packet."""
        reader = PacketReader(data)
        # PLI_TOALL is gchar-length-prefixed then the raw message (client's
        # build_chat matches GServer-v2 Player::msgPLI_TOALL readString(
        # readGUChar())). Reading remaining() instead kept the length byte as
        # the message's first char — every relayed line gained a leading
        # chr(len+32) garbage character. (The QA chat test used a substring
        # match, so it never caught this; a playtester saw it immediately.)
        message = reader.read_gstring().strip()

        if not message or self.is_muted:
            return

        self.chat = message
        logger.info(f"[Chat] {self.nickname}: {message}")

        # Broadcast to level
        if self.level:
            packet = build_chat(self.id, message)
            await self.server.broadcast_to_level(self.level.name, packet)

            # Trigger NPC events
            await self.server.npc_manager.on_player_chats(self, message)

    @handles(PLI.PRIVATEMESSAGE)
    async def _handle_private_message(self, data: bytes):
        """Handle PLI_PRIVATEMESSAGE packet.

        Format: [gshort count][gshort player_id]*count[raw message].
        """
        if self.is_muted:
            return

        reader = PacketReader(data)
        count = reader.read_gshort()
        target_ids = [reader.read_gshort() for _ in range(count)]
        message = reader.remaining().decode('latin-1', errors='replace')
        is_mass = len(target_ids) > 1

        for target_id in target_ids:
            # ids >= 16000 are external pseudo-players (channels / channel
            # users, see irc.py) - GServer-v2 branches the same way
            # (Player.cpp:1639-1651, cross-server pmExternalPlayer).
            if target_id >= 16000:
                irc = getattr(self.server, 'irc_manager', None)
                if irc is not None:
                    await irc.route_external_pm(self, target_id, message)
                continue
            target = self.server.get_player(target_id)
            if target:
                packet = build_private_message(
                    self.id, self.nickname, message, is_mass=is_mass
                )
                await target.send_raw(packet)

    @handles(PLI.SHOWIMG)
    async def _handle_show_img(self, data: bytes):
        """Handle PLI_SHOWIMG packet (used here for level chat).

        The client parses PLO_SHOWIMG with the same layout as chat
        (gshort id + message), so relay it via build_chat.
        """
        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace')

        if self.level and not self.is_muted:
            packet = build_chat(self.id, message)
            await self.server.broadcast_to_level(self.level.name, packet)

    @handles(PLI.FLAGSET)
    async def _handle_flag_set(self, data: bytes):
        """Handle PLI_FLAGSET packet."""
        reader = PacketReader(data)
        flag_data = reader.remaining().decode('latin-1', errors='replace')
        if '=' not in flag_data:
            self.flags[flag_data.strip()] = True
            return
        name, value = flag_data.split('=', 1)
        name = name.strip()
        value = value.strip()
        if value:
            self.flags[name] = value
        else:
            self.flags.pop(name, None)

    @handles(PLI.FLAGDEL)
    async def _handle_flag_del(self, data: bytes):
        """Handle PLI_FLAGDEL packet."""
        flag_name = data.decode('latin-1', errors='replace').strip()
        self.flags.pop(flag_name, None)

    @handles(PLI.TRIGGERACTION)
    async def _handle_trigger_action(self, data: bytes):
        """Handle PLI_TRIGGERACTION packet.

        Wire format (GServer-v2 msgPLI_TRIGGERACTION, PlayerClientPackets.cpp):
            {GUINT3 npc_id}{GCHAR x*2}{GCHAR y*2}{action CSV}
        npc_id is a 3-byte GInt (readGUInt() == readGInt(), 3 bytes on the
        wire, not 4) - it must be consumed before x/y or every triggeraction
        parses garbage.
        """
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        action = reader.remaining().decode('latin-1', errors='replace').strip()

        logger.debug(f"Trigger action at ({x}, {y}): {action}")

        # Handle serverside triggers
        handled = False
        if action.startswith(("serverside", "servernpc")):
            handled = await self.server.handle_trigger_action(self, x, y, action)

        # Relay to other players on the level (GServer-v2 msgPLI_TRIGGERACTION:
        # sendPacketToOneLevelPart(..., { m_id }) when sendplayertriggers=true,
        # the default; excludes the sender).
        if self.level and not handled:
            packet = build_trigger_action(self.id, npc_id, x, y, action)
            await self.server.broadcast_to_level(self.level.name, packet, exclude={self.id})

        # Notify NPC manager
        if not handled:
            await self.server.npc_manager.on_trigger_action(self, x, y, action)
