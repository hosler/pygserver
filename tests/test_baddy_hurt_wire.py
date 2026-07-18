"""Regression tests for the PLI_BADDYHURT / PLO_BADDYHURT wire format.

Pinned spec (GServer-v2 msgPLI_BADDYHURT, PlayerClientPackets.cpp:523-539,
commit e0cd07af9bb4be09c54c0335f222dd0eacb71c1):
    [GUChar baddyId][GChar hurtDX][GChar hurtDY][GUChar damage, half-hearts]
hurtDX/hurtDY use the "midpoint: 64" gchar idiom noted in that handler:
raw byte -> (byte - 32) via read_gchar_signed(), then an extra -64 to
recenter (mirrors GServer's PropertyHurtDxDy<MidPoint>::deserialize).

Player._handle_baddy_hurt previously only read the legacy 2-field
[baddy_id][damage] shape; this locks in the 4-field parse plus backward
tolerance for that legacy shape, and the matching PLO_BADDYHURT relay build.
"""
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))

from pygserver.baddy import BaddyManager, BaddyType
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import PacketBuilder, PacketReader, build_baddy_hurt


def _make_player(mock_server):
    reader = AsyncMock()
    writer = MagicMock()
    player = Player(mock_server, 1, reader, writer)
    player.account_name = "hosler"
    player.send_raw = AsyncMock()
    return player


class TestBuildBaddyHurt:
    """Byte-level PLO_BADDYHURT build/parse round-trip."""

    def test_round_trips_baddy_id_direction_and_damage(self):
        packet = build_baddy_hurt(baddy_id=5, hurt_dx=-1.0, hurt_dy=0.5, damage=4)

        reader = PacketReader(packet)
        assert reader.read_gchar() == PLO.BADDYHURT
        assert reader.read_gchar() == 5
        # mid-64: raw read_gchar_signed() (byte-32) minus another 64.
        assert reader.read_gchar_signed() - 64 == -64
        assert reader.read_gchar_signed() - 64 == 32
        assert reader.read_gchar() == 4

    def test_clamps_direction_to_unit_range(self):
        # Values outside -1.0..1.0 must not corrupt the mid-64 byte.
        packet = build_baddy_hurt(baddy_id=1, hurt_dx=5.0, hurt_dy=-5.0, damage=1)

        reader = PacketReader(packet)
        reader.read_gchar()  # packet id
        reader.read_gchar()  # baddy id
        assert reader.read_gchar_signed() - 64 == 64
        assert reader.read_gchar_signed() - 64 == -64


class TestHandleBaddyHurtParse:
    """Player._handle_baddy_hurt wire parsing."""

    def test_parses_new_4field_format(self):
        async def main():
            mock_server = MagicMock()
            mock_server.baddy_manager = MagicMock()
            mock_server.baddy_manager.handle_baddy_hurt = AsyncMock()
            player = _make_player(mock_server)
            player.level = MagicMock()

            payload = (
                PacketBuilder()
                .write_gchar(7)                 # baddy id
                .write_gchar_signed(20 + 64)     # hurtDX -> decodes to +20 (dropped)
                .write_gchar_signed(-15 + 64)    # hurtDY -> decodes to -15 (dropped)
                .write_gchar(3)                  # damage
                .build()
            )
            await player._handle_baddy_hurt(payload)

            mock_server.baddy_manager.handle_baddy_hurt.assert_awaited_once_with(
                player, 7, 3
            )

        asyncio.run(main())

    def test_falls_back_to_legacy_2field_format(self):
        async def main():
            mock_server = MagicMock()
            mock_server.baddy_manager = MagicMock()
            mock_server.baddy_manager.handle_baddy_hurt = AsyncMock()
            player = _make_player(mock_server)
            player.level = MagicMock()

            # Old client: just [baddy_id][damage], no knockback fields.
            payload = PacketBuilder().write_gchar(9).write_gchar(2).build()
            await player._handle_baddy_hurt(payload)

            mock_server.baddy_manager.handle_baddy_hurt.assert_awaited_once_with(
                player, 9, 2
            )

        asyncio.run(main())

    def test_no_level_is_a_noop(self):
        async def main():
            mock_server = MagicMock()
            mock_server.baddy_manager = MagicMock()
            mock_server.baddy_manager.handle_baddy_hurt = AsyncMock()
            player = _make_player(mock_server)
            player.level = None

            payload = PacketBuilder().write_gchar(1).write_gchar(1).build()
            await player._handle_baddy_hurt(payload)

            mock_server.baddy_manager.handle_baddy_hurt.assert_not_awaited()

        asyncio.run(main())


def test_handle_baddy_hurt_end_to_end_relay_uses_new_format():
    """BaddyManager.handle_baddy_hurt's PLO_BADDYHURT broadcast carries the
    server-computed knockback direction in the new 4-field shape (not the
    old [id][power][from_x*2][from_y*2] position payload)."""
    async def main():
        broadcasts = []

        class FakeServer:
            def __init__(self):
                self.world = MagicMock()

            async def broadcast_to_level(self, level_name, packet, exclude=None):
                broadcasts.append((level_name, packet))

        class FakePlayer:
            def __init__(self, level):
                self.id = 1
                self.x = 10.0
                self.y = 10.0
                self.level = level

        server = FakeServer()
        mgr = BaddyManager(server)
        level = Level("t.nw")
        baddy = await mgr.add_baddy(level, 12.0, 10.0, BaddyType.GRAYSNAKE)
        baddy.health = 5  # survive the hit so this stays scoped to BADDYHURT
        player = FakePlayer(level)

        broadcasts.clear()  # drop add_baddy's own spawn broadcast
        await mgr.handle_baddy_hurt(player, baddy.id, damage=1)

        assert len(broadcasts) == 1
        level_name, packet = broadcasts[0]
        assert level_name == "t.nw"

        reader = PacketReader(packet)
        assert reader.read_gchar() == PLO.BADDYHURT
        assert reader.read_gchar() == baddy.id
        hurt_dx = reader.read_gchar_signed() - 64
        hurt_dy = reader.read_gchar_signed() - 64
        damage = reader.read_gchar()
        # Baddy sits directly east of the player -> knockback direction is
        # (+1, 0), i.e. hurtDX at the positive edge, hurtDY centered.
        assert hurt_dx == 64
        assert hurt_dy == 0
        assert damage == 1

    asyncio.run(main())


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
