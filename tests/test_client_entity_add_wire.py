"""Wire decoding for client-created level NPCs and baddies."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pygserver.baddy import BaddyManager, BaddyType
from pygserver.config import ServerConfig
from pygserver.handlers.entities import EntityHandlers
from pygserver.protocol.packets import PacketBuilder


class Handler(EntityHandlers):
    pass


def encoded_string(value):
    return bytes(PacketBuilder().write_gstring(value).data)


def test_putnpc_decodes_file_image_and_position(tmp_path):
    script = tmp_path / "scripts" / "guard.txt"
    script.parent.mkdir()
    script.write_text("setimg scripted.png;\r\n", encoding="latin-1")
    level = MagicMock(name="level")
    level.name = "test.nw"
    level.add_npc.side_effect = lambda npc: setattr(npc, "level", level)
    server = MagicMock()
    server.config = ServerConfig(putnpc_enabled=True)
    server.broadcast_to_level = AsyncMock()
    from pygserver.filesystem import FileSystem
    from pygserver.npc import NPCManager
    server.filesystem = FileSystem(server, str(tmp_path))
    server.npc_manager = NPCManager(server)
    player = Handler()
    player.server = server
    player.level = level
    payload = (
        encoded_string("guard.png") + encoded_string("guard.txt")
        + bytes((44 + 32, 25 + 32))
    )

    asyncio.run(player._handle_putnpc(payload))

    npc = next(iter(server.npc_manager._npcs.values()))
    assert (npc.image, npc.x, npc.y) == ("guard.png", 22.0, 12.5)
    assert npc.gs1_source == "setimg scripted.png;\n"
    server.broadcast_to_level.assert_awaited_once()


def test_putnpc_disabled_creates_nothing():
    player = Handler()
    player.level = MagicMock()
    player.server = SimpleNamespace(
        config=ServerConfig(putnpc_enabled=False),
        npc_manager=MagicMock(),
        filesystem=MagicMock(),
    )

    asyncio.run(player._handle_putnpc(b""))

    player.server.npc_manager.create_npc.assert_not_called()


def test_baddyadd_decodes_fields_clamps_power_and_disables_respawn():
    level = MagicMock()
    level.name = "test.nw"
    server = MagicMock()
    server.broadcast_to_level = AsyncMock()
    server.baddy_manager = BaddyManager(server)
    player = Handler()
    player.server = server
    player.level = level
    payload = bytes((20 + 32, 31 + 32, int(BaddyType.SPIDER) + 32, 20 + 32)) + b"custom"

    asyncio.run(player._handle_baddy_add(payload))

    baddy = server.baddy_manager.get_baddy("test.nw", 1)
    assert (baddy.x, baddy.y, baddy.baddy_type) == (10.0, 15.5, BaddyType.SPIDER)
    assert (baddy.health, baddy.max_health, baddy.image) == (12, 12, "custom.gif")
    assert baddy.respawn_enabled is False
    server.broadcast_to_level.assert_awaited_once()
