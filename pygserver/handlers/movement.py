"""Warps, player property updates and adjacent-level preloading."""

import logging

from ..protocol.constants import PLI, PLO, PLPROP
from ..protocol.packets import (
    PacketReader,
    build_level_name,
    build_other_player_props,
    build_raw_data_announcement,
    parse_player_props,
)
from .registry import handles

logger = logging.getLogger(__name__)

# Props relayed to the rest of the level, and the Player attribute each is read
# back from after being applied above. Position, chat and gear are relayed
# separately: position because classic X/Y and hi-res X2/Y2 both normalize onto
# self.x/self.y, chat because only a CHANGED value goes out, and gear because it
# is relayed as a (power, image) pair.
_RELAYED_PROPS = {
    PLPROP.DIRECTION: 'direction',
    PLPROP.SPRITE: 'sprite',
    PLPROP.GANI: 'gani',
    PLPROP.HEADIMAGE: 'head_image',
    PLPROP.BODYIMAGE: 'body_image',
    PLPROP.COLORS: 'colors',
}


class MovementHandlers:
    """Mixin: PLI_LEVELWARP/LEVELWARPMOD/PLAYERPROPS/ADJACENTLEVEL."""

    @handles(PLI.LEVELWARP)
    async def _handle_level_warp(self, data: bytes):
        """Handle PLI_LEVELWARP packet."""
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()

        if level_name:
            await self.warp(level_name, x, y)

    @handles(PLI.LEVELWARPMOD)
    async def _handle_level_warp_mod(self, data: bytes):
        """Handle PLI_LEVELWARPMOD packet (modified warp).

        Wire format (GServer-v2 msgPLI_LEVELWARP, PlayerClientPackets.cpp:
        52-58): LEVELWARPMOD carries a leading GUINT5 modtime before the
        x/y/level body that plain LEVELWARP has. Without consuming it first,
        the 5 modtime bytes get read as x/y and the start of the level name,
        corrupting every modified warp.
        """
        reader = PacketReader(data)
        reader.read_gint5()  # modtime, unused
        await self._handle_level_warp(reader.remaining())

    @handles(PLI.PLAYERPROPS)
    async def _handle_player_props(self, data: bytes):
        """Handle PLI_PLAYERPROPS packet."""
        props = parse_player_props(data)

        # Update position
        if PLPROP.X2 in props:
            self.x = props[PLPROP.X2]
        elif PLPROP.X in props:
            self.x = props[PLPROP.X] / 2.0

        if PLPROP.Y2 in props:
            self.y = props[PLPROP.Y2]
        elif PLPROP.Y in props:
            self.y = props[PLPROP.Y] / 2.0

        if PLPROP.DIRECTION in props:
            self.direction = props[PLPROP.DIRECTION]

        if PLPROP.SPRITE in props:
            self.sprite = props[PLPROP.SPRITE]

        if PLPROP.CARRYSPRITE in props:
            self.carrysprite = props[PLPROP.CARRYSPRITE]

        if PLPROP.CARRYNPC in props:
            # Unconditional like CARRYSPRITE: 0 means the carried NPC was
            # released, so a stale id must not survive a drop-without-throw.
            self.npc_id = props[PLPROP.CARRYNPC]

        if PLPROP.GANI in props:
            self.gani = props[PLPROP.GANI]

        # Local level chat (PLPROP_CURCHAT, sent by Client.send_level_chat via
        # PLI_PLAYERPROPS) fires the GS1 "playerchats" NPC event, e.g. the
        # qa_tier3.nw fixture's unfreezeplayer-on-chat handler. An empty value
        # is a real update (the client clearing its chat bubble), so it must
        # reach self.chat and the relay below; only an unchanged value is
        # dropped, which is also how the reference server guards the event
        # (GServer-v2 PlayerProps.cpp:354-356 `chatChanged`).
        chat_changed = PLPROP.CURCHAT in props and props[PLPROP.CURCHAT] != self.chat
        if chat_changed:
            self.chat = props[PLPROP.CURCHAT]
            if self.level and getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.on_player_chats(self, self.chat)

        # Appearance updates
        if PLPROP.HEADIMAGE in props:
            self.head_image = props[PLPROP.HEADIMAGE]
        if PLPROP.BODYIMAGE in props:
            self.body_image = props[PLPROP.BODYIMAGE]
        if PLPROP.COLORS in props:
            # Body colour slots, parsed and then dropped like the gear below.
            # The reference server stores them verbatim at this point
            # (PlayerProps.cpp:380-388); the value clamp lives in the decoder.
            self.colors = list(props[PLPROP.COLORS])

        # Gear. SWORDPOWER/SHIELDPOWER carry a power and an image name in one
        # property; both were parsed and then dropped, so a mid-session sword or
        # shield change never reached the server state or any other client.
        # The power is clamped HERE rather than in the parser, mirroring
        # Player::setProp (PlayerProps.cpp:266-290): the wire form has to stay
        # faithful for the rest of the packet to stay aligned, but a client is
        # free to claim any power in it.
        config = self.server.config
        if PLPROP.SWORDPOWER in props:
            self.sword_power = config.apply_sword_power(props[PLPROP.SWORDPOWER])
            if props.get('sword_image'):
                self.sword_image = props['sword_image']
        if PLPROP.SHIELDPOWER in props:
            self.shield_power = config.apply_shield_power(props[PLPROP.SHIELDPOWER])
            if props.get('shield_image'):
                self.shield_image = props['shield_image']

        # Health: the client is authoritative for its own damage (e.g. baddies it
        # drives as leader), reporting new hearts via CURPOWER (= hearts * 2). A
        # transition to <= 0 means the player died, so kick off the death/respawn
        # flow once (it would otherwise never fire for client-side damage).
        if PLPROP.CURPOWER in props:
            new_hearts = props[PLPROP.CURPOWER] / 2.0
            was_alive = self.hearts > 0
            self.hearts = new_hearts
            if new_hearts <= 0 and was_alive and hasattr(self.server, 'combat_manager'):
                await self.server.combat_manager.handle_player_death(self)

        # Relay the parts of the update other players can see.
        if self.level:
            broadcast_props = {}
            # Position: clients may send classic X/Y (15/16, half-tiles) OR
            # X2/Y2 (78/79) - keying the relay on X2/Y2 alone silently
            # dropped every movement update from classic-prop senders, so
            # other players saw them frozen at their spawn position. Relay
            # as X2/Y2 (self.x/y were normalized above) whichever arrived.
            if PLPROP.X in props or PLPROP.X2 in props:
                broadcast_props[PLPROP.X2] = self.x
            if PLPROP.Y in props or PLPROP.Y2 in props:
                broadcast_props[PLPROP.Y2] = self.y
            for prop_id, attr in _RELAYED_PROPS.items():
                if prop_id in props:
                    broadcast_props[prop_id] = getattr(self, attr)
            if chat_changed:
                broadcast_props[PLPROP.CURCHAT] = self.chat
            # Gear goes out as the (power, image) pair so the image name is on
            # the wire; a bare power would only tell the other client which
            # preset sprite to guess at.
            if PLPROP.SWORDPOWER in props:
                broadcast_props[PLPROP.SWORDPOWER] = (self.sword_power, self.sword_image)
            if PLPROP.SHIELDPOWER in props:
                broadcast_props[PLPROP.SHIELDPOWER] = (self.shield_power, self.shield_image)

            if broadcast_props:
                packet = build_other_player_props(self.id, broadcast_props)
                await self.server.broadcast_to_level(
                    self.level.name, packet, exclude={self.id}
                )

            # Fire GS1 playertouchsme for NPCs the player has walked onto
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.check_touches(self)

    @handles(PLI.ADJACENTLEVEL)
    async def _handle_adjacent_level(self, data: bytes):
        """Handle PLI_ADJACENTLEVEL - client preloading a neighbouring GMAP
        segment. Send that level's name + board so the client can stitch the
        world together; no warp and no player-add (the player stays put)."""
        reader = PacketReader(data)
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()
        if not level_name:
            return
        level = self.server.world.get_level(level_name)
        if not level:
            logger.debug(f"Adjacent level not found: {level_name}")
            return

        # Only the board — adjacent segments are for rendering. Their signs/links
        # belong to that segment and are sent when the player actually warps in;
        # sending them here leaks e.g. neighbouring signs into the current level.
        level_name_pkt = build_level_name(level.name)
        tile_data = level.get_board_packet()
        board_packet = bytes([PLO.BOARDPACKET + 32]) + tile_data + b'\n'
        announcement = build_raw_data_announcement(len(board_packet))
        await self.send_raw(level_name_pkt + announcement + board_packet)
