"""Board edits, profiles, and the remaining server/misc client packets."""

import logging

from ..board_changes import respawn_context, send_near
from ..protocol.constants import PLI
from ..protocol.packets import PacketReader
from .registry import handles
from .versions import client_version

# CLVER_2_1: clients before this lay their own bush and vase drops.
CLIENT_DROPS_ITEMS_SINCE = 2.1

logger = logging.getLogger(__name__)


class MiscHandlers:
    """Mixin: board/profile/server/text packets."""

    @handles(PLI.BOARDMODIFY)
    async def _handle_board_modify(self, data: bytes):
        """Handle PLI_BOARDMODIFY packet."""
        if not self.level:
            return

        # Parse modification data
        reader = PacketReader(data)
        x = reader.read_gchar()
        y = reader.read_gchar()
        w = reader.read_gchar()
        h = reader.read_gchar()
        tile_data = reader.remaining()

        # Validate bounds
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            return
        if x + w > 64 or y + h > 64:
            return

        # Check permissions (require admin rights for permanent changes)
        # For now, allow all players to modify tiles (temporary changes)
        # Permanent changes would require: if not (self.admin_rights & PLPERM.UPDATELEVEL):

        # Apply tile changes
        expected_size = w * h * 2  # 2 bytes per tile
        if len(tile_data) < expected_size:
            return

        # Level::alterBoard (PlayerClientPackets.cpp:125, Level.cpp:1610-1749):
        # the change is recorded beside the level's tile array, a 2x2 one
        # respawns after `respawntime` seconds (board_changes.py).
        tiles = [reader.read_gshort() for _ in range(w * h)]
        now, respawn_seconds = respawn_context(self.server)
        corrections = []
        accepted = self.level.alter_board(x, y, w, h, tiles, now, respawn_seconds,
                                          corrections=corrections)

        # Relayed to everyone else (PlayerClientPackets.cpp:125-134): the
        # level's players, and on a grid map the players of the segments
        # around this one (Server::sendPacketToNearby, Server.cpp:2390-2413).
        if accepted is not None:
            from ..protocol.packets import build_board_modify, build_board_modify2
            gmap_info = self.server.world.get_gmap_for_level(self.level.name)
            if gmap_info:
                _, map_x, map_y = gmap_info
                packet = build_board_modify2(
                    map_x, map_y, x, y, w, h, tile_data[:expected_size]
                )
            else:
                packet = build_board_modify(
                    x, y, w, h, tile_data[:expected_size]
                )
            await send_near(self.server, self.level.name, packet,
                            gmap_info=gmap_info, exclude={self.id})
        # Entries evicted at the list bound (board_changes.py): their
        # rebuilt rectangles go to the same players, the sender included.
        # Each already shows this change, so the order does not matter.
        for fix in corrections:
            await self.server.board_changes.send_to_level(self.level, fix)

        # Items laid by cutting a bush or breaking a vase
        # (PlayerClientPackets.cpp:139-166). Older clients drop them
        # themselves (:140); the cut is relayed either way (:125-133). The
        # tile tested is the level's base tile (:144-148), which board changes
        # never rewrite, so every cut rolls again, including a repeat of the
        # same packet before the tile respawns; only the level's item cap
        # (ItemManager.max_items_per_level) bounds how many items that makes.
        if client_version(self) >= CLIENT_DROPS_ITEMS_SINCE:
            item_manager = getattr(self.server, 'item_manager', None)
            if item_manager is not None:
                await item_manager.drop_board_item(
                    self, x, y, self.level.get_base_tile(x, y))

    @handles(PLI.REQUESTUPDATEBOARD)
    async def _handle_request_update_board(self, data: bytes):
        """Handle PLI_REQUESTUPDATEBOARD packet."""
        if self.level:
            # Resend level board
            await self._send_level(self.level)

    @handles(PLI.PROFILEGET)
    async def _handle_profile_get(self, data: bytes):
        """Handle PLI_PROFILEGET packet (request another player's profile).

        Payload is the raw target account name, no length prefix (see
        build_profile_get in pyReborn). GServer-v2 just forwards this to the
        list server as SVO_GETPROF and relays whatever SVI_PROFILE comes
        back; pygserver has no such external profile service, so it answers
        from the locally-persisted account profile fields instead (see
        ProfileManager).
        """
        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'profile_manager'):
            profile = self.server.profile_manager.get_profile(account_name)
            if not profile:
                return
            from ..protocol.packets import build_profile
            packet = build_profile(profile['account'], profile, profile.get('online_time', ''))
            await self.send_raw(packet)

    @handles(PLI.PROFILESET)
    async def _handle_profile_set(self, data: bytes):
        """Handle PLI_PROFILESET packet (update our own profile).

        Payload: {GCHAR len}{account} then 9 free-text fields. GServer-v2
        (Player.cpp msgPLI_PROFILESET) rejects the packet outright if the
        embedded account name isn't the sender's own - mirror that here
        before persisting anything.
        """
        from ..protocol.packets import parse_profile
        profile_data = parse_profile(data)

        if profile_data.get('account') != self.account_name:
            return

        if hasattr(self.server, 'profile_manager'):
            self.server.profile_manager.set_profile(self, profile_data)

    @handles(PLI.MAPINFO)
    async def _handle_map_info(self, data: bytes):
        """Handle PLI_MAPINFO packet.

        PLI_MAPINFO (39) is defined in GServer-v2's own packet enum
        (dependencies/gs2lib/include/IEnums.h) but is never wired to a
        handler there either (absent from IPacketHandler.h's
        FOR_INPUT_PACKETS list) - the reference server silently drops it
        too. True no-op, not a missing feature.
        """
        pass

    @handles(PLI.SERVERWARP)
    async def _handle_server_warp(self, data: bytes):
        """Handle PLI_SERVERWARP packet (warp to another server).

        GServer-v2 (PlayerClientPackets.cpp msgPLI_SERVERWARP) just forwards
        this to the connected list server as SVO_SERVERINFO ({GUSHORT player
        id}{raw server name}); the list server looks up the named server and
        replies with SVI_SERVERINFO, which is relayed back to the client
        verbatim as PLO_SERVERWARP (see ServerListClient.request_server_info
        / _handle_server_info). A single pygserver instance has no server
        directory of its own to consult, so without a live list server
        connection there's nowhere to look this up - log and drop.
        """
        reader = PacketReader(data)
        server_name = reader.remaining().decode('latin-1', errors='replace')

        listserver = getattr(self.server, 'listserver', None)
        if listserver is not None and listserver.connected:
            await listserver.request_server_info(self.id, server_name)
        else:
            logger.info(
                f"{self.account_name} requested serverwarp to '{server_name}' "
                f"but no list server connection is available"
            )

    @handles(PLI.PACKETCOUNT)
    async def _handle_packet_count(self, data: bytes):
        """Handle PLI_PACKETCOUNT packet."""
        # Client reporting packet count - used for sync checking
        pass

    @handles(PLI.LANGUAGE)
    async def _handle_language(self, data: bytes):
        """Handle PLI_LANGUAGE packet."""
        reader = PacketReader(data)
        language = reader.remaining().decode('latin-1', errors='replace')
        logger.debug(f"Player {self.id} language: {language}")

    @handles(PLI.MUTEPLAYER)
    async def _handle_mute_player(self, data: bytes):
        """Handle PLI_MUTEPLAYER packet.

        Format (IEnums.h comment): {GSHORT playerId}{GBYTE 1/0}. GServer-v2
        lists this in FOR_INPUT_PACKETS for packet-name tracing but never
        assigns it a handler function - muting is purely a client-side
        playerlist feature (it filters chat locally), so the server has
        nothing to do besides not choke on the bytes. Parse for
        observability only; true no-op otherwise.
        """
        reader = PacketReader(data)
        target_id = reader.read_gshort()
        muted = bool(reader.read_gchar())
        logger.debug(f"{self.account_name} {'muted' if muted else 'unmuted'} player id {target_id} (client-local only)")

    @handles(PLI.PROCESSLIST)
    async def _handle_process_list(self, data: bytes):
        """Handle PLI_PROCESSLIST packet.

        GServer-v2 (PlayerClientPackets.cpp msgPLI_PROCESSLIST) detokenizes
        the client's process list and discards it without acting on it -
        this is the same no-op, just with the parse for observability.
        """
        reader = PacketReader(data)
        processes = reader.remaining().decode('latin-1', errors='replace')
        logger.debug(f"{self.account_name} process list: {processes!r}")

    @handles(PLI.CLAIMPKER)
    async def _handle_claim_pker(self, data: bytes):
        """Handle PLI_CLAIMPKER packet."""
        killer = self.server.get_player(PacketReader(data).read_gshort())
        if killer is None or killer is self:
            return
        # GServer-v2/server/src/player/packets/PlayerClientPackets.cpp:405-414.
        killer.flags['playerkiller'] = True
        # Stricter than the reference, which trusts any claim: require a
        # pending, uncredited death. Sword kills already credit server-side
        # in handle_player_death; their victim's follow-up must not count twice.
        if (not getattr(self, 'pending_revive', None)
                or getattr(self, 'kill_credited', False)):
            logger.debug("Ignoring kill claim from player %s: alive or credited", self.id)
            return
        await self.server.combat_manager.credit_kill(killer, self)

    @handles(PLI.RAWDATA)
    async def _handle_raw_data(self, data: bytes):
        """Handle PLI_RAWDATA packet."""
        reader = PacketReader(data)
        size = reader.read_gint3()
        # Raw data follows

    @handles(PLI.REQUESTTEXT)
    async def _handle_request_text(self, data: bytes):
        """Handle PLI_REQUESTTEXT packet.

        Payload is gtokenized `weapon,texttype,textoption[,params...]` (the
        client engine prepends the calling weapon's name; GServer-v2
        PlayerRequestText.cpp msgPLI_REQUESTTEXT). A prior revision here
        implemented a "get server variable" protocol that matched no oracle.
        """
        text = data.decode('latin-1', errors='replace')
        irc = getattr(self.server, 'irc_manager', None)
        if irc is not None:
            await irc.handle_request_text(self, text)

    @handles(PLI.SENDTEXT)
    async def _handle_send_text(self, data: bytes):
        """Handle PLI_SENDTEXT packet (same wire shape as REQUESTTEXT; the
        command half of the text-op surface - irc login/join/part/privmsg)."""
        text = data.decode('latin-1', errors='replace')
        irc = getattr(self.server, 'irc_manager', None)
        if irc is not None:
            await irc.handle_send_text(self, text)

    @handles(PLI.NPCSERVERQUERY)
    async def _handle_npc_server_query(self, data: bytes):
        """Handle PLI_NPCSERVERQUERY packet."""
        # Query about NPC server capabilities
        pass
