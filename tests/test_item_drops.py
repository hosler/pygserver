"""Item drops: player death, bushes/grass/swamp, vases, level-item expiry.

Reference (GServer-v2 origin/beta4 25396a18): PlayerClient::dropItemsOnDeath
(player/PlayerClient.cpp:1898-1998), the STATUS transition that triggers it
(player/PlayerProps.cpp:550-579), msgPLI_BOARDMODIFY drops
(player/packets/PlayerClientPackets.cpp:109-169), Server::rollBushItemDrop
(Server.cpp:1438-1453) and the bushitemtypes/deathitemtypes parsers
(Server.cpp:603-635), Level::addItem (level/Level.cpp:2424-2471).

PLO ids here are the protocol's own (PLAYERPROPS = 9, ITEMADD = 22); expected
bytes are composed by hand, not with the builders under test. The RNG is
injected (`ItemManager.rng`) and scripted, so every draw is checked.
"""

import asyncio
import random
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.combat import CombatManager
from pygserver.config import ServerConfig
from pygserver.items import ItemManager
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import LevelItemType, PLI, PLPROP, PLSTATUS
from pygserver.protocol.packets import PacketBuilder, build_item_add
from pygserver.rc import RCManager
from pygserver.server import GameServer

GREEN, BLUE, RED, BOMBS, DARTS, HEART, GOLD = 0, 1, 2, 3, 4, 5, 19


def run(coro):
    return asyncio.run(coro)


def g(value):
    return bytes([value + 32])


class ScriptedRng:
    """random.Random stand-in: draws come from a queue (checked against the
    requested range), then fall back to the lowest legal value near zero."""

    def __init__(self, *draws):
        self.queue = list(draws)
        self.calls = []

    def randint(self, low, high):
        self.calls.append((low, high))
        if self.queue:
            value = self.queue.pop(0)
            assert low <= value <= high, (value, low, high)
            return value
        return min(max(0, low), high)


class Rig:
    """A real GameServer + ItemManager with packet capture instead of sockets."""

    def __init__(self, **options):
        self.config = ServerConfig(**options)
        self.server = GameServer(self.config)
        self.manager = ItemManager(self.server)
        self.server.item_manager = self.manager
        self.server.combat_manager = CombatManager(self.server)
        self.sent = []          # (level_name, packet, exclude)

        async def broadcast(level_name, packet, exclude=None):
            self.sent.append((level_name, packet, exclude))

        self.server.broadcast_to_level = broadcast
        self.level = self.add_level("a.nw")

    def add_level(self, name):
        level = Level(name)
        self.server.world.add_level(level)
        return level

    def player(self, level=None, x=30.0, y=40.0, rupees=100, arrows=10, bombs=5):
        player = SimpleNamespace(
            id=2, level=level or self.level, x=x, y=y, z=0.0,
            rupees=rupees, arrows=arrows, bombs=bombs, deaths=0,
            attach_npc=(0, 0), send_props=AsyncMock())
        return player

    def items(self, level_name="a.nw"):
        return [(item.item_type, item.x, item.y)
                for item in self.manager.get_items_on_level(level_name)]

    def item_types(self, level_name="a.nw"):
        return [int(kind) for kind, _x, _y in self.items(level_name)]

    def death(self, player):
        run(self.manager.drop_items_on_death(player))


def props_blob(props):
    builder = PacketBuilder()
    for key, value in sorted(props.items()):
        builder.write_gchar(key).write_gchar(value)
    return builder.build()


# -- death: algorithm, packets, positions --------------------------------------

def test_reference_worked_example_bytes():
    """Spec example: gralats 100 -> 30 dropped, tile (30,40), d=(+1,-2),
    one red rupee: ITEMADD 36 61 70 22; the dying player's own deduction
    packet is PLO_PLAYERPROPS 3/4/5."""
    rig = Rig(mindeathgralats=30, maxdeathgralats=30)
    rig.manager.rng = ScriptedRng(30, 0, 0, 1, -2)
    player = rig.player()
    rig.death(player)
    assert rig.manager.rng.calls == [(30, 30), (0, 3), (0, 3), (-4, 4), (-4, 4)]
    assert [(name, packet, exclude) for name, packet, exclude in rig.sent] == [
        ("a.nw", b"\x36\x61\x70\x22\n", None)]       # no exclusion: the dier sees it
    assert (player.rupees, player.arrows, player.bombs) == (70, 10, 5)
    player.send_props.assert_awaited_once_with({
        PLPROP.RUPEESCOUNT: 70, PLPROP.ARROWSCOUNT: 10, PLPROP.BOMBSCOUNT: 5})
    assert rig.items() == [(LevelItemType.REDRUPEE, 32.5, 40.0)]


def test_deduction_goes_to_the_dying_player_only_in_the_reference_encoding():
    from pygserver.protocol.packets import build_player_props
    rig = Rig(mindeathgralats=100, maxdeathgralats=100)
    rig.manager.rng = ScriptedRng(100, 2, 1)
    player = rig.player(rupees=200, arrows=10, bombs=5)
    rig.death(player)
    # gralats 200-100, arrows 10-10, bombs 5-5.
    player.send_props.assert_awaited_once_with({
        PLPROP.RUPEESCOUNT: 100, PLPROP.ARROWSCOUNT: 0, PLPROP.BOMBSCOUNT: 0})
    # PLO_PLAYERPROPS (9), GRALATS gint3(100), ARROWS gchar, BOMBS gchar
    # (PlayerClient.cpp:1938).
    assert build_player_props(player.send_props.await_args.args[0]) == (
        g(9) + g(3) + g(0) + g(0) + g(100) + g(4) + g(0) + g(5) + g(0) + b"\n")
    # Every other packet is an ITEMADD through the level broadcast, never excluded.
    assert rig.sent and all(
        packet[:1] == g(22) and exclude is None for _name, packet, exclude in rig.sent)


@pytest.mark.parametrize("gralats,expected", [
    (29, [BLUE] * 5 + [GREEN] * 4),
    (30, [RED]),
    (100, [GOLD]),
    (99, [RED] * 3 + [BLUE] + [GREEN] * 4),
    (136, [GOLD, RED, BLUE, GREEN]),
    (1, [GREEN]),
])
def test_greedy_split_and_order(gralats, expected):
    rig = Rig(mindeathgralats=gralats, maxdeathgralats=gralats)
    rig.manager.rng = ScriptedRng(gralats, 0, 0)
    rig.death(rig.player(rupees=gralats))
    assert rig.item_types() == expected


def test_gralats_are_capped_by_what_the_player_holds():
    rig = Rig(mindeathgralats=50, maxdeathgralats=50)
    rig.manager.rng = ScriptedRng(50, 0, 0)
    player = rig.player(rupees=7)
    rig.death(player)
    assert player.rupees == 0
    assert rig.item_types() == [BLUE, GREEN, GREEN]


def test_arrows_and_bombs_are_units_of_five_and_follow_the_gralats():
    rig = Rig(mindeathgralats=1, maxdeathgralats=1)
    rig.manager.rng = ScriptedRng(1, 3, 2)
    player = rig.player(rupees=1, arrows=40, bombs=40)
    rig.death(player)
    assert (player.arrows, player.bombs) == (25, 30)
    assert rig.item_types() == [GREEN, DARTS, DARTS, DARTS, BOMBS, BOMBS]


def test_arrow_and_bomb_units_are_cut_to_what_is_held():
    rig = Rig(mindeathgralats=1, maxdeathgralats=1)
    rig.manager.rng = ScriptedRng(1, 3, 3)
    player = rig.player(rupees=0, arrows=12, bombs=4)
    rig.death(player)
    assert (player.arrows, player.bombs) == (2, 4)
    assert rig.item_types() == [DARTS, DARTS]


def test_a_missing_item_type_zeroes_that_drop_without_deducting():
    rig = Rig(deathitemtypes=["greenrupee", "bombs"])
    rig.manager.rng = ScriptedRng(3, 3, 3)
    player = rig.player(rupees=100, arrows=50, bombs=50)
    rig.death(player)
    assert (player.arrows, player.bombs) == (50, 35)       # darts not allowed
    assert rig.item_types() == [GREEN] * 3 + [BOMBS] * 3


def test_deduct_before_type_check_loses_the_gralats():
    """No rupee type allowed: step 4 already took them, nothing lands."""
    rig = Rig(deathitemtypes=["bombs", "darts"], mindeathgralats=40, maxdeathgralats=40)
    rig.manager.rng = ScriptedRng(40, 0, 0)
    player = rig.player(rupees=100)
    rig.death(player)
    assert player.rupees == 60
    assert rig.item_types() == []
    player.send_props.assert_awaited_once()


def test_missing_green_leaves_the_remainder_lost():
    rig = Rig(deathitemtypes=["bluerupee"], mindeathgralats=12, maxdeathgralats=12)
    rig.manager.rng = ScriptedRng(12, 0, 0)
    player = rig.player(rupees=100)
    rig.death(player)
    assert rig.item_types() == [BLUE, BLUE]
    assert player.rupees == 88


def test_death_item_names_may_be_numbers_and_unknown_names_are_skipped():
    config = ServerConfig(deathitemtypes=["0", "nonsense", " DARTS ", "99"])
    assert config.death_drop_items() == frozenset({0, 4})


def test_max_zero_draws_no_gralats_at_all():
    rig = Rig(mindeathgralats=1, maxdeathgralats=0)
    rig.manager.rng = ScriptedRng(1, 1)          # arrows, bombs only
    player = rig.player(rupees=100, arrows=50, bombs=50)
    rig.death(player)
    assert rig.manager.rng.calls[:2] == [(0, 3), (0, 3)]
    assert player.rupees == 100
    assert rig.item_types() == [DARTS, BOMBS]


def test_min_above_max_is_treated_as_min_equal_to_max():
    """Deliberate guard: undefined behaviour in the reference (:1913)."""
    rig = Rig(mindeathgralats=100, maxdeathgralats=10)
    rig.manager.rng = ScriptedRng(10, 0, 0)
    rig.death(rig.player())
    assert rig.manager.rng.calls[0] == (10, 10)


def test_dropitemsdead_false_does_nothing():
    rig = Rig(dropitemsdead=False)
    rig.manager.rng = ScriptedRng()
    player = rig.player()
    rig.death(player)
    assert rig.manager.rng.calls == [] and rig.sent == [] and player.rupees == 100
    player.send_props.assert_not_awaited()


def test_no_level_does_nothing():
    rig = Rig()
    player = rig.player()
    player.level = None
    rig.death(player)
    assert rig.sent == [] and player.rupees == 100


def test_default_options_match_the_reference():
    config = ServerConfig()
    assert (config.dropitemsdead, config.mindeathgralats, config.maxdeathgralats) == (True, 1, 50)
    assert config.death_drop_items() == frozenset({GREEN, BLUE, RED, GOLD, BOMBS, DARTS})
    assert (config.bushitems, config.vasesdrop) == (True, True)


def test_default_options_never_exceed_nine_gralat_items_and_six_others():
    rng = random.Random(7)
    for _ in range(200):
        rig = Rig()
        rig.manager.rng = rng
        rig.death(rig.player(rupees=9_999_999, arrows=99, bombs=99))
        types = rig.item_types()
        assert len(types) <= 9 + 3 + 3
        assert types.count(DARTS) <= 3 and types.count(BOMBS) <= 3


# -- death: positions, wire and bounds ---------------------------------------------

def test_displacement_is_x_then_y_per_item_and_truncates_to_half_tiles():
    rig = Rig(mindeathgralats=2, maxdeathgralats=2)
    rig.manager.rng = ScriptedRng(2, 0, 0, -4, 4, 3, -1)
    rig.death(rig.player(x=10.3125, y=20.0, rupees=2))
    # tile = 165/16 -> 10.3125; pX = 10.3125+1.5-4 = 7.8125 -> half 15 -> 7.5
    #                            pY = 20+2+4   = 26      -> half 52 -> 26.0
    #                            pX = 10.3125+1.5+3 = 14.8125 -> 29 -> 14.5, pY 21 -> 21.0
    assert rig.items() == [(LevelItemType.GREENRUPEE, 7.5, 26.0),
                           (LevelItemType.GREENRUPEE, 14.5, 21.0)]


def test_bytes_never_hold_an_unframed_newline_at_extreme_positions():
    for x in (0.0, 0.5, 31.5, 62.5, 63.5, 63.9375):
        for y in (0.0, 0.5, 31.5, 62.5, 63.5, 63.9375):
            for dx in (-4, 0, 4):
                for dy in (-4, 0, 4):
                    rig = Rig(mindeathgralats=1, maxdeathgralats=1)
                    rig.manager.rng = ScriptedRng(1, 1, 1, dx, dy, dx, dy, dx, dy)
                    rig.death(rig.player(x=x, y=y, rupees=1))
                    for _name, packet, _exclude in rig.sent:
                        assert packet[-1:] == b"\n" and b"\n" not in packet[:-1], (
                            x, y, dx, dy, packet)
                        assert packet[0] == 22 + 32 and len(packet) == 5
                        assert min(packet[1:4]) >= 32


def test_negative_local_coordinates_wrap_to_the_unsigned_ceiling():
    """Tile (0,0), d=(-4,-4): pX=-2.5 -> half -5, pY=-2 -> half -4; the
    reference writes (char)(-5), which writeGChar caps at 223 (byte 255)."""
    rig = Rig(mindeathgralats=1, maxdeathgralats=1)
    rig.manager.rng = ScriptedRng(1, 0, 0, -4, -4)
    rig.death(rig.player(x=0.0, y=0.0, rupees=1))
    assert rig.sent == [("a.nw", b"\x36\xff\xff\x20\n", None)]
    assert build_item_add(-2.5, -2.0, 0) == b"\x36\xff\xff\x20\n"


def test_a_drop_past_the_edge_of_a_single_level_reaches_nobody():
    """Segment (1, 0) has no players on a plain level (Server.cpp:2361-2375)."""
    rig = Rig(mindeathgralats=1, maxdeathgralats=1)
    rig.manager.rng = ScriptedRng(1, 0, 0, 4, 0)
    rig.death(rig.player(x=63.5, y=10.0, rupees=1))      # pX = 69 -> pixel 1104
    assert rig.sent == [] and rig.items() == []


class FakeGmap:
    def __init__(self, grid):
        self.grid = grid

    def get_level_at(self, gx, gy):
        return self.grid.get((gx, gy))


def test_a_gmap_drop_past_the_edge_lands_in_the_neighbouring_segment():
    rig = Rig(mindeathgralats=1, maxdeathgralats=1)
    rig.add_level("b.nw")
    gmap = FakeGmap({(0, 0): "a.nw", (1, 0): "b.nw"})
    rig.server.world.get_gmap_for_level = lambda name: (
        (gmap, 0, 0) if name == "a.nw" else (gmap, 1, 0) if name == "b.nw" else None)
    rig.manager.rng = ScriptedRng(1, 0, 0, 4, 0)
    rig.death(rig.player(x=63.5, y=10.0, rupees=1))
    assert rig.items("a.nw") == []
    assert rig.items("b.nw") == [(LevelItemType.GREENRUPEE, 5.0, 12.0)]
    assert [name for name, _p, _e in rig.sent] == ["b.nw"]
    # Local half-tiles 10 and 24 -> GCHARs.
    assert rig.sent[0][1] == b"\x36" + g(10) + g(24) + g(0) + b"\n"


# -- death: bounds and the level cap -------------------------------------------------

def test_absurd_options_stay_bounded_and_stop_at_the_level_cap():
    rig = Rig(mindeathgralats=9_999_999, maxdeathgralats=9_999_999)
    rig.manager.rng = ScriptedRng(9_999_999, 0, 0)
    player = rig.player(rupees=9_999_999)
    start = time.perf_counter()
    rig.death(player)
    assert time.perf_counter() - start < 1.0
    assert len(rig.manager.get_items_on_level("a.nw")) == rig.manager.max_items_per_level
    assert player.rupees == 0     # deducted up front, as in the reference


def test_unplaceable_drops_cannot_spin_the_loop():
    """Drops that land outside every segment never reach the cap, so the loop
    has its own attempt bound."""
    rig = Rig(mindeathgralats=9_999_999, maxdeathgralats=9_999_999)
    rig.manager.rng = ScriptedRng(9_999_999, 0, 0)
    player = rig.player(x=5000.0, y=5000.0, rupees=9_999_999)
    start = time.perf_counter()
    rig.death(player)
    assert time.perf_counter() - start < 1.0
    assert rig.manager.rng.calls.count((-4, 4)) <= 2 * rig.manager.max_items_per_level


def test_death_stops_quietly_when_the_level_is_full():
    rig = Rig(mindeathgralats=40, maxdeathgralats=40)
    rig.manager.max_items_per_level = 3
    rig.manager.rng = ScriptedRng(40, 3, 3)
    rig.death(rig.player(rupees=100, arrows=50, bombs=50))
    assert len(rig.items()) == 3


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 1e300])
def test_a_garbage_player_position_never_raises(bad):
    rig = Rig(mindeathgralats=3, maxdeathgralats=3)
    rig.manager.rng = ScriptedRng(3, 1, 1)
    rig.death(rig.player(x=bad, y=bad, rupees=3))


# -- the trigger: STATUS goes not-DEAD -> DEAD ---------------------------------------

def status_rig(**options):
    rig = Rig(**options)
    player = Player(rig.server, 2, AsyncMock(), MagicMock())
    player.level = rig.level
    player.x, player.y = 30.0, 40.0
    player.rupees, player.arrows, player.bombs = 100, 10, 5
    player.send_raw = AsyncMock()
    player.send_props = AsyncMock()
    rig.server.players[2] = player
    return rig, player


def send_status(player, status):
    run(player._dispatch_packet(PLI.PLAYERPROPS, props_blob({PLPROP.STATUS: status})))


def test_status_dead_counts_a_death_and_drops_once():
    rig, player = status_rig(mindeathgralats=5, maxdeathgralats=5)
    rig.manager.rng = ScriptedRng(5, 0, 0)
    send_status(player, PLSTATUS.DEAD)
    assert player.deaths == 1
    assert rig.item_types() == [BLUE]
    send_status(player, PLSTATUS.DEAD)                 # same value: ignored
    assert player.deaths == 1 and len(rig.items()) == 1
    send_status(player, PLSTATUS.DEAD | PLSTATUS.PAUSED)   # still dead: no new transition
    assert player.deaths == 1 and len(rig.items()) == 1


def test_the_death_count_is_not_sent_to_the_dying_player():
    """PlayerProps.cpp:573-580 only increments; no DEATHS prop is queued."""
    rig, player = status_rig(mindeathgralats=5, maxdeathgralats=5)
    rig.manager.rng = ScriptedRng(5, 0, 0)
    send_status(player, PLSTATUS.DEAD)
    assert player.deaths == 1
    sent_ids = {key for call in player.send_props.await_args_list
                for key in call.args[0]}
    assert PLPROP.DEATHSCOUNT not in sent_ids


def test_a_second_death_after_revival_counts_again():
    rig, player = status_rig(mindeathgralats=5, maxdeathgralats=5)
    rig.manager.rng = ScriptedRng(5, 0, 0, 0, 0, 5, 0, 0, 0, 0)
    send_status(player, PLSTATUS.DEAD)
    send_status(player, 0)
    send_status(player, PLSTATUS.DEAD)
    assert player.deaths == 2 and len(rig.items()) == 2


def test_hearts_reaching_zero_alone_neither_counts_nor_drops():
    rig, player = status_rig()
    rig.manager.rng = ScriptedRng()
    player.hearts = 0.5
    run(rig.server.combat_manager.apply_damage(player, 1, 0, 0))
    for task in list(rig.server.combat_manager._respawn_tasks):
        task.cancel()
    assert player.deaths == 0 and rig.sent == [] and rig.manager.rng.calls == []


def test_a_sparring_zone_neither_counts_the_death_nor_drops():
    rig, player = status_rig()
    rig.level.is_sparring_zone = True
    rig.manager.rng = ScriptedRng()
    send_status(player, PLSTATUS.DEAD)
    assert player.deaths == 0 and rig.sent == [] and rig.manager.rng.calls == []
    assert player.rupees == 100


def test_no_level_or_no_id_neither_counts_nor_drops():
    rig, player = status_rig()
    rig.manager.rng = ScriptedRng()
    player.level = None
    send_status(player, PLSTATUS.DEAD)
    assert player.deaths == 0
    rig2, player2 = status_rig()
    player2.id = 0
    rig2.manager.rng = ScriptedRng()
    send_status(player2, PLSTATUS.DEAD)
    assert player2.deaths == 0 and rig2.sent == []


def test_death_option_changes_apply_to_the_next_death_via_rc():
    rig, player = status_rig(mindeathgralats=5, maxdeathgralats=5)
    rig.server.rc_manager = RCManager(rig.server)
    rig.manager.rng = ScriptedRng(5, 0, 0)
    send_status(player, PLSTATUS.DEAD)
    assert rig.item_types() == [BLUE]
    send_status(player, 0)
    rig.server.rc_manager._apply_server_options(
        "dropitemsdead = false\nmaxdeathgralats = 9\nmindeathgralats = 9")
    rig.manager.rng = ScriptedRng()
    send_status(player, PLSTATUS.DEAD)
    assert player.deaths == 2 and len(rig.items()) == 1     # counted, nothing dropped
    send_status(player, 0)
    rig.server.rc_manager._apply_server_options(
        "dropitemsdead = true\ndeathitemtypes = greenrupee")
    rig.manager.rng = ScriptedRng(9, 0, 0)
    send_status(player, PLSTATUS.DEAD)
    assert rig.item_types() == [BLUE] + [GREEN] * 9


# -- bushes, grass, swamp ----------------------------------------------------------------

def bush_player(rig):
    return rig.player(level=rig.level)


@pytest.mark.parametrize("roll,expected", [
    (1, GREEN), (25, GREEN), (26, BLUE), (27, BLUE), (28, HEART), (32, HEART),
    (33, BOMBS), (37, BOMBS), (38, None), (100, None),
])
def test_default_roll_table_is_sequential_cumulative(roll, expected):
    rig = Rig()
    rig.manager.rng = ScriptedRng(roll)
    dropped = rig.manager.roll_bush_drop(rig.config)
    assert rig.manager.rng.calls == [(1, 100)]
    assert dropped == (-1 if expected is None else expected)


def cut(rig, tile, x=10, y=20):
    run(rig.manager.drop_board_item(bush_player(rig), x, y, tile))


@pytest.mark.parametrize("tile", [0x002, 0x1a4, 0x1ff, 0x7ff, 0x3ff, 0x5d9, 0x34f])
def test_each_bush_tile_drops_and_the_packet_matches(tile):
    rig = Rig()
    rig.manager.rng = ScriptedRng(1)
    cut(rig, tile)
    assert rig.sent == [("a.nw", b"\x36\x34\x48\x20\n", None)]     # greenrupee at (10, 20)


@pytest.mark.parametrize("tile", [0x000, 0x001, 0x2ab, 0x2ad, 0x800])
def test_other_tiles_never_roll(tile):
    rig = Rig()
    rig.manager.rng = ScriptedRng(1)
    cut(rig, tile)
    assert rig.sent == [] and rig.manager.rng.calls == []


def test_bushitems_false_disables_bush_drops_but_not_vases():
    rig = Rig(bushitems=False)
    rig.manager.rng = ScriptedRng(1)
    cut(rig, 0x002)
    assert rig.sent == [] and rig.manager.rng.calls == []
    cut(rig, 0x2ac)
    assert rig.item_types() == [HEART]


def test_vase_always_drops_a_heart_without_a_roll():
    rig = Rig()
    rig.manager.rng = ScriptedRng()
    cut(rig, 0x2ac)
    assert rig.manager.rng.calls == []
    assert rig.sent == [("a.nw", b"\x36\x34\x48\x25\n", None)]


def test_vasesdrop_false_disables_vases():
    rig = Rig(vasesdrop=False)
    cut(rig, 0x2ac)
    assert rig.sent == []


def option_config(tmp_path, text):
    path = tmp_path / "serveroptions.txt"
    path.write_text(text)
    return ServerConfig.from_file(str(path))


def test_spawn_rates_are_ignored_while_bushitemtypes_is_absent(tmp_path):
    """The reference builds the table before the options file is read and only
    re-parses it when bushitemtypes itself is set (Settings.h trackOne,
    Settings.cpp load), so the default list keeps the default rates."""
    config = option_config(tmp_path, "spawnrategreenrupee = 90\nspawnrateheart = 0\n")
    assert config.bush_drops == [(GREEN, 25), (BLUE, 2), (HEART, 5), (BOMBS, 5)]


def test_spawn_rates_apply_once_bushitemtypes_is_set(tmp_path):
    config = option_config(tmp_path, (
        "spawnrateheart = 40\nbushitemtypes = greenrupee, HEART , nonsense, 3, darts\n"
        "spawnrategreenrupee = 7\nspawnrate3 = 9\n"))
    # unknown name skipped; "3" is bombs by number and reads spawnrate3;
    # darts has no rate of its own, so 0.
    assert config.bush_drops == [(GREEN, 7), (HEART, 40), (BOMBS, 9), (DARTS, 0)]


def test_empty_bushitemtypes_drops_nothing(tmp_path):
    config = option_config(tmp_path, "bushitemtypes =\n")
    assert config.bush_drops == []
    rig = Rig()
    rig.server.config = config
    rig.manager.rng = ScriptedRng(1)
    cut(rig, 0x002)
    assert rig.sent == []


def test_rates_that_sum_past_100_still_walk_in_order(tmp_path):
    config = option_config(tmp_path, (
        "bushitemtypes = bluerupee,greenrupee\nspawnratebluerupee = 80\n"
        "spawnrategreenrupee = 80\n"))
    rig = Rig()
    rig.server.config = config
    for roll, expected in ((80, BLUE), (81, GREEN), (100, GREEN)):
        rig.manager.rng = ScriptedRng(roll)
        assert rig.manager.roll_bush_drop(config) == expected


def test_runtime_bush_option_change_through_rc():
    rig = Rig()
    rc = RCManager(rig.server)
    rc._apply_server_options("spawnrategreenrupee = 90")
    assert rig.config.bush_drops[0] == (GREEN, 25)       # not re-parsed yet
    rc._apply_server_options("bushitemtypes = bombs,greenrupee")
    assert rig.config.bush_drops == [(BOMBS, 5), (GREEN, 90)]
    rc._apply_server_options("spawnratebombs = 1\nbushitems = false")
    assert rig.config.bush_drops == [(BOMBS, 5), (GREEN, 90)]   # still the snapshot
    assert rig.config.bushitems is False
    assert rig.config.spawn_rates == {"greenrupee": 90, "bombs": 1}


def test_options_round_trip_through_option_lines(tmp_path):
    config = ServerConfig(maxdeathgralats=9, deathitemtypes=["bombs"],
                          bushitemtypes=["heart"], spawn_rates={"heart": 50},
                          bushitems=False)
    path = tmp_path / "serveroptions.txt"
    path.write_text("\n".join(config.option_lines()) + "\n")
    loaded = ServerConfig.from_file(str(path))
    assert (loaded.maxdeathgralats, loaded.deathitemtypes, loaded.bushitems) == (
        9, ["bombs"], False)
    assert loaded.bush_drops == [(HEART, 50)]


def test_boolean_drop_options_accept_only_true_or_false(tmp_path):
    config = option_config(tmp_path, "dropitemsdead = maybe\nbushitems = no\nvasesdrop = FALSE\n")
    assert (config.dropitemsdead, config.bushitems, config.vasesdrop) == (True, True, False)


def test_a_cut_drops_into_a_full_level_without_raising():
    rig = Rig()
    rig.manager.max_items_per_level = 0
    rig.manager.rng = ScriptedRng(1)
    cut(rig, 0x002)
    assert rig.sent == []


# -- the PLI_BOARDMODIFY handler -----------------------------------------------------------

def modify_packet(x, y, w, h, tiles):
    builder = PacketBuilder().write_gchar(x).write_gchar(y).write_gchar(w).write_gchar(h)
    for tile in tiles:
        builder.write_gshort(tile)
    return builder.build()


def handler_rig(version=None, **options):
    rig = Rig(**options)
    player = Player(rig.server, 2, AsyncMock(), MagicMock())
    player.level = rig.level
    player.send_raw = AsyncMock()
    if version is not None:
        player.client_version = version
    rig.server.players[2] = player
    return rig, player


def test_a_cut_tests_the_base_tile_so_a_repeated_packet_rolls_again():
    """The reference tests the level's base tile array, which board changes
    never rewrite (PlayerClientPackets.cpp:144-148, Level.cpp:1743): the same
    cut twice rolls twice. Only the level's item cap bounds that."""
    rig, player = handler_rig()
    rig.level.set_tile(10, 20, 0x002)
    rig.manager.rng = ScriptedRng(1, 1)
    packet = modify_packet(10, 20, 2, 2, [0x1a0] * 4)
    run(player._dispatch_packet(PLI.BOARDMODIFY, packet))
    assert rig.item_types() == [GREEN]
    # The relay (to everyone but the sender) and the drop (to everyone) are separate.
    assert [exclude for _n, p, exclude in rig.sent if p[:1] == g(22)] == [None]
    assert rig.level.get_tile(10, 20) == 0x1a0 and rig.level.get_base_tile(10, 20) == 0x002
    run(player._dispatch_packet(PLI.BOARDMODIFY, packet))
    assert rig.item_types() == [GREEN, GREEN]
    assert rig.manager.rng.calls == [(1, 100), (1, 100)]


def test_a_vase_break_drops_a_heart_through_the_handler():
    rig, player = handler_rig()
    rig.level.set_tile(3, 4, 0x2ac)
    run(player._dispatch_packet(PLI.BOARDMODIFY, modify_packet(3, 4, 2, 2, [0] * 4)))
    assert rig.item_types() == [HEART]


def test_clients_before_2_1_lay_their_own_drops():
    rig, player = handler_rig(version=2.0)
    rig.level.set_tile(10, 20, 0x002)
    rig.manager.rng = ScriptedRng(1)
    run(player._dispatch_packet(PLI.BOARDMODIFY, modify_packet(10, 20, 2, 2, [0] * 4)))
    assert rig.items() == [] and rig.manager.rng.calls == []
    assert rig.level.get_tile(10, 20) == 0                    # the cut itself still applies


def test_a_flood_of_cut_packets_is_bounded_by_the_level_item_cap():
    rig, player = handler_rig()
    rig.manager.max_items_per_level = 7
    rig.level.set_tile(10, 20, 0x002)
    rig.manager.rng = ScriptedRng(*[1] * 50)
    for _ in range(50):
        run(player._dispatch_packet(PLI.BOARDMODIFY, modify_packet(10, 20, 2, 2, [0] * 4)))
    assert rig.item_types() == [GREEN] * 7


# -- level-item lifetime ----------------------------------------------------------------------

def test_items_live_8_point_2_seconds_and_expire_silently():
    """The reference removes them without a packet (Level.cpp:711-716); clients
    run their own timer and answer with PLI_ITEMDEL."""
    rig = Rig()
    item = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.HEART))
    assert item.despawn_time == 8.2
    rig.sent.clear()
    item.created_at -= 8.1
    run(rig.manager._tick())
    assert rig.items() == [(LevelItemType.HEART, 5.0, 6.0)]
    item.created_at -= 0.2
    run(rig.manager._tick())
    assert rig.items() == []
    assert rig.sent == []


def test_client_item_del_relays_to_the_others_and_removes_silently():
    """PlayerClientPackets.cpp:385-403: PLO_ITEMDEL goes to the level's other
    players whether or not the item is still there, then the item at that
    position is removed; a missing one stops there."""
    rig, player = handler_rig()
    run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.HEART))
    rig.sent.clear()
    packet = g(10) + g(12)
    run(player._dispatch_packet(PLI.ITEMDEL, packet))
    assert rig.items() == []
    assert rig.sent == [("a.nw", b"\x37" + g(10) + g(12) + b"\n", {2})]
    run(player._dispatch_packet(PLI.ITEMDEL, packet))          # already gone
    assert rig.items() == []
    assert rig.sent[1:] == [("a.nw", b"\x37" + g(10) + g(12) + b"\n", {2})]


def test_client_item_del_leaves_other_items_alone():
    rig, player = handler_rig()
    run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.HEART))
    run(rig.manager.spawn_item(rig.level, 9.0, 6.0, LevelItemType.GREENRUPEE))
    run(player._dispatch_packet(PLI.ITEMDEL, g(10) + g(12)))
    assert rig.items() == [(LevelItemType.GREENRUPEE, 9.0, 6.0)]


ITEMDEL_5_6 = b"\x37" + g(10) + g(12) + b"\n"


def test_a_failed_item_take_is_still_relayed_to_the_others():
    """PLI_ITEMTAKE shares the PLI_ITEMDEL handler (PlayerClient.cpp:91):
    PLO_ITEMDEL goes to everyone else before the lookup
    (PlayerClientPackets.cpp:389), so a take that finds nothing (already
    taken or gone) is relayed too; the taker is sent nothing and gains
    nothing."""
    rig, player = handler_rig()
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))  # nothing there
    assert rig.sent == [("a.nw", ITEMDEL_5_6, {2})]
    player.send_raw.assert_not_awaited()


def test_a_take_of_an_expired_item_is_relayed_and_removes_it():
    rig, player = handler_rig()
    item = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GREENRUPEE))
    item.created_at -= 9.0                                     # past 8.2 s
    player.x, player.y, player.rupees = 4.5, 5.0, 0
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert rig.sent == [("a.nw", ITEMDEL_5_6, {2})]
    assert rig.items() == [] and player.rupees == 0
    player.send_raw.assert_not_awaited()


def test_a_successful_item_take_relays_first_then_grants_without_an_echo():
    """The relay to the others goes out before the removal and the grant
    (PlayerClientPackets.cpp:389, :394-399); the taker is sent no
    PLO_ITEMDEL, its client dropped the item when it sent the take."""
    rig, player = handler_rig()
    item = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GREENRUPEE))
    item.created_at -= 1.0                                     # old enough to collect
    player.x, player.y, player.rupees = 4.5, 5.0, 0            # standing on it
    rig.sent.clear()
    events = []
    relay = rig.server.broadcast_to_level

    async def broadcast(level_name, packet, exclude=None):
        events.append(("relay", len(rig.items())))
        await relay(level_name, packet, exclude)

    grant = rig.manager.give_item_to_player

    async def give(who, kind):
        events.append(("grant", len(rig.items())))
        await grant(who, kind)

    rig.server.broadcast_to_level = broadcast
    rig.manager.give_item_to_player = give
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert events == [("relay", 1), ("grant", 0)]
    assert rig.items() == []
    assert player.rupees == 1
    assert rig.sent == [("a.nw", ITEMDEL_5_6, {2})]            # the others, once
    assert [c.args[0] for c in player.send_raw.await_args_list
            if c.args[0][:1] == b"\x37"] == []                  # no echo


def test_an_out_of_reach_take_of_a_live_item_changes_nothing():
    """pygserver's reach check (not in the reference): the item stays on the
    ground for everyone, nobody is told anything, nothing is granted."""
    rig, player = handler_rig()
    item = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GREENRUPEE))
    item.created_at -= 1.0
    player.x, player.y, player.rupees = 40.0, 40.0, 0          # far away
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert rig.sent == []
    assert rig.items() == [(LevelItemType.GREENRUPEE, 5.0, 6.0)]
    assert player.rupees == 0
    player.send_raw.assert_not_awaited()
    player.x, player.y = 4.5, 5.0                              # walks over: takes it
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert rig.sent == [("a.nw", ITEMDEL_5_6, {2})] and player.rupees == 1


def test_a_take_inside_the_age_guard_is_ignored_with_no_relay():
    """A live item that the 0.2 s age guard refuses (TServerLevel.cpp:1416) is
    neither relayed nor removed nor granted: the others keep seeing it."""
    rig, player = handler_rig()
    run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GREENRUPEE))
    player.x, player.y, player.rupees = 4.5, 5.0, 0
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert rig.sent == []
    assert rig.items() == [(LevelItemType.GREENRUPEE, 5.0, 6.0)]
    assert player.rupees == 0


def test_a_take_from_another_layer_is_ignored_with_no_relay():
    rig, player = handler_rig()
    item = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GREENRUPEE))
    item.created_at -= 1.0
    player.x, player.y, player.rupees, player.layer = 4.5, 5.0, 0, 1
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert rig.sent == []
    assert rig.items() == [(LevelItemType.GREENRUPEE, 5.0, 6.0)]
    assert player.rupees == 0
    player.layer = 0                                           # same layer: taken
    run(player._dispatch_packet(PLI.ITEMTAKE, g(10) + g(12)))
    assert rig.sent == [("a.nw", ITEMDEL_5_6, {2})] and player.rupees == 1


def test_a_take_resolves_to_the_item_at_the_packet_position():
    """Gold at (5,6) and green at (5.5,6.5), a take at (5.5,6.5): the green
    one is credited and the others are told that item's own position."""
    rig, player = handler_rig()
    gold = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GOLDRUPEE))
    green = run(rig.manager.spawn_item(rig.level, 5.5, 6.5, LevelItemType.GREENRUPEE))
    gold.created_at -= 1.0
    green.created_at -= 1.0
    player.x, player.y, player.rupees = 4.5, 5.0, 0
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(11) + g(13)))
    assert player.rupees == 1
    assert rig.items() == [(LevelItemType.GOLDRUPEE, 5.0, 6.0)]
    assert rig.sent == [("a.nw", b"\x37" + g(11) + g(13) + b"\n", {2})]


def test_a_take_with_no_exact_match_picks_the_nearest_item_and_relays_its_position():
    rig, player = handler_rig()
    far = run(rig.manager.spawn_item(rig.level, 6.0, 6.5, LevelItemType.GREENRUPEE))
    near = run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GOLDRUPEE))
    far.created_at -= 1.0
    near.created_at -= 1.0
    player.x, player.y, player.rupees = 4.5, 5.0, 0
    rig.sent.clear()
    run(player._dispatch_packet(PLI.ITEMTAKE, g(11) + g(12)))     # (5.5, 6.0)
    assert player.rupees == 100
    assert rig.items() == [(LevelItemType.GREENRUPEE, 6.0, 6.5)]
    assert rig.sent == [("a.nw", ITEMDEL_5_6, {2})]


def test_item_del_removes_the_item_at_the_position_not_a_neighbour():
    rig, player = handler_rig()
    run(rig.manager.spawn_item(rig.level, 5.0, 6.0, LevelItemType.GOLDRUPEE))
    run(rig.manager.spawn_item(rig.level, 5.25, 6.25, LevelItemType.GREENRUPEE))
    run(player._dispatch_packet(PLI.ITEMDEL, g(10) + g(12)))
    assert rig.items() == [(LevelItemType.GREENRUPEE, 5.25, 6.25)]


@pytest.mark.parametrize("tiles", [-200.0, -63.5, -11.0, -10.5, -2.5, -0.5, 0.0, 0.5, 63.5,
                                   111.0, 111.5, 112.0, 500.0])
def test_item_coordinates_never_encode_an_unframed_newline(tiles):
    """-11 tiles is half-tile -22, which this module's wrapping GCHAR writer
    would turn into 0x0A; the reference caps at 223 instead."""
    from pygserver.protocol.packets import build_item_del
    for packet in (build_item_add(tiles, tiles, GREEN), build_item_del(tiles, tiles)):
        assert packet[-1:] == b"\n" and b"\n" not in packet[:-1]
        assert min(packet[1:3]) >= 32 and max(packet[1:3]) <= 255


def test_drops_at_every_integer_local_position_stay_framed():
    """Whole-board sweep, edges and a garbage client position included."""
    for tile in range(-90, 150):
        rig = Rig(mindeathgralats=1, maxdeathgralats=1)
        rig.manager.rng = ScriptedRng(1, 0, 0, 0, 0)
        rig.death(rig.player(x=float(tile), y=float(tile), rupees=1))
        for _name, packet, _exclude in rig.sent:
            assert packet[-1:] == b"\n" and b"\n" not in packet[:-1], (tile, packet)
