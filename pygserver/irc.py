"""Server-local serverlist-chat / IRC channels (the Login chat weapon's leg).

pygserver acts as its own list server here: the combined gserver+lister
behavior from GServer-v2 (server/src/player/PlayerRequestText.cpp,
server/src/ServerList.cpp) and the open-source lister
(graal-serverlist/server/src/ServerConnection.cpp, IrcServer.cpp) is
implemented in-process, so two local clients can chat without any external
lister leg.

Wire surface (PLI_REQUESTTEXT 152 / PLI_SENDTEXT 154 in, PLO_SERVERTEXT 82 /
pseudo-player packets out): the payload is CString::gtokenize comma-text whose
first field is the sending weapon's name (the client engine prepends it), then
texttype, textoption, params. Channels are materialized to clients as
pseudo-players with ids >= 16000 (PLAYERID_GEN_EXTERNAL,
GServer-v2/server/include/utilities/CommonTypes.h:122) carrying
ACCOUNTNAME "irc:#channel", NICKNAME "#channel (n,0)" and the
PLAYERLISTCATEGORY bit-flags prop.

Two deliberate divergences from GServer-v2, both flagged in the audited spec:
- part actually removes membership (GServer-v2 defines but never calls
  Player::removeChatChannel, so parted players keep receiving relays);
- PLAYERLISTCATEGORY carries the official client's full flag set (the live
  server sends external|channel = 3; GServer-v2 sends only EXTERNAL = 1).
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .protocol.constants import PLO, PLPROP
from .protocol.packets import PacketBuilder, build_other_player_props

logger = logging.getLogger(__name__)

# PLAYERID_GEN_EXTERNAL (CommonTypes.h:122); ceiling is the gshort encoding
# range of this codec's two-byte writer.
EXTERNAL_ID_BASE = 16000
EXTERNAL_ID_MAX = 28767
MAX_CHANNELS_PER_PLAYER = 32
MAX_TOTAL_CHANNELS = 256
MAX_CHANNEL_NAME_LENGTH = 32

# PLAYERLISTCATEGORY (prop 81) bit-flags as the official client decodes them
# (Preagonal/FourPlay/quattroplay/src/TServerPlayer.cpp:1942-1954): 1 =
# isexternal, 2 = ischannel, 4 = ischanneluser, 8 = ischannelopen. The chat
# weapon's double-click join/part branch keys on ischannelopen.
CATEGORY_EXTERNAL = 0x1
CATEGORY_CHANNEL = 0x2
CATEGORY_CHANNELUSER = 0x4
CATEGORY_CHANNELOPEN = 0x8

_PROP_DISCONNECT = PLPROP.PCONNECTED        # 51, "DISCONNECT" in GServer-v2
_PROP_LISTCATEGORY = PLPROP.UNKNOWN81       # 81, PLAYERLISTCATEGORY


def gtokenize(fields: Sequence[str]) -> str:
    """Join fields the way CString::gtokenize joins lines into comma-text.

    A field is quoted (with '"' and '\\' doubled) if it starts with a quote,
    is blank/whitespace, or contains a non-printable, ',' or '/'.
    """
    tokens = []
    for f in fields:
        f = str(f).replace('\r', '').replace('\n', '')
        if f == '':
            tokens.append('')
            continue
        needs_quotes = (f[0] == '"' or f.strip() == '' or
                        any(ord(c) < 33 or ord(c) > 126 or c in ',/'
                            for c in f))
        if needs_quotes:
            esc = f.replace('\\', '\\\\').replace('"', '""')
            tokens.append('"' + esc + '"')
        else:
            tokens.append(f)
    return ','.join(tokens)


def guntokenize(text: str) -> List[str]:
    """Split CString::gtokenize comma-text back into fields (inverse of
    gtokenize; doubled quotes/backslashes inside a quoted field collapse)."""
    tokens: List[str] = []
    i, n = 0, len(text)
    while i <= n:
        if i < n and text[i] == '"':
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(c)
                i += 1
            tokens.append(''.join(buf).replace('\\\\', '\\'))
            if i < n and text[i] == ',':
                i += 1
            elif i >= n:
                break
        else:
            end = text.find(',', i)
            if end == -1:
                tokens.append(text[i:])
                break
            tokens.append(text[i:end])
            i = end + 1
    return tokens


def build_server_text_tokens(fields: Sequence[str]) -> bytes:
    """Build PLO_SERVERTEXT(82) carrying gtokenized fields
    (weapon, texttype, textoption, textlines...)."""
    builder = PacketBuilder().write_gchar(PLO.SERVERTEXT)
    builder.write_string(gtokenize(fields))
    builder.write_newline()
    return builder.build()


@dataclass
class IrcChannel:
    """One chat channel and its pseudo-player materialization."""
    name: str                                     # display name, "#..."
    pseudo_id: int
    # member player id -> that member's channel-user pseudo-player id
    members: Dict[int, int] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.name.lower()


class IrcManager:
    """Channel state + the PLI_REQUESTTEXT/PLI_SENDTEXT text-op surface."""

    DEFAULT_CHANNEL = "#reborn"

    def __init__(self, server):
        self.server = server
        self.channels: Dict[str, IrcChannel] = {}
        # player ids that sent irc,login (they receive roster pushes)
        self.sessions: Set[int] = set()
        self._next_external_id = EXTERNAL_ID_BASE
        # pseudo id -> ("channel", key) | ("member", key, player_id)
        self._external_index: Dict[int, Tuple] = {}
        # The platform channel always exists (the live Login lister always
        # reports exactly one such channel); it survives its last part.
        self._get_or_create_channel(self.DEFAULT_CHANNEL)

    # =========================================================================
    # Text-op entry points (called from handlers/misc.py)
    # =========================================================================

    async def handle_request_text(self, player, text: str):
        """PLI_REQUESTTEXT: information queries (PlayerRequestText.cpp
        msgPLI_REQUESTTEXT; the irc type is a server-side no-op there)."""
        fields = guntokenize(text)
        weapon = fields[0] if len(fields) > 0 else ""
        texttype = fields[1] if len(fields) > 1 else ""
        option = fields[2] if len(fields) > 2 else ""

        if texttype == "lister" and option in ("simplelist", "simpleserverlist"):
            await self._send_simple_server_list(player, weapon)
        else:
            logger.debug(f"[irc] requesttext unhandled from "
                         f"{player.account_name}: {text!r}")

    async def handle_send_text(self, player, text: str):
        """PLI_SENDTEXT: commands (PlayerRequestText.cpp msgPLI_SENDTEXT)."""
        fields = guntokenize(text)
        texttype = fields[1] if len(fields) > 1 else ""
        option = fields[2] if len(fields) > 2 else ""

        if texttype != "irc":
            logger.debug(f"[irc] sendtext unhandled from "
                         f"{player.account_name}: {text!r}")
            return

        if option == "login":
            await self._irc_login(player)
        elif option == "join" and len(fields) > 3:
            await self.join_channel(player, fields[3])
        elif option == "part" and len(fields) > 3:
            await self.part_channel(player, fields[3])
        elif option == "privmsg" and len(fields) > 4:
            await self.channel_privmsg(player, fields[3], fields[4])

    # =========================================================================
    # IRC flows
    # =========================================================================

    async def _irc_login(self, player):
        """irc,login,-: enroll the viewer and materialize every existing
        channel (and channel user) as pseudo-players."""
        self.sessions.add(player.id)
        for channel in self.channels.values():
            await player.send_raw(self._channel_props_packet(channel, player))
            for member_id, member_pseudo in channel.members.items():
                member = self.server.get_player(member_id)
                if member:
                    await player.send_raw(self._member_props_packet(
                        member, channel, member_pseudo, player))

    async def join_channel(self, player, channel_name: str):
        normalized = self._normalize_channel_name(channel_name)
        if normalized is None:
            logger.debug(f"[irc] invalid channel from "
                         f"{player.account_name}: {channel_name!r}")
            return
        key = normalized.lower()
        channel = self.channels.get(key)
        if channel is not None and player.id in channel.members:
            # ServerList.cpp:955 confirms only when addChatChannel is new
            return

        is_default = key == self.DEFAULT_CHANNEL.lower()
        joined_count = sum(player.id in item.members
                           for item in self.channels.values())
        if not is_default and joined_count >= MAX_CHANNELS_PER_PLAYER:
            logger.debug(f"[irc] channel limit reached by "
                         f"{player.account_name}")
            return
        if (channel is None and not is_default
                and len(self.channels) >= MAX_TOTAL_CHANNELS):
            logger.debug("[irc] global channel limit reached")
            return
        required_ids = 2 if channel is None else 1
        capacity = EXTERNAL_ID_MAX - EXTERNAL_ID_BASE + 1
        configured_players = max(
            0, int(getattr(getattr(self.server, "config", None),
                           "max_players", 100)))
        # Non-default joins leave one member id per configured player reserved
        # for the persistent channel.
        if (not is_default
                and len(self._external_index) + required_ids
                > capacity - configured_players):
            logger.info("[irc] external id headroom reached")
            return

        channel_pseudo = None
        if channel is None:
            channel_pseudo = self._allocate_external_id(("channel", key))
            if channel_pseudo is None:
                logger.info("[irc] channel id allocation refused")
                return
        member_pseudo = self._allocate_external_id(
            ("member", key, player.id))
        if member_pseudo is None:
            if channel_pseudo is not None:
                self._external_index.pop(channel_pseudo, None)
            logger.info("[irc] member id allocation refused")
            return

        if channel is None:
            channel = IrcChannel(name=normalized, pseudo_id=channel_pseudo)
            self.channels[key] = channel
        channel.members[player.id] = member_pseudo
        # join implies roster interest even if the login op was never sent
        self.sessions.add(player.id)

        await player.send_raw(build_server_text_tokens(
            [self._weapon_for(player), "irc", "join", channel.name]))
        await self._push_channel_update(channel)
        await self._push_member_add(player, channel)

    async def part_channel(self, player, channel_name: str):
        channel = self.channels.get(str(channel_name).lower())
        if not channel or player.id not in channel.members:
            return
        member_pseudo = channel.members.pop(player.id)
        self._external_index.pop(member_pseudo, None)

        await player.send_raw(build_server_text_tokens(
            [self._weapon_for(player), "irc", "part", channel.name]))
        await self._push_member_remove(member_pseudo)

        if not channel.members and channel.key != self.DEFAULT_CHANNEL.lower():
            # IrcServer.cpp:123-157: channels die with their last member
            del self.channels[channel.key]
            self._external_index.pop(channel.pseudo_id, None)
            await self._push_channel_remove(channel.pseudo_id)
        else:
            await self._push_channel_update(channel)

    async def channel_privmsg(self, player, channel_name: str, message: str):
        """Relay a channel message to every member, INCLUDING the sender if a
        member (the sender's echo, ServerList.cpp handleText:385-411); a
        non-member sender reaches the members but gets no echo."""
        channel = self.channels.get(str(channel_name).lower())
        if not channel:
            logger.debug(f"[irc] privmsg from {player.account_name} to "
                         f"unknown target {channel_name!r} dropped")
            return
        for member_id in list(channel.members):
            member = self.server.get_player(member_id)
            if member:
                await member.send_raw(build_server_text_tokens(
                    [self._weapon_for(member), "irc", "privmsg",
                     player.account_name, channel.name, message]))

    async def route_external_pm(self, sender, target_id: int,
                                message: str) -> bool:
        """PLI_PRIVATEMESSAGE aimed at a pseudo-player id (>= 16000).

        A channel-user pseudo resolves to its local real player (ordinary
        PLO_PRIVATEMESSAGE); a channel pseudo is treated as a message to the
        channel. There is no external lister, so nothing to forward otherwise
        (the cross-server SVO_PMPLAYER leg, PlayerExternalPlayers.cpp:192-206,
        is dead upstream anyway)."""
        entry = self._external_index.get(target_id)
        if entry is None:
            return False
        if entry[0] == "member":
            target = self.server.get_player(entry[2])
            if target:
                from .protocol.packets import build_private_message
                await target.send_raw(build_private_message(
                    sender.id, sender.nickname, message))
            return True
        if entry[0] == "channel":
            channel = self.channels.get(entry[1])
            if channel:
                await self.channel_privmsg(sender, channel.name, message)
            return True
        return False

    async def remove_player(self, player):
        """Disconnect cleanup: part everything, drop the roster session."""
        self.sessions.discard(player.id)
        for channel in list(self.channels.values()):
            if player.id in channel.members:
                member_pseudo = channel.members.pop(player.id)
                self._external_index.pop(member_pseudo, None)
                await self._push_member_remove(member_pseudo)
                if (not channel.members
                        and channel.key != self.DEFAULT_CHANNEL.lower()):
                    del self.channels[channel.key]
                    self._external_index.pop(channel.pseudo_id, None)
                    await self._push_channel_remove(channel.pseudo_id)
                else:
                    await self._push_channel_update(channel)

    # =========================================================================
    # lister ops
    # =========================================================================

    async def _send_simple_server_list(self, player, weapon: str):
        """lister,simplelist: we are our own lister, so answer with the one
        entry describing this server. Reply shape per the lister
        (graal-serverlist/server/src/ServerConnection.cpp:1480-1505):
        fields = weapon, "lister", "simpleserverlist", then one gtokenized
        (name, typed-name, playercount) triple per server; GServer relays the
        confirm verbatim as PLO_SERVERTEXT (ServerList.cpp:1005-1008)."""
        name = getattr(getattr(self.server, 'config', None), 'name',
                       'pygserver')
        count = str(self.server.get_player_count()
                    if hasattr(self.server, 'get_player_count') else 0)
        entry = gtokenize([name, "U " + name, count])
        await player.send_raw(build_server_text_tokens(
            [weapon, "lister", "simpleserverlist", entry]))

    # =========================================================================
    # Pseudo-player packets
    # =========================================================================

    def _weapon_for(self, player) -> str:
        """Receiver-dependent weapon field rewrite (ServerList.cpp:947)."""
        return "GraalEngine" if self._is_rc(player) else "-Serverlist_Chat"

    def _is_rc(self, player) -> bool:
        rc = getattr(self.server, 'rc_manager', None)
        return bool(rc and rc.is_rc(player.id))

    def _channel_flags(self, channel: IrcChannel, viewer) -> int:
        flags = CATEGORY_EXTERNAL | CATEGORY_CHANNEL
        if viewer is not None and viewer.id in channel.members:
            flags |= CATEGORY_CHANNELOPEN
        return flags

    def _channel_props_packet(self, channel: IrcChannel, viewer) -> bytes:
        """The channel as a pseudo-player. Live Login reference bytes (spec
        §2a): id 16000, ACCOUNTNAME "irc:#channel", NICKNAME "#channel (1,0)",
        PLAYERLISTCATEGORY 3. The nick's parenthesized first column is the
        user count (FourPlay getChannelFullNick), so ours carries the real
        member count; props are emitted in ascending id order (the ecosystem
        parser convention), unlike the live capture's 34,0,81."""
        account = "irc:" + channel.name
        nick = f"{channel.name} ({len(channel.members)},0)"
        flags = self._channel_flags(channel, viewer)
        if viewer is not None and self._is_rc(viewer):
            return self._build_add_player(channel.pseudo_id, account, nick,
                                          flags)
        return build_other_player_props(channel.pseudo_id, {
            PLPROP.NICKNAME: nick,
            PLPROP.ACCOUNTNAME: account,
            _PROP_LISTCATEGORY: flags,
        })

    def _member_props_packet(self, member, channel: IrcChannel,
                             pseudo_id: int, viewer) -> bytes:
        """A channel member as a channel-user pseudo-player: nick
        "nick (on #channel)" (the shape updatePMPlayers uses for externals,
        PlayerExternalPlayers.cpp:113), flags channeluser|external."""
        nick = f"{member.nickname} (on {channel.name})"
        flags = CATEGORY_EXTERNAL | CATEGORY_CHANNELUSER
        if viewer is not None and self._is_rc(viewer):
            return self._build_add_player(pseudo_id, member.account_name,
                                          nick, flags)
        return build_other_player_props(pseudo_id, {
            PLPROP.NICKNAME: nick,
            PLPROP.ACCOUNTNAME: member.account_name,
            _PROP_LISTCATEGORY: flags,
        })

    @staticmethod
    def _build_add_player(pseudo_id: int, account: str, nick: str,
                          flags: int) -> bytes:
        """PLO_ADDPLAYER(55), the RC shape (PlayerRequestText.cpp:166):
        {gshort id}{gchar len}{account}{prop 0 nick}{prop 81 flags}."""
        builder = PacketBuilder().write_gchar(PLO.ADDPLAYER)
        builder.write_gshort(pseudo_id)
        builder.write_gstring(account)
        builder.write_gchar(PLPROP.NICKNAME).write_gstring(nick)
        builder.write_gchar(_PROP_LISTCATEGORY).write_gchar(flags)
        builder.write_newline()
        return builder.build()

    def _remove_packet(self, pseudo_id: int, viewer) -> bytes:
        """Pseudo-player removal (PlayerExternalPlayers.cpp:123-127):
        PLO_DELPLAYER for RC, OTHERPLPROPS + the void DISCONNECT prop (51)
        for clients."""
        if viewer is not None and self._is_rc(viewer):
            builder = PacketBuilder().write_gchar(PLO.DELPLAYER)
            builder.write_gshort(pseudo_id)
            builder.write_newline()
            return builder.build()
        return build_other_player_props(pseudo_id, {_PROP_DISCONNECT: None})

    # =========================================================================
    # Roster pushes
    # =========================================================================

    def _session_players(self):
        for pid in list(self.sessions):
            player = self.server.get_player(pid)
            if player is None:
                self.sessions.discard(pid)
            else:
                yield player

    async def _push_channel_update(self, channel: IrcChannel):
        for viewer in self._session_players():
            await viewer.send_raw(self._channel_props_packet(channel, viewer))

    async def _push_channel_remove(self, pseudo_id: int):
        for viewer in self._session_players():
            await viewer.send_raw(self._remove_packet(pseudo_id, viewer))

    async def _push_member_add(self, member, channel: IrcChannel):
        pseudo_id = channel.members[member.id]
        for viewer in self._session_players():
            await viewer.send_raw(self._member_props_packet(
                member, channel, pseudo_id, viewer))

    async def _push_member_remove(self, pseudo_id: int):
        for viewer in self._session_players():
            await viewer.send_raw(self._remove_packet(pseudo_id, viewer))

    # =========================================================================
    # Allocation
    # =========================================================================

    @staticmethod
    def _normalize_channel_name(name: str) -> Optional[str]:
        name = str(name)
        if (not name.startswith("#")
                or len(name) > MAX_CHANNEL_NAME_LENGTH
                or any(ord(char) < 33 or ord(char) > 126
                       for char in name)):
            return None
        return name

    def _get_or_create_channel(self, name: str) -> Optional[IrcChannel]:
        key = str(name).lower()
        channel = self.channels.get(key)
        if channel is None:
            pseudo_id = self._allocate_external_id(("channel", key))
            if pseudo_id is None:
                return None
            channel = IrcChannel(name=str(name), pseudo_id=pseudo_id)
            self.channels[key] = channel
        return channel

    def _allocate_external_id(self, entry: Tuple) -> Optional[int]:
        for _ in range(EXTERNAL_ID_MAX - EXTERNAL_ID_BASE + 1):
            candidate = self._next_external_id
            self._next_external_id += 1
            if self._next_external_id > EXTERNAL_ID_MAX:
                self._next_external_id = EXTERNAL_ID_BASE
            if candidate not in self._external_index:
                self._external_index[candidate] = entry
                return candidate
        return None
