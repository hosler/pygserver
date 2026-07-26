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
        # {GCHAR player_power}{GCHAR timer}: power is bits 0-1, timer is
        # 50ms increments (+50ms) - see GServer-v2 msgPLI_BOMBADD
        power = (reader.read_gchar() & 0x03) if reader.remaining() else 1
        time_left = (reader.read_gchar() * 0.05 + 0.05) if reader.remaining() else 3.0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_bomb_add(self, x, y, power, time_left)

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
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_fire_spy(self, x, y)

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
        829-844): {GCHAR radius}{GCHAR x*2}{GCHAR y*2}{GCHAR power} - radius
        comes first and is a raw byte (not a half-tile value). Previously
        this read x/y before radius/power (wrong field order) and had no
        `self.level` guard, so it raised AttributeError for any player not
        currently on a level, and it broadcast back to the sender too.

        The PLO_EXPLOSION relay GServer actually sends also prepends a
        (short) owner id that build_explosion() here does not write; leave
        that as-is since pyReborn's parser (pyReborn/pyreborn/packets.py
        parse_explosion) expects [x][y][radius][power] with no owner id, and
        the client side is owned by another team.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        radius = reader.read_gchar() if reader.remaining() else 4
        x = reader.read_gchar() / 2.0 if reader.remaining() else self.x
        y = reader.read_gchar() / 2.0 if reader.remaining() else self.y
        power = reader.read_gchar() if reader.remaining() else 2

        # Broadcast explosion effect
        from ..protocol.packets import build_explosion
        packet = build_explosion(x, y, radius, power)
        await self.server.broadcast_to_level(
            self.level.name, packet, exclude={self.id}
        )

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
