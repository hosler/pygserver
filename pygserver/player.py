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

What stays here: the connection lifecycle (`run`, `_cleanup`), the send
helpers and the small player-data accessors the managers and NPC scripts use.
Level transitions are `level_entry.LevelTransitions`.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, Set, Tuple

from .level_cache import VisitedLevels
from .level_entry import LevelTransitions
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
from .protocol.constants import PLI, PLPROP
from .protocol.packets import (
    PacketBuilder,
    PacketReader,
    build_player_props,
    build_other_player_props,
    build_player_left,
    build_is_leader,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .level import Level

logger = logging.getLogger(__name__)

# GServer-v2/server/src/player/PlayerClient.cpp:221: no sends or receives.
PLAYER_DATA_TIMEOUT = 300.0


class Player(LevelTransitions, MovementHandlers, CombatHandlers, ItemHandlers,
             EntityHandlers, CommunicationHandlers, FileHandlers, MiscHandlers):
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
        self._last_absolute_position = (self.x, self.y)
        self.inventory = Inventory()
        self.status = Status()
        self.login_service = LoginService(self)

        # Current level
        self.level: Optional['Level'] = None
        self.group: str = ""  # Stored only; no group level instancing.
        # When this client last left each level (see level_cache).
        self.visited_levels = VisitedLevels()
        # PlayerClient::m_loaded: false until the login has sent the first
        # level (PlayerClient.cpp:449,659); set by LoginService.
        self.level_loaded = False

        # PLI packet id -> bound handler, from the mixins' @handles decorations.
        self._handlers: Dict[int, Any] = {
            packet_id: getattr(self, name)
            for packet_id, name in _HANDLER_NAMES.items()
        }

        # Packet ids that reached neither a handler nor the RC/NC ranges. Kept
        # so "the server ignored that" is visible instead of silent.
        self._unhandled_packet_ids: Set[int] = set()

        # PLI_RAWDATA announces that the next packet is length-framed binary
        # data rather than newline-framed text. Keep this state on the
        # connection because the announcement and raw packet may arrive in
        # separate decoded bundles.
        self._raw_packet_size: Optional[int] = None
        self._raw_packet_data = bytearray()

    # =========================================================================
    # Connection lifecycle
    # =========================================================================

    @property
    def remote_ip(self) -> Optional[str]:
        """Return the peer IP without its port, for sparring checks."""
        writer = getattr(self.session, 'writer', None)
        peer = writer.get_extra_info('peername') if writer else None
        return peer[0] if peer else None

    async def run(self):
        """Main player loop - handle packets until disconnect."""
        try:
            # Wait for login packet
            if not await self._handle_login():
                return

            # Main packet loop
            while self.connected:
                try:
                    data = await self.session.read(timeout=30.0)
                except asyncio.TimeoutError:
                    if time.time() - self.last_data_time > PLAYER_DATA_TIMEOUT:
                        logger.info(f"Player {self.id} timed out")
                        break
                    continue

                if not data:
                    break

                self.last_data_time = self.last_packet_time = time.time()
                await self._process_data(data)

        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"Player {self.id} error: {e}")
        finally:
            try:
                await self._cleanup()
            finally:
                await self.session.close()

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
        # Nothing of the record outlives the connection (PlayerClient.cpp:184-185).
        self.visited_levels.clear()

        # Dismount horse if mounted
        horse_manager = getattr(self.server, 'horse_manager', None)
        if horse_manager is not None:
            await horse_manager.handle_dismount(self)

        # Save account: only a fully accepted client login ever loaded one
        # (AccountManager.player_state_saveable, Player.cpp:358-362).
        account_manager = getattr(self.server, 'account_manager', None)
        if account_manager is not None:
            account_manager.save_player(self)
        self.account_loaded = False

        # Unregister RC/NC sessions
        for name in ('rc_manager', 'nc_manager'):
            manager = getattr(self.server, name, None)
            if manager is not None:
                manager.unregister_session(self.id)

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
            decrypted = self.session.decode_packet(packet_data)
            if not decrypted:
                continue

            await self._handle_packets(decrypted)

    async def _handle_packets(self, data: bytes):
        """Handle decoded packet data (may contain multiple packets)."""
        pos = 0
        while pos < len(data):
            if self._raw_packet_size is not None:
                needed = self._raw_packet_size - len(self._raw_packet_data)
                take = min(needed, len(data) - pos)
                self._raw_packet_data.extend(data[pos:pos + take])
                pos += take
                if len(self._raw_packet_data) < self._raw_packet_size:
                    break

                packet_bytes = bytes(self._raw_packet_data)
                self._raw_packet_size = None
                self._raw_packet_data.clear()
                if packet_bytes:
                    await self._dispatch_packet(
                        packet_bytes[0] - 32, packet_bytes[1:])
                continue

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

            if packet_id == PLI.RAWDATA:
                reader = PacketReader(packet_body)
                self._raw_packet_size = reader.read_gint3()
                self._raw_packet_data.clear()
                continue

            await self._dispatch_packet(packet_id, packet_body)

    async def _dispatch_packet(self, packet_id: int, packet_body: bytes):
        """Dispatch one fully framed packet."""

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
            return

        # Check for RC/NC packets
        if (51 <= packet_id <= 98 or packet_id in (
                PLI.RC_LARGEFILESTART, PLI.RC_LARGEFILEEND, PLI.RC_FOLDERDELETE)):
            # RC packet (the large-file and folder-delete ids sit above the main
            # block; GServer-v2 PlayerRCPackets.cpp:1523,1537,1567)
            if hasattr(self.server, 'rc_manager'):
                await self.server.rc_manager.handle_packet(self, packet_id, packet_body)
            return
        elif packet_id >= 103 and packet_id <= 119:
            # NC packet
            if hasattr(self.server, 'nc_manager'):
                await self.server.nc_manager.handle_packet(self, packet_id, packet_body)
            return
        elif packet_id in [150, 151]:
            # NC level list packets
            if hasattr(self.server, 'nc_manager'):
                await self.server.nc_manager.handle_packet(self, packet_id, packet_body)
            return

        if packet_id not in self._unhandled_packet_ids:
            self._unhandled_packet_ids.add(packet_id)
            logger.debug(
                f"Player {self.id}: no handler for packet id {packet_id}")

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
        gmap_info = (
            self.server.world.get_gmap_for_level(self.level.name)
            if self.level else None
        )
        if gmap_info:
            gmap, grid_x, grid_y = gmap_info
            current_level = (
                gmap.name if gmap.name.endswith('.gmap')
                else gmap.name + '.gmap'
            )
        else:
            grid_x = grid_y = None
            current_level = self.level.name if self.level else ""

        props = {
            PLPROP.NICKNAME: self.nickname,
            PLPROP.X2: self.x,
            PLPROP.Y2: self.y,
            PLPROP.CARRYNPC: self.npc_id,
            PLPROP.ATTACHNPC: getattr(self, '_attachment_wire', self.attach_npc),
            PLPROP.DIRECTION: self.direction,
            PLPROP.SPRITE: self.sprite,
            PLPROP.GANI: self.gani,
            # (power, image): the biased form is the only one that carries the
            # gear's image name, which is what the other client renders.
            PLPROP.SWORDPOWER: (self.sword_power, self.sword_image),
            PLPROP.SHIELDPOWER: (self.shield_power, self.shield_image),
            PLPROP.HEADIMAGE: self.head_image,
            PLPROP.BODYIMAGE: self.body_image,
            PLPROP.CURLEVEL: current_level,
            PLPROP.COLORS: self.colors,
            PLPROP.MAGICPOINTS: self.mp,
            PLPROP.ALIGNMENT: self.ap,
        }
        if gmap_info:
            props[PLPROP.GMAPLEVELX] = grid_x
            props[PLPROP.GMAPLEVELY] = grid_y
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
    'last_data_time': ('session', 'last_data_time'),
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
    'z': ('character', 'z'),
    'direction': ('character', 'direction'),
    'carrysprite': ('character', 'carrysprite'),
    'npc_id': ('character', 'npc_id'),
    'attach_npc': ('character', 'attach_npc'),
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
    'elo_rating': ('character', 'elo_rating'),
    'elo_deviation': ('character', 'elo_deviation'),
    'last_spar_time': ('character', 'last_spar_time'),
    'head_image': ('character', 'head_image'),
    'body_image': ('character', 'body_image'),
    'sword_image': ('character', 'sword_image'),
    'shield_image': ('character', 'shield_image'),
    'colors': ('character', 'colors'),
    'mp': ('character', 'mp'),
    'ap': ('character', 'ap'),
    'ap_counter': ('character', 'ap_counter'),
    'gani': ('character', 'gani'),
    'sprite': ('character', 'sprite'),
    'chat': ('character', 'chat'),

    # --- inventory ----------------------------------------------------------
    'weapons': ('inventory', 'weapons'),
    'flags': ('inventory', 'flags'),
    'gattribs': ('inventory', 'gattribs'),

    # --- status -------------------------------------------------------------
    'logged_in': ('status', 'logged_in'),
    'account_loaded': ('status', 'account_loaded'),
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
