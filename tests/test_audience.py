"""The audience/spatial-query service.

The shapes are deliberately different per event (see audience.py's module
docstring for what the GServer-v2 oracle does), so these tests pin the shapes as
policy, not as a thing to normalize.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pygserver.audience import (
    ARROW_HIT_PLAYERS,
    BOMB_BLAST_BADDIES,
    BOMB_BLAST_NPCS,
    BOMB_BLAST_PLAYERS,
    FIRESPY_HIT_PLAYERS,
    GS1_EXPLOSION_PLAYERS,
    Audience,
    Shape,
    contains,
)
from pygserver.level import Level
from pygserver.world import GMap


class Entity:
    def __init__(self, x, y, visible=True):
        self.x = x
        self.y = y
        self.visible = visible


class AudienceServer:
    def __init__(self, levels):
        self.world = MagicMock()
        self._levels = {level.name: level for level in levels}
        self.world.get_level = MagicMock(side_effect=self._levels.get)
        self.world.get_gmap_for_level = MagicMock(return_value=None)
        self.players = {}
        self.npc_manager = MagicMock()
        self.npcs = []
        self.npc_manager.get_npcs_on_level = MagicMock(
            side_effect=lambda level: self.npcs)
        self.audience = Audience(self)

    def get_player(self, player_id):
        return self.players.get(player_id)

    def add_player(self, level, player_id, x=0.0, y=0.0):
        player = MagicMock()
        player.id = player_id
        player.x, player.y = x, y
        player.send_raw = AsyncMock()
        self.players[player_id] = player
        level.add_player(player)
        return player


# -- shapes -----------------------------------------------------------------

def test_circle_is_euclidean():
    # (3, 4) is exactly 5 away: outside a radius-5 circle (strict <).
    assert contains(Shape.CIRCLE, 0, 0, 5, 3, 3.9)
    assert not contains(Shape.CIRCLE, 0, 0, 5, 3, 4)


def test_box_is_axis_aligned():
    """The diagonal corner a circle excludes is inside a box of the same
    radius - the difference the named policies keep explicit."""
    assert contains(Shape.BOX, 0, 0, 5, 3, 4)
    assert contains(Shape.BOX, 0, 0, 5, 4.9, -4.9)
    assert not contains(Shape.BOX, 0, 0, 5, 5, 0)


def test_named_policies_keep_the_historical_shapes():
    assert BOMB_BLAST_PLAYERS is Shape.CIRCLE
    assert BOMB_BLAST_NPCS is Shape.CIRCLE
    assert BOMB_BLAST_BADDIES is Shape.CIRCLE
    assert ARROW_HIT_PLAYERS is Shape.BOX
    assert FIRESPY_HIT_PLAYERS is Shape.BOX
    # gs1_host's putexplosion disagrees with the bomb path; recorded, not fixed.
    assert GS1_EXPLOSION_PLAYERS is not BOMB_BLAST_PLAYERS


# -- membership -------------------------------------------------------------

def test_players_on_level_reads_the_levels_own_player_set():
    level, other = Level("a.nw"), Level("b.nw")
    server = AudienceServer([level, other])
    here = server.add_player(level, 2)
    server.add_player(other, 3)

    assert server.audience.players_on_level("a.nw") == [here]


def test_players_on_level_honours_exclude():
    level = Level("a.nw")
    server = AudienceServer([level])
    server.add_player(level, 2)
    kept = server.add_player(level, 3)

    assert server.audience.players_on_level("a.nw", exclude={2}) == [kept]


def test_players_on_level_is_empty_for_an_unknown_level():
    server = AudienceServer([])
    assert server.audience.players_on_level("nope.nw") == []


def test_players_on_level_skips_ids_the_server_no_longer_knows():
    """A level keeps its id set; a player that has already been removed from the
    server (disconnecting) must not be handed back."""
    level = Level("a.nw")
    server = AudienceServer([level])
    gone = server.add_player(level, 2)
    del server.players[2]

    assert server.audience.players_on_level("a.nw") == []
    assert gone.id == 2


def test_players_in_world_includes_other_segments_but_not_unrelated_levels():
    first, second, unrelated = (
        Level("a.nw"), Level("b.nw"), Level("elsewhere.nw"))
    server = AudienceServer([first, second, unrelated])
    gmap = GMap("world")
    gmap.grid = {(0, 0): first.name, (1, 0): second.name}
    server.world.get_gmap_for_level = MagicMock(
        side_effect=lambda name: (
            (gmap, 0, 0) if name == first.name
            else (gmap, 1, 0) if name == second.name
            else None
        )
    )
    across_segment = server.add_player(second, 2)
    server.add_player(unrelated, 3)

    assert server.audience.players_in_world(first.name) == [across_segment]


# -- spatial queries --------------------------------------------------------

def test_players_near_filters_by_shape():
    level = Level("a.nw")
    server = AudienceServer([level])
    near = server.add_player(level, 2, x=10.0, y=10.0)
    server.add_player(level, 3, x=10.0, y=14.0)

    found = server.audience.players_near("a.nw", 10.0, 10.0, 2.0, Shape.CIRCLE)

    assert found == [near]


def test_npcs_near_skips_invisible_npcs():
    level = Level("a.nw")
    server = AudienceServer([level])
    visible = Entity(10.0, 10.0)
    server.npcs = [visible, Entity(10.0, 10.5, visible=False)]

    found = server.audience.npcs_near(level, 10.0, 10.0, 2.0, Shape.CIRCLE)

    assert found == [visible]


def test_entities_near_works_on_a_managers_own_set():
    baddies = [Entity(5.0, 5.0), Entity(20.0, 20.0)]

    found = Audience.entities_near(baddies, 5.5, 5.5, 2.0, Shape.CIRCLE)

    assert found == [baddies[0]]


# -- delivery ---------------------------------------------------------------

def test_broadcast_to_level_sends_to_everyone_but_the_excluded():
    level = Level("a.nw")
    server = AudienceServer([level])
    first = server.add_player(level, 2)
    second = server.add_player(level, 3)

    asyncio.run(server.audience.broadcast_to_level("a.nw", b"pkt", exclude={3}))

    first.send_raw.assert_awaited_once_with(b"pkt")
    second.send_raw.assert_not_awaited()
