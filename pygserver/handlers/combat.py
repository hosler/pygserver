"""Bombs, arrows, firespy, thrown objects, hurt reports and explosions."""

import logging

from ..protocol.constants import PLI
from ..protocol.packets import PacketReader
from .registry import handles

logger = logging.getLogger(__name__)


class CombatHandlers:
    """Mixin: the client-side combat packets."""

    @handles(PLI.BOMBADD)
    async def _handle_bomb_add(self, data: bytes):
        """Handle PLI_BOMBADD packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        # FourPlay TClient.cpp:1661-1680: retain the player-owner bit.
        # GServer-v2 msgPLI_BOMBADD relays the already compensated ticks.
        power = (reader.read_gchar() & 0x0f) if reader.remaining() else 1
        timer_ticks = reader.read_gchar() if reader.remaining() else 59

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_bomb_add(
                self, x, y, power, timer_ticks=timer_ticks)

    @handles(PLI.BOMBDEL)
    async def _handle_bomb_del(self, data: bytes):
        """Handle PLI_BOMBDEL packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_bomb_del(self, x, y)

    @handles(PLI.ARROWADD)
    async def _handle_arrow_add(self, data: bytes):
        """Handle PLI_ARROWADD packet.

        Wire format (GServer-v2 msgPLI_ARROWADD, PlayerClientPackets.cpp):
            {GCHAR x*2}{GCHAR y*2}{GCHAR flags}{GCHAR sprite}{GCHAR power}
        flags: bit0-1 direction, bit2 reflect, bit3 fromPlayer.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        flags = reader.read_gchar() if reader.remaining() else (int(self.direction) & 0x03)
        sprite = reader.read_gchar() if reader.remaining() else 0
        power = reader.read_gchar() if reader.remaining() else 1

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_arrow_add(self, x, y, flags, sprite, power)

    @handles(PLI.FIRESPY)
    async def _handle_fire_spy(self, data: bytes):
        """Handle PLI_FIRESPY packet (fire from wand)."""
        if not self.level:
            return
        reader = PacketReader(data)
        # PlayerClientPackets.cpp:314: low three bits are power, rest length.
        length_power = reader.read_gchar()

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_fire_spy(self, length_power)

    @handles(PLI.THROWCARRIED)
    async def _handle_throw_carried(self, data: bytes):
        """Handle the payload-less PLI_THROWCARRIED packet."""
        if not self.level:
            return

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_throw_carried(
                self, direction=self.direction, carrysprite=self.carrysprite
            )

    @handles(PLI.HURTPLAYER)
    async def _handle_hurt_player(self, data: bytes):
        """Handle PLI_HURTPLAYER packet.

        Wire format (GServer-v2 msgPLI_HURTPLAYER, PlayerClientPackets.cpp:
        811-820):
        - victim_id (gshort)
        - hurt_dx (SIGNED gchar) - knockback X direction
        - hurt_dy (SIGNED gchar) - knockback Y direction
        - power (gchar) - damage amount
        - npc_id (gint3) - optional
        hurt_dx/hurt_dy must use the signed reader: the unsigned read_gchar()
        clamps negative values to 0, silently dropping all left/up knockback.
        """
        reader = PacketReader(data)
        target_id = reader.read_gshort()
        hurt_dx = reader.read_gchar_signed()
        hurt_dy = reader.read_gchar_signed()
        power = reader.read_gchar() if reader.remaining() else 1
        # npc_id = reader.read_gint3() if reader.remaining() else 0  # Not used yet

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_hurt_player(self, target_id, power, hurt_dx, hurt_dy)

    @handles(PLI.EXPLOSION)
    async def _handle_explosion(self, data: bytes):
        """Handle PLI_EXPLOSION packet.

        Wire format (GServer-v2 msgPLI_EXPLOSION, PlayerClientPackets.cpp:
        829-844): {GCHAR radius}{GCHAR x*2}{GCHAR y*2}{GCHAR power_flags}.
        Radius comes first and is a raw byte, not a half-tile value.

        GServer-v2 msgPLI_EXPLOSION prefixes the sender id on relay;
        FourPlay TClient.cpp:5461-5473 reads that id before radius/x/y/flags.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        radius = reader.read_gchar() if reader.remaining() else 4
        from ..attachments import absolute_position
        px, py = absolute_position(self)
        x = reader.read_gchar() / 2.0 if reader.remaining() else px
        y = reader.read_gchar() / 2.0 if reader.remaining() else py
        power = reader.read_gchar() if reader.remaining() else 2

        manager = getattr(self.server, 'combat_manager', None)
        if manager is not None and await manager.handle_held_bomb_explosion(
                self, x, y, radius, power):
            return

        # Broadcast explosion effect
        from ..protocol.packets import build_explosion
        packet = build_explosion(self.id, x, y, radius, power)
        await self.server.broadcast_to_level(
            self.level.name, packet, exclude={self.id}
        )
        if manager is not None:
            await manager.blast_level_objects(self.level, x, y, radius, power, self.id)

    @handles(PLI.HITOBJECTS)
    async def _handle_hit_objects(self, data: bytes):
        """Handle PLI_HITOBJECTS packet (client-detected sword hit at a
        probe location - the real server-side "sword swing hit something"
        report; see combat.handle_hit_objects for what happens with it).

        Wire format (GServer-v2 msgPLI_HITOBJECTS, PlayerClientPackets.cpp:
        1017-1026): {GCHAR power*2}{GCHAR x*2}{GCHAR y*2}[{GINT3 npc_id}].
        Previously this read x,y,power (wrong order/scale) and then looped
        reading gint3 "object ids" that the real client never sends -
        PLI_HITOBJECTS carries a single probe location and an optional
        trailing npc_id, not a pre-resolved list of hit object ids; the
        server is the one that determines what's actually at that location.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        power = reader.read_gchar() / 2.0
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        npc_id = reader.read_gint3() if reader.remaining() else None

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_hit_objects(self, x, y, power, npc_id)

    @handles(PLI.SHOOT)
    async def _handle_shoot(self, data: bytes):
        """Handle PLI_SHOOT packet."""
        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_shoot(self, data)

    @handles(PLI.SHOOT2)
    async def _handle_shoot2(self, data: bytes):
        """Handle PLI_SHOOT2 packet."""
        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_shoot2(self, data)
