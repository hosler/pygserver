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

from .protocol.constants import PLPROP
from .protocol.packets import build_player_props, parse_login_packet

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
            decrypted = codec.decode_packet(packet_data)
            if not decrypted:
                logger.warning(f"Failed to decode login from {player.id}")
                return False

            # Parse login
            login = parse_login_packet(decrypted)
            logger.info(f"Login from {login.get('username', '?')}, protocol={login.get('protocol', '?')}")

            protocol = login.get('protocol', '')
            if protocol not in KNOWN_PROTOCOLS:
                logger.warning(f"Unsupported protocol: {protocol}")

            # Set encryption key
            codec.set_key(login.get('encryption_key', 0))

            # Store account info
            player.account_name = login.get('username', f'player_{player.id}')
            player.nickname = player.account_name

            account_manager = getattr(server, 'account_manager', None)
            if account_manager:
                account = account_manager.get_account(player.account_name)
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

        await self.send_login_response()

        player.logged_in = True
        player.login_time = time.time()
        logger.info(f"Player {player.id} logged in as {player.account_name}")

        listserver = getattr(player.server, 'listserver', None)
        if listserver:
            await listserver.add_player(player)

        config = player.server.config
        await player.warp(config.start_level, config.start_x, config.start_y)

    async def send_login_response(self):
        """Send PLO_PLAYERPROPS with the player's initial state."""
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
        }

        await player.session.send_login_response(build_player_props(props))
        logger.debug(f"Login response sent for player {player.id}")
