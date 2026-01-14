"""
pygserver.rc - Remote Control (RC) system

Handles admin connections and RC packet processing.
Based on GServer-v2 TPlayerRC.cpp implementation.
"""

import asyncio
import logging
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
    build_rc_file_browser_dir,
    build_rc_file_browser_message,
    build_rc_max_upload_filesize,
)

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
        """Handle RC_APINCREMENTSET - Set AP increment."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return
        # AP (Alignment Points) not commonly used
        logger.debug("AP increment set (not implemented)")

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
            packet = build_rc_player_props(player_name, props)
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
            packet = build_rc_player_props(player.account_name, props)
            await session.player.send_raw(packet)

    async def _handle_player_props_get3(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSGET3 - Get player props by account."""
        await self._handle_player_props_get(session, data)

    async def _handle_player_props_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSSET - Set player properties."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        player_name = reader.read_string()
        # Parse props from remaining data
        # Apply to player

        await self._broadcast_rc_message(
            f"{session.player.account_name} modified props for {player_name}"
        )

    async def _handle_player_props_set2(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSSET2 - Set player properties (alt)."""
        await self._handle_player_props_set(session, data)

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
            PLPROP.MAXPOWER: int(player.max_hearts * 2),
            PLPROP.RUPEESCOUNT: player.rupees,
            PLPROP.BOMBSCOUNT: player.bombs,
            PLPROP.ARROWSCOUNT: player.arrows,
        }

    async def _handle_disconnect_player(self, session: RCSession, data: bytes):
        """Handle RC_DISCONNECTPLAYER - Disconnect a player."""
        if not session.has_right(PLPERM.DISCONNECT):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player_by_name(player_name)
        if player:
            await player.disconnect()
            await self._broadcast_rc_message(
                f"{session.player.account_name} disconnected {player_name}"
            )

    async def _handle_warp_player(self, session: RCSession, data: bytes):
        """Handle RC_WARPPLAYER - Warp a player."""
        if not session.has_right(PLPERM.WARPTOPLAYER):
            return

        reader = PacketReader(data)
        player_name = reader.read_string()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player_by_name(player_name)
        if player:
            await player.warp(level_name, x, y)
            await self._broadcast_rc_message(
                f"{session.player.account_name} warped {player_name} to {level_name}"
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
        player_name = reader.read_string()
        rights = reader.read_gint5()

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
        player_name = reader.read_string()
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
        player_name = reader.read_string()
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

        reader = PacketReader(data)
        flags_str = reader.remaining().decode('latin-1', errors='replace')

        # Parse flags
        if hasattr(self.server, 'server_flags'):
            for line in flags_str.split('\n'):
                if '=' in line:
                    key, value = line.split('=', 1)
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

        reader = PacketReader(data)
        account_name = reader.read_string()
        password = reader.remaining().decode('latin-1', errors='replace')

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

        account_data = {}
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(account_name)
            if account:
                account_data = {
                    'name': account.account_name,
                    'banned': account.is_banned,
                    'rights': account.admin_rights,
                }

        packet = build_rc_account_get(account_name, account_data)
        await session.player.send_raw(packet)

    async def _handle_account_set(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTSET - Set account details."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        account_name = reader.read_string()
        # Parse and apply account changes
        logger.info(f"Account {account_name} modified by {session.player.account_name}")

    # =========================================================================
    # RC Communication Handlers
    # =========================================================================

    async def _handle_rc_chat(self, session: RCSession, data: bytes):
        """Handle RC_CHAT - RC chat message."""
        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace')

        # Format: "account: message"
        formatted = f"{session.player.account_name}: {message}"
        packet = build_rc_chat(formatted)

        # Broadcast to all RCs
        await self.broadcast_to_rcs(packet)

        logger.info(f"[RC] {formatted}")

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
        player_name = reader.read_string()
        message = reader.remaining().decode('latin-1', errors='replace')

        player = self.server.get_player_by_name(player_name)
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
        if hasattr(self.server.world, 'reload_levels'):
            self.server.world.reload_levels()

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

        # Send max upload size
        packet = build_rc_max_upload_filesize(self.max_upload_size)
        await session.player.send_raw(packet)

        # Send initial directory listing
        await self._send_directory_listing(session)

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
            return

        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            path = session.file_browser_path + "/" + filename if session.file_browser_path else filename
            await self.server.filesystem.send_file(session.player, path)

    async def _handle_file_browser_up(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_UP - Upload file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        # File upload is handled by large file transfer
        logger.debug("File upload initiated")

    async def _handle_file_browser_move(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_MOVE - Move file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        src = reader.read_string()
        dst = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            self.server.filesystem.move_file(src, dst)
            await self._send_directory_listing(session)

    async def _handle_file_browser_delete(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_DELETE - Delete file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            path = session.file_browser_path + "/" + filename if session.file_browser_path else filename
            self.server.filesystem.delete_file(path)
            await self._send_directory_listing(session)

    async def _handle_file_browser_rename(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_RENAME - Rename file."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        old_name = reader.read_string()
        new_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            old_path = session.file_browser_path + "/" + old_name if session.file_browser_path else old_name
            new_path = session.file_browser_path + "/" + new_name if session.file_browser_path else new_name
            self.server.filesystem.move_file(old_path, new_path)
            await self._send_directory_listing(session)

    async def _handle_folder_delete(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERDELETE - Delete folder."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        folder = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            path = session.file_browser_path + "/" + folder if session.file_browser_path else folder
            self.server.filesystem.delete_folder(path)
            await self._send_directory_listing(session)

    async def _send_directory_listing(self, session: RCSession):
        """Send directory listing to RC session."""
        files = []

        if hasattr(self.server, 'filesystem'):
            files = self.server.filesystem.list_directory(session.file_browser_path)

        packet = build_rc_file_browser_dir(session.file_browser_path, files)
        await session.player.send_raw(packet)

    # =========================================================================
    # Large File Transfer Handlers
    # =========================================================================

    async def _handle_large_file_start(self, session: RCSession, data: bytes):
        """Handle RC_LARGEFILESTART - Start large file upload."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        reader = PacketReader(data)
        file_size = reader.read_gint5()
        filename = reader.remaining().decode('latin-1', errors='replace')

        logger.info(f"Large file upload started: {filename} ({file_size} bytes)")
        # Store state for receiving file data

    async def _handle_large_file_end(self, session: RCSession, data: bytes):
        """Handle RC_LARGEFILEEND - End large file upload."""
        if not session.has_right(PLPERM.SETATTRIBUTES):
            return

        logger.info("Large file upload completed")
        # Finalize file

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def _broadcast_rc_message(self, message: str):
        """Broadcast a message to all RC sessions."""
        packet = build_rc_chat(message)
        await self.broadcast_to_rcs(packet)
        logger.info(f"[RC] {message}")
