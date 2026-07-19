"""Regressions from the wave-2 adversarial review: dead playerswimming,
gint3 OR-vs-ADD decode, stale carried-NPC id."""

import asyncio
from unittest.mock import MagicMock

from pygserver import tiletypes
from pygserver.gs1_host import GS1Host
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import PLPROP
from pygserver.protocol.packets import PacketReader, parse_player_props

WATER_TILE = tiletypes.TILE_TYPES.index(tiletypes.WATER)
LAVA_TILE = tiletypes.TILE_TYPES.index(tiletypes.LAVA)

# b1=1, b2=160, b3=0: ADD carries into bit 14 (36864); OR loses it (20480).
CARRY_BYTES = bytes((1 + 32, 160 + 32, 0 + 32))
CARRY_VALUE = (1 << 14) + (160 << 7)


class FakeSwimmer:
    def __init__(self, level):
        self.level = level
        self.x = 10.0
        self.y = 10.0


def _swim_level(tile_id):
    level = Level("swim.nw")
    # Probe point for (10.0, 10.0) is floor(x+1.5), floor(y+2.0) = (11, 12).
    level.set_tile(11, 12, tile_id)
    return level


def test_playerswimming_true_on_water_and_lava():
    host = GS1Host()
    assert host._player_is_swimming(FakeSwimmer(_swim_level(WATER_TILE)))
    assert host._player_is_swimming(FakeSwimmer(_swim_level(LAVA_TILE)))


def test_playerswimming_false_on_land():
    host = GS1Host()
    assert not host._player_is_swimming(FakeSwimmer(_swim_level(0)))


def test_read_gint3_carries_across_bit14():
    assert PacketReader(CARRY_BYTES).read_gint3() == CARRY_VALUE


def test_parse_player_props_gint3_carry():
    for prop in (PLPROP.CARRYNPC, PLPROP.RUPEESCOUNT, PLPROP.TEXTCODEPAGE):
        data = bytes([int(prop) + 32]) + CARRY_BYTES
        assert parse_player_props(data)[prop] == CARRY_VALUE


def test_carrynpc_zero_clears_npc_id():
    player = Player(MagicMock(), 1, MagicMock(), MagicMock())
    player.level = None
    player.npc_id = 1337
    data = bytes([int(PLPROP.CARRYNPC) + 32, 32, 32, 32])
    asyncio.run(player._handle_player_props(data))
    assert player.npc_id == 0
