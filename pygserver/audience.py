"""
pygserver.audience - one spatial-query service for "who is affected by this".

Every "which players/NPCs/baddies does this event reach" question used to be
answered by its own inline loop (level broadcast, join roster, bomb blast, baddy
blast, the GS1 host's explosion), and those loops did not agree on either the
membership source or the shape of the affected area. This module provides
membership and shared shapes; combat.py owns sprite and explosion-cell tests.

Membership. `players_on_level` is the single definition of "attached to this
level", and it reads the level's own player set (`Level.get_player_ids`), which
is what the reference server does too - Server::sendPacketToOneLevelPart walks
Level::findPlayersInLevelPart over `Level::m_players`
(server/src/Server.cpp:2682-2688, server/src/level/Level.cpp:3086-3095). That is
also O(players on the level) rather than O(players on the server). Level
membership and `Player.level` are kept in lockstep: Level.add_player /
Level.remove_player are only ever called from Player.warp / Player._cleanup,
which set `Player.level` in the same synchronous step.

Shapes are an explicit, named policy per event, NOT normalized, because the
choice is a gameplay hitbox:

- `Shape.CIRCLE` - euclidean `distance < radius`. Used for NPC/baddy blasts.
- `Shape.BOX` - axis-aligned `|dx| < radius and |dy| < radius`. Used for
  firespy hits, and the shape GServer-v2 itself uses
  for its audience query (Level::findInRangePlayers, server/src/level/Level.cpp:
  3025, `abs(dx) <= syncx && abs(dy) <= syncy` - inclusive there, strict here,
  kept strict so no existing hit test changes by a boundary tile).
- `Shape.BOX_INCLUSIVE` - the same box with `<=`, i.e. a boundary tile counts.
  Only the GS1 explosion uses it, and only because that is the hitbox it has
  always had (see GS1_EXPLOSION_PLAYERS).

What the oracle says about explosions specifically: GServer-v2 uses NEITHER a
circle nor a square. Level::addExplosion (server/src/level/Level.cpp:2131-2144)
tests a PLUS shape - two 1-tile-wide strips, one vertical of height
(1 + 2*radius) tiles and one horizontal of the same width - and it applies that
only to NPCs (`hurtAndPush(..., EXPLODED)`); players and baddies take no
server-side blast damage there at all, since the reference server trusts the
client's own PLI_HURTPLAYER / PLI_BADDYHURT report. pygserver is deliberately
authoritative for damage instead. Player bomb hits use the official client's
cross of explosion cells, and arrow hits use sprite/body rectangles, both in
combat.py. NPC/baddy blasts retain the circle policies below.
"""

import math
from enum import Enum, auto
from typing import Callable, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING

from .attachments import absolute_position
from .protocol.constants import PLPROP

if TYPE_CHECKING:
    from .level import Level
    from .player import Player
    from .server import GameServer


class Shape(Enum):
    """The area an event covers around its origin."""

    CIRCLE = auto()          # euclidean distance < radius
    BOX = auto()             # |dx| < radius and |dy| < radius
    BOX_INCLUSIVE = auto()   # |dx| <= radius and |dy| <= radius


def contains(shape: Shape, cx: float, cy: float, radius: float,
             x: float, y: float) -> bool:
    """True if (x, y) lies inside `shape` of `radius` centred on (cx, cy)."""
    dx = x - cx
    dy = y - cy
    if shape is Shape.BOX:
        return abs(dx) < radius and abs(dy) < radius
    if shape is Shape.BOX_INCLUSIVE:
        return abs(dx) <= radius and abs(dy) <= radius
    return math.hypot(dx, dy) < radius


# =============================================================================
# Named policies: which shape each event uses. Changing one of these changes a
# gameplay hitbox, so they are declared here instead of hidden in a loop.
# =============================================================================

# The player props the reference sends to every player rather than the ones
# around the sender (SetResults::sendToAll): nickname
# (server/src/player/PlayerProps.cpp:203), head image (:419) and the
# player-list status (:1001); the GS1 `sethead` forces it for the head
# (server/src/scripting/gs1/GS1Commands.cpp:2533). Every other prop stays on
# sendToLevel, which is the 3x3 on a gmap. One packet carries both kinds as
# two separate buffers (outAll / outLevel,
# server/src/utilities/PropertySerializers.cpp:726-737), so a sendToAll prop
# is never repeated in the nearby packet. The player-list status is prop 53,
# PSTATUSMSG in PLPROP and PLAYERLISTSTATUS in the reference
# (PlayerProps.h:76; GATTRIB10 is 54).
SEND_TO_ALL_PROPS = frozenset({PLPROP.NICKNAME, PLPROP.HEADIMAGE,
                               PLPROP.PSTATUSMSG})

BOMB_BLAST_NPCS = Shape.CIRCLE
BOMB_BLAST_BADDIES = Shape.CIRCLE
FIRESPY_HIT_PLAYERS = Shape.BOX
# GS1 putexplosion retains its inclusive box policy. Bombs use cell tests
# in combat.py; the two callers have separate collision rules.
GS1_EXPLOSION_PLAYERS = Shape.BOX_INCLUSIVE


class Audience:
    """Spatial/level queries against the server's live player and NPC sets."""

    def __init__(self, server: 'GameServer'):
        self.server = server

    # -- membership --------------------------------------------------------

    def players_on_level(self, level_name: str,
                         exclude: Optional[Set[int]] = None) -> List['Player']:
        """Every client attached to `level_name`.

        Returns a list, not a generator: callers await sends while iterating,
        and a disconnect during that await mutates the level's player set.
        """
        exclude = exclude or frozenset()
        level = self.server.world.get_level(level_name)
        if level is None:
            return []
        out = []
        for player_id in level.get_player_ids():
            if player_id in exclude:
                continue
            player = self.server.get_player(player_id)
            if player is not None:
                out.append(player)
        return out

    def players_in_world(self, level_name: str,
                         exclude: Optional[Set[int]] = None) -> List['Player']:
        """Every client in the map containing `level_name`.

        A standalone level is its own world. A grid map is one contiguous
        world, so membership includes every loaded segment in its grid.
        """
        gmap_info = self.server.world.get_gmap_for_level(level_name)
        if gmap_info is None:
            return self.players_on_level(level_name, exclude)

        gmap = gmap_info[0]
        players = []
        seen = set()
        for segment_name in gmap.grid.values():
            for player in self.players_on_level(segment_name, exclude):
                if player.id not in seen:
                    seen.add(player.id)
                    players.append(player)
        return players

    def levels_in_range_for_data(self, level_name: str,
                                 cell: Optional[Tuple[int, int]] = None) -> List[str]:
        """The level names whose players and NPCs are in range for data of
        `level_name`: on a gmap the segments of the 3x3 around its cell (or
        around `cell`, a map position that need not be the level's own),
        clipped to the map (Level.cpp:3341-3344, :3446-3449); otherwise the
        level itself."""
        gmap_info = self.server.world.get_gmap_for_level(level_name)
        if gmap_info is None:
            return [level_name]
        gmap, cell_x, cell_y = gmap_info
        if cell is not None:
            cell_x, cell_y = cell
        names = []
        for step_y in (-1, 0, 1):
            for step_x in (-1, 0, 1):
                name = gmap.get_level_at(cell_x + step_x, cell_y + step_y)
                if name and name not in names:
                    names.append(name)
        return names

    def players_in_range_for_data(self, level_name: str,
                                  exclude: Optional[Set[int]] = None,
                                  cell: Optional[Tuple[int, int]] = None) -> List['Player']:
        """Level::findPlayersInRangeForData (Level.cpp:3320-3372): on a gmap
        the players whose cell lies in the 3x3 around `level_name`'s cell;
        off a gmap the level's players. Live NPC updates go to these
        (Server::sendPacketToNearby, Server.cpp:2390-2414). `cell` takes the
        3x3 around another map position instead (a leaver's destination)."""
        players = []
        for name in self.levels_in_range_for_data(level_name, cell):
            players.extend(self.players_on_level(name, exclude))
        return players

    def npcs_in_range_for_data(self, level_name: str) -> List:
        """Level::findNPCsInRangeForData (Level.cpp:3425-3477): the NPCs of
        the 3x3 around `level_name`'s cell on a gmap, else the level's."""
        npcs = []
        for name in self.levels_in_range_for_data(level_name):
            level = self.server.world.get_level(name)
            if level is not None:
                npcs.extend(level.get_npcs())
        return npcs

    def players_in_range_for_sync(self, level_name: str, x: float, y: float,
                                  exclude: Optional[Set[int]] = None,
                                  cell: Optional[Tuple[int, int]] = None) -> List['Player']:
        """Level::findPlayersInRangeForSync (Level.cpp:3265-3318) for a player
        at level-local tiles (x, y) on `level_name`: on a gmap the players
        within `syncdistancex` / `syncdistancey` tiles on both axes,
        inclusive, in gmap tiles (default 192, Server.h:127); every player of
        the gmap once the box covers the whole map. Off a gmap the level's
        players (`syncbydistanceinside` is not modelled; its default is off,
        Server.h:126)."""
        gmap_info = self.server.world.get_gmap_for_level(level_name)
        if gmap_info is None:
            return self.players_on_level(level_name, exclude)
        gmap, cell_x, cell_y = gmap_info
        if cell is not None:
            cell_x, cell_y = cell
        config = getattr(self.server, 'config', None)
        sync_x = getattr(config, 'syncdistancex', 192)
        sync_y = getattr(config, 'syncdistancey', 192)
        everyone = self.players_in_world(level_name, exclude)
        if sync_x >= gmap.width * 64 and sync_y >= gmap.height * 64:
            return everyone
        origin_x = cell_x * 64 + x
        origin_y = cell_y * 64 + y
        out = []
        for player in everyone:
            info = self.server.world.get_gmap_for_level(player.level.name)
            if info is None:
                continue
            px, py = absolute_position(player)
            if (abs(info[1] * 64 + px - origin_x) <= sync_x
                    and abs(info[2] * 64 + py - origin_y) <= sync_y):
                out.append(player)
        return out

    # -- spatial -----------------------------------------------------------

    def players_near(self, level_name: str, x: float, y: float, radius: float,
                     shape: Shape,
                     exclude: Optional[Set[int]] = None) -> List['Player']:
        """Players on `level_name` inside `shape` of `radius` around (x, y)."""
        return [
            p for p in self.players_on_level(level_name, exclude)
            if contains(shape, x, y, radius, absolute_position(p)[0], absolute_position(p)[1])
        ]

    def npcs_near(self, level: 'Level', x: float, y: float, radius: float,
                  shape: Shape, visible_only: bool = True) -> List:
        """NPCs on `level` inside `shape` of `radius` around (x, y)."""
        manager = getattr(self.server, 'npc_manager', None)
        if manager is None or not hasattr(manager, 'get_npcs_on_level'):
            return []
        return [
            npc for npc in manager.get_npcs_on_level(level)
            if (not visible_only or getattr(npc, 'visible', True))
            and contains(shape, x, y, radius, npc.x, npc.y)
        ]

    @staticmethod
    def entities_near(entities: Iterable, x: float, y: float, radius: float,
                      shape: Shape,
                      position: Callable = lambda e: (e.x, e.y)) -> List:
        """Generic form for entity sets a manager owns itself (e.g. baddies)."""
        out = []
        for entity in entities:
            ex, ey = position(entity)
            if contains(shape, x, y, radius, ex, ey):
                out.append(entity)
        return out

    # -- delivery ----------------------------------------------------------

    async def broadcast_to_level(self, level_name: str, packet: bytes,
                                 exclude: Optional[Set[int]] = None):
        """Send `packet` to every client attached to `level_name`."""
        for player in self.players_on_level(level_name, exclude):
            await player.send_raw(packet)

    async def broadcast_to_world(self, level_name: str, packet: bytes,
                                 exclude: Optional[Set[int]] = None):
        """Send `packet` to every client in the containing map."""
        for player in self.players_in_world(level_name, exclude):
            await player.send_raw(packet)

    async def broadcast_nearby(self, level_name: str, packet: bytes,
                               exclude: Optional[Set[int]] = None):
        """Send `packet` to the players in range for data of `level_name`."""
        for player in self.players_in_range_for_data(level_name, exclude):
            await player.send_raw(packet)
