"""PLI_PLAYERPROPS handling: gear changes and chat clearing.

Both defects were "parsed and then dropped": the shared prop decoder returns the
values correctly, but _handle_player_props never assigned them, so nothing
server-side or on any other client ever saw them.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from reborn_protocol.props import PLAYER_PROPS, encode_value

from pygserver.audience import Audience
from pygserver.config import ServerConfig
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import PacketBuilder, PacketReader, parse_player_props


class PropsServer:
    """Minimal server double: one level, a recording broadcast, an NPC manager."""

    def __init__(self, level):
        self.world = MagicMock()
        self.world.get_level = MagicMock(
            side_effect=lambda name: level if name == level.name else None)
        self.world.get_gmap_for_level = MagicMock(return_value=None)
        self.audience = Audience(self)
        self.config = ServerConfig()
        self.players = {}
        self.broadcasts = []
        self.npc_manager = MagicMock()
        self.npc_manager.on_player_chats = AsyncMock()
        self.npc_manager.check_touches = AsyncMock()

    def get_player(self, player_id):
        return self.players.get(player_id)

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet, exclude))


def make_player():
    level = Level("t.nw")
    server = PropsServer(level)
    player = Player(server, 2, AsyncMock(), MagicMock())
    player.send_raw = AsyncMock()
    player.level = level
    level.add_player(player)
    server.players[2] = player
    return server, player


def props_payload(pairs):
    """Build a PLI_PLAYERPROPS body from (prop_id, natural value) pairs."""
    builder = PacketBuilder()
    for prop_id, value in pairs:
        builder.write_gchar(prop_id)
        builder.write_bytes(encode_value(PLAYER_PROPS[prop_id], value))
    return builder.build()


def relayed_props(server):
    """The props dict from the last PLO_OTHERPLPROPS broadcast."""
    _level_name, packet, _exclude = server.broadcasts[-1]
    reader = PacketReader(packet)
    assert reader.read_gchar() == PLO.OTHERPLPROPS
    reader.read_gshort()
    return parse_player_props(reader.remaining()[:-1])


# -- gear -------------------------------------------------------------------

def test_sword_power_and_image_are_applied_and_relayed():
    server, player = make_player()

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.SWORDPOWER, (3, "blade.png"))])))

    assert (player.sword_power, player.sword_image) == (3, "blade.png")
    relayed = relayed_props(server)
    assert relayed[PLPROP.SWORDPOWER] == 3
    assert relayed['sword_image'] == "blade.png"


def test_claimed_sword_power_is_clamped_to_swordlimit():
    """A client may claim any power in PLI_PLAYERPROPS, so the stored value is
    clamped to `swordlimit` (default 3, Server.h:156). The wire form still
    decodes faithfully -- only the stored power is limited."""
    server, player = make_player()

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.SWORDPOWER, (99, "blade.png"))])))

    assert player.sword_power == 3
    assert player.sword_image == "blade.png"

    server.config.sword_limit = 10
    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.SWORDPOWER, (99, "blade.png"))])))
    assert player.sword_power == 10


def test_claimed_shield_power_is_clamped_to_shieldlimit():
    server, player = make_player()

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.SHIELDPOWER, (99, "buckler.png"))])))

    assert player.shield_power == 3


def test_shield_power_and_image_are_applied_and_relayed():
    server, player = make_player()

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.SHIELDPOWER, (2, "buckler.png"))])))

    assert (player.shield_power, player.shield_image) == (2, "buckler.png")
    relayed = relayed_props(server)
    assert relayed[PLPROP.SHIELDPOWER] == 2
    assert relayed['shield_image'] == "buckler.png"


def test_bare_sword_power_keeps_the_previous_image():
    """A bare power carries no image on the wire (PropertySwordPower::serialize
    for power <= 4), so the decoder synthesises the reference default rather
    than reporting one the client sent."""
    server, player = make_player()
    player.sword_image = "blade.png"

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.SWORDPOWER, 2)])))

    assert player.sword_power == 2
    assert player.sword_image == "sword2.png"  # preset_power_image default


# -- chat -------------------------------------------------------------------

def test_chat_is_applied_and_fires_the_script_event():
    server, player = make_player()

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.CURCHAT, "hello")])))

    assert player.chat == "hello"
    server.npc_manager.on_player_chats.assert_awaited_once_with(player, "hello")
    assert relayed_props(server)[PLPROP.CURCHAT] == "hello"


def test_empty_chat_clears_the_bubble_and_notifies_scripts():
    """An empty CURCHAT is the client clearing its bubble. It used to be dropped
    entirely, so Player.chat kept the stale line and no other client cleared."""
    server, player = make_player()
    player.chat = "hello"

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.CURCHAT, "")])))

    assert player.chat == ""
    server.npc_manager.on_player_chats.assert_awaited_once_with(player, "")
    # PLO_OTHERPLPROPS carries the cleared value so the bubble disappears.
    _level_name, packet, _exclude = server.broadcasts[-1]
    assert bytes((PLPROP.CURCHAT + 32, 32)) in packet


def test_unchanged_chat_does_not_refire_the_script_event():
    """GServer-v2 guards PLAYERCHATS on `chatChanged` (PlayerProps.cpp:354-356);
    without that, a client re-sending its current chat (or clearing an already
    empty one) would fire playerchats on every movement packet."""
    server, player = make_player()
    player.chat = "hello"

    asyncio.run(player._handle_player_props(
        props_payload([(PLPROP.CURCHAT, "hello")])))

    server.npc_manager.on_player_chats.assert_not_awaited()
    assert not server.broadcasts


# -- persistence ------------------------------------------------------------

def test_gear_images_round_trip_through_the_account(tmp_path):
    """The account persisted sword_image/shield_image but neither the load nor
    the save used them, so a gear change survived only as its power number."""
    from pygserver.account import AccountManager

    manager = AccountManager(MagicMock(), str(tmp_path))
    account = manager.create_account("hosler")
    _server, player = make_player()
    player.sword_power, player.sword_image = 4, "blade.png"
    player.shield_power, player.shield_image = 2, "buckler.png"

    manager.save_player_to_account(player, account)
    _server, fresh = make_player()
    manager.load_player_from_account(fresh, account)

    assert (fresh.sword_power, fresh.sword_image) == (4, "blade.png")
    assert (fresh.shield_power, fresh.shield_image) == (2, "buckler.png")


def test_weapons_survive_a_server_restart(tmp_path):
    """account.weapons was serialised to JSON and read back, but never exchanged
    with the player, so a weapon lasted exactly one session - and the login-time
    GS2 announce (complete_login -> announce_weapons) had nothing to announce."""
    from pygserver.account import AccountManager

    manager = AccountManager(MagicMock(), str(tmp_path))
    account = manager.create_account("hosler")
    _server, player = make_player()
    player.add_weapon("qa_gs2vm")

    manager.save_player_to_account(player, account)
    manager._save_executor.shutdown()   # drain the single writer thread

    # A second manager reads the JSON back off disk, as a restart does.
    restarted = AccountManager(MagicMock(), str(tmp_path))
    reloaded = restarted.get_account("hosler")
    _server, fresh = make_player()
    restarted.load_player_from_account(fresh, reloaded)

    assert reloaded.weapons == ["qa_gs2vm"]
    assert fresh.weapons == ["qa_gs2vm"]


def test_the_players_weapon_list_is_not_shared_with_the_account(tmp_path):
    """_save_account snapshots to_dict() on the caller's thread but writes on
    the executor thread, so the player must never hold the same list object the
    pending write is about to serialise."""
    from pygserver.account import AccountManager

    manager = AccountManager(MagicMock(), str(tmp_path))
    account = manager.create_account("hosler")
    account.weapons = ["bow"]
    _server, player = make_player()

    manager.load_player_from_account(player, account)
    player.add_weapon("bomb")

    assert account.weapons == ["bow"]
