"""Warps, player property updates and adjacent-level preloading."""

import logging
import time

from reborn_protocol import PLSTATUS

from ..attachments import absolute_position, set_attachment
from ..protocol.colors import normalize_colors
from ..protocol.constants import PLI, PLPROP
from ..protocol.packets import (
    PacketReader,
    build_other_player_props,
    parse_player_props,
)
from ..text_safety import sanitize_value, strip_control_chars
from .registry import handles

logger = logging.getLogger(__name__)

# Props relayed to the rest of the level, and the Player attribute each is read
# back from after being applied above. Position, chat and gear are relayed
# separately: position because classic X/Y and hi-res X2/Y2 both normalize onto
# self.x/self.y, chat because only a CHANGED value goes out, and gear because it
# is relayed as a (power, image) pair.
_RELAYED_PROPS = {
    PLPROP.CARRYSPRITE: 'carrysprite',
    PLPROP.CARRYNPC: 'npc_id',
    PLPROP.DIRECTION: 'direction',
    PLPROP.SPRITE: 'sprite',
    PLPROP.GANI: 'gani',
    PLPROP.HEADIMAGE: 'head_image',
    PLPROP.BODYIMAGE: 'body_image',
    PLPROP.COLORS: 'colors',
}


def resolve_nickname(account_name: str, requested: str) -> str:
    """Player::setNick for a client request (Player.cpp:891-960).

    A guild suffix needs the guild manager / list server to verify it, which
    this server has neither of, so it falls through to the unverified outcome:
    the suffix is dropped.
    """
    desired = strip_control_chars(requested).strip()
    guild_start = desired.find('(')
    nick = desired if guild_start < 0 else desired[:guild_start]
    if nick.startswith('*'):
        nick = nick[1:]
    nick = nick.strip() or 'unknown'
    return ('*' if nick == account_name else '') + nick


class MovementHandlers:
    """Mixin: PLI_LEVELWARP/LEVELWARPMOD/PLAYERPROPS/ADJACENTLEVEL."""

    @staticmethod
    def _read_warp(reader: PacketReader):
        """x and y in half tiles, then the level name (the rest), as
        msgPLI_LEVELWARP reads them (PlayerClientPackets.cpp:59-60)."""
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()
        return x, y, level_name

    @handles(PLI.LEVELWARP)
    async def _handle_level_warp(self, data: bytes):
        """PLI_LEVELWARP: no cached time, so a level the player has left this
        session keeps its board (PlayerClientPackets.cpp:54)."""
        x, y, level_name = self._read_warp(PacketReader(data))
        if level_name:
            await self.client_warp(level_name, x, y, None)

    @handles(PLI.LEVELWARPMOD)
    async def _handle_level_warp_mod(self, data: bytes):
        """PLI_LEVELWARPMOD: a GUInt5 mod time of the client's cached copy,
        then the PLI_LEVELWARP body (PlayerClientPackets.cpp:56-57)."""
        reader = PacketReader(data)
        cached_time = reader.read_gint5()
        x, y, level_name = self._read_warp(reader)
        if level_name:
            await self.client_warp(level_name, x, y, cached_time)

    @handles(PLI.PLAYERPROPS)
    async def _handle_player_props(self, data: bytes):
        """Handle PLI_PLAYERPROPS packet."""
        # Choke point for every client-settable string prop (nickname, chat,
        # gani, head/body/sword/shield image, ...): control characters are
        # stripped once here, before anything is stored, relayed or saved.
        props = {key: sanitize_value(value)
                 for key, value in parse_player_props(data).items()}
        pending_revive = getattr(self, 'pending_revive', None)

        # Capture the last absolute position before replacing any wire axes.
        absolute_position(self)
        if PLPROP.ATTACHNPC in props:
            kind, npc_id = props[PLPROP.ATTACHNPC]
            if npc_id <= 0 or kind == 0:
                set_attachment(self, props[PLPROP.ATTACHNPC])
            else:
                props.pop(PLPROP.ATTACHNPC)

        # Store NPC-relative axes while attached, level-local axes otherwise.
        if PLPROP.X2 in props:
            self.x = props[PLPROP.X2]
        elif PLPROP.X in props:
            self.x = props[PLPROP.X] / 2.0

        if PLPROP.Y2 in props:
            self.y = props[PLPROP.Y2]
        elif PLPROP.Y in props:
            self.y = props[PLPROP.Y] / 2.0
        absolute_position(self)

        # Crossing gmap cells: the client reports its new cell with
        # GMAPLEVELX/Y and sends no warp for a level of its map (FourPlay
        # TClient.cpp:1354-1356); the server records the cell left and sends
        # the new cell's dynamic data (level_entry.gmap_hop). A client warp
        # for such a hop is still accepted (client_warp).
        map_axes = [(prop, value) for prop, value in props.items()
                    if prop in (PLPROP.GMAPLEVELX, PLPROP.GMAPLEVELY)]
        if map_axes and self.level:
            await self.apply_client_map_position(map_axes)
            absolute_position(self)

        # PlayerProps.cpp:495-511: Z (tiles) then Z2 (pixels), unless locked.
        z_changed = False
        z_reverted = False
        if PLPROP.Z in props or PLPROP.Z2 in props:
            if getattr(self.server.config, 'lockplayerz', False):
                z_reverted = True
            else:
                if PLPROP.Z in props:
                    self.z = float(props[PLPROP.Z])
                if PLPROP.Z2 in props:
                    self.z = props[PLPROP.Z2]
                z_changed = True
                # PlayerProps.cpp:507-510: a height update un-pauses the
                # player and counts as movement.
                self.client_status = (getattr(self, 'client_status', 0)
                                      & ~PLSTATUS.PAUSED)
                self.last_movement_time = time.time()

        # PlayerProps.cpp:180-203: an unchanged nickname is ignored; a changed
        # one is normalised, echoed back to the sender if altered, and sent on.
        nickname_changed = False
        nickname_adjusted = False
        if PLPROP.NICKNAME in props and props[PLPROP.NICKNAME] != self.nickname:
            requested = props[PLPROP.NICKNAME]
            self.nickname = resolve_nickname(self.account_name, requested)
            nickname_adjusted = self.nickname != requested
            nickname_changed = True

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
            npc_manager = getattr(self.server, 'npc_manager', None)
            if npc_manager is not None and self.level:
                await npc_manager.on_player_gani(self)

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
            # Body colour slots. The parser read colors_wire_width() values
            # (5, or 8 under servermode newworld); the reference replaces the
            # whole stored 8-slot array with the freshly deserialised one, so
            # the slots the wire did not carry become 0 (PlayerProps.cpp:453-460,
            # PlayerProps.cpp:1191-1192, PropertySerializers.h:730-806).
            self.colors = normalize_colors(props[PLPROP.COLORS])

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

        if PLPROP.STATUS in props:
            old_status = getattr(self, 'client_status', 0)
            self.client_status = props[PLPROP.STATUS]
            if pending_revive and not self.client_status & PLSTATUS.DEAD:
                await self.server.combat_manager.revive_player(self)
            # PlayerProps.cpp:553: an unchanged status is ignored. Dying is the
            # not-DEAD -> DEAD transition of this prop (:574), not hearts at 0.
            if (self.client_status != old_status
                    and not old_status & PLSTATUS.DEAD
                    and self.client_status & PLSTATUS.DEAD
                    and getattr(self.server, 'combat_manager', None) is not None):
                await self.server.combat_manager.handle_status_death(self)

        # Relay the parts of the update other players can see.
        if self.level:
            broadcast_props = {}
            # Position: clients may send classic X/Y (15/16, half-tiles) OR
            # X2/Y2 (78/79) - keying the relay on X2/Y2 alone silently
            # dropped every movement update from classic-prop senders, so
            # other players saw them frozen at their spawn position. Relay
            # as X2/Y2 (self.x/y were normalized above) whichever arrived.
            # Preserve NPC-relative offsets verbatim while attached.
            if PLPROP.X in props or PLPROP.X2 in props:
                broadcast_props[PLPROP.X2] = self.x
            if PLPROP.Y in props or PLPROP.Y2 in props:
                broadcast_props[PLPROP.Y2] = self.y
            if PLPROP.ATTACHNPC in props:
                broadcast_props[PLPROP.ATTACHNPC] = props[PLPROP.ATTACHNPC]
            position_changed = (
                PLPROP.X in props or PLPROP.X2 in props
                or PLPROP.Y in props or PLPROP.Y2 in props
            )
            if position_changed:
                gmap_info = self.server.world.get_gmap_for_level(self.level.name)
                if gmap_info:
                    broadcast_props[PLPROP.GMAPLEVELX] = gmap_info[1]
                    broadcast_props[PLPROP.GMAPLEVELY] = gmap_info[2]
            for prop_id, attr in _RELAYED_PROPS.items():
                if prop_id in props:
                    broadcast_props[prop_id] = getattr(self, attr)
            if chat_changed:
                broadcast_props[PLPROP.CURCHAT] = self.chat
            if z_changed:
                broadcast_props[PLPROP.Z] = int(self.z)
                broadcast_props[PLPROP.Z2] = self.z
            # Gear goes out as the (power, image) pair so the image name is on
            # the wire; a bare power would only tell the other client which
            # preset sprite to guess at.
            if PLPROP.SWORDPOWER in props:
                broadcast_props[PLPROP.SWORDPOWER] = (self.sword_power, self.sword_image)
            if PLPROP.SHIELDPOWER in props:
                broadcast_props[PLPROP.SHIELDPOWER] = (self.shield_power, self.shield_image)

            if broadcast_props:
                packet = build_other_player_props(self.id, broadcast_props)
                await self.server.broadcast_to_world(
                    self.level.name, packet, exclude={self.id}
                )

        # A changed nickname goes to every player on the server, not just the
        # level (SetResults::sendToAll, PlayerProps.cpp:203).
        if nickname_changed:
            await self.server.broadcast_to_all(
                build_other_player_props(
                    self.id, {PLPROP.NICKNAME: self.nickname}),
                exclude={self.id})

        if self.level:
            # Fire GS1 playertouchsme for NPCs the player has walked onto
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.check_touches(self)

        # Corrections go back to the sender only (SETPROP_SENDBACK /
        # SETPROP_RETURN_REVERT, PlayerProps.cpp:149-171).
        corrections = {}
        if nickname_adjusted:
            corrections[PLPROP.NICKNAME] = self.nickname
        if z_reverted:
            corrections[PLPROP.Z] = int(self.z)
            corrections[PLPROP.Z2] = self.z
        if corrections:
            await self.send_props(corrections)

    def _adjacent_level(self, level_name: str):
        """Levels PLI_ADJACENTLEVEL may name: a sublevel of the player's own
        gmap, else a level the server already holds in memory. Never one that
        merely exists on disk (PlayerClientPackets.cpp:994-1014)."""
        world = self.server.world
        if self.level is not None:
            gmap_info = world.get_gmap_for_level(self.level.name)
            if gmap_info and gmap_info[0].find_level(level_name) is not None:
                return world.get_level(level_name)
        for cached in world.get_all_levels():
            if cached.name == level_name:
                return cached
        return None

    @handles(PLI.ADJACENTLEVEL)
    async def _handle_adjacent_level(self, data: bytes):
        """PLI_ADJACENTLEVEL: the client asks for a neighbour's static data,
        its cached mod time first (PlayerClientPackets.cpp:994-1016). The
        answer is the full static block with the visited rule; the request
        records nothing, the player stays put and no dynamic data goes out.
        Links and signs follow the reference's NPC-server rule
        (PlayerClient.cpp:1495-1500), as nothing here is an entry."""
        reader = PacketReader(data)
        cached_time = reader.read_gint5()
        level_name = reader.remaining().decode('latin-1', errors='replace')
        if not level_name:
            return
        level = self._adjacent_level(level_name)
        if not level:
            logger.debug(f"Adjacent level not served: {level_name}")
            return
        config = self.server.config
        await self.send_static_level_data(
            level, cached_time,
            links_and_signs=not getattr(config, 'serverside', True))
