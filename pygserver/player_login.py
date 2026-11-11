"""
pygserver.player_login - login handshake and login completion.

Two entry points reach the same finish line, which is why completion lives in
one idempotent place:

1. local verification - Player._handle_login decodes the login packet, checks
   the password itself (or skips the check) and completes inline;
2. list-server verification - _handle_login returns as soon as the SVO_VERIACC2
   request is away, and ServerListClient calls Player.send_login() later, from
   its own task, when SVI_VERIACC2 comes back.

Both used to duplicate the logged-in flag, the list-server player registration
and the starting warp, so a fix landed in only one path. `complete_login` now
owns all three and is a no-op on a second call, which also covers the
ServerListClient.verify_account fallback that calls send_login() directly when
the list server is unreachable.
"""

import asyncio
import logging
import struct
import time
from typing import TYPE_CHECKING

from reborn_protocol.constants import PLTYPE

from .protocol.constants import PLPROP
from .spar_rating import decay_deviation
from .protocol.builders.world import map_option_text
from .protocol.packets import (
    build_bigmap,
    build_minimap,
    build_clear_weapons,
    build_flag_set,
    build_has_npc_server,
    build_player_props,
    build_serverlist_connected,
    build_rc_chat,
    build_rc_max_upload_filesize,
    build_server_mode,
    build_staff_guilds,
    parse_login_packet,
)

if TYPE_CHECKING:
    from .player import Player

logger = logging.getLogger(__name__)

# Protocol strings we have been tested against; anything else is logged and
# still allowed through (a client build we don't know about usually still
# speaks a version we handle).
KNOWN_PROTOCOLS = ('G3D0311C', 'G3D0511C', 'GNW03014')


class LoginService:
    """Drives one player's login from the raw packet to the starting warp."""

    def __init__(self, player: 'Player'):
        self.player = player

    async def handle_login(self) -> bool:
        """Read and process the initial login packet. Returns False to drop."""
        player = self.player
        server = player.server
        try:
            # Read login packet (plain zlib compressed)
            data = await player.session.read(timeout=30.0)
            if not data or len(data) < 2:
                return False

            # Extract length and packet
            length = struct.unpack('>H', data[:2])[0]
            packet_data = data[2:2 + length]

            # Create codec with no encryption key yet
            codec = player.session.start_codec(0)
            decrypted = player.session.decode_packet(packet_data)
            if not decrypted:
                logger.warning(f"Failed to decode login from {player.id}")
                return False

            # Parse login
            login = parse_login_packet(decrypted)
            logger.info(f"Login from {login.get('username', '?')}, protocol={login.get('protocol', '?')}")

            protocol = login.get('protocol', '')
            if protocol not in KNOWN_PROTOCOLS:
                logger.warning(f"Unsupported protocol: {protocol}")

            # Select both directions for the lifetime of this connection.
            generation = login.get('encryption_generation', 5)
            if generation == 2:
                codec = player.session.start_codec(generation=2)
            else:
                codec.set_key(login.get('encryption_key', 0))

            # Store account info
            player.account_name = login.get('username', f'player_{player.id}')
            player.nickname = player.account_name
            raw_client_type = login.get('client_type', 0)
            candidate_type = PLTYPE(1 << raw_client_type) \
                if isinstance(raw_client_type, int) and raw_client_type >= 0 \
                else PLTYPE.CLIENT
            valid_types = {
                member for member in PLTYPE
                if member.value > 0 and member.value & (member.value - 1) == 0
            }
            player.connection_type = candidate_type \
                if candidate_type in valid_types else PLTYPE.CLIENT

            # `onlystaff` needs only the account name, so a refused login must
            # not load or create an account file first (a flood of random names
            # would otherwise fill the accounts directory). RC and NC keep the
            # reference's own refusal text (Player.cpp:685-694 precedes :701).
            if (getattr(server.config, 'onlystaff', False) is True
                    and not server.is_staff(player.account_name)):
                logger.info(f"Refused non-staff login for {player.account_name!r}: staff only")
                await player.disconnect(
                    "You do not have RC rights."
                    if player.connection_type & (PLTYPE.ANYRC | PLTYPE.NC)
                    else "This server is currently restricted to staff only.")
                return False

            account_manager = getattr(server, 'account_manager', None)
            if account_manager:
                account_in_use = any(
                    other is not player and other.connected
                    and other.account_name.lower() == player.account_name.lower()
                    for other in getattr(server, 'players', {}).values()
                )
                # QA fixtures restock ammo/reset position on disk between logins.
                # Refresh idle accounts like GServer-v2/server/src/player/Player.cpp:663-666
                # and server/src/loader/flatfile/FlatFileAccountLoader.cpp:100-116.
                account = account_manager.get_account(
                    player.account_name, reload=not account_in_use)
                if not account:
                    account = account_manager.create_account(player.account_name)
                account_manager.load_player_from_account(player, account)
                player.admin_rights = account.admin_rights

                if account.is_banned:
                    await player.disconnect(f"You are banned: {account.ban_reason}")
                    return False

            password = login.get('password', '')
            if server.config.verify_login and account_manager:
                if not account_manager.verify_password(player.account_name, password):
                    await player.disconnect("Invalid password")
                    return False

            listserver = getattr(server, 'listserver', None)
            if server.config.verify_login and listserver:
                # The list server owns the verdict; it calls back into
                # Player.send_login() -> complete_login() when it answers.
                logger.debug(f"Requesting account verification from listserver for {player.account_name}")
                await listserver.verify_account(player, password)
                return True

            await self.complete_login()
            return True

        except asyncio.TimeoutError:
            logger.warning(f"Login timeout for {player.id}")
            return False
        except Exception as e:
            import traceback
            logger.error(f"Login error for {player.id}: {e}")
            logger.error(traceback.format_exc())
            return False

    async def complete_login(self):
        """Send the login response, register the player and warp them in.

        Idempotent: a second call (list-server verification racing the local
        fallback) does nothing.
        """
        player = self.player
        if player.logged_in:
            logger.debug(f"complete_login: player {player.id} already logged in")
            return

        connection_type = player.connection_type
        is_rc = bool(connection_type & PLTYPE.ANYRC)
        is_nc = connection_type == PLTYPE.NC

        # Player.cpp:686-694 sends the same PLO_DISCMESSAGE to RC and NC.
        if is_rc and not player.server.register_rc_session(player):
            logger.info(f"Refused RC login for {player.account_name}")
            await player.disconnect("You do not have RC rights.")
            return
        if is_nc and not player.server.register_nc_session(player):
            logger.info(f"Refused NC login for {player.account_name}")
            await player.disconnect("You do not have RC rights.")
            return

        # Player.cpp:697-706: `onlystaff` turns away every non-staff login.
        # The reference tests it for client types only, because RC and NC are
        # staff-gated above already; here it holds for any connection type so
        # an odd login type cannot slip past it. Staff status is the account
        # name against the `staff` option (Server.cpp:1774), never the nickname.
        if (getattr(player.server.config, 'onlystaff', False) is True
                and not player.server.is_staff(player.account_name)):
            logger.info(f"Refused non-staff login for {player.account_name}: staff only")
            await player.disconnect("This server is currently restricted to staff only.")
            return

        # GServer-v2/server/src/player/PlayerClient.cpp:453-471: whole days
        # truncate toward zero, and even capped deviation resets the clock.
        # PlayerClient only, so RC and NC logins leave the rating alone.
        if connection_type & PLTYPE.ANYCLIENT:
            now = int(time.time())
            days = int((now - player.last_spar_time) / 86400)
            if days != 0:
                player.elo_deviation = decay_deviation(player.elo_deviation, days)
                player.last_spar_time = now

        listserver = getattr(player.server, 'listserver', None)

        # The client reads the first frame as plain zlib, so the leading
        # packets have to ride in the login-response frame itself.
        # PlayerClient.cpp:487-492 (new tree): SERVERLISTCONNECTED when the
        # list server is up, then the reset, then the Stage-1 props.
        # PlayerRC.cpp:233-252: reset first, SERVERLISTCONNECTED after the
        # welcome text. PlayerNC.cpp:181-221 sends neither.
        connected = bool(listserver and listserver.connected)
        serverlist_connected = build_serverlist_connected() if connected else b''
        if connection_type & PLTYPE.ANYCLIENT:
            lead = serverlist_connected + build_clear_weapons()
            # PlayerClient.cpp:494-512: PLO_BIGMAP then PLO_MINIMAP follow
            # the reset when the option is exactly four comma tokens.
            for option, build in (('bigmap', build_bigmap),
                                  ('minimap', build_minimap)):
                text = map_option_text(getattr(player.server.config, option, ''))
                if text is not None:
                    lead += build(text)
        elif is_rc:
            lead = build_clear_weapons() + serverlist_connected
        else:
            lead = b''
        # Player.cpp:746-752 (origin/beta4): the PLO_KINGDOMSMODE /
        # PLO_NEWWORLDMODE announcement leads every login type (client, RC and
        # NC) where the reference puts PLO_SIGNATURE; `normal` sends neither.
        lead = build_server_mode(
            getattr(player.server, 'server_mode', 'normal')) + lead

        await self.send_login_response(lead)

        player.logged_in = True
        player.login_time = time.time()
        logger.info(f"Player {player.id} logged in as {player.account_name}")

        # Registered only once logged_in is set: the server's disconnect path
        # calls remove_player only for logged-in players, so a failed
        # login-response send must not leave a ghost on the list server.
        if listserver:
            await listserver.add_player(player)

        if is_rc:
            await self.send_rc_login_sequence()
            return
        if is_nc:
            return

        for name, value in list(player.flags.items()):
            await player.send_raw(build_flag_set(name, value))
        for name, value in list(getattr(player.server, "server_flags", {}).items()):
            if player.server.flag_is_public(name):
                await player.send_raw(build_flag_set(name, value))

        gs2 = getattr(player.server, 'gs2_manager', None)
        if gs2 is not None:
            await gs2.announce_weapons(player)
            await self._grant_protected_weapons(player, gs2)

        config = player.server.config
        await player.warp(config.start_level, config.start_x, config.start_y)

        rc_manager = getattr(player.server, 'rc_manager', None)
        if rc_manager:
            await rc_manager.notify_player_join(player)

    async def _grant_protected_weapons(self, player: 'Player', gs2) -> None:
        """PlayerClient.cpp:605-610: every `protectedweapons` entry the account
        lacks is added and sent at login. Player::addWeapon(name) needs a
        weapon the server knows (Player.cpp:1103-1107), so an entry naming no
        weapon grants nothing; a built-in item weapon counts as known here
        because pygserver registers those on first use, not at boot."""
        from .items import DEFAULT_WEAPON_IDS
        owned = {name.lower() for name in player.weapons}
        for entry in getattr(player.server.config, 'protectedweapons', ()):
            script = gs2.get_weapon(entry)
            name = script.name if script is not None else (
                entry.lower() if entry.lower() in DEFAULT_WEAPON_IDS else None)
            if name is None or name.lower() in owned:
                continue
            owned.add(name.lower())
            player.add_weapon(name)
            await gs2.announce_weapon(player, name)

    async def send_rc_login_sequence(self):
        """Send the control-client welcome and current player roster."""
        player = self.player
        server = player.server
        manager = server.rc_manager

        await player.send_raw(build_rc_chat(f"Welcome to {server.config.name}."))
        await player.send_raw(build_rc_max_upload_filesize(
            manager.max_upload_size))
        guilds = getattr(server.config, 'staff_guilds', None) \
            or getattr(server.config, 'staff', [])
        await player.send_raw(build_staff_guilds(guilds))

        for online_player in server.get_all_players():
            if (online_player.id != player.id
                    and online_player.connection_type & PLTYPE.ANYCLIENT):
                await player.send_raw(manager.build_add_player(online_player))

        await manager.broadcast_chat(
            f"New RC: {player.account_name}", exclude={player.id})

    async def send_login_response(self, lead: bytes = b''):
        """Send PLO_PLAYERPROPS with the player's initial state.

        `lead` is bundled ahead of the props in the same frame (see
        complete_login).
        """
        player = self.player
        logger.debug(f"Sending login response for player {player.id}")

        props = {
            PLPROP.NICKNAME: player.nickname,
            # MAXPOWER is FULL hearts on the wire (GServer-v2
            # PlayerProps.cpp:171-186, LevelItem.cpp:148-151); only CURPOWER
            # is in halves. Sending halves here doubled max hearts once the
            # client decoded it reference-correctly.
            PLPROP.MAXPOWER: int(player.max_hearts),
            PLPROP.CURPOWER: int(player.hearts * 2),
            PLPROP.RUPEESCOUNT: player.rupees,
            PLPROP.ARROWSCOUNT: player.arrows,
            PLPROP.BOMBSCOUNT: player.bombs,
            PLPROP.GLOVEPOWER: player.glove_power,
            # (power, image) rather than a bare power: the biased power+image
            # form is what carries the gear's sprite name to the client.
            PLPROP.SWORDPOWER: (player.sword_power, player.sword_image),
            PLPROP.SHIELDPOWER: (player.shield_power, player.shield_image),
            PLPROP.HEADIMAGE: player.head_image,
            PLPROP.BODYIMAGE: player.body_image,
            PLPROP.ACCOUNTNAME: player.account_name,
            PLPROP.COLORS: player.colors,
            PLPROP.MAGICPOINTS: player.mp,
            PLPROP.ALIGNMENT: player.ap,
            PLPROP.RATING: (player.elo_rating, player.elo_deviation),
        }

        await player.session.send_login_response(lead + build_player_props(props))
        # GServer-v2/server/src/player/Player.cpp:754-760 sends a bare
        # PLO_HASNPCSERVER only to game clients when an NPC server is present.
        # pygserver's serverside mode IS that NPC server (server.py
        # flag_is_public mirrors Server.cpp:1935 for the same reason).
        if (player.connection_type & PLTYPE.ANYCLIENT
                and getattr(player.server.config, 'serverside', False)):
            await player.send_raw(build_has_npc_server(True))
        logger.debug(f"Login response sent for player {player.id}")
