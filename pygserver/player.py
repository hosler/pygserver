"""
pygserver.player - Player connection and state management

Player is the façade one connected client is reached through. The pieces behind
it live in their own modules:

- `player_session.PlayerSession` - socket, codec, framing buffer (transport);
- `player_state` - Identity/Character/Inventory/Status state holders, exposed
  under their historical flat names via `_STATE_ALIASES` below;
- `player_login.LoginService` - the login handshake and the single idempotent
  login-completion path both verification flows converge on;
- `handlers/` - the PLI packet handlers, as domain mixins whose `@handles`
  decorations build the dispatch table.

What stays here: level membership and transitions (`warp`, `_send_level`), the
connection lifecycle (`run`, `_cleanup`), the send helpers and the small
player-data accessors the managers and NPC scripts use.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, Set, Tuple

from .handlers import (
    CombatHandlers,
    CommunicationHandlers,
    EntityHandlers,
    FileHandlers,
    ItemHandlers,
    MiscHandlers,
    MovementHandlers,
    collect_handler_names,
)
from .player_login import LoginService
from .player_session import PlayerSession
from .player_state import Character, Identity, Inventory, Status
from .protocol.constants import PLO, PLPROP
from .protocol.packets import (
    PacketBuilder,
    build_player_props,
    build_other_player_props,
    build_warp,
    build_warp2,
    build_player_left,
    build_level_name,
    build_raw_data_announcement,
    build_is_leader,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .level import Level

logger = logging.getLogger(__name__)


class Player(MovementHandlers, CombatHandlers, ItemHandlers, EntityHandlers,
             CommunicationHandlers, FileHandlers, MiscHandlers):
    """
    Represents a connected player.

    Handles connection I/O, login, packet dispatch, and player state.
    Implements all PLI (Player Input) packet handlers.
    """

    def __init__(self, server: 'GameServer', player_id: int,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.id = player_id

        # Transport, state and login (see the module docstring). Every field on
        # the state components is also reachable under its historical flat name
        # on the player itself - _STATE_ALIASES installs those properties.
        self.session = PlayerSession(reader, writer)
        self.identity = Identity()
        self.character = Character()
        self.inventory = Inventory()
        self.status = Status()
        self.login_service = LoginService(self)

        # Current level
        self.level: Optional['Level'] = None

        # PLI packet id -> bound handler, from the mixins' @handles decorations.
        self._handlers: Dict[int, Any] = {
            packet_id: getattr(self, name)
            for packet_id, name in _HANDLER_NAMES.items()
        }

        # Packet ids that reached neither a handler nor the RC/NC ranges. Kept
        # so "the server ignored that" is visible instead of silent.
        self._unhandled_packet_ids: Set[int] = set()

    # =========================================================================
    # Connection lifecycle
    # =========================================================================

    async def run(self):
        """Main player loop - handle packets until disconnect."""
        try:
            # Wait for login packet
            if not await self._handle_login():
                return

            # Main packet loop
            while self.connected:
                try:
                    data = await self.session.read(timeout=300.0)  # 5 minutes
                except asyncio.TimeoutError:
                    logger.info(f"Player {self.id} timed out")
                    break

                if not data:
                    break

                self.last_packet_time = time.time()
                await self._process_data(data)

        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"Player {self.id} error: {e}")
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Clean up player resources on disconnect."""
        self.connected = False

        # Leave current level. The leave broadcast itself is left to
        # GameServer._remove_player (server.py), which runs right after this
        # in the connection's finally block - broadcasting it here too sent
        # every disconnect as two duplicate PLO_OTHERPLPROPS leave packets.
        if self.level:
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.on_player_leaves(self, self.level)
            self.level.remove_player(self)
            if (not self.server.world.get_gmap_for_level(self.level.name)
                    and self.level.get_leader_id() is not None):
                new_leader = self.server.get_player(self.level.get_leader_id())
                if new_leader:
                    await new_leader.send_raw(build_is_leader())

        # Dismount horse if mounted
        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_dismount(self)

        # Save account
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(self.account_name)
            if account:
                self.server.account_manager.save_player_to_account(self, account)

        # Unregister RC/NC sessions
        if hasattr(self.server, 'rc_manager'):
            self.server.rc_manager.unregister_session(self.id)
        if hasattr(self.server, 'nc_manager'):
            self.server.nc_manager.unregister_session(self.id)

    async def disconnect(self, message: str = ""):
        """Disconnect the player."""
        if message:
            from .protocol.packets import build_disc_message
            try:
                packet = build_disc_message(message)
                await self.send_raw(packet)
            except Exception:
                pass

        await self.session.close()

    # =========================================================================
    # Login (see player_login.LoginService)
    # =========================================================================

    async def _handle_login(self) -> bool:
        """Handle the initial login packet."""
        return await self.login_service.handle_login()

    async def send_login(self):
        """Finish login. Called by ServerListClient once it has verified us."""
        await self.login_service.complete_login()

    async def _send_login_response(self):
        """Send the login response packet."""
        await self.login_service.send_login_response()

    # =========================================================================
    # Packet dispatch
    # =========================================================================

    async def _process_data(self, data: bytes):
        """Process received data."""
        self._buffer.add_data(data)

        for packet_data in self._buffer.get_packets():
            decrypted = self._codec.decode_packet(packet_data)
            if not decrypted:
                continue

            await self._handle_packets(decrypted)

    async def _handle_packets(self, data: bytes):
        """Handle decoded packet data (may contain multiple packets)."""
        pos = 0
        while pos < len(data):
            # Find newline
            newline = data.find(b'\n', pos)
            if newline == -1:
                break

            packet_bytes = data[pos:newline]
            pos = newline + 1

            if len(packet_bytes) < 1:
                continue

            # Extract packet ID
            packet_id = packet_bytes[0] - 32
            packet_body = packet_bytes[1:] if len(packet_bytes) > 1 else b""

            # Dispatch to a registered game-client handler first. The RC/NC
            # packet id ranges below overlap ordinary PLI ids (e.g.
            # PLI_PROFILEGET=80/PLI_PROFILESET=81 both fall inside 51-98), so
            # checking the ranges first hijacked those packets and made the
            # registered _handle_profile_get/_handle_profile_set handlers
            # unreachable for game clients.
            handler = self._handlers.get(packet_id)
            if handler:
                try:
                    await handler(packet_body)
                except Exception as e:
                    logger.error(f"Packet handler error (id={packet_id}): {e}")
                continue

            # Check for RC/NC packets
            if packet_id >= 51 and packet_id <= 98:
                # RC packet
                if hasattr(self.server, 'rc_manager'):
                    await self.server.rc_manager.handle_packet(self, packet_id, packet_body)
                continue
            elif packet_id >= 103 and packet_id <= 119:
                # NC packet
                if hasattr(self.server, 'nc_manager'):
                    await self.server.nc_manager.handle_packet(self, packet_id, packet_body)
                continue
            elif packet_id in [150, 151]:
                # NC level list packets
                if hasattr(self.server, 'nc_manager'):
                    await self.server.nc_manager.handle_packet(self, packet_id, packet_body)
                continue

            if packet_id not in self._unhandled_packet_ids:
                self._unhandled_packet_ids.add(packet_id)
                logger.debug(
                    f"Player {self.id}: no handler for packet id {packet_id}")

    # =========================================================================
    # Level transitions
    # =========================================================================

    async def warp(self, level_name: str, x: float, y: float):
        """Warp player to a level."""
        logger.info(f"Player {self.id} warping to {level_name} at ({x}, {y})")

        # Find or load the destination FIRST: a bad/nonexistent level name
        # must not detach the player from their current level (this used to
        # remove_player + broadcast leave before validating, stranding the
        # player in limbo server-side while the client kept playing).
        level = self.server.world.get_level(level_name)
        if not level:
            logger.warning(f"Level not found: {level_name}")
            if self.level:
                # Snap the (possibly optimistic) client back to where it is:
                # warp packet for position, full level re-send so the client
                # (which may have already reset its local level state for the
                # bogus name) recovers name/board/entities.
                await self.send_raw(build_warp(self.x, self.y, self.level.name))
                await self._send_level(self.level)
            return

        old_level = self.level

        # Update level membership FIRST and synchronously - no `await` between
        # detaching from the old level and attaching to the new one. NPC/chest
        # broadcasts are scoped by reading player.level live (see
        # broadcast_to_level/get_players_on_level), so if self.level only
        # flipped after the awaited leave-notification/broadcast below, a
        # concurrently-scheduled coroutine (an NPC's dirty-tick push, another
        # player's packet handler) could still see this player attached to
        # the old level during that gap and send it the old level's NPC/chest
        # props. The client, having already optimistically switched its own
        # current-level tracking the moment it sent PLI_LEVELWARP, then tags
        # those late old-level packets as belonging to the new level - the
        # start level's villagers/chests bleeding into whatever the player
        # warped to.
        if old_level:
            old_level.remove_player(self)
            if (not self.server.world.get_gmap_for_level(old_level.name)
                    and old_level.get_leader_id() is not None):
                new_leader = self.server.get_player(old_level.get_leader_id())
                if new_leader:
                    await new_leader.send_raw(build_is_leader())
        self.x = x
        self.y = y
        self.level = level
        level.add_player(self)

        # Leave current level. GS1's "playerleaves" event previously only
        # fired on disconnect (_cleanup), never on a normal warp to another
        # level, so NPCs never saw a player leave via warp.
        if old_level:
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.on_player_leaves(self, old_level)
            await self.server.broadcast_to_level(
                old_level.name, build_player_left(self.id), exclude={self.id}
            )

        # Handle horse warp
        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_player_warp(self, old_level, level)

        # Send level data
        await self._send_level(level)

        # Notify NPCs
        await self.server.npc_manager.on_player_enters(self, level)

    async def _send_level(self, level: 'Level'):
        """Send level data to player."""
        logger.info(f"Sending level {level.name} to player {self.id}")

        # Build packets
        level_name_pkt = build_level_name(level.name)

        # Board data format: [packet_id + 32] + [8192 tile bytes] + [\n]
        tile_data = level.get_board_packet()  # 8192 bytes
        board_packet = bytes([PLO.BOARDPACKET + 32]) + tile_data + b'\n'

        # Announce raw data size (1 + 8192 + 1 = 8194)
        announcement = build_raw_data_announcement(len(board_packet))

        # In a GMAP, warp via PLO_PLAYERWARP2, which carries LOCAL coords plus the
        # segment's grid (gmap_x/gmap_y) separately. PLO_PLAYERWARP packs the
        # position into a single gchar (max ~111 tiles), so world coords for grid
        # cell 2+ (x >= 128) overflow and the player lands at the wrong spot; the
        # client recombines local + grid*64 itself.
        gmap_info = self.server.world.get_gmap_for_level(level.name)
        if gmap_info:
            _, gx, gy = gmap_info
            warp_packet = build_warp2(self.x, self.y, level.name, gx, gy)
        else:
            warp_packet = build_warp(self.x, self.y, level.name)

        combined = level_name_pkt + announcement + board_packet + warp_packet
        await self.send_raw(combined)

        # If this level is a GMAP segment, announce the .gmap name. That makes the
        # client request the gmap file (PLI_WANTFILE), build the grid, enter gmap
        # mode and request adjacent segments — without it the client treats the
        # segment as a standalone level (no stitching, broken edge warps).
        if gmap_info:
            gmap = gmap_info[0]
            # The client keys gmap handling off the ".gmap" suffix; gmap.name is
            # the bare stem ("chicken"), so announce the full filename.
            gmap_file = gmap.name if gmap.name.endswith('.gmap') else gmap.name + '.gmap'
            await self.send_raw(build_level_name(gmap_file))

        # Send signs and links on level
        from .protocol.packets import build_level_sign, build_level_link
        for (sx, sy), text in level.get_signs().items():
            await self.send_raw(build_level_sign(sx, sy, text))
        for link in level.get_links():
            await self.send_raw(build_level_link(
                link['dest_level'], link['x'], link['y'],
                link['width'], link['height'], link['dest_x'], link['dest_y'],
            ))

        # Send NPCs on level
        for npc in level.get_npcs():
            await self.send_raw(npc.build_props_packet())
            showimgs = npc.build_showimgs_packet()
            if showimgs is not None:
                await self.send_raw(showimgs)

        # Send items on level
        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.send_level_items(self, level)

        # Send baddies on level
        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.send_level_baddies(self, level)

        # Send horses on level
        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.send_level_horses(self, level)

        if level.is_player_leader(self) or gmap_info:
            await self.send_raw(build_is_leader())

        # Exchange props with everyone else already on the level (see
        # audience.Audience for the one definition of who that is).
        for other in self.server.audience.players_on_level(
                level.name, exclude={self.id}):
            await self.send_raw(other.build_props_packet())
            await other.send_raw(self.build_props_packet())

    # =========================================================================
    # Sending
    # =========================================================================

    async def send_raw(self, data: bytes):
        """Send raw packet data (will be encoded)."""
        await self.session.send(data)

    async def send_packet(self, packet_id: int, data: bytes = b""):
        """Send a packet with given ID and data."""
        packet = PacketBuilder().write_gchar(packet_id).write_bytes(data).write_byte(ord('\n')).build()
        await self.send_raw(packet)

    async def send_props(self, props: dict):
        """Send player props to this player (PLO_PLAYERPROPS)."""
        packet = build_player_props(props)
        await self.send_raw(packet)

    def build_props_packet(self) -> bytes:
        """Build PLO_OTHERPLPROPS packet for this player."""
        props = {
            PLPROP.NICKNAME: self.nickname,
            PLPROP.X2: self.x,
            PLPROP.Y2: self.y,
            PLPROP.DIRECTION: self.direction,
            PLPROP.SPRITE: self.sprite,
            PLPROP.GANI: self.gani,
            # (power, image): the biased form is the only one that carries the
            # gear's image name, which is what the other client renders.
            PLPROP.SWORDPOWER: (self.sword_power, self.sword_image),
            PLPROP.SHIELDPOWER: (self.shield_power, self.shield_image),
            PLPROP.HEADIMAGE: self.head_image,
            PLPROP.BODYIMAGE: self.body_image,
            PLPROP.CURLEVEL: self.level.name if self.level else "",
            PLPROP.COLORS: self.colors,
            PLPROP.MAGICPOINTS: self.mp,
            PLPROP.ALIGNMENT: self.ap,
        }
        return build_other_player_props(self.id, props)

    def build_leave_packet(self) -> bytes:
        """Build PLO_PLAYERLEFT packet."""
        return build_player_left(self.id)

    # =========================================================================
    # Player data accessors (used by the managers and NPC scripts)
    # =========================================================================

    def get_flag(self, name: str) -> str:
        """Get player flag value."""
        return self.flags.get(name, "")

    def set_flag(self, name: str, value: str):
        """Set player flag value."""
        self.flags[name] = value

    def has_weapon(self, name: str) -> bool:
        """Check if player has a weapon."""
        return name in self.weapons

    def add_weapon(self, name: str):
        """Add a weapon to player."""
        if name not in self.weapons:
            self.weapons.append(name)

    def remove_weapon(self, name: str):
        """Remove a weapon from player."""
        if name in self.weapons:
            self.weapons.remove(name)


# =============================================================================
# State-component aliases
#
# Player attribute name -> (component attribute on Player, field on it). The
# state lives on the components in player_state.py / player_session.py; each
# entry below installs a get/set property so every historical flat name still
# works - server.py, every manager, the GS1 host, account persistence and the
# test suite read and write these directly. Adding state means adding it to a
# component AND listing it here.
# =============================================================================

_STATE_ALIASES: Dict[str, Tuple[str, str]] = {
    # --- transport ----------------------------------------------------------
    'connected': ('session', 'connected'),
    '_codec': ('session', 'codec'),
    '_buffer': ('session', 'buffer'),
    '_reader': ('session', 'reader'),
    '_writer': ('session', 'writer'),

    # --- identity -----------------------------------------------------------
    'account_name': ('identity', 'account_name'),
    'nickname': ('identity', 'nickname'),
    'guild_name': ('identity', 'guild_name'),
    'guild_nickname': ('identity', 'guild_nickname'),
    'connection_type': ('identity', 'connection_type'),

    # --- character ----------------------------------------------------------
    'x': ('character', 'x'),
    'y': ('character', 'y'),
    'direction': ('character', 'direction'),
    'carrysprite': ('character', 'carrysprite'),
    'npc_id': ('character', 'npc_id'),
    'hearts': ('character', 'hearts'),
    'max_hearts': ('character', 'max_hearts'),
    'rupees': ('character', 'rupees'),
    'arrows': ('character', 'arrows'),
    'bombs': ('character', 'bombs'),
    'glove_power': ('character', 'glove_power'),
    'sword_power': ('character', 'sword_power'),
    'shield_power': ('character', 'shield_power'),
    'kills': ('character', 'kills'),
    'deaths': ('character', 'deaths'),
    'head_image': ('character', 'head_image'),
    'body_image': ('character', 'body_image'),
    'sword_image': ('character', 'sword_image'),
    'shield_image': ('character', 'shield_image'),
    'colors': ('character', 'colors'),
    'mp': ('character', 'mp'),
    'ap': ('character', 'ap'),
    'gani': ('character', 'gani'),
    'sprite': ('character', 'sprite'),
    'chat': ('character', 'chat'),

    # --- inventory ----------------------------------------------------------
    'weapons': ('inventory', 'weapons'),
    'flags': ('inventory', 'flags'),
    'gattribs': ('inventory', 'gattribs'),

    # --- status -------------------------------------------------------------
    'logged_in': ('status', 'logged_in'),
    'is_frozen': ('status', 'is_frozen'),
    'is_ghost': ('status', 'is_ghost'),
    'is_muted': ('status', 'is_muted'),
    'admin_rights': ('status', 'admin_rights'),
    'login_time': ('status', 'login_time'),
    'last_packet_time': ('status', 'last_packet_time'),
}


def _state_alias(component: str, field: str) -> property:
    """Build the Player property that reads/writes component.field."""

    def getter(self):
        return getattr(getattr(self, component), field)

    def setter(self, value):
        setattr(getattr(self, component), field, value)

    return property(getter, setter,
                    doc="Alias of self.%s.%s." % (component, field))


for _alias_name, (_alias_component, _alias_field) in _STATE_ALIASES.items():
    if hasattr(Player, _alias_name):
        raise RuntimeError(
            "state alias %r would shadow an existing Player attribute"
            % _alias_name)
    setattr(Player, _alias_name, _state_alias(_alias_component, _alias_field))


# PLI id -> handler method name, built once from the mixins' @handles marks.
_HANDLER_NAMES: Dict[int, str] = collect_handler_names(Player)
