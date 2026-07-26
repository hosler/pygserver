"""Player-as-façade tests: state aliases, dispatch table, login completion.

player.py was split into player_session / player_state / player_login /
handlers; everything here pins the seams that split has to keep intact.
"""

import asyncio
import struct
import zlib
from unittest.mock import AsyncMock, MagicMock

from pygserver.player import Player, _HANDLER_NAMES, _STATE_ALIASES
from pygserver.player_login import LoginService
from pygserver.protocol.constants import PLI, PLTYPE
from pygserver.protocol.packets import PacketBuilder


def make_player(server=None):
    return Player(server or MagicMock(), 2, AsyncMock(), MagicMock())


# -- state aliases ----------------------------------------------------------

def test_every_component_field_has_an_alias():
    """A field added to a state component but not aliased would be invisible
    under its historical flat name."""
    player = make_player()
    aliased = {(component, field)
               for component, field in _STATE_ALIASES.values()}
    for component in ('identity', 'character', 'inventory', 'status'):
        for field in vars(getattr(player, component)):
            assert (component, field) in aliased, \
                f"{component}.{field} is not exposed on Player"


def test_aliases_read_and_write_through_to_the_component():
    player = make_player()

    player.x = 12.5
    player.account_name = "hosler"
    player.logged_in = True
    player.connection_type = PLTYPE.RC

    assert player.character.x == 12.5
    assert player.identity.account_name == "hosler"
    assert player.status.logged_in is True
    assert player.identity.connection_type == PLTYPE.RC

    player.character.hearts = 1.5
    assert player.hearts == 1.5


def test_mutable_containers_are_shared_not_copied():
    player = make_player()

    player.weapons.append("bow")
    player.flags["quest"] = "done"

    assert player.inventory.weapons == ["bow"]
    assert player.inventory.flags == {"quest": "done"}


def test_transport_aliases_point_at_the_session():
    reader, writer = AsyncMock(), MagicMock()
    player = Player(MagicMock(), 2, reader, writer)

    assert player._reader is reader
    assert player._writer is writer
    assert player.connected is True

    player.connected = False
    assert player.session.connected is False


# -- dispatch table ---------------------------------------------------------

def test_handler_table_is_bound_methods_named_after_the_packet():
    player = make_player()
    handlers = player._handlers

    assert handlers[PLI.PLAYERPROPS] == player._handle_player_props
    assert handlers[PLI.TOALL] == player._handle_chat
    assert handlers[PLI.BADDYHURT] == player._handle_baddy_hurt


def test_npc_edit_packets_are_not_registered():
    """PLI_NPCPROPS/PUTNPC/NPCDEL are refused outright by GServer-v2 when the
    server runs its own NPC server (PlayerClientPackets.cpp:191-193, 755-757,
    784-786). They used to be registered as handlers that parsed the packet and
    then did nothing, which made the table claim coverage it did not have."""
    for packet_id in (PLI.NPCPROPS, PLI.PUTNPC, PLI.NPCDEL):
        assert packet_id not in _HANDLER_NAMES


def test_unhandled_packet_ids_are_recorded():
    player = make_player()
    unregistered = PLI.NPCDEL  # not a handler, not in the RC/NC ranges

    asyncio.run(player._handle_packets(bytes((unregistered + 32,)) + b"\n"))

    assert player._unhandled_packet_ids == {unregistered}


# -- login completion -------------------------------------------------------

class LoginServer:
    def __init__(self, with_listserver=True):
        self.config = MagicMock()
        self.config.start_level = "start.nw"
        self.config.start_x = 30.0
        self.config.start_y = 30.5
        self.listserver = MagicMock() if with_listserver else None
        if self.listserver:
            self.listserver.add_player = AsyncMock()


def make_login_player(with_listserver=True):
    player = Player(LoginServer(with_listserver), 2, AsyncMock(), MagicMock())
    player.login_service.send_login_response = AsyncMock()
    player.warp = AsyncMock()
    return player


def test_complete_login_marks_logged_in_registers_and_warps():
    player = make_login_player()

    asyncio.run(player.login_service.complete_login())

    assert player.logged_in is True
    assert player.login_time > 0
    player.server.listserver.add_player.assert_awaited_once_with(player)
    player.warp.assert_awaited_once_with("start.nw", 30.0, 30.5)


def test_complete_login_is_idempotent():
    """The list-server callback (Player.send_login) and the local path can both
    reach completion - e.g. ServerListClient.verify_account falls back to
    send_login() when it is not connected. The second one must be a no-op."""
    player = make_login_player()

    asyncio.run(player.login_service.complete_login())
    asyncio.run(player.send_login())

    assert player.login_service.send_login_response.await_count == 1
    assert player.warp.await_count == 1
    player.server.listserver.add_player.assert_awaited_once()


def test_send_login_is_the_listserver_entry_point():
    """Player.send_login() is what ServerListClient calls; it must do the full
    completion, not just send the response packet."""
    player = make_login_player()

    asyncio.run(player.send_login())

    assert player.logged_in is True
    player.warp.assert_awaited_once()


def test_complete_login_without_a_listserver_still_warps():
    player = make_login_player(with_listserver=False)

    asyncio.run(player.login_service.complete_login())

    assert player.logged_in is True
    player.warp.assert_awaited_once()


def test_login_service_is_attached_to_its_player():
    player = make_player()
    assert isinstance(player.login_service, LoginService)
    assert player.login_service.player is player


# -- login handshake (both verification paths) ------------------------------

def login_frame(username="hosler", password="pw", protocol="G3D0311C"):
    """The client's first packet: 2-byte length + plain zlib body."""
    body = (PacketBuilder()
            .write_gchar(PLTYPE.CLIENT)
            .write_gchar(7)                     # encryption key
            .write_string(protocol)
            .write_gstring(username)
            .write_gstring(password)
            .build())
    compressed = zlib.compress(body)
    return struct.pack('>H', len(compressed)) + compressed


class HandshakeServer(LoginServer):
    """LoginServer plus the account manager and verify_login switch."""

    def __init__(self, verify_login, with_listserver=True):
        super().__init__(with_listserver)
        self.config.verify_login = verify_login
        self.account_manager = MagicMock()
        account = MagicMock()
        account.is_banned = False
        account.admin_rights = 0
        self.account_manager.get_account = MagicMock(return_value=account)
        self.account_manager.verify_password = MagicMock(return_value=True)
        if self.listserver:
            self.listserver.verify_account = AsyncMock()


def make_handshake_player(verify_login, with_listserver=True):
    player = Player(HandshakeServer(verify_login, with_listserver), 2,
                    AsyncMock(), MagicMock())
    player.session.read = AsyncMock(return_value=login_frame())
    player.login_service.send_login_response = AsyncMock()
    player.warp = AsyncMock()
    return player


def test_local_verification_completes_login_inline():
    player = make_handshake_player(verify_login=False)

    assert asyncio.run(player._handle_login()) is True

    assert player.account_name == "hosler"
    assert player.logged_in is True
    player.warp.assert_awaited_once()


def test_listserver_verification_defers_completion_to_the_callback():
    """handle_login returns True with the player NOT yet logged in; the list
    server answers later and calls send_login()."""
    player = make_handshake_player(verify_login=True)

    assert asyncio.run(player._handle_login()) is True

    player.server.listserver.verify_account.assert_awaited_once()
    assert player.logged_in is False
    player.warp.assert_not_awaited()

    asyncio.run(player.send_login())

    assert player.logged_in is True
    player.warp.assert_awaited_once()


def test_banned_account_is_disconnected_before_completion():
    player = make_handshake_player(verify_login=False)
    account = player.server.account_manager.get_account.return_value
    account.is_banned = True
    account.ban_reason = "cheating"
    player.disconnect = AsyncMock()

    assert asyncio.run(player._handle_login()) is False

    assert player.logged_in is False
    player.disconnect.assert_awaited_once()


def test_wrong_password_is_disconnected_before_completion():
    player = make_handshake_player(verify_login=True, with_listserver=False)
    player.server.account_manager.verify_password.return_value = False
    player.disconnect = AsyncMock()

    assert asyncio.run(player._handle_login()) is False

    assert player.logged_in is False
    player.disconnect.assert_awaited_once()
