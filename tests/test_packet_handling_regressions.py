import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pygserver.combat import CombatManager
from pygserver.handlers.files import FileHandlers
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import PacketBuilder, PacketReader
from pygserver.server import GameServer


class FilePlayer(FileHandlers):
    def __init__(self, server):
        self.server = server


class RelayServer:
    def __init__(self):
        self.world = SimpleNamespace(get_level=lambda name: None)
        self.players = {}
        self.broadcasts = []

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet, exclude))
        for player in self.players.values():
            if player.level and player.level.name == level_name:
                if not exclude or player.id not in exclude:
                    await player.send_raw(packet)


def make_player(server, player_id, level):
    player = SimpleNamespace(
        id=player_id,
        level=level,
        bombs=1,
        arrows=1,
        x=10.0,
        y=10.0,
        send_raw=AsyncMock(),
        send_props=AsyncMock(),
    )
    server.players[player_id] = player
    return player


def packet_type(packet):
    return PacketReader(packet).read_gchar()


def test_updatefile_parses_modtime_before_filename():
    filesystem = SimpleNamespace(handle_update_file=AsyncMock())
    player = FilePlayer(SimpleNamespace(filesystem=filesystem))
    payload = (
        PacketBuilder()
        .write_gint5(0)
        .write_string("chicken.gmap")
        .build()
    )

    asyncio.run(player._handle_update_file(payload))

    filesystem.handle_update_file.assert_awaited_once_with(
        player, 0, "chicken.gmap"
    )


def test_bomb_del_relay_reaches_other_player():
    async def main():
        server = RelayServer()
        manager = CombatManager(server)
        level = Level("relay.nw")
        sender = make_player(server, 1, level)
        recipient = make_player(server, 2, level)

        await manager.handle_bomb_del(sender, 12.5, 8.0)

        recipient.send_raw.assert_awaited_once()
        assert packet_type(recipient.send_raw.await_args.args[0]) == PLO.BOMBDEL
        sender.send_raw.assert_not_awaited()

    asyncio.run(main())


def test_arrow_relay_fires_with_zero_ammo():
    async def main():
        server = RelayServer()
        manager = CombatManager(server)
        level = Level("relay.nw")
        sender = make_player(server, 1, level)
        sender.arrows = 0
        recipient = make_player(server, 2, level)

        arrow = await manager.handle_arrow_add(
            sender, 10.0, 10.0, flags=2, sprite=0, power=1
        )

        assert arrow is None
        recipient.send_raw.assert_awaited_once()
        assert packet_type(recipient.send_raw.await_args.args[0]) == PLO.ARROWADD
        sender.send_raw.assert_not_awaited()

    asyncio.run(main())


def test_server_prefixed_flag_broadcasts_to_all_players():
    async def main():
        server = GameServer.__new__(GameServer)
        server.server_flags = {}
        sender = Player(server, 1, AsyncMock(), MagicMock())
        recipient = Player(server, 2, AsyncMock(), MagicMock())
        sender.logged_in = True
        recipient.logged_in = True
        sender.send_raw = AsyncMock()
        recipient.send_raw = AsyncMock()
        server.players = {sender.id: sender, recipient.id: recipient}

        await sender._handle_flag_set(b"server.weather=sunny")

        assert server.server_flags["server.weather"] == "sunny"
        sender.send_raw.assert_awaited_once()
        recipient.send_raw.assert_awaited_once()
        assert packet_type(sender.send_raw.await_args.args[0]) == PLO.FLAGSET

    asyncio.run(main())
