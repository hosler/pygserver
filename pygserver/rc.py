"""
pygserver.rc - Remote Control (RC) system

Handles admin connections and RC packet processing.
Based on GServer-v2 TPlayerRC.cpp implementation.
"""

import asyncio
import csv
import dataclasses
import io
import math
import os
import tempfile
from pathlib import Path, PureWindowsPath
import logging
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Set, Callable
from dataclasses import dataclass, field

from .account import RCRight, mask_rc_rights
from .attachments import absolute_position
from .audience import SEND_TO_ALL_PROPS

from .protocol.colors import colors_wire_width, normalize_colors
from .protocol.constants import PLI, PLO, PLPROP, PLTYPE
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
from reborn_protocol.props import PLAYER_PROPS, parse_prop_stream

from .protocol.builders.admin import from_csv, guntokenize_text, to_csv
from .protocol.parsers import _INBOUND_PROP_HANDLERS, _INBOUND_STREAM, _store
from .config import OPTIONS_FILE_ENCODING, ServerConfig, atomic_write_text
from .level import ITEM_NAME_TO_ID
from .text_safety import has_control_chars, sanitize_value, strip_control_chars

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player

logger = logging.getLogger(__name__)


# PlayerRC.h:27-70
ADMIN_SERVER_OPTIONS = frozenset("""
name description url serverip serverport serverinterface localip upnp listip
listport maxplayers onlystaff nofoldersconfig serverside sharefolder language
protectdbnpcs triggerhack_weapons triggerhack_guilds triggerhack_groups
triggerhack_files triggerhack_rc triggerhack_execscript triggerhack_props
triggerhack_levels flaghack_movement flaghack_ip generation servermode
runallscriptevents clientsidesigns clientsidelinks localhost
""".split())
# ServerConfig attributes that the option aliases below can reach by their Python
# name; the reference has no equivalent key, so they count as admin-only too.
INTERNAL_CONFIG_NAMES = frozenset("""
port host max_players server_name staff verify_login enable_listserver
hq_password hq_level base_dir levels_dir weapons_dir npcs_dir accounts_dir gmaps
options_path
""".split())
# Loader keys that the reference's AdminServerOptions list has no entry for but
# that grant admin-level control here (login verification bypass).
ADMIN_ONLY_LOADER_KEYS = frozenset({"noverifylogin"})
MAX_EXTRA_OPTIONS = 256
MAX_EXTRA_OPTION_KEY = 64
MAX_EXTRA_OPTION_VALUE = 1024
_CONFIG_FIELDS = frozenset(f.name for f in dataclasses.fields(ServerConfig))


class _LoggedList(list):
    """List that records which indices the config loader assigns."""

    def __init__(self, items, log):
        super().__init__(items)
        self._log = log

    def __setitem__(self, index, value):
        self._log[index] = value
        super().__setitem__(index, value)


class _OptionProbe(ServerConfig):
    """Scratch ServerConfig that records what ServerConfig.from_file assigns.

    Running a candidate option line through the real loader gives the RC path
    the loader's own key table, parsers and typing without a second copy.
    """

    def __init__(self):
        super().__init__()
        self.__dict__['_assigned'] = {}
        self.__dict__['_aptime_assigned'] = {}
        self.__dict__['aptime'] = _LoggedList(self.aptime, self._aptime_assigned)

    def __setattr__(self, name, value):
        assigned = self.__dict__.get('_assigned')
        if assigned is not None:
            assigned[name] = value
        super().__setattr__(name, value)


def _probe_option(key: str, value: str):
    """Parse one option line with the config loader.

    Returns (fields, aptime) for a recognised key, ({}, {}) for an unknown key,
    and raises ValueError when the value does not convert.
    """
    encoding = OPTIONS_FILE_ENCODING   # what ServerConfig.from_file reads
    with tempfile.TemporaryDirectory() as scratch:
        path = os.path.join(scratch, "serveroptions.txt")
        try:
            with open(path, 'w', encoding=encoding) as handle:
                handle.write(f"{key} = {value}\n")
        except UnicodeEncodeError as exc:
            raise ValueError("unencodable option") from exc
        probe = _OptionProbe.from_file(path)
    fields = {name: val for name, val in probe._assigned.items()
              if name in _CONFIG_FIELDS and name not in ('aptime', 'options_path')}
    for val in list(fields.values()) + list(probe._aptime_assigned.values()):
        if isinstance(val, float) and not math.isfinite(val):
            raise ValueError("non-finite number")
    return fields, dict(probe._aptime_assigned)


PROTECTED_DOWNLOADS = frozenset((
    "accounts/(defaultaccount).txt", "config/adminconfig.txt",
    "config/allowedversions.txt", "config/rchelp.txt",
))
IMPORTANT_FILES = {
    "accounts/(defaultaccount).txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/adminconfig.txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/allowedversions.txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/foldersconfig.txt": RCRight.SETFOLDEROPTIONS,
    "config/ipbans.txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/rchelp.txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/rcmessage.txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/rules.txt": RCRight.MODIFYSTAFFACCOUNT,
    "config/servermessage.html": RCRight.SETSERVEROPTIONS,
    "config/serveroptions.txt": RCRight.SETSERVEROPTIONS,
}


def _store_rc_head(props, value):
    # PlayerProps.cpp:397-400: a preset head id names head<id>.png.
    props[PLPROP.HEADIMAGE] = value if isinstance(value, str) else f"head{value}.png"


# Decoders for the RC prop stream: the client-prop table plus the account
# counters the RC editor shows (PlayerProps.h:229-245).
_RC_PROP_HANDLERS = {
    **_INBOUND_PROP_HANDLERS,
    PLPROP.HEADIMAGE: _store_rc_head,
    PLPROP.KILLSCOUNT: _store(PLPROP.KILLSCOUNT),
    PLPROP.DEATHSCOUNT: _store(PLPROP.DEATHSCOUNT),
    PLPROP.RATING: _store(PLPROP.RATING),
}
_GATTRIB_INDEX = {
    prop_id: int(desc.name[len('GATTRIB'):]) - 1
    for prop_id, desc in PLAYER_PROPS.items() if desc.name.startswith('GATTRIB')}
# Props echoed to the edited player after an RC edit, and the attribute each
# is read back from; position goes out with the closing re-warp instead.
_RC_PUSHED_PROPS = (
    (PLPROP.NICKNAME, 'nickname'), (PLPROP.MAXPOWER, 'max_hearts'),
    (PLPROP.CURPOWER, 'hearts'), (PLPROP.RUPEESCOUNT, 'rupees'),
    (PLPROP.ARROWSCOUNT, 'arrows'), (PLPROP.BOMBSCOUNT, 'bombs'),
    (PLPROP.GLOVEPOWER, 'glove_power'), (PLPROP.SWORDPOWER, 'sword_power'),
    (PLPROP.SHIELDPOWER, 'shield_power'), (PLPROP.GANI, 'gani'),
    (PLPROP.HEADIMAGE, 'head_image'), (PLPROP.COLORS, 'colors'),
    (PLPROP.STATUS, 'client_status'), (PLPROP.MAGICPOINTS, 'mp'),
    (PLPROP.KILLSCOUNT, 'kills'), (PLPROP.DEATHSCOUNT, 'deaths'),
    (PLPROP.ALIGNMENT, 'ap'), (PLPROP.BODYIMAGE, 'body_image'),
    (PLPROP.RATING, 'elo_rating'),
)
# The ones other players on the level see (PlayerProps.h clientPropsSharedLocal).
_RC_RELAYED_PROPS = frozenset((
    PLPROP.NICKNAME, PLPROP.SWORDPOWER, PLPROP.SHIELDPOWER, PLPROP.GANI,
    PLPROP.HEADIMAGE, PLPROP.COLORS, PLPROP.MAGICPOINTS, PLPROP.ALIGNMENT,
    PLPROP.BODYIMAGE, *_GATTRIB_INDEX))


def _wildcard_match(pattern: str, text: str) -> bool:
    """Case-sensitive wildcard match of one path segment: `*` any run, `?` one char.

    The reference compiles each segment into a regex with only `*` rewritten
    (FilePermissions.cpp:60,80-90), so `.` and `?` act as regex syntax there. We
    match literally instead; no pattern is ever compiled, so none can backtrack.
    Segments are split on `/` first, so `*` never crosses a separator.
    """
    p = t = 0
    star = -1
    mark = 0
    while t < len(text):
        if p < len(pattern) and pattern[p] == '*':
            star, mark = p, t
            p += 1
        elif p < len(pattern) and pattern[p] in ('?', text[t]):
            p += 1
            t += 1
        elif star >= 0:
            mark += 1
            t = mark
            p = star + 1
        else:
            return False
    while p < len(pattern) and pattern[p] == '*':
        p += 1
    return p == len(pattern)


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

    def has_right(self, right: RCRight) -> bool:
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
        # Unrecognised option lines, string-valued and never read by typed code.
        self.extra_server_options: Dict[str, str] = {}

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
        # PlayerRCPackets.cpp:44: control connection only.

        # Build options string
        options = self._build_server_options_string()
        packet = build_rc_server_options(options)
        await session.player.send_raw(packet)

    async def _handle_server_options_set(self, session: RCSession, data: bytes):
        """Handle RC_SERVEROPTIONSSET - Set server options."""
        # PlayerRCPackets.cpp:63
        if not await self._require_right(
                session, RCRight.SETSERVEROPTIONS,
                f"Server: {session.player.account_name} is not authorized to change the server options."):
            return

        reader = PacketReader(data)
        options_str = reader.remaining().decode('latin-1', errors='replace')

        # PlayerRCPackets.cpp:78-81: the body is comma-text, closed with a comma.
        if not options_str.endswith(','):
            options_str += ','
        options = guntokenize_text(options_str).split('\n')
        # PlayerRCPackets.cpp:84; PlayerRC.h:27
        if not session.has_right(RCRight.MODIFYSTAFFACCOUNT):
            options = [line for line in options
                       if line.partition('=')[0].strip().lower()
                       not in ADMIN_SERVER_OPTIONS | INTERNAL_CONFIG_NAMES]
        accepted: List[tuple] = []
        for message in self._apply_server_options(
                '\n'.join(options),
                allow_admin=session.has_right(RCRight.MODIFYSTAFFACCOUNT),
                accepted=accepted):
            await self._reply(session, message)

        # PlayerRCPackets.cpp:107-112 rewrites serveroptions.txt with the sent
        # text. Here only the accepted lines are merged into the file this
        # config was loaded from; lines the RC did not send are kept.
        path = getattr(self.server.config, 'options_path', '')
        if path and accepted:
            try:
                ServerConfig.update_options_file(path, accepted)
            except (OSError, ValueError) as error:
                logger.error("Could not save server options: %s", error)
                await self._reply(session, "Server: Could not save the server options.")

        await self._broadcast_rc_message(
            f"{session.player.account_name} has updated the server options.")

    def _build_server_options_string(self) -> str:
        """serveroptions.txt as one toCSV row (PlayerRCPackets.cpp:50-57).

        Read from the file the config came from; an in-memory config is
        serialised the way ServerConfig.to_file would write it.
        """
        config = self.server.config
        path = getattr(config, 'options_path', '')
        lines: Optional[List[str]] = None
        if path:
            try:
                lines = Path(path).read_text(
                    encoding='latin-1').replace('\r', '').split('\n')
            except OSError:
                lines = None
        if lines is None:
            lines = (config.option_lines() if isinstance(config, ServerConfig)
                     else [])
            lines += [f"{key} = {value}"
                      for key, value in self.extra_server_options.items()]
        options = to_csv(lines)
        if options.endswith(','):
            options = options[:-1]
        return options

    def _apply_server_options(self, options_str: str,
                              allow_admin: bool = True,
                              accepted: Optional[list] = None) -> List[str]:
        """Apply option lines; returns reference-style rejection messages.

        Only keys the config loader understands reach the typed config, each
        converted by that loader. Any other key is kept as a string in
        `extra_server_options`, which no typed code path reads (the reference
        stores unrecognised lines verbatim in serveroptions.txt,
        PlayerRCPackets.cpp:63-130). A line that fails to convert changes nothing.
        """
        rejected: List[str] = []
        for line in options_str.split('\n'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip().lower()
            value = value.strip()
            shown = key[:MAX_EXTRA_OPTION_KEY]
            if (not key or any(c.isspace() or not c.isprintable() for c in key)
                    or has_control_chars(value.replace('\t', ''))):
                rejected.append(f"Server: Invalid server option {shown!r}.")
                continue
            try:
                fields, aptime = _probe_option(key, value)
            except ValueError:
                rejected.append(f"Server: Invalid value for server option {shown}.")
                continue
            if not fields and not aptime:
                if (len(key) > MAX_EXTRA_OPTION_KEY
                        or len(value) > MAX_EXTRA_OPTION_VALUE
                        or (key not in self.extra_server_options
                            and len(self.extra_server_options) >= MAX_EXTRA_OPTIONS)):
                    rejected.append(f"Server: Server option {shown} is too large.")
                else:
                    self.extra_server_options[key] = value
                    if accepted is not None:
                        accepted.append((key, value))
                continue
            if not allow_admin and (
                    key in ADMIN_SERVER_OPTIONS | ADMIN_ONLY_LOADER_KEYS
                    or INTERNAL_CONFIG_NAMES & fields.keys()):
                continue
            config = self.server.config
            for name, converted in fields.items():
                if name == 'spawn_rates':
                    # The probe only knows the one line; keep the other rates.
                    converted = {**config.spawn_rates, **converted}
                setattr(config, name, converted)
            if 'servermode' in fields:
                # Server.cpp:194-195: read once at start-up. The new value is
                # persisted with the other options; GameServer.server_mode (the
                # COLORS width and the login 176/177 announcement) keeps the
                # mode this process started with.
                logger.info("servermode set to %r: takes effect at the next "
                            "server start (running mode stays %r)",
                            fields['servermode'],
                            getattr(self.server, 'server_mode', 'normal'))
            if 'bushitemtypes' in fields:
                # The reference rebuilds its roll table only when this option
                # is parsed (Server.cpp:603-621), reading the rates then.
                config.rebuild_bush_drops(rates_from_options=True)
            for index, converted in aptime.items():
                config.aptime[index] = converted
            if accepted is not None:
                accepted.append((key, value))
        return rejected

    # =========================================================================
    # Folder Config Handlers
    # =========================================================================

    async def _handle_folder_config_get(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERCONFIGGET - Get folder configuration."""
        # PlayerRCPackets.cpp:136: control connection only.
        # :142-146: nothing is sent when there is no foldersconfig.txt.
        folders = self._get_folder_config()
        if folders is not None:
            await session.player.send_raw(build_rc_folder_config(folders))

    async def _handle_folder_config_set(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERCONFIGSET - Set folder configuration."""
        # PlayerRCPackets.cpp:153
        if not await self._require_right(
                session, RCRight.SETFOLDEROPTIONS,
                f"Server: {session.player.account_name} is not authorized to change the folder config."):
            return

        filesystem = getattr(self.server, 'filesystem', None)
        path = self._folder_config_path()
        if path is None:
            return
        reader = PacketReader(data)
        config_str = reader.remaining().decode('latin-1', errors='replace')
        # PlayerRCPackets.cpp:164-169: the whole file is replaced by the lines.
        # Unlike there, a line whose glob could make a folder outside the
        # world public, or that holds a control or line-break character, is
        # dropped (filesystem.py folders_config_line_allowed).
        sent_lines = from_csv(config_str)
        lines = [line for line in sent_lines
                 if filesystem.folders_config_line_allowed(line)]
        lines = lines[:filesystem.FOLDERS_CONFIG_MAX_LINES]
        if len(lines) != len(sent_lines):
            logger.info("Folder config from %s: dropped %d line(s)",
                        session.player.account_name, len(sent_lines) - len(lines))
        try:
            atomic_write_text(path, ''.join(line + '\n' for line in lines))
        except (OSError, ValueError) as error:
            logger.error("Could not save the folder config: %s", error)
            await self._reply(session, "Server: Could not save the folder config.")
            return
        filesystem.invalidate_folders_config()

        await self._broadcast_rc_message(
            f"{session.player.account_name} updated the folder config.")

    def _folder_config_path(self) -> Optional[Path]:
        """config/foldersconfig.txt under the served folder, the file the file
        system reads its public roots from (filesystem.py)."""
        filesystem = getattr(self.server, 'filesystem', None)
        if not isinstance(getattr(filesystem, 'base_path', None), (str, os.PathLike)):
            return None
        return Path(filesystem.folders_config_path())

    def _get_folder_config(self) -> Optional[str]:
        """foldersconfig.txt as one toCSV row (PlayerRCPackets.cpp:142-145),
        or None when there is no such file."""
        path = self._folder_config_path()
        if path is None:
            return None
        try:
            text = path.read_text(encoding=OPTIONS_FILE_ENCODING)
        except OSError:
            return None
        return to_csv([line[:-1] if line.endswith('\r') else line
                       for line in text.split('\n')])

    # =========================================================================
    # Settings Handlers
    # =========================================================================

    async def _handle_respawn_set(self, session: RCSession, data: bytes):
        """Ignore the deprecated packet (PlayerRCPackets.cpp:176)."""
        return

    async def _handle_horse_life_set(self, session: RCSession, data: bytes):
        """Ignore the deprecated packet (PlayerRCPackets.cpp:182)."""
        return

    async def _handle_ap_increment_set(self, session: RCSession, data: bytes):
        """Ignore the deprecated packet (PlayerRCPackets.cpp:188)."""
        return

    async def _handle_baddy_respawn_set(self, session: RCSession, data: bytes):
        """Ignore the deprecated packet (PlayerRCPackets.cpp:194)."""
        return

    # =========================================================================
    # Player Management Handlers
    # =========================================================================

    async def _handle_player_props_get(self, session: RCSession, data: bytes):
        """Ignore the deprecated packet (PlayerRCPackets.cpp:200)."""
        return

    async def _handle_player_props_get2(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSGET2 - Get player props by ID."""
        # PlayerRCPackets.cpp:517
        if not await self._require_right(
                session, RCRight.VIEWATTRIBUTES,
                f"Server: You are not authorized to view player props."):
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
        # PlayerRCPackets.cpp:545
        if not await self._require_right(
                session, RCRight.VIEWATTRIBUTES,
                f"Server: You are not authorized to view player props."):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        if not self._valid_account_path(player_name):
            return

        player = self.server.get_player_by_name(player_name)
        if player:
            props = self._build_player_props(player)
            packet = self._build_rc_player_props_packet(player, props)
            await session.player.send_raw(packet)

    async def _handle_player_props_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSSET: [GUSHORT player id][RC prop stream]
        (PlayerRCPackets.cpp:206-231)."""
        reader = PacketReader(data)
        player_id = reader.read_gshort()

        player = self.server.get_player(player_id)
        if player is None:
            return
        target_name = player.account_name
        # PlayerRCPackets.cpp:213
        if not await self._can_set_attributes(session, target_name):
            return
        await self._set_props_from_rc(session, reader, target_name, player)

    async def _handle_player_props_set2(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSSET2: [GUCHAR-len account][RC prop stream]
        for an online or offline account (PlayerRCPackets.cpp:615-654)."""
        reader = PacketReader(data)
        player_name = reader.read_gstring()
        if not self._valid_account_path(player_name):
            return
        player = self.server.get_player_by_name(player_name)
        if player is not None and not self._is_game_client(player):
            player = None
        account_manager = getattr(self.server, 'account_manager', None)
        account = (account_manager.get_account(player_name)
                   if account_manager is not None else None)
        # :621-630: an unknown account is ignored without a reply.
        if player is None and account is None:
            return
        # Accounts are looked up case-insensitively, so the packet's spelling
        # is never used as a key: the online player's name (what chests and
        # sessions are keyed on) or the stored account's own name.
        target_name = (player.account_name if player is not None
                       else account.account_name)
        # PlayerRCPackets.cpp:632,642
        if not await self._can_set_attributes(session, target_name):
            return
        # :642, case-folded like the account store.
        if (player_name.lower() == "(defaultaccount)"
                and not session.has_right(RCRight.MODIFYSTAFFACCOUNT)):
            await self._reply(
                session, "Server: You are not authorized to modify the default account.")
            return
        await self._set_props_from_rc(session, reader, target_name, player)

    @staticmethod
    def _is_game_client(player: Any) -> bool:
        return bool(getattr(player, 'connection_type', PLTYPE.CLIENT)
                    & PLTYPE.ANYCLIENT)

    async def _set_props_from_rc(self, session: RCSession, reader: PacketReader,
                                 target_name: str, player: Optional['Player']):
        """Player::setPropsFromRCPacket (PlayerProps.cpp:1241-1322), then the
        account save and RC notice of PlayerRCPackets.cpp:224-228.

        The whole stream is decoded before anything changes: a truncated
        stream is refused rather than read as zero counts, which would wipe
        the account's flags and weapons.
        """
        stream = self._parse_rc_prop_stream(reader)
        if stream is None:
            await self._reply(session, "Server: Invalid player properties packet.")
            return
        props, flags, chests, weapons = stream

        account_manager = getattr(self.server, 'account_manager', None)
        account = (account_manager.get_account(target_name)
                   if account_manager is not None else None)
        online = player if player is not None and self._is_game_client(player) else None
        if online is None and account is None:
            return

        state = online if online is not None else account
        changes = self._rc_prop_changes(session, props, state)
        for target in (online, account):
            if target is not None:
                self._apply_rc_prop_changes(target, changes, target is online)

        flag_sync = self._sync_rc_flags(online, account, flags)
        chest_ids = [f"{level}:{x},{y}" for level, x, y in chests]
        if account is not None:
            account.chests_opened = chest_ids
        self._sync_rc_chests(target_name, chests)
        added, removed = self._sync_rc_weapons(online, account, weapons)

        if online is not None:
            await self._push_rc_changes(online, changes, flag_sync, added, removed, weapons)

        if account is not None and account_manager is not None:
            account_manager.save_account(account)

        await self._broadcast_rc_message(
            f"{session.player.account_name} set the attributes of player {target_name}")

    @staticmethod
    def _parse_rc_prop_stream(reader: PacketReader):
        """Decode [world][props][flags][chests][weapons] (PlayerProps.cpp:
        1246-1307); None when any section runs past the packet."""
        data, pos = reader.data, reader.pos

        def take(count: int) -> bytes:
            nonlocal pos
            if count < 0 or pos + count > len(data):
                raise ValueError("truncated")
            chunk = data[pos:pos + count]
            pos += count
            return chunk

        def gchar() -> int:
            value = take(1)[0] - 32
            if value < 0:
                raise ValueError("bad length")
            return value

        def gshort() -> int:
            high, low = take(2)
            if high < 32 or low < 32:
                raise ValueError("bad count")
            return ((high - 32) << 7) + (low - 32)

        def text(count: int) -> str:
            return take(count).decode('latin-1')

        try:
            take(gchar())                                  # world name, unused
            props_blob = take(gchar())
            flags = [text(gchar()) for _ in range(gshort())]
            chests = []
            for _ in range(gshort()):
                length = gchar()
                if length < 2:
                    raise ValueError("bad chest")
                x, y = gchar(), gchar()
                chests.append((text(length - 2), x, y))
            weapons = []
            for _ in range(gchar()):
                length = gchar()
                if length:                                 # :1298 skips empties
                    weapons.append(text(length))
        except (ValueError, IndexError):
            return None
        # PLPROP_COLORS is colors_wire_width() bytes here too: the RC set goes
        # through the same PropertyColors<> deserialiser (PlayerProps.cpp:1183-1198,
        # Player::setPropsFromRCPacket PlayerProps.cpp:1243-1255).
        props, _clean, _end = parse_prop_stream(
            props_blob, 0, _INBOUND_STREAM.with_colors_len(colors_wire_width()),
            _RC_PROP_HANDLERS)
        props = {key: sanitize_value(value) for key, value in props.items()}
        return props, flags, chests, weapons

    def _rc_prop_changes(self, session: RCSession, props: Dict[Any, Any],
                         state: Any) -> Dict[str, Any]:
        """Player::setProp for SetBy::SERVER (PlayerProps.cpp:126-1180),
        reduced to the props the RC editor shows (PlayerProps.h:229-245) plus
        the hi-res position and gani attributes. Returns attribute -> value."""
        config = self.server.config
        changes: Dict[str, Any] = {}
        if PLPROP.NICKNAME in props:
            # :196, Player.cpp:906-911: a server-set nickname is taken as is.
            changes['nickname'] = props[PLPROP.NICKNAME][:223]
        max_hearts = getattr(state, 'max_hearts', 3.0)
        hearts = getattr(state, 'hearts', max_hearts)
        if PLPROP.MAXPOWER in props:
            apply_max = getattr(config, 'apply_max_hearts',
                                lambda value: max(0, min(int(value), 20)))
            max_hearts = float(apply_max(props[PLPROP.MAXPOWER]))
            # :217-218 fills the hearts; clamped to the new maximum here.
            hearts = max_hearts
            changes['max_hearts'] = max_hearts
            changes['hearts'] = hearts
        if PLPROP.CURPOWER in props:
            new_hearts = max(0.0, min(props[PLPROP.CURPOWER] / 2.0, max_hearts))
            # :234-235: under 40 AP a heal is refused, for a server set too.
            if not (getattr(state, 'ap', 50) < 40 and new_hearts > hearts):
                changes['hearts'] = new_hearts
        if PLPROP.RUPEESCOUNT in props and self._can_change_gralats(session):
            changes['rupees'] = min(int(props[PLPROP.RUPEESCOUNT]), 9999999)
        for prop_id, attr, limit in (
                (PLPROP.ARROWSCOUNT, 'arrows', 99),       # :266
                (PLPROP.BOMBSCOUNT, 'bombs', 99),         # :276
                (PLPROP.GLOVEPOWER, 'glove_power', 3),    # :289
                (PLPROP.MAGICPOINTS, 'mp', 100),
                (PLPROP.ALIGNMENT, 'ap', 100)):           # :843
            if prop_id in props:
                changes[attr] = max(0, min(int(props[prop_id]), limit))
        for prop_id, attr in ((PLPROP.KILLSCOUNT, 'kills'),
                              (PLPROP.DEATHSCOUNT, 'deaths')):
            if prop_id in props:
                changes[attr] = max(0, int(props[prop_id]))
        if PLPROP.SWORDPOWER in props:
            changes['sword_power'] = config.apply_sword_power(props[PLPROP.SWORDPOWER])
            if props.get('sword_image'):
                changes['sword_image'] = props['sword_image'][:223]
        if PLPROP.SHIELDPOWER in props:
            changes['shield_power'] = config.apply_shield_power(props[PLPROP.SHIELDPOWER])
            if props.get('shield_image'):
                changes['shield_image'] = props['shield_image'][:223]
        if PLPROP.GANI in props:
            changes['gani'] = (props[PLPROP.GANI] or 'idle')[:223]
        if PLPROP.HEADIMAGE in props:
            changes['head_image'] = props[PLPROP.HEADIMAGE][:123]
        if PLPROP.BODYIMAGE in props:
            changes['body_image'] = props[PLPROP.BODYIMAGE][:223]
        if PLPROP.COLORS in props:
            changes['colors'] = normalize_colors(props[PLPROP.COLORS])
        if PLPROP.STATUS in props:
            changes['client_status'] = int(props[PLPROP.STATUS])
        if PLPROP.RATING in props:
            changes['elo_rating'], changes['elo_deviation'] = (
                float(value) for value in props[PLPROP.RATING])
        if PLPROP.X2 in props:
            changes['x'] = float(props[PLPROP.X2])
        elif PLPROP.X in props:
            changes['x'] = props[PLPROP.X] / 2.0
        if PLPROP.Y2 in props:
            changes['y'] = float(props[PLPROP.Y2])
        elif PLPROP.Y in props:
            changes['y'] = props[PLPROP.Y] / 2.0
        if props.get(PLPROP.CURLEVEL):
            changes['level_name'] = props[PLPROP.CURLEVEL]
        gattribs = {_GATTRIB_INDEX[prop_id]: value[:223]
                    for prop_id, value in props.items()
                    if prop_id in _GATTRIB_INDEX and isinstance(value, str)}
        if gattribs:
            changes['gattribs'] = gattribs
        return changes

    def _can_change_gralats(self, session: RCSession) -> bool:
        """PlayerProps.cpp:1205-1210."""
        if getattr(self.server.config, 'normaladminscanchangegralats', True):
            return True
        is_staff = getattr(self.server, 'is_staff', None)
        staff = bool(is_staff(session.player.account_name)) if callable(is_staff) else False
        return staff and session.has_right(RCRight.SETRIGHTS)

    @staticmethod
    def _apply_rc_prop_changes(target: Any, changes: Dict[str, Any], is_player: bool):
        """Write decoded changes onto a live player or a stored account."""
        for attr, value in changes.items():
            if attr in ('level_name', 'gattribs'):
                continue
            if attr in ('gani', 'client_status') and not is_player:
                continue
            setattr(target, attr, list(value) if isinstance(value, list) else value)
        gattribs = changes.get('gattribs', {})
        if is_player:
            current = getattr(target, 'gattribs', None)
            if isinstance(current, dict):
                current.update(gattribs)
        else:
            if 'level_name' in changes:
                target.level_name = changes['level_name']
            stored = getattr(target, 'gattribs', None)
            if isinstance(stored, list):
                for index, value in gattribs.items():
                    if 0 <= index < len(stored):
                        stored[index] = value

    @staticmethod
    def _sync_rc_flags(online: Any, account: Any, flags: List[str]):
        """Player::synchronizeFlags (Player.cpp:1017-1083): the sent list
        replaces the flags. A flag cut at 220 chars by the props reply keeps
        its stored value when that still starts with the sent one
        (PlayerProps.cpp:1259-1276)."""
        current = dict(getattr(online if online is not None else account,
                               'flags', {}) or {})
        result: Dict[str, str] = {}
        for entry in flags:
            entry = strip_control_chars(entry)
            if len(entry) > 220:
                name, _, value = entry[:220].partition('=')
                existing = current.get(name)
                if isinstance(existing, str) and existing.startswith(value.strip()):
                    result.setdefault(name, existing)
                    continue
            name, _, value = entry.partition('=')
            if name:
                result.setdefault(name, value)
        for target in (online, account):
            if target is not None:
                target.flags = dict(result)
        return current, result

    def _sync_rc_chests(self, account_name: str, chests: List[tuple]):
        """Replace which level chests count as opened for this account."""
        items = getattr(self.server, 'item_manager', None)
        level_chests = getattr(items, '_chests', None)
        if not isinstance(level_chests, dict):
            return
        wanted = {(level, x, y) for level, x, y in chests}
        for level_name, entries in level_chests.items():
            for chest in entries:
                if (level_name, chest.x, chest.y) in wanted:
                    chest.opened_by.add(account_name)
                else:
                    chest.opened_by.discard(account_name)

    def _weapon_exists(self, name: str) -> bool:
        """Server::getWeapon: a stored weapon or a built-in one
        (Server.cpp:1050-1056)."""
        from .items import DEFAULT_WEAPON_IDS
        manager = getattr(self.server, 'gs2_manager', None)
        stored = manager.get_weapon(name) if manager is not None else None
        return stored is not None or name in DEFAULT_WEAPON_IDS

    def _sync_rc_weapons(self, online: Any, account: Any, weapons: List[str]):
        """Player::synchronizeWeapons (Player.cpp:1154-1177): drop what is not
        listed, add what is listed and exists on the server (:1103-1107)."""
        source = online if online is not None else account
        current = list(getattr(source, 'weapons', []) or [])
        removed = [name for name in current if name not in weapons]
        kept = [name for name in current if name in weapons]
        added = []
        for name in weapons:
            name = strip_control_chars(name)
            if name and name not in kept and self._weapon_exists(name):
                kept.append(name)
                added.append(name)
        for target in (online, account):
            if target is not None:
                target.weapons = list(kept)
        return added, removed

    async def _push_rc_changes(self, player: Any, changes: Dict[str, Any],
                               flag_sync: tuple, added: List[str],
                               removed: List[str], sent_weapons: List[str]):
        """Tell the edited player and the level what changed, then re-warp
        (PlayerProps.cpp:1253, 1277, 1308-1321)."""
        from .protocol.packets import (
            build_flag_del, build_flag_set, build_npc_weapon_del,
            build_other_player_props)
        own = {}
        for prop_id, attr in _RC_PUSHED_PROPS:
            if attr in changes:
                own[prop_id] = self._rc_prop_value(player, prop_id, attr)
        gattribs = changes.get('gattribs', {})
        for prop_id, index in _GATTRIB_INDEX.items():
            if index in gattribs:
                own[prop_id] = gattribs[index]
        if own:
            await player.send_props(own)
            level = getattr(player, 'level', None)
            everyone = {prop_id: value for prop_id, value in own.items()
                        if prop_id in SEND_TO_ALL_PROPS}
            if everyone:
                await self.server.broadcast_to_all(
                    build_other_player_props(player.id, everyone),
                    exclude={player.id})
            visible = {prop_id: value for prop_id, value in own.items()
                       if prop_id in _RC_RELAYED_PROPS
                       and prop_id not in SEND_TO_ALL_PROPS}
            if visible and level is not None:
                await self.server.broadcast_nearby(
                    level.name, build_other_player_props(player.id, visible),
                    exclude={player.id})

        before, after = flag_sync
        for name in before:
            if name not in after:
                await player.send_raw(build_flag_del(name))
        for name, value in after.items():
            if before.get(name) != value:
                await player.send_raw(build_flag_set(name, value if value else True))

        for name in removed:
            await player.send_raw(build_npc_weapon_del(name))
        manager = getattr(self.server, 'gs2_manager', None)
        if manager is not None:
            for name in added:
                await manager.announce_weapon(player, name)
        # :1310-1314: the built-in bomb and bow go unless the list kept them.
        if not any(name in ('bomb', 'Bomb') for name in sent_weapons):
            await player.send_raw(build_npc_weapon_del("Bomb"))
        if not any(name in ('bow', 'Bow') for name in sent_weapons):
            await player.send_raw(build_npc_weapon_del("Bow"))

        level = getattr(player, 'level', None)
        level_name = changes.get('level_name') or (level.name if level is not None else '')
        if level_name and callable(getattr(player, 'warp', None)):
            await player.warp(level_name, player.x, player.y)

    @staticmethod
    def _rc_prop_value(player: Any, prop_id: int, attr: str) -> Any:
        if prop_id == PLPROP.MAXPOWER:
            return int(player.max_hearts)
        if prop_id == PLPROP.CURPOWER:
            return int(player.hearts * 2)
        if prop_id == PLPROP.SWORDPOWER:
            return (player.sword_power, player.sword_image)
        if prop_id == PLPROP.SHIELDPOWER:
            return (player.shield_power, player.shield_image)
        if prop_id == PLPROP.RATING:
            return (int(player.elo_rating), int(player.elo_deviation))
        return getattr(player, attr)

    async def _handle_player_props_reset(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERPROPSRESET - Reset player properties."""
        # PlayerRCPackets.cpp:562
        if not await self._require_right(
                session, RCRight.RESETATTRIBUTES,
                f"Server: You are not authorized to reset accounts."):
            return

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')
        if not self._valid_account_path(player_name):
            return

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
            PLPROP.X2: absolute_position(player)[0],
            PLPROP.Y2: absolute_position(player)[1],
            PLPROP.CURPOWER: int(player.hearts * 2),
            # MAXPOWER is FULL hearts on the wire (unlike CURPOWER's halves)
            # - GServer-v2 PlayerProps.cpp:171-186.
            PLPROP.MAXPOWER: int(player.max_hearts),
            PLPROP.RUPEESCOUNT: player.rupees,
            PLPROP.BOMBSCOUNT: player.bombs,
            PLPROP.ARROWSCOUNT: player.arrows,
        }

    def _build_rc_player_props_packet(self, player: 'Player', props: Dict[int, Any]) -> bytes:
        """Wrap a standard player-property stream in the RC response tail,
        opened chests included (PlayerProps.cpp:1377-1382): an RC edit sends
        the chest list back as a replacement, so leaving it out wiped it."""
        props_packet = build_player_props(props)
        props_blob = props_packet[1:-1]
        return build_rc_player_props(
            player.id, player.account_name, props_blob,
            flags=getattr(player, 'flags', {}),
            chests=self._opened_chests(player.account_name),
            weapons=getattr(player, 'weapons', ()),
        )

    def _opened_chests(self, account_name: str) -> List[tuple]:
        items = getattr(self.server, 'item_manager', None)
        level_chests = getattr(items, '_chests', None)
        if not isinstance(level_chests, dict):
            return []
        return [(level_name, chest.x, chest.y)
                for level_name, entries in level_chests.items()
                for chest in entries if account_name in chest.opened_by]

    async def _handle_disconnect_player(self, session: RCSession, data: bytes):
        """Handle RC_DISCONNECTPLAYER - Disconnect a player."""
        # PlayerRCPackets.cpp:238
        if not await self._require_right(
                session, RCRight.DISCONNECT,
                f"Server: {session.player.account_name} is not authorized to disconnect players."):
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
        # PlayerRCPackets.cpp:783
        if not await self._require_right(
                session, RCRight.WARPTOPLAYER,
                f"Server: You are not authorized to warp players.\n"):
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
        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')
        if not self._valid_account_path(player_name):
            return
        # PlayerRCPackets.cpp:807
        if (not self._is_own_account(session, player_name)
                and not await self._require_right(
                    session, RCRight.SETRIGHTS,
                    "Server: You are not authorized to view that player's rights.")):
            return

        # Get rights from account
        rights = 0
        admin_ip = ""
        folders = ""
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                rights = account.admin_rights
                admin_ip = account.admin_ip
                folders = self._encode_lines(account.folders)

        packet = build_rc_player_rights(player_name, rights, admin_ip, folders)
        await session.player.send_raw(packet)

    async def _handle_player_rights_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERRIGHTSSET - Set player rights."""
        # PlayerRCPackets.cpp:842
        if not await self._require_right(
                session, RCRight.SETRIGHTS,
                f"Server: You are not authorized to set player rights."):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        if not self._valid_account_path(player_name):
            return
        rights = reader.read_gint5()
        admin_ip = reader.read_gstring()
        folders = reader.read_gstring_short()

        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                account.admin_rights = mask_rc_rights(
                    session.rights, rights,
                    self._is_own_account(session, account.account_name))
                # PlayerRCPackets.cpp:885-900: IPs are unrestricted; folders are filtered.
                account.admin_ip = admin_ip
                account.folders = [
                    entry for entry in self._decode_lines(folders)
                    if ':' not in entry and '..' not in entry and ' /*' not in entry
                ]
                self.server.account_manager.save_account(account)
                for active in list(self._sessions.values()):
                    if active.player.account_name.lower() == player_name.lower():
                        active.rights = account.admin_rights
                        active.large_file_uploads.clear()
                        if active.file_browser_active:
                            active.file_browser_active = False
                            await self._handle_file_browser_start(active, b"")
                # PlayerRCPackets.cpp:913-918: losing NPC control drops the NC session.
                if not account.admin_rights & RCRight.NPCCONTROL:
                    await self._drop_nc_sessions(player_name)

        await self._broadcast_rc_message(
            f"{session.player.account_name} set rights for {player_name}"
        )

    # =========================================================================
    # Comments/Ban Handlers
    # =========================================================================

    async def _handle_player_comments_get(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERCOMMENTSGET - Get player comments."""
        # PlayerRCPackets.cpp:943: control connection only.

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')
        if not self._valid_account_path(player_name):
            return

        comments = ""
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account and hasattr(account, 'comments'):
                comments = account.comments

        packet = build_rc_player_comments(player_name, comments)
        await session.player.send_raw(packet)

    async def _handle_player_comments_set(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERCOMMENTSSET - Set player comments."""
        # PlayerRCPackets.cpp:972
        if not await self._require_right(
                session, RCRight.SETCOMMENTS,
                f"Server: You are not authorized to set player comments."):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        if not self._valid_account_path(player_name):
            return
        comments = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(player_name)
            if account:
                account.comments = comments
                self.server.account_manager.save_account(account)

    async def _handle_player_ban_get(self, session: RCSession, data: bytes):
        """Handle RC_PLAYERBANGET - Get player ban info."""
        # PlayerRCPackets.cpp:1011: control connection only.

        reader = PacketReader(data)
        player_name = reader.remaining().decode('latin-1', errors='replace')
        if not self._valid_account_path(player_name):
            return

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
        # PlayerRCPackets.cpp:1040
        if not await self._require_right(
                session, RCRight.BAN,
                f"Server: You are not authorized to set player bans."):
            return

        reader = PacketReader(data)
        player_name = reader.read_gstring()
        if not self._valid_account_path(player_name):
            return
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
        """Ignore the deprecated packet (PlayerRCPackets.cpp:324)."""
        return

    # =========================================================================
    # Server Flags Handlers
    # =========================================================================

    async def _handle_server_flags_get(self, session: RCSession, data: bytes):
        """Handle RC_SERVERFLAGSGET - Get server flags."""
        # PlayerRCPackets.cpp:332: control connection only.

        flags = {}
        if hasattr(self.server, 'server_flags'):
            flags = self.server.server_flags

        packet = build_rc_server_flags(flags)
        await session.player.send_raw(packet)

    async def _handle_server_flags_set(self, session: RCSession, data: bytes):
        """Handle RC_SERVERFLAGSSET - Set server flags."""
        # PlayerRCPackets.cpp:356
        if not await self._require_right(
                session, RCRight.SETSERVERFLAGS,
                f"Server: You are not authorized to set the server flags."):
            return

        # Wire format (msgPLI_RC_SERVERFLAGSSET): [GUSHORT count] then
        # count x [GUCHAR len][chars] "name=value" pairs - not a
        # newline-delimited blob.
        reader = PacketReader(data)
        count = reader.read_gshort()

        if hasattr(self.server, 'server_flags'):
            for _ in range(count):
                pair = reader.read_gstring()
                key, separator, value = pair.partition('=')
                if key.strip():
                    self.server.server_flags[key.strip()] = value.strip() if separator else True

        await self._broadcast_rc_message(
            f"{session.player.account_name} updated server flags"
        )

    # =========================================================================
    # Account Management Handlers
    # =========================================================================

    async def _handle_account_add(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTADD - Add account."""
        # PlayerRCPackets.cpp:405
        if not await self._require_right(
                session, RCRight.MODIFYSTAFFACCOUNT,
                f"Server: You are not authorized to create new accounts."):
            return

        # Wire format (msgPLI_RC_ACCOUNTADD): [GUCHAR-len account][GUCHAR-len
        # password][GUCHAR-len email][GUCHAR banned][GUCHAR onlyLoad]
        # [GUCHAR adminlevel, deprecated].
        reader = PacketReader(data)
        account_name = reader.read_gstring()
        if not self._valid_account_path(account_name):
            return
        password = reader.read_gstring()
        email = reader.read_gstring()
        banned = reader.read_gchar() != 0
        only_load = reader.read_gchar() != 0
        reader.read_gchar()  # admin level, deprecated

        world = reader.read_gstring()
        ban_reason = reader.read_gstring()
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.create_account(account_name, password)
            self._set_account_metadata(session, account, email, only_load,
                                       banned, ban_reason)

        await self._broadcast_rc_message(
            f"{session.player.account_name} created account {account_name}"
        )

    async def _handle_account_del(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTDEL - Delete account."""
        # PlayerRCPackets.cpp:439
        if not await self._require_right(
                session, RCRight.MODIFYSTAFFACCOUNT,
                f"Server: You are not authorized to delete accounts."):
            return

        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')
        if not self._valid_account_path(account_name):
            return

        # PlayerRCPackets.cpp:450; the account store ignores case.
        if account_name.lower() == "(defaultaccount)":
            await self._reply(
                session, "Server: You are not allowed to delete the default account.")
            return
        if hasattr(self.server, 'account_manager'):
            self.server.account_manager.delete_account(account_name)

        await self._broadcast_rc_message(
            f"{session.player.account_name} deleted account {account_name}"
        )

    async def _handle_account_list_get(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTLISTGET - Get account list."""
        # PlayerRCPackets.cpp:477: control connection only.

        accounts = []
        if hasattr(self.server, 'account_manager'):
            accounts = self.server.account_manager.list_accounts()

        packet = build_rc_account_list(accounts)
        await session.player.send_raw(packet)

    async def _handle_account_get(self, session: RCSession, data: bytes):
        """Handle RC_ACCOUNTGET - Get account details."""
        # PlayerRCPackets.cpp:662: control connection only.

        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')
        if not self._valid_account_path(account_name):
            return

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
        # PlayerRCPackets.cpp:702
        if not await self._require_right(
                session, RCRight.MODIFYSTAFFACCOUNT,
                f"Server: You are not authorized to edit accounts."):
            return

        # Wire format (msgPLI_RC_ACCOUNTSET): [GUCHAR-len account][GUCHAR-len
        # password][GUCHAR-len email][GUCHAR banned][GUCHAR loadOnly]
        # [GUCHAR adminlevel, deprecated][GUCHAR-len world][GUCHAR-len
        # banreason].
        reader = PacketReader(data)
        account_name = reader.read_gstring()
        if not self._valid_account_path(account_name):
            return
        password = reader.read_gstring()
        email = reader.read_gstring()
        banned = reader.read_gchar() != 0
        load_only = reader.read_gchar() != 0
        reader.read_gchar()  # admin level, deprecated
        world = reader.read_gstring()
        ban_reason = reader.read_gstring()

        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(account_name)
            if account:
                self._set_account_metadata(session, account, email, load_only,
                                           banned, ban_reason)
        logger.info(f"Account {account_name} modified by {session.player.account_name}")

    # =========================================================================
    # RC Communication Handlers
    # =========================================================================

    async def _handle_rc_chat(self, session: RCSession, data: bytes):
        """Handle RC_CHAT - RC chat message."""
        # PlayerRCPackets.cpp:767
        if getattr(session.player, 'connection_type', None) == PLTYPE.NC:
            return
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
                if not session.has_right(RCRight.UPDATELEVEL):
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
        if not session.has_right(RCRight.UPDATELEVEL):
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
        if not session.has_right(RCRight.UPDATELEVEL):
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
        # PlayerRCPackets.cpp:285
        if not await self._require_right(
                session, RCRight.ADMINMSG,
                f"Server: You are not authorized to send an admin message."):
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
        # PlayerRCPackets.cpp:298
        if not await self._require_right(
                session, RCRight.ADMINMSG,
                f"Server: You are not authorized to send an admin message."):
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
        """Ignore the deprecated packet (PlayerRCPackets.cpp:312)."""
        return

    async def _handle_disconnect_rc(self, session: RCSession, data: bytes):
        """Ignore the deprecated packet (PlayerRCPackets.cpp:318)."""
        return

    # =========================================================================
    # Level Management Handlers
    # =========================================================================

    async def _handle_update_levels(self, session: RCSession, data: bytes):
        """Handle RC_UPDATELEVELS - Reload levels."""
        # PlayerRCPackets.cpp:266
        if not await self._require_right(
                session, RCRight.UPDATELEVEL,
                f"Server: {session.player.account_name} is not authorized to update levels."):
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
        # PlayerRCPackets.cpp:1091,1103-1125
        folders = self._get_file_browser_folders(session)
        folder_map = self._folder_map(session)
        if not folders or not folder_map:
            session.file_browser_active = False
            session.file_browser_path = ""
            session.large_file_uploads.clear()
            return
        current = session.file_browser_path.rstrip('/')
        session.file_browser_path = (current if current in folder_map
                                     else sorted(folder_map)[0])
        session.file_browser_active = True
        await session.player.send_raw(build_rc_file_browser_dir_list(folders))
        await session.player.send_raw(
            build_rc_file_browser_message("Welcome to the File Browser."))
        await self._send_directory_listing(session)

    async def _handle_file_browser_cd(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_CD - Select an authorized folder."""
        directory = data.decode('latin-1', errors='replace')
        if not self._valid_browser_path(directory):
            return
        # PlayerRCPackets.cpp:1197
        directory = directory.rstrip('/')
        if directory not in self._folder_map(session):
            return
        session.file_browser_path = directory
        session.file_browser_active = True
        await self._send_file_browser_message(
            session, f"Folder changed to {directory}/")
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
        error = self._file_access_error(session, path, 'r')
        if error:
            await self._send_file_browser_message(session, error)
            return
        # PlayerRCPackets.cpp:1267
        if (self._protected_path(path) in PROTECTED_DOWNLOADS
                and not session.has_right(RCRight.MODIFYSTAFFACCOUNT)):
            await self._send_file_browser_message(
                session, f"Insufficient rights to download/view {path}")
            return
        resolved = self._resolve_browser_path(path)
        filesystem = self.server.filesystem
        # Send this exact resolved file; the general lookup searches other folders.
        if resolved.is_file():
            if resolved.stat().st_size > filesystem.large_file_threshold:
                await filesystem._send_large_file(session.player, filename, resolved)
            else:
                await filesystem._send_file(session.player, filename, resolved)
        else:
            from .protocol.packets import build_file_send_failed
            await session.player.send_raw(build_file_send_failed(filename))

    async def _handle_file_browser_up(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_UP - Upload file."""
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
        dst = dest_dir.rstrip('/') + "/" + filename if dest_dir else filename
        # PlayerRCPackets.cpp:1367: source veto, even for staff administrators.
        # The reference only vetoes the source; an important file as the
        # destination would be overwritten by the rename, so it is vetoed too.
        if (self._protected_path(src) in IMPORTANT_FILES
                or self._protected_path(dst) in IMPORTANT_FILES):
            await self._send_file_browser_message(
                session, f"Not allowed to move file {src}")
            return
        if not await self._check_file_paths(session, src, dst):
            return
        if self.server.filesystem.move_file(src, dst):
            message = f"Moved file {src} to {dst}."
        else:
            message = f"Could not move file {src} to {dst}."
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _handle_file_browser_delete(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_DELETE - Delete file."""
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
        # PlayerRCPackets.cpp:1404
        if self._protected_path(path) in IMPORTANT_FILES:
            await self._send_file_browser_message(
                session, f"Not allowed to delete file {path}")
            return
        if not await self._check_file_paths(session, path):
            return
        deleted = self.server.filesystem.delete_file(path)
        message = (f"Deleted file {filename}." if deleted
                   else f"Could not delete file {filename}.")
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _handle_file_browser_rename(self, session: RCSession, data: bytes):
        """Handle RC_FILEBROWSER_RENAME - Rename file."""
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
        # PlayerRCPackets.cpp:1472
        if (self._protected_path(old_path) in IMPORTANT_FILES
                or self._protected_path(new_path) in IMPORTANT_FILES):
            await self._send_file_browser_message(
                session, f"Not allowed to rename/overwrite file {old_path} or {new_path}")
            return
        if not await self._check_file_paths(session, old_path, new_path):
            return
        renamed = self.server.filesystem.move_file(old_path, new_path)
        message = (f"Renamed file {old_name} to {new_name}." if renamed
                   else f"Could not rename file {old_name} to {new_name}.")
        await self._send_file_browser_message(session, message)
        await self._send_directory_listing(session)

    async def _handle_folder_delete(self, session: RCSession, data: bytes):
        """Handle RC_FOLDERDELETE - Delete folder."""
        reader = PacketReader(data)
        folder = reader.remaining().decode('latin-1', errors='replace')

        if not self._valid_browser_path(folder) or not folder:
            await self._send_file_browser_message(
                session, f"Invalid folder name: {folder}")
            return
        if not hasattr(self.server, 'filesystem'):
            await self._send_file_browser_message(
                session, "File browser storage is unavailable.")
            return
        path = folder.rstrip('/')
        if not await self._check_file_paths(session, path + '/'):
            return
        # PlayerRCPackets.cpp:1577: only an empty directory can be removed.
        resolved = self._resolve_browser_path(path)
        deleted = False
        if resolved != self._resolve_browser_path(""):
            try:
                resolved.rmdir()
                deleted = True
            except OSError:
                pass
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
                and not PureWindowsPath(normalized).drive
                and ':' not in normalized
                and '..' not in normalized.split('/'))

    @staticmethod
    def _browser_path(session: RCSession, name: str) -> str:
        """Join a direct child name to the browser's current folder."""
        if session.file_browser_path:
            return session.file_browser_path.rstrip('/') + "/" + name
        return name

    def _upload_error(
            self, session: RCSession, filename: str, size: int) -> Optional[str]:
        """Return a user-visible upload rejection, or None when allowed."""
        if not session.file_browser_active:
            return "Upload refused: file browser is not active."
        if not self._valid_browser_name(filename):
            return f"Upload refused: invalid filename {filename}."
        path = self._browser_path(session, filename)
        error = self._file_access_error(session, path, 'w')
        if error:
            return error
        # PlayerRCPackets.cpp:1317-1328
        required = IMPORTANT_FILES.get(self._protected_path(path))
        if (required is not None
                and not session.has_right(RCRight.MODIFYSTAFFACCOUNT)
                and not session.has_right(required)):
            return f"Insufficent rights to upload {path}"
        if size > self.max_upload_size:
            return (f"Upload refused: {filename} exceeds the maximum upload "
                    f"size of {self.max_upload_size} bytes.")
        if not hasattr(self.server, 'filesystem'):
            return "Upload refused: file browser storage is unavailable."
        return None

    async def _send_directory_listing(self, session: RCSession):
        """Send only matching files inside the selected authorized folder."""
        path = session.file_browser_path
        if (path.rstrip('/') not in self._folder_map(session)
                or self._resolve_browser_path(path) is None):
            return
        files = []
        rights = {}
        # PlayerRCPackets.cpp:1136-1154: match each filename's wildcard.
        for info in self.server.filesystem.list_directory(path):
            candidate = self._browser_path(session, info.name)
            entry_rights = self._file_rights(session, candidate)
            if (not info.is_directory and entry_rights
                    and self._resolve_browser_path(candidate) is not None):
                files.append(info)
                rights[info.name] = entry_rights
        packet = build_rc_file_browser_dir(path, files, rights=rights)
        await session.player.send_raw(packet)

    def _get_file_browser_folders(self, session: RCSession) -> List[str]:
        """Return stored folder rights; an empty list grants no access."""
        account = None
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(
                session.player.account_name)
        folders = getattr(account, 'folders', None)
        if folders is None:
            folders = getattr(account, 'folder_list', None)
        return list(folders) if isinstance(folders, (list, tuple)) else []

    def _folder_map(self, session: RCSession) -> Dict[str, list]:
        result = {}
        for entry in self._get_file_browser_folders(session):
            rights, separator, pattern = entry.partition(' ')
            pattern = pattern.strip().replace('\\', '/')
            if not separator or not pattern or rights.startswith('-'):
                continue
            if pattern.endswith('/'):
                folder, wildcard = pattern.rstrip('/'), '*'
            else:
                folder, _, wildcard = pattern.rpartition('/')
            if folder == '.':
                folder = ''
            if self._resolve_browser_path(folder) is not None:
                result.setdefault(folder, []).append((rights.lower(), wildcard))
        return result

    def _file_rights(self, session: RCSession, path: str) -> str:
        # FilePermissions.cpp:19-33,80-94: deny first, match equal segment counts.
        allowed = set()
        denied = set()
        for entry in self._get_file_browser_folders(session):
            flags, separator, pattern = entry.partition(' ')
            pattern = pattern.strip().replace('\\', '/')
            if not separator:
                continue
            if pattern.startswith('./'):
                pattern = pattern[2:]
            if pattern.endswith('/'):
                pattern += '*'
            segments = path.split('/')
            patterns = pattern.split('/')
            if len(segments) != len(patterns):
                continue
            if all(_wildcard_match(p, segment)
                   for p, segment in zip(patterns, segments)):
                (denied if flags.startswith('-') else allowed).update(flags.lower())
        return ''.join(flag for flag in 'rw' if flag in allowed and flag not in denied)

    def _resolve_browser_path(self, path: str) -> Optional[Path]:
        if not self._valid_browser_path(path):
            return None
        base = getattr(getattr(self.server, 'filesystem', None), 'base_path', None)
        if not isinstance(base, (str, os.PathLike)):
            return None
        try:
            base = Path(base).resolve()
            resolved = (base / path.replace('\\', '/')).resolve()
            resolved.relative_to(base)
            return resolved
        except (ValueError, OSError, RuntimeError):
            return None

    def _protected_path(self, path: str) -> str:
        resolved = self._resolve_browser_path(path)
        base = self._resolve_browser_path('')
        if resolved is None or base is None:
            return path
        relative = resolved.relative_to(base).as_posix()
        if relative == 'accounts/(defaultaccount).json':
            return 'accounts/(defaultaccount).txt'
        return relative

    def _file_access_error(self, session: RCSession, path: str,
                           flag: str) -> Optional[str]:
        if self._resolve_browser_path(path) is None:
            return "Invalid file path."
        if not session.file_browser_active:
            return "File browser is not active."
        if flag not in self._file_rights(session, path):
            return ("Upload refused: current folder is read-only." if flag == 'w'
                    else "Insufficient folder rights to read file.")
        return None

    async def _check_file_paths(self, session: RCSession, *paths: str) -> bool:
        for path in paths:
            error = self._file_access_error(session, path, 'w')
            if error:
                await self._send_file_browser_message(session, error)
                return False
        return True

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

    async def _require_right(self, session: RCSession, right: RCRight,
                             message: str) -> bool:
        if session.has_right(right):
            return True
        if right == RCRight.SETSERVEROPTIONS:
            logger.info("%s attempted to set the server options.", session.player.account_name)
        elif right == RCRight.SETFOLDEROPTIONS:
            logger.info("%s attempted to set the folder config.", session.player.account_name)
        await self._reply(session, message)
        return False

    @staticmethod
    def _is_own_account(session: RCSession, name: str) -> bool:
        """Whether `name` is the sender's account. Accounts are stored and
        looked up case-insensitively (account.py get_account), so a
        wrong-case spelling of the sender's own name is the same account."""
        return name.lower() == session.player.account_name.lower()

    async def _can_set_attributes(self, session: RCSession, name: str) -> bool:
        # PlayerRCPackets.cpp:213,632.
        right = (RCRight.SETSELFATTRIBUTES if self._is_own_account(session, name)
                 else RCRight.SETATTRIBUTES)
        if not session.has_right(right):
            logger.info("%s attempted to set a player's properties.",
                        session.player.account_name)
        return await self._require_right(
            session, right,
            f"Server: {session.player.account_name} is not authorized to set the properties of {name}")

    def _set_account_metadata(self, session: RCSession, account: Any,
                              email: str, load_only: bool, banned: bool,
                              reason: str):
        account.profile_email = email
        account.load_only = load_only
        # PlayerRCPackets.cpp:425,732
        if session.has_right(RCRight.BAN):
            account.is_banned = banned
            account.ban_reason = reason
        self.server.account_manager.save_account(account)

    async def _drop_nc_sessions(self, account_name: str):
        nc_manager = getattr(self.server, 'nc_manager', None)
        if nc_manager is None:
            return
        for nc_session in nc_manager.get_all_sessions():
            player = nc_session.player
            if player.account_name.lower() == account_name.lower():
                nc_manager.unregister_session(player.id)
                await player.disconnect()

    def _valid_account_path(self, name: str) -> bool:
        if not self._valid_browser_name(name) or ':' in name:
            return False
        directory = getattr(getattr(self.server, 'account_manager', None),
                            'accounts_dir', None)
        if isinstance(directory, (str, os.PathLike)):
            base = Path(directory).resolve()
            try:
                (base / (name + '.json')).resolve().relative_to(base)
            except (ValueError, OSError, RuntimeError):
                return False
        return True

    @staticmethod
    def _decode_lines(value: str) -> List[str]:
        if '\n' in value:
            return value.splitlines()
        return next(csv.reader([value]), [])

    @staticmethod
    def _encode_lines(lines: List[str]) -> str:
        output = io.StringIO()
        csv.writer(output, lineterminator='').writerow(lines)
        return output.getvalue()

    async def _broadcast_rc_message(self, message: str):
        """Broadcast a message to all RC sessions."""
        packet = build_rc_chat(message)
        await self.broadcast_to_rcs(packet)
        logger.info(f"[RC] {message}")
