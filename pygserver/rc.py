"""
pygserver.rc - Remote Control (RC) system

Handles admin connections and RC packet processing.
Based on GServer-v2 TPlayerRC.cpp implementation.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Set, Callable
from dataclasses import dataclass, field

from .protocol.constants import PLI, PLO, PLPROP, PLPERM
from .protocol.packets import (
    PacketBuilder,
    PacketReader,
    build_rc_chat,
    build_rc_server_options,
    build_rc_folder_config,
    build_rc_server_flags,
    build_rc_player_props,
    build_rc_player_rights,
    build_rc_player_comments,
    build_rc_player_ban,
    build_rc_account_list,
    build_rc_account_get,
    build_rc_file_browser_dir_list,
    build_rc_file_browser_dir,
    build_rc_file_browser_message,
    build_player_props,
)
from .level import ITEM_NAME_TO_ID

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player

logger = logging.getLogger(__name__)


@dataclass
class RCSession:
    """Represents an RC admin session."""
    player: 'Player'
    rights: int = 0
    file_browser_path: str = ""
    file_browser_active: bool = False
    # Chunked RC large-file uploads in flight, keyed by filename (mirrors
    # GServer-v2 PlayerRC::m_rcLargeFiles, a per-connection map populated by
    # RC_LARGEFILESTART and flushed on RC_LARGEFILEEND).
    large_file_uploads: Dict[str, bytearray] = field(default_factory=dict)

    def has_right(self, right: PLPERM) -> bool:
        """Check if session has a specific right."""
        return bool(self.rights & right)


class RCManager:
    """
    Manages Remote Control (RC) admin connections.

    Handles:
    - RC authentication and sessions
    - Server options management
    - Player administration (props, rights, bans)
    - Account management
    - File browser operations
    - Server flags
    - Admin messaging
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Active RC sessions
        self._sessions: Dict[int, RCSession] = {}  # player_id -> RCSession
        self._eval_npcs: Dict[str, Any] = {}
        self._eval_times: Dict[str, float] = {}

        # RC packet handlers
        self._handlers: Dict[int, Callable] = {
            PLI.RC_SERVEROPTIONSGET: self._handle_server_options_get,
            PLI.RC_SERVEROPTIONSSET: self._handle_server_options_set,
            PLI.RC_FOLDERCONFIGGET: self._handle_folder_config_get,
            PLI.RC_FOLDERCONFIGSET: self._handle_folder_config_set,
            PLI.RC_RESPAWNSET: self._handle_respawn_set,
            PLI.RC_HORSELIFESET: self._handle_horse_life_set,
            PLI.RC_APINCREMENTSET: self._handle_ap_increment_set,
            PLI.RC_BADDYRESPAWNSET: self._handle_baddy_respawn_set,
            PLI.RC_PLAYERPROPSGET: self._handle_player_props_get,
            PLI.RC_PLAYERPROPSSET: self._handle_player_props_set,
            PLI.RC_DISCONNECTPLAYER: self._handle_disconnect_player,
            PLI.RC_UPDATELEVELS: self._handle_update_levels,
            PLI.RC_ADMINMESSAGE: self._handle_admin_message,
            PLI.RC_PRIVADMINMESSAGE: self._handle_priv_admin_message,
            PLI.RC_LISTRCS: self._handle_list_rcs,
            PLI.RC_DISCONNECTRC: self._handle_disconnect_rc,
            PLI.RC_APPLYREASON: self._handle_apply_reason,
            PLI.RC_SERVERFLAGSGET: self._handle_server_flags_get,
            PLI.RC_SERVERFLAGSSET: self._handle_server_flags_set,
            PLI.RC_ACCOUNTADD: self._handle_account_add,
            PLI.RC_ACCOUNTDEL: self._handle_account_del,
            PLI.RC_ACCOUNTLISTGET: self._handle_account_list_get,
            PLI.RC_PLAYERPROPSGET2: self._handle_player_props_get2,
            PLI.RC_PLAYERPROPSGET3: self._handle_player_props_get3,
            PLI.RC_PLAYERPROPSRESET: self._handle_player_props_reset,
            PLI.RC_PLAYERPROPSSET2: self._handle_player_props_set2,
            PLI.RC_ACCOUNTGET: self._handle_account_get,
            PLI.RC_ACCOUNTSET: self._handle_account_set,
            PLI.RC_CHAT: self._handle_rc_chat,
            PLI.RC_WARPPLAYER: self._handle_warp_player,
            PLI.RC_PLAYERRIGHTSGET: self._handle_player_rights_get,
            PLI.RC_PLAYERRIGHTSSET: self._handle_player_rights_set,
            PLI.RC_PLAYERCOMMENTSGET: self._handle_player_comments_get,
            PLI.RC_PLAYERCOMMENTSSET: self._handle_player_comments_set,
            PLI.RC_PLAYERBANGET: self._handle_player_ban_get,
            PLI.RC_PLAYERBANSET: self._handle_player_ban_set,
            PLI.RC_FILEBROWSER_START: self._handle_file_browser_start,
            PLI.RC_FILEBROWSER_CD: self._handle_file_browser_cd,
            PLI.RC_FILEBROWSER_END: self._handle_file_browser_end,
            PLI.RC_FILEBROWSER_DOWN: self._handle_file_browser_down,
            PLI.RC_FILEBROWSER_UP: self._handle_file_browser_up,
            PLI.RC_FILEBROWSER_MOVE: self._handle_file_browser_move,
            PLI.RC_FILEBROWSER_DELETE: self._handle_file_browser_delete,
            PLI.RC_FILEBROWSER_RENAME: self._handle_file_browser_rename,
            PLI.RC_LARGEFILESTART: self._handle_large_file_start,
            PLI.RC_LARGEFILEEND: self._handle_large_file_end,
            PLI.RC_FOLDERDELETE: self._handle_folder_delete,
        }

        # Settings
        self.max_upload_size = 1024 * 1024  # 1MB default

    def register_session(self, player: 'Player', rights: int) -> RCSession:
        """
        Register an RC session for a player.

        Args:
            player: The admin player
            rights: Admin rights bitmask

        Returns:
            The created RCSession
        """
        session = RCSession(player=player, rights=rights)
        self._sessions[player.id] = session
        logger.info(f"RC session registered for {player.account_name} with rights {rights}")
        return session

    def unregister_session(self, player_id: int):
        """Unregister an RC session."""
        session = self._sessions.pop(player_id, None)
        if session:
            account = session.player.account_name
            npc = self._eval_npcs.pop(account, None)
            self._eval_times.pop(account, None)
            if npc is not None:
                if npc.level is not None:
                    npc.level.remove_npc(npc)
                self.server.npc_manager._npcs.pop(npc.id, None)
            logger.info(f"RC session unregistered for player {player_id}")

    def get_session(self, player_id: int) -> Optional[RCSession]:
        """Get an RC session by player ID."""
        return self._sessions.get(player_id)

    def is_rc(self, player_id: int) -> bool:
        """Check if a player has an active RC session."""
        return player_id in self._sessions

    def get_all_sessions(self) -> List[RCSession]:
        """Get all active RC sessions."""
        return list(self._sessions.values())

    @staticmethod
    def build_add_player(player: 'Player', flags: int = 0) -> bytes:
        """Build the player-list entry shape consumed by control clients."""
        return RCManager.build_add_player_fields(
            player.id, player.account_name, player.nickname, flags)

    @staticmethod
    def build_add_player_fields(player_id: int, account: str, nickname: str,
                                flags: int = 0) -> bytes:
        """Build a control-client player-list entry from explicit fields."""
        builder = PacketBuilder().write_gchar(PLO.ADDPLAYER)
        builder.write_gshort(player_id)
        builder.write_gstring(account)
        builder.write_gchar(PLPROP.NICKNAME).write_gstring(nickname)
        builder.write_gchar(PLPROP.UNKNOWN81).write_gchar(flags)
        builder.write_newline()
        return builder.build()

    @staticmethod
    def build_del_player(player_id: int) -> bytes:
        """Build a control-client player-list removal."""
        builder = PacketBuilder().write_gchar(PLO.DELPLAYER)
        builder.write_gshort(player_id)
        builder.write_newline()
        return builder.build()

    async def notify_player_join(self, player: 'Player'):
        """Add a newly logged-in game player to every control roster."""
        packet = self.build_add_player(player)
        for session in self.get_all_sessions():
            await session.player.send_raw(packet)

    async def notify_player_leave(self, player_id: int):
        """Remove a departing game player from every control roster."""
        packet = self.build_del_player(player_id)
        for session in self.get_all_sessions():
            await session.player.send_raw(packet)

    async def broadcast_chat(self, message: str, exclude: Set[int] = None):
        """Send a chat line to active control sessions."""
        excluded = exclude or set()
        packet = build_rc_chat(message)
        for session in self.get_all_sessions():
            if session.player.id not in excluded:
                await session.player.send_raw(packet)

    async def handle_packet(self, player: 'Player', packet_id: int, data: bytes):
        """
        Handle an RC packet.

        Args:
            player: Player sending packet
            packet_id: Packet ID
            data: Packet data
        """
        session = self.get_session(player.id)
        if not session:
            logger.warning(f"RC packet from non-RC player {player.id}")
            return

        handler = self._handlers.get(packet_id)
        if handler:
            try:
                await handler(session, data)
            except Exception as e:
                logger.error(f"RC handler error (packet {packet_id}): {e}")
        else:
            logger.warning(f"Unhandled RC packet: {packet_id}")

    async def broadcast_to_rcs(self, packet: bytes, exclude: Optional[Set[int]] = None):
        """
        Broadcast a packet to all RC sessions.

        Args:
            packet: Packet to send
            exclude: Player IDs to exclude
        """
        exclude = exclude or set()
        for session in self._sessions.values():
            if session.player.id not in exclude:
                await session.player.send_raw(packet)

    # =========================================================================
    # Server Options Handlers
    # =========================================================================

    async def _handle_server_options_get(self, session: RCSession, data: bytes):
        """Handle RC_SERVEROPTIONSGET - Get server options."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        # Build options string
        options = self._build_server_options_string()
        packet = build_rc_server_options(options)
        await session.player.send_raw(packet)

    async def _handle_server_options_set(self, session: RCSession, data: bytes):
        """Handle RC_SERVEROPTIONSSET - Set server options."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        options_str = reader.remaining().decode('latin-1', errors='replace')

        # Parse and apply options
        self._apply_server_options(options_str)

        # Notify other RCs
        msg = f"{session.player.account_name} updated server options"
        await self._broadcast_rc_message(msg)

    def _build_server_options_string(self) -> str:
        """Build server options string for RC."""
        config = self.server.config
        lines = []

        # Add common options
        if hasattr(config, 'server_name'):
            lines.append(f"name={config.server_name}")
        if hasattr(config, 'description'):
            lines.append(f"description={config.description}")
        if hasattr(config, 'start_level'):
            lines.append(f"startlevel={config.start_level}")
        if hasattr(config, 'start_x'):
            lines.append(f"startx={config.start_x}")
        if hasattr(config, 'start_y'):
            lines.append(f"starty={config.start_y}")

        return '\n'.join(lines)

    def _apply_server_options(self, options_str: str):
        """Apply server options from string."""
        for line in options_str.split('\n'):
            line = line.strip()
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip().lower()
                value = value.strip()
                # Apply to config
                if hasattr(self.server.config, key):
                    setattr(self.server.config, key, value)

    # =========================================================================
    # Folder Config Handlers
    # =========================================================================

    async def _handle_folder_config_get(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERCONFIGGET - Get folder configuration."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        config = build_rc_folder_config(self._get_folder_config())
        await session.player.send_raw(config)

    async def _handle_folder_config_set(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERCONFIGSET - Set folder configuration."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        config_str = reader.remaining().decode('latin-1', errors='replace')
        # Apply folder config
        logger.info(f"Folder config set by {session.player.account_name}")

    def _get_folder_config(self) -> str:
        """Get folder configuration string."""
        return "levels\ngani\nimages\n"

    # =========================================================================
    # Settings Handlers
    # =========================================================================

    async def _handle_respawn_set(self, session: RCSession, data: bytes):
        """Handle RC_RESPAWNSET - Set respawn time."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        respawn_time = reader.read_gchar()

        if hasattr(self.server, 'combat_manager'):
            self.server.combat_manager.respawn_time = respawn_time

        await self._broadcast_rc_message(
            f"{session.player.account_name} set respawn time to {respawn_time}"
        )

    async def _handle_horse_life_set(self, session: RCSession, data: bytes):
        """Handle RC_HORSELIFESET - Set horse lifetime."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        horse_life = reader.read_gchar()

        if hasattr(self.server, 'horse_manager'):
            self.server.horse_manager.default_respawn_time = horse_life

        await self._broadcast_rc_message(
            f"{session.player.account_name} set horse life to {horse_life}"
        )

    async def _handle_ap_increment_set(self, session: RCSession, data: bytes):
        """Handle RC_APINCREMENTSET - Set AP (alignment) increment.

        GServer-v2 (PlayerRCPackets.cpp msgPLI_RC_APINCREMENTSET) marks this
        "Deprecated" and returns without even reading pPacket - same as its
        RESPAWNSET/HORSELIFESET/BADDYRESPAWNSET siblings, which pygserver
        otherwise reimplements as real local settings (combat_manager /
        horse_manager / baddy_manager respawn times). Unlike those, there's
        no local alignment-increment-per-kill system anywhere in pygserver
        for this to configure (account.ap is a static persisted stat, never
        auto-adjusted), so there's nothing sensible to wire it into. Parse
        and log for observability, but this stays a genuine no-op.
        """
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        ap_increment = reader.read_gchar()
        logger.debug(f"AP increment set to {ap_increment} (no local effect - see docstring)")

    async def _handle_baddy_respawn_set(self, session: RCSession, data: bytes):
        """Handle RC_BADDYRESPAWNSET - Set baddy respawn time."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        respawn_time = reader.read_gchar()

        if hasattr(self.server, 'baddy_manager'):
            self.server.baddy_manager.default_respawn_time = respawn_time

        await self._broadcast_rc_message(
            f"{session.player.account_name} set baddy respawn to {respawn_time}"
        )

    # =========================================================================
    # Player Management Handlers
    # =========================================================================

    async def _handle_player_props_get(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSGET - Get player properties."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player_by_name(player_name)
        if player:
            props = self._build_player_props(player)
            packet = self._build_rc_player_props_packet(player, props)
            await session.player.send_raw(packet)

    async def _handle_player_props_get2(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSGET2 - Get player props by ID."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        reader = PacketReader(data)
        player_id = reader.read_gshort()

        player = self.server.get_player(player_id)
        if player:
            props = self._build_player_props(player)
            packet = self._build_rc_player_props_packet(player, props)
            await session.player.send_raw(packet)

    async def _handle_player_props_get3(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSGET3 - Get player props by account.

        Wire format (msgPLI_RC_PLAYERPROPSGET3): [GUCHAR-len account name],
        unlike RC_PLAYERPROPSGET's raw-to-end name - this used to delegate
        to that handler, which read the leading length byte as part of the
        name (a leading garbage character on every lookup).
        """
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()

        player = self.server.get_player_by_name(player_name)
        if player:
            props = self._build_player_props(player)
            packet = self._build_rc_player_props_packet(player, props)
            await session.player.send_raw(packet)

    async def _handle_player_props_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSSET - Set player properties.

        Wire format (PlayerRCPackets.cpp msgPLI_RC_PLAYERPROPSSET):
        [GUSHORT player id][prop stream: world/props/flags/chests/weapons -
        see Player::setPropsFromRCPacket]. Applying the prop stream is not
        implemented here (stub), so it is left unparsed - only the leading
        id is read, to identify the target and to stop this handler from
        misreading the id as a length-prefixed name (the previous
        `read_string()` call had no length argument and always raised).

        NOTE: if the prop stream is ever applied here, any CURLEVEL change
        must route through player.warp() rather than mutating position
        directly - GServer-v2 always re-warps at the end of
        setPropsFromRCPacket (commit 36f409da) so the client's level state
        stays in sync.
        """
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        player_id = reader.read_gshort()
        # Remainder is the prop stream (world/props/flags/chests/weapons) -
        # not applied; see docstring.

        player = self.server.get_player(player_id)
        target_name = player.account_name if player else str(player_id)

        await self._broadcast_rc_message(
            f"{session.player.account_name} modified props for {target_name}"
        )

    async def _handle_player_props_set2(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSSET2 - Set player properties by account name.

        Wire format (msgPLI_RC_PLAYERPROPSSET2): [GUCHAR-len account
        name][prop stream] - unlike RC_PLAYERPROPSSET's GUSHORT player id,
        so this does NOT simply delegate to _handle_player_props_set (that
        would misread the name's length byte as the high byte of an id).
        Same stub caveat as _handle_player_props_set applies to the prop
        stream.
        """
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        # Remainder is the prop stream (world/props/flags/chests/weapons) -
        # not applied; see _handle_player_props_set docstring.

        await self._broadcast_rc_message(
            f"{session.player.account_name} modified props for {player_name}"
        )

    async def _handle_player_props_reset(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSRESET - Reset player properties."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player_by_name(player_name)
        if player:
            # Reset to defaults
            player.hearts = player.max_hearts
            player.rupees = 0
            player.bombs = 0
            player.arrows = 0

            await self._broadcast_rc_message(
                f"{session.player.account_name} reset props for {player_name}"
            )

    def _build_player_props(self, player: 'Player') -> Dict[int, Any]:
        """Build player properties dict for RC."""
        return {
            PLPROP.NICKNAME: player.nickname,
            PLPROP.CURLEVEL: player.level.name if player.level else "",
            PLPROP.X2: player.x,
            PLPROP.Y2: player.y,
            PLPROP.CURPOWER: int(player.hearts * 2),
            # MAXPOWER is FULL hearts on the wire (unlike CURPOWER's halves)
            # - GServer-v2 PlayerProps.cpp:171-186.
            PLPROP.MAXPOWER: int(player.max_hearts),
            PLPROP.RUPEESCOUNT: player.rupees,
            PLPROP.BOMBSCOUNT: player.bombs,
            PLPROP.ARROWSCOUNT: player.arrows,
        }

    @staticmethod
    def _build_rc_player_props_packet(player: 'Player', props: Dict[int, Any]) -> bytes:
        """Wrap a standard player-property stream in the RC response tail."""
        props_packet = build_player_props(props)
        props_blob = props_packet[1:-1]
        return build_rc_player_props(
            player.id, player.account_name, props_blob,
            flags=getattr(player, 'flags', {}),
            weapons=getattr(player, 'weapons', ()),
        )

    async def _handle_disconnect_player(self, session: RCSession, data: bytes):
        """Handle RC_DISCONNECTPLAYER - Disconnect a player."""
        if not session.has_right(PLPERM.DISCONNECT):
            return

        reader = PacketReader(data)
        player_id = reader.read_gshort()
        reason = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player(player_id)
        if player:
            await player.disconnect(reason)
            await self._broadcast_rc_message(
                f"{session.player.account_name} disconnected {player.account_name}"
            )

    async def _handle_warp_player(self, session: RCSession, data: bytes):
        """Handle RC_WARPPLAYER - Warp a player."""
        if not session.has_right(PLPERM.WARPTOPLAYER):
            return

        reader = PacketReader(data)
        player_id = reader.read_gshort()
        # Half-tile units, signed (knockback-style GCHAR, not clamped to 0 -
        # a warp north/west of the origin needs negative values).
        x = reader.read_gchar_signed() / 2.0
        y = reader.read_gchar_signed() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player(player_id)
        if player:
            await player.warp(level_name, x, y)
            await self._broadcast_rc_message(
                f"{session.player.account_name} warped {player.account_name} to {level_name}"
            )

    # =========================================================================
    # Rights Handlers
    # =========================================================================

    async def _handle_player_rights_get(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERRIGHTSGET - Get player rights."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')

        # Get rights from account
        rights = 0
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                rights = account.admin_rights

        packet = build_rc_player_rights(player_name, rights)
        await session.player.send_raw(packet)

    async def _handle_player_rights_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERRIGHTSSET - Set player rights."""
        if not session.has_right(PLPERM.SETRIGHTS):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        rights = reader.read_gint5()
        # admin_ip/folders are parsed to consume the packet correctly but
        # not applied yet - only adminRights is wired up below, matching
        # this handler's prior scope.
        admin_ip = reader.read_gstring()
        folders = reader.read_gstring_short()

        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                account.admin_rights = rights
                self.server.account_manager.save_account(account)

        await self._broadcast_rc_message(
            f"{session.player.account_name} set rights for {player_name}"
        )

    # =========================================================================
    # Comments/Ban Handlers
    # =========================================================================

    async def _handle_player_comments_get(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERCOMMENTSGET - Get player comments."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')

        comments = ""
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account and hasattr(account, 'comments'):
                comments = account.comments

        packet = build_rc_player_comments(player_name, comments)
        await session.player.send_raw(packet)

    async def _handle_player_comments_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERCOMMENTSSET - Set player comments."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        comments = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                account.comments = comments
                self.server.account_manager.save_account(account)

    async def _handle_player_ban_get(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERBANGET - Get player ban info."""
        if not session.has_right(PLPERM.BAN):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')

        is_banned = False
        ban_reason = ""
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                is_banned = account.is_banned
                ban_reason = account.ban_reason

        packet = build_rc_player_ban(player_name, is_banned, ban_reason)
        await session.player.send_raw(packet)

    async def _handle_player_ban_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERBANSET - Set player ban."""
        if not session.has_right(PLPERM.BAN):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        is_banned = reader.read_gchar() != 0
        ban_reason = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                account.is_banned = is_banned
                account.ban_reason = ban_reason
                self.server.account_manager.save_account(account)

        action = "banned" if is_banned else "unbanned"
        await self._broadcast_rc_message(
            f"{session.player.account_name} {action} {player_name}: {ban_reason}"
        )

    async def _handle_apply_reason(self, session: RCSession, data: bytes):
        """Handle RC_APPLYREASON - Apply ban/mute reason."""
        reader = PacketReader(data)
        reason = reader.remaining().decode('latin-1', errors='replace')
        logger.info(f"RC apply reason from {session.player.account_name}: {reason}")

    # =========================================================================
    # Server Flags Handlers
    # =========================================================================

    async def _handle_server_flags_get(self, session: RCSession, data: bytes):
        """Handle RC_SERVERFLAGSGET - Get server flags."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        flags = {}
        if hasattr(self.server, 'server_flags'):
            flags = self.server.server_flags

        packet = build_rc_server_flags(flags)
        await session.player.send_raw(packet)

    async def _handle_server_flags_set(self, session: RCSession, data: bytes):
        """Handle RC_SERVERFLAGSSET - Set server flags."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        # Wire format (msgPLI_RC_SERVERFLAGSSET): [GUSHORT count] then
        # count x [GUCHAR len][chars] "name=value" pairs - not a
        # newline-delimited blob.
        reader = PacketReader(data)
        count = reader.read_gshort()

        if hasattr(self.server, 'server_flags'):
            for _ in range(count):
                pair = reader.read_gstring()
                if '=' in pair:
                    key, value = pair.split('=', 1)
                    self.server.server_flags[key.strip()] = value.strip()

        await self._broadcast_rc_message(
            f"{session.player.account_name} updated server flags"
        )

    # =========================================================================
    # Account Management Handlers
    # =========================================================================

    async def _handle_account_add(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTADD - Add account."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        # Wire format (msgPLI_RC_ACCOUNTADD): [GUCHAR-len account][GUCHAR-len
        # password][GUCHAR-len email][GUCHAR banned][GUCHAR onlyLoad]
        # [GUCHAR adminlevel, deprecated].
        reader = PacketReader(data)
        account_name = reader.read_gstring()
        password = reader.read_gstring()
        email = reader.read_gstring()
        banned = reader.read_gchar() != 0
        only_load = reader.read_gchar() != 0
        reader.read_gchar()  # admin level, deprecated

        if hasattr(self.server, 'account_manager'):
            self.server.account_manager.create_account(account_name, password)

        await self._broadcast_rc_message(
            f"{session.player.account_name} created account {account_name}"
        )

    async def _handle_account_del(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTDEL - Delete account."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'account_manager'):
            self.server.account_manager.delete_account(account_name)

        await self._broadcast_rc_message(
            f"{session.player.account_name} deleted account {account_name}"
        )

    async def _handle_account_list_get(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTLISTGET - Get account list."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        accounts = []
        if hasattr(self.server, 'account_manager'):
            accounts = self.server.account_manager.list_accounts()

        packet = build_rc_account_list(accounts)
        await session.player.send_raw(packet)

    async def _handle_account_get(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTGET - Get account details."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')

        email = ""
        is_banned = False
        load_only = False
        ban_length = ""
        ban_reason = ""
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(account_name)
            if account:
                email = getattr(account, 'profile_email', '')
                is_banned = account.is_banned
                load_only = getattr(account, 'load_only', False)
                ban_length = getattr(account, 'ban_length', '')
                ban_reason = account.ban_reason

        packet = build_rc_account_get(
            account_name, email, is_banned, load_only, ban_length, ban_reason)
        await session.player.send_raw(packet)

    async def _handle_account_set(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTSET - Set account details."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        # Wire format (msgPLI_RC_ACCOUNTSET): [GUCHAR-len account][GUCHAR-len
        # password][GUCHAR-len email][GUCHAR banned][GUCHAR loadOnly]
        # [GUCHAR adminlevel, deprecated][GUCHAR-len world][GUCHAR-len
        # banreason]. Applying these fields is not implemented (stub); only
        # parsed here so the packet doesn't desync/crash.
        reader = PacketReader(data)
        account_name = reader.read_gstring()
        password = reader.read_gstring()
        email = reader.read_gstring()
        banned = reader.read_gchar() != 0
        load_only = reader.read_gchar() != 0
        reader.read_gchar()  # admin level, deprecated
        world = reader.read_gstring()
        ban_reason = reader.read_gstring()

        logger.info(f"Account {account_name} modified by {session.player.account_name}")

    # =========================================================================
    # RC Communication Handlers
    # =========================================================================

    async def _handle_rc_chat(self, session: RCSession, data: bytes):
        """Handle RC_CHAT - RC chat message."""
        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace')

        await self.process_chat(message, session)

    async def process_chat(self, message: str,
                           session: Optional[RCSession] = None):
        """Process chat from an RC or the GS1 ``sendtorc`` bridge.

        A missing session denotes server-originated GS1 text.  Keeping this
        entry point shared ensures slash commands take the same route as text
        entered by an RC instead of accidentally being broadcast as chat.
        Eval source is limited to 8 KiB and one run per account per 250 ms;
        the interpreter also enforces its statement and loop budgets.
        """
        if not message:
            return

        if message.startswith('/'):
            if message.split(None, 1)[0].startswith('/npc'):
                # TODO(c8fa015f): tokenize message[4:] with the GS1 quote-aware
                # tokenizer and dispatch rcchat/#p(index) to the control NPC
                # once pygserver has a control-NPC/NPC-server model.
                return
            command, *arguments = message.split(None, 1)
            if command.lower() in ('/sign', '/chest', '/link'):
                if session is None:
                    return
                await self._process_level_object(command.lower(), arguments, session)
                return
            if command.lower() == '/eval':
                if session is None:
                    return
                await self._process_eval(arguments, session)
                return
            # `/updatelevel <level>` is the REFERENCE spelling of a reload
            # (GServer-v2 Server::processRCChat, Server.cpp:2370, same
            # PLPERM_UPDATELEVEL gate). Accepting it means a client can send
            # the portable spelling everywhere instead of probing which
            # server it is talking to. `/reloadlevel` stays as an alias.
            if command.lower() in ('/savelevel', '/reloadlevel', '/updatelevel'):
                if session is None:
                    return
                if not session.has_right(PLPERM.UPDATELEVEL):
                    await session.player.send_raw(build_rc_chat(
                        f"{command}: permission denied (UPDATELEVEL required)"
                    ))
                    return
                if not arguments or not arguments[0].strip():
                    await session.player.send_raw(build_rc_chat(
                        f"{command}: level name required"
                    ))
                    return
                level_name = arguments[0].strip()
                try:
                    if command.lower() == '/savelevel':
                        level = self.server.world.get_level(level_name)
                        if level is None:
                            raise FileNotFoundError(f"level not found: {level_name}")
                        path = level.save_to_file()
                        reply = f"Saved level {level.name} to {path}"
                    else:
                        current_level = self.server.world.get_level(level_name)
                        if current_level is None:
                            raise FileNotFoundError(
                                f"level not found: {level_name}")
                        retired_npc_ids = tuple(
                            npc.id for npc in current_level.get_npcs()
                            if getattr(npc, 'file_defined', False))
                        level = await self.server.world.reload_level(level_name)
                        await self.server.resend_level(
                            level.name, retired_npc_ids=retired_npc_ids)
                        reply = f"Reloaded level {level.name} and resent it to players"
                except Exception as error:
                    reply = f"{command} {level_name} failed: {error}"
                await session.player.send_raw(build_rc_chat(reply))
                return
            return

        # RC-authored chat is labelled; server-originated sendtorc text is not.
        formatted = (f"{session.player.account_name}: {message}"
                     if session is not None else message)
        packet = build_rc_chat(formatted)

        # Broadcast to all RCs
        await self.broadcast_to_rcs(packet)

        logger.info(f"[RC] {formatted}")

    async def _reply(self, session: RCSession, message: str):
        """Send one control-chat reply to a specific session."""
        await session.player.send_raw(build_rc_chat(message))

    async def _process_level_object(self, command: str, arguments: List[str],
                                    session: RCSession):
        """Apply one live level-object editor command and resend the level."""
        if not session.has_right(PLPERM.UPDATELEVEL):
            await self._reply(
                session, f"{command}: permission denied (UPDATELEVEL required)")
            return
        raw = arguments[0] if arguments else ''
        parts = raw.split()
        try:
            action = parts[0].lower()
            if action not in ('add', 'del'):
                raise ValueError("action must be add or del")
            minimum = {'/sign': 5, '/chest': 5, '/link': 9}[command]
            if action == 'del':
                minimum = 4
            if len(parts) < minimum:
                raise ValueError("missing arguments")
            level_name = parts[1]
            level = self.server.world.get_level(level_name)
            if level is None:
                raise FileNotFoundError(f"unknown level: {level_name}")
            x, y = int(parts[2]), int(parts[3])
            if not (0 <= x < level.WIDTH and 0 <= y < level.HEIGHT):
                raise ValueError(f"coordinates out of range: {x} {y}")

            if command == '/sign':
                key = (x, y)
                if action == 'add':
                    text = raw.split(None, 4)[4]
                    level._signs[key] = text
                    reply = f"Added sign in {level.name} at {x} {y}: {text}"
                else:
                    if key not in level._signs:
                        raise LookupError(f"no sign at {x} {y}")
                    del level._signs[key]
                    reply = f"Deleted sign in {level.name} at {x} {y}"
            elif command == '/chest':
                matches = [entry for entry in level._chests
                           if entry['x'] == x and entry['y'] == y]
                if action == 'add':
                    item_text = parts[4]
                    item = ITEM_NAME_TO_ID.get(item_text.lower())
                    if item is None:
                        try:
                            item = int(item_text)
                        except ValueError:
                            raise ValueError(f"unknown item: {item_text}") from None
                    sign = int(parts[5]) if len(parts) > 5 else 0
                    level._chests.append(
                        {'x': x, 'y': y, 'item': item, 'sign': sign})
                    reply = f"Added chest in {level.name} at {x} {y}: {item_text} {sign}"
                else:
                    if not matches:
                        raise LookupError(f"no chest at {x} {y}")
                    level._chests.remove(matches[0])
                    reply = f"Deleted chest in {level.name} at {x} {y}"
            else:
                matches = [entry for entry in level._links
                           if entry['x'] == x and entry['y'] == y]
                if action == 'add':
                    width, height = int(parts[4]), int(parts[5])
                    if (width <= 0 or height <= 0 or
                            x + width > level.WIDTH or y + height > level.HEIGHT):
                        raise ValueError(
                            f"link area out of range: {x} {y} {width} {height}")
                    dest_x, dest_y = int(parts[7]), int(parts[8])
                    if not (0 <= dest_x < level.WIDTH and
                            0 <= dest_y < level.HEIGHT):
                        raise ValueError(
                            f"destination coordinates out of range: {dest_x} {dest_y}")
                    level._links.append({
                        'dest_level': parts[6], 'x': x, 'y': y,
                        'width': width, 'height': height,
                        'dest_x': str(dest_x), 'dest_y': str(dest_y),
                    })
                    reply = (f"Added link in {level.name} at {x} {y} "
                             f"to {parts[6]} {dest_x} {dest_y}")
                else:
                    if not matches:
                        raise LookupError(f"no link at {x} {y}")
                    level._links.remove(matches[0])
                    reply = f"Deleted link in {level.name} at {x} {y}"
            await self.server.resend_level(level.name)
        except Exception as error:
            reply = f"{command} failed: {error}"
        await self._reply(session, reply)

    async def _process_eval(self, arguments: List[str], session: RCSession):
        """Compile and immediately run bounded GS1 source on a scratch NPC."""
        if not session.has_right(PLPERM.UPDATELEVEL):
            await self._reply(
                session, "/eval: permission denied (UPDATELEVEL required)")
            return
        raw = arguments[0] if arguments else ''
        pieces = raw.split(None, 1)
        if len(pieces) != 2:
            await self._reply(session, "/eval failed: level and code required")
            return
        level_name, code = pieces
        code = code.replace('\xa7', '\n')
        level = self.server.world.get_level(level_name)
        if level is None:
            await self._reply(session, f"/eval failed: unknown level: {level_name}")
            return
        if len(code.encode('latin-1', errors='replace')) > 8192:
            await self._reply(session, "/eval failed: code exceeds 8192 bytes")
            return
        account = session.player.account_name
        now = time.monotonic()
        if now - self._eval_times.get(account, 0.0) < 0.25:
            await self._reply(session, "/eval failed: rate limit (250 ms)")
            return
        self._eval_times[account] = now
        from .gs1_host import compile_gs1, run_npc_event
        try:
            program = compile_gs1(code, raise_errors=True)
        except Exception as error:
            await self._reply(session, f"[eval compile error] {error}")
            return
        npc = self._eval_npcs.get(account)
        if npc is None:
            npc = self.server.npc_manager.create_npc(
                name=f"eval:{account}", level=level)
            npc._rc_eval_scratch = True
            npc.visible = False
            self._eval_npcs[account] = npc
        elif npc.level is not level:
            if npc.level is not None:
                npc.level.remove_npc(npc)
            level.add_npc(npc)
        npc.gs1_program = program
        npc.gs1_source = code
        npc._rc_session = session
        errors = []
        npc._gs1_error_handler = errors.append
        run_npc_event(npc, 'created', self.server, None)
        if errors:
            await self._reply(session, f"[eval runtime error] {errors[0]}")
        else:
            await self._reply(session, f"[eval ok] {level.name}")

    async def _handle_admin_message(self, session: RCSession, data: bytes):
        """Handle RC_ADMINMESSAGE - Admin message to all players."""
        if not session.has_right(PLPERM.ADMINMSG):
            return

        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace')

        # Broadcast to all players
        from .protocol.packets import build_admin_message
        packet = build_admin_message(message)
        await self.server.broadcast_to_all(packet)

        await self._broadcast_rc_message(
            f"{session.player.account_name} sent admin message: {message}"
        )

    async def _handle_priv_admin_message(self, session: RCSession, data: bytes):
        """Handle RC_PRIVADMINMESSAGE - Private admin message."""
        if not session.has_right(PLPERM.ADMINMSG):
            return

        reader = PacketReader(data)
        player_id = reader.read_gshort()
        message = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player(player_id)
        if player:
            from .protocol.packets import build_admin_message
            packet = build_admin_message(message)
            await player.send_raw(packet)

    async def _handle_list_rcs(self, session: RCSession, data: bytes):
        """Handle RC_LISTRCS - List RC users."""
        rc_list = []
        for rc_session in self._sessions.values():
            rc_list.append(rc_session.player.account_name)

        # Send list
        packet = build_rc_chat("RC Users: " + ", ".join(rc_list))
        await session.player.send_raw(packet)

    async def _handle_disconnect_rc(self, session: RCSession, data: bytes):
        """Handle RC_DISCONNECTRC - Disconnect an RC user."""
        if not session.has_right(PLPERM.DISCONNECT):
            return

        reader = PacketReader(data)
        rc_name = reader.remaining().decode('latin-1', errors='replace')

        for rc_session in self._sessions.values():
            if rc_session.player.account_name == rc_name:
                await rc_session.player.disconnect()
                await self._broadcast_rc_message(
                    f"{session.player.account_name} disconnected RC {rc_name}"
                )
                break

    # =========================================================================
    # Level Management Handlers
    # =========================================================================

    async def _handle_update_levels(self, session: RCSession, data: bytes):
        """Handle RC_UPDATELEVELS - Reload levels."""
        if not session.has_right(PLPERM.UPDATELEVEL):
            return

        # Reload levels
        await self.server.world.reload_levels()

        await self._broadcast_rc_message(
            f"{session.player.account_name} reloaded levels"
        )

    # =========================================================================
    # File Browser Handlers
    # =========================================================================

    async def _handle_file_browser_start(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_START - Start file browser."""
        if not session.has_right(PLPERM.VIEWATTRIBUTES):
            return

        session.file_browser_active = True
        session.file_browser_path = ""

        folders = self._get_file_browser_folders(session)
        await session.player.send_raw(build_rc_file_browser_dir_list(folders))
        await session.player.send_raw(
            build_rc_file_browser_message("Welcome to the File Browser."))

    async def _handle_file_browser_cd(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_CD - Change directory."""
        if not session.file_browser_active:
            return

        reader = PacketReader(data)
        directory = reader.remaining().decode('latin-1', errors='replace')

        # Update path
        if directory == "..":
            # Go up
            if '/' in session.file_browser_path:
                session.file_browser_path = session.file_browser_path.rsplit('/', 1)[0]
            else:
                session.file_browser_path = ""
        else:
            # Go into directory
            if session.file_browser_path:
                session.file_browser_path += "/" + directory
            else:
                session.file_browser_path = directory

        await self._send_directory_listing(session)

    async def _handle_file_browser_end(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_END - End file browser."""
        session.file_browser_active = False
        session.file_browser_path = ""

    async def _handle_file_browser_down(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_DOWN - Download file."""
        if not session.file_browser_active:
            await self._send_file_browser_message(
                session, "File browser is not active.")
            return

        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if not self._valid_browser_name(filename):
            await self._send_file_browser_message(
                session, f"Invalid filename: {filename}")
            return
        if not hasattr(self.server, 'filesystem'):
            await self._send_file_browser_message(
                session, "File browser storage is unavailable.")
            return

        path = self._browser_path(session, filename)
        await self.server.filesystem.send_file(session.player, path)

    async def _handle_file_browser_up(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_UP - Upload file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to upload files.")
            return

        reader = PacketReader(data)
        filename = reader.read_gstring()
        file_data = reader.remaining()

        error = self._upload_error(session, filename, len(file_data))
        if error:
            await self._send_file_browser_message(session, error)
            return

        large_buffer = session.large_file_uploads.get(filename)
        if large_buffer is not None:
            if len(large_buffer) + len(file_data) > self.max_upload_size:
                session.large_file_uploads.pop(filename, None)
                await self._send_file_browser_message(
                    session,
                    f"Upload refused: {filename} exceeds the maximum upload "
                    f"size of {self.max_upload_size} bytes.")
                return
            large_buffer.extend(file_data)
            await self._send_file_browser_message(
                session, f"Accepted upload data for {filename}.")
            return

        path = self._browser_path(session, filename)
        if not self.server.filesystem.write_file(path, file_data):
            await self._send_file_browser_message(
                session, f"Could not write file {filename}.")
            return

        logger.info(f"Uploaded file {path} ({len(file_data)} bytes)")
        await self._send_file_browser_message(
            session, f"Uploaded file {filename}.")
        await self._send_directory_listing(session)

    async def _handle_file_browser_move(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_MOVE - Move file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to move files.")
            return

        # Wire format (msgPLI_RC_FILEBROWSER_MOVE): [GUCHAR-len destination
        # dir][filename to end] - the filename is moved from the RC's
        # current folder into the given destination dir.
        reader = PacketReader(data)
        dest_dir = reader.read_gstring()
        filename = reader.remaining().decode('latin-1', errors='replace')

        if (not self._valid_browser_name(filename)
                or not self._valid_browser_path(dest_dir)):
            await self._send_file_browser_message(
                session, "Invalid source or destination path.")
            return
        if not hasattr(self.server, 'filesystem'):
            await self._send_file_browser_message(
                session, "File browser storage is unavailable.")
            return
        src = self._browser_path(session, filename)
        dst = dest_dir + "/" + filename if dest_dir else filename
        if self.server.filesystem.move_file(src, dst):
            message = f"Moved file {src} to {dst}."
        else:
            message = f"Could not move file {src} to {dst}."
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _handle_file_browser_delete(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_DELETE - Delete file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to delete files.")
            return

        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if not self._valid_browser_name(filename):
            await self._send_file_browser_message(
                session, f"Invalid filename: {filename}")
            return
        if not hasattr(self.server, 'filesystem'):
            await self._send_file_browser_message(
                session, "File browser storage is unavailable.")
            return
        path = self._browser_path(session, filename)
        deleted = self.server.filesystem.delete_file(path)
        message = (f"Deleted file {filename}." if deleted
                   else f"Could not delete file {filename}.")
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _handle_file_browser_rename(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_RENAME - Rename file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to rename files.")
            return

        # Wire format (msgPLI_RC_FILEBROWSER_RENAME): [GUCHAR-len old
        # name][GUCHAR-len new name] - new_name is also length-prefixed,
        # not the rest of the packet.
        reader = PacketReader(data)
        old_name = reader.read_gstring()
        new_name = reader.read_gstring()

        if (not self._valid_browser_name(old_name)
                or not self._valid_browser_name(new_name)):
            await self._send_file_browser_message(
                session, "Invalid old or new filename.")
            return
        if not hasattr(self.server, 'filesystem'):
            await self._send_file_browser_message(
                session, "File browser storage is unavailable.")
            return
        old_path = self._browser_path(session, old_name)
        new_path = self._browser_path(session, new_name)
        renamed = self.server.filesystem.move_file(old_path, new_path)
        message = (f"Renamed file {old_name} to {new_name}." if renamed
                   else f"Could not rename file {old_name} to {new_name}.")
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _handle_folder_delete(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERDELETE - Delete folder."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to delete folders.")
            return

        reader = PacketReader(data)
        folder = reader.remaining().decode('latin-1', errors='replace')

        if not self._valid_browser_name(folder):
            await self._send_file_browser_message(
                session, f"Invalid folder name: {folder}")
            return
        if not hasattr(self.server, 'filesystem'):
            await self._send_file_browser_message(
                session, "File browser storage is unavailable.")
            return
        path = self._browser_path(session, folder)
        deleted = self.server.filesystem.delete_folder(path)
        message = (f"Folder {folder} has been removed." if deleted
                   else f"Could not remove folder {folder}.")
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _send_file_browser_message(
            self, session: RCSession, message: str):
        """Send one visible file-browser operation result."""
        await session.player.send_raw(build_rc_file_browser_message(message))

    @staticmethod
    def _valid_browser_name(name: str) -> bool:
        """Return whether a name stays directly inside the current folder."""
        return bool(name and name not in ('.', '..') and '\x00' not in name
                    and '/' not in name and '\\' not in name)

    @staticmethod
    def _valid_browser_path(path: str) -> bool:
        """Return whether a browser-relative path cannot traverse upward."""
        normalized = path.replace('\\', '/')
        return (not normalized.startswith('/') and '\x00' not in normalized
                and '..' not in normalized.split('/'))

    @staticmethod
    def _browser_path(session: RCSession, name: str) -> str:
        """Join a direct child name to the browser's current folder."""
        if session.file_browser_path:
            return session.file_browser_path.strip('/') + "/" + name
        return name

    def _upload_error(
            self, session: RCSession, filename: str, size: int) -> Optional[str]:
        """Return a user-visible upload rejection, or None when allowed."""
        if not session.file_browser_active:
            return "Upload refused: file browser is not active."
        if 'w' not in self._get_file_browser_rights(session).lower():
            return "Upload refused: current folder is read-only."
        if not self._valid_browser_name(filename):
            return f"Upload refused: invalid filename {filename}."
        if size > self.max_upload_size:
            return (f"Upload refused: {filename} exceeds the maximum upload "
                    f"size of {self.max_upload_size} bytes.")
        if not hasattr(self.server, 'filesystem'):
            return "Upload refused: file browser storage is unavailable."
        return None

    async def _send_directory_listing(self, session: RCSession):
        """Send directory listing to RC session."""
        files = []

        if hasattr(self.server, 'filesystem'):
            files = self.server.filesystem.list_directory(session.file_browser_path)

        rights = self._get_file_browser_rights(session)
        packet = build_rc_file_browser_dir(
            session.file_browser_path, files, rights=rights)
        await session.player.send_raw(packet)

    def _get_file_browser_folders(self, session: RCSession) -> List[str]:
        """Return the folder-rights entries available to an RC account."""
        account = None
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(
                session.player.account_name)
        folders = (getattr(account, 'folders', None)
                   or getattr(account, 'folder_list', None))
        resolved = list(folders) if folders else []
        return resolved or ["rw *"]

    def _get_file_browser_rights(self, session: RCSession) -> str:
        """Find the rights attached to the current folder's wildcard entry."""
        path = session.file_browser_path.strip('/')
        for folder_entry in self._get_file_browser_folders(session):
            rights, separator, pattern = folder_entry.partition(' ')
            if not separator:
                continue
            base = pattern.rstrip('*').rstrip('/')
            if not base or path == base or path.startswith(base + '/'):
                return rights
        return "r"

    # =========================================================================
    # Large File Transfer Handlers
    # =========================================================================

    async def _handle_large_file_start(self, session: RCSession, data: bytes):
        """Handle RC_LARGEFILESTART - Start a chunked large-file upload.

        Wire format (msgPLI_RC_LARGEFILESTART): [filename = rest of
        packet] - there is no size field on the wire (GServer-v2 just keys
        an empty CString buffer by filename; bytes stream in afterward via
        RC_FILEBROWSER_UP chunks and get flushed on RC_LARGEFILEEND).
        """
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to upload files.")
            return

        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        error = self._upload_error(session, filename, 0)
        if error:
            await self._send_file_browser_message(session, error)
            return

        session.large_file_uploads[filename] = bytearray()
        logger.info(f"Large file upload started: {filename}")

    async def _handle_large_file_end(self, session: RCSession, data: bytes):
        """Handle RC_LARGEFILEEND - Finish a chunked large-file upload.

        Wire format (msgPLI_RC_LARGEFILEEND): [filename = rest of packet] -
        the filename that keys the buffered upload started by
        RC_LARGEFILESTART. Previously this never read it, so the buffered
        upload could never be matched up and finalized.
        """
        if not session.has_right(PLPERM.SETATTRIBUTES):
            await self._send_file_browser_message(
                session, "Insufficient rights to upload files.")
            return

        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        buf = session.large_file_uploads.pop(filename, None)
        if buf is None:
            logger.warning(f"Large file upload ended for unknown file: {filename}")
            await self._send_file_browser_message(
                session, f"Upload refused: no upload was started for {filename}.")
            return

        error = self._upload_error(session, filename, len(buf))
        if error:
            await self._send_file_browser_message(session, error)
            return

        path = self._browser_path(session, filename)
        if not self.server.filesystem.write_file(path, bytes(buf)):
            await self._send_file_browser_message(
                session, f"Could not write file {filename}.")
            return

        logger.info(f"Large file upload completed: {filename} ({len(buf)} bytes)")
        await self._send_file_browser_message(
            session, f"Uploaded large file {filename}.")
        await self._send_directory_listing(session)

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def _broadcast_rc_message(self, message: str):
        """Broadcast a message to all RC sessions."""
        packet = build_rc_chat(message)
        await self.broadcast_to_rcs(packet)
        logger.info(f"[RC] {message}")
