"""Baddies, the player's weapon list, and the NPC packets we deliberately drop.

PLI_NPCPROPS (3), PLI_PUTNPC (21) and PLI_NPCDEL (22) are NOT registered here.
GServer-v2 refuses all three outright when the server has an NPC server of its
own - each handler's first statement is `if (m_server->hasNPCServer()) return;`
(PlayerClientPackets.cpp:191-193, 755-757, 784-786), because a client must not
be able to edit NPCs the server's own scripting engine owns. pygserver always
runs its NPCManager, so the reference behaviour is "never accept these", and
they are left unregistered rather than kept as handlers that parse a packet and
fall through to `pass` - which made the dispatch table claim coverage it did not
have.
"""

import logging

from ..protocol.constants import PLI, PLPERM, BDPROP, BDMODE
from ..protocol.packets import PacketReader, build_baddy_props
from .registry import handles

logger = logging.getLogger(__name__)


class EntityHandlers:
    """Mixin: PLI_BADDY* and the weapon add/delete packets."""

    @handles(PLI.BADDYPROPS)
    async def _handle_baddy_props(self, data: bytes):
        """Handle PLI_BADDYPROPS packet."""
        if not self.level or not self.level.is_player_leader(self):
            return

        reader = PacketReader(data)
        baddy_id = reader.read_gchar()
        props = {}
        while reader.has_data():
            prop_id = reader.read_gchar()
            if prop_id == BDPROP.ID:
                props[prop_id] = reader.read_gchar()
            elif prop_id in (BDPROP.X, BDPROP.Y):
                props[prop_id] = reader.read_gchar() / 2.0
            elif prop_id == BDPROP.TYPE:
                props[prop_id] = reader.read_gchar()
            elif prop_id == BDPROP.POWERIMAGE:
                props[prop_id] = (reader.read_gchar(), reader.read_gstring())
            elif prop_id == BDPROP.MODE:
                props[prop_id] = reader.read_gchar()
            elif prop_id in (BDPROP.ANI, BDPROP.DIR):
                props[prop_id] = reader.read_gchar()
            elif prop_id in (BDPROP.VERSESIGHT, BDPROP.VERSEHURT,
                              BDPROP.VERSEATTACK):
                props[prop_id] = reader.read_gstring()
            else:
                break

        manager = getattr(self.server, 'baddy_manager', None)
        baddy = manager.get_baddy(self.level.name, baddy_id) if manager else None
        if not baddy:
            return

        if BDPROP.POWERIMAGE in props:
            baddy.health = props[BDPROP.POWERIMAGE][0]
        if props.get(BDPROP.MODE) == BDMODE.DEAD:
            await manager.handle_baddy_death(baddy, self, exclude={self.id})
        else:
            await self.server.broadcast_to_level(
                self.level.name, build_baddy_props(baddy_id, props),
                exclude={self.id},
            )

    @handles(PLI.BADDYHURT)
    async def _handle_baddy_hurt(self, data: bytes):
        """Handle PLI_BADDYHURT packet.

        Wire format (GServer-v2 msgPLI_BADDYHURT, PlayerClientPackets.cpp:
        523-539, commit e0cd07af9bb4be09c54c0335f222dd0eacb71c1):
            [GUChar baddyId][GChar hurtDX][GChar hurtDY][GUChar damage,
            half-hearts]
        hurtDX/hurtDY are commented there as "midpoint: 64" - the same
        recentering idiom as read_gchar_signed() (byte - 32) with an extra
        -64 on top, i.e. value = read_gchar_signed() - 64 (mirrors GServer's
        PropertyHurtDxDy<MidPoint>::deserialize: dx = readGChar() - MidPoint).
        GServer-v2 itself treats these as a client-trust artifact and never
        parses them server-side (it just relays the raw packet to the
        baddy's leader) - pygserver is authoritative for baddy damage
        (BaddyManager.handle_baddy_hurt already computes its own knockback
        direction from baddy/player position), so hurt_dx/hurt_dy are parsed
        here and intentionally dropped rather than fed into knockback.

        Backward tolerance: older pyReborn builds sent the legacy 2-field
        [baddy_id][damage] payload with no knockback fields - fall back to
        that when the packet is too short for the 4-field format.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        if len(data) >= 4:
            baddy_id = reader.read_gchar()
            hurt_dx = reader.read_gchar_signed() - 64  # noqa: F841 (parsed, unused - see docstring)
            hurt_dy = reader.read_gchar_signed() - 64  # noqa: F841
            damage = reader.read_gchar()
        else:
            logger.debug(
                f"PLI_BADDYHURT: {len(data)}-byte packet too short for the "
                "4-field format, falling back to legacy [id][damage]"
            )
            baddy_id = reader.read_gchar()
            damage = reader.read_gchar() if reader.remaining() else 1

        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.handle_baddy_hurt(self, baddy_id, damage)

    @handles(PLI.BADDYADD)
    async def _handle_baddy_add(self, data: bytes):
        """Handle PLI_BADDYADD packet (admin adding baddy)."""
        if not self.level:
            return
        if not self.admin_rights & PLPERM.SETATTRIBUTES:
            return

        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        baddy_type = reader.read_gchar() if reader.remaining() else 0

        if hasattr(self.server, 'baddy_manager'):
            from ..baddy import BaddyType
            await self.server.baddy_manager.add_baddy(
                self.level, x, y, BaddyType(baddy_type)
            )

    @handles(PLI.NPCWEAPONDEL)
    async def _handle_npc_weapon_del(self, data: bytes):
        """Handle PLI_NPCWEAPONDEL packet."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        if weapon_name in self.weapons:
            self.weapons.remove(weapon_name)

    @handles(PLI.WEAPONADD)
    async def _handle_weapon_add(self, data: bytes):
        """Handle PLI_WEAPONADD packet (client requesting to add weapon)."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        if weapon_name and weapon_name not in self.weapons:
            self.weapons.append(weapon_name)
