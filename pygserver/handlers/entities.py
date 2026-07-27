"""Baddies, client-created level NPCs, and the player's weapon list."""

import logging
from pathlib import Path

from ..protocol.constants import PLI, BDPROP, BDMODE
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
        """Handle {x*2}{y*2}{type}{power in half-hearts}{image=rest}."""
        if not self.level:
            return

        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        baddy_type = reader.read_gchar()
        power = min(reader.read_gchar(), 12)
        image = reader.remaining().decode('latin-1', errors='replace')
        if image and not Path(image).suffix:
            image += ".gif"

        if hasattr(self.server, 'baddy_manager'):
            from ..baddy import BaddyType
            baddy = await self.server.baddy_manager.add_baddy(
                self.level, x, y, BaddyType(baddy_type),
                respawn_enabled=False,
                power=power,
                image=image,
            )

    @handles(PLI.PUTNPC)
    async def _handle_putnpc(self, data: bytes):
        """Create a level NPC from {image}{scriptfile}{x*2}{y*2}."""
        if not self.level or not getattr(self.server.config, 'putnpc_enabled', False):
            return

        reader = PacketReader(data)
        image = reader.read_gstring()
        scriptfile = reader.read_gstring()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        filesystem = getattr(self.server, 'filesystem', None)
        manager = getattr(self.server, 'npc_manager', None)
        script_path = filesystem._find_file(scriptfile) if filesystem else None
        if script_path is None or manager is None:
            return

        code = script_path.read_text(encoding='latin-1').replace('\r', '')
        npc = manager.create_npc(level=self.level, x=x, y=y)
        npc.image = image
        manager.attach_gs1(npc, code)
        await self.server.broadcast_to_level(
            self.level.name, npc.build_props_packet()
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
