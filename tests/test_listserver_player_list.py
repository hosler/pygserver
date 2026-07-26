"""ServerListClient.send_players regression.

send_players() filtered on `player.loaded`, which no Player has ever had, so the
first player in the list raised AttributeError. It only fires on a list-server
(re)connection made while somebody is online, and _connect()'s broad
`except Exception` logged it as "Failed to connect to list server", which is why
it survived.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pygserver.listserver import ServerListClient


def make_client():
    server = MagicMock()
    server.config = MagicMock()
    client = ServerListClient(server)
    client.connected = True
    client._send_packet = AsyncMock()
    client.add_player = AsyncMock()
    return server, client


def make_player(player_id, logged_in):
    player = MagicMock(spec=['id', 'logged_in'])
    player.id = player_id
    player.logged_in = logged_in
    return player


def test_send_players_lists_logged_in_players():
    server, client = make_client()
    online = make_player(2, True)
    connecting = make_player(3, False)
    server.players = {2: online, 3: connecting}

    asyncio.run(client.send_players())

    assert [call.args[0] for call in client.add_player.await_args_list] == [online]


def test_send_players_does_not_touch_a_nonexistent_loaded_attribute():
    """`spec=[...]` makes any other attribute access raise, as the real Player
    did - this is the AttributeError the old filter hit."""
    server, client = make_client()
    server.players = {2: make_player(2, True)}

    asyncio.run(client.send_players())  # would raise on player.loaded

    client.add_player.assert_awaited_once()


def test_send_players_clears_the_list_first():
    from reborn_protocol import SVO

    server, client = make_client()
    server.players = {}

    asyncio.run(client.send_players())

    first_packet = client._send_packet.await_args_list[0].args[0]
    assert first_packet[0] == SVO.SETPLYR + 32
