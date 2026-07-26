"""
pygserver.audience - one spatial-query service for "who is affected by this".

Every "which players/NPCs/baddies does this event reach" question used to be
answered by its own inline loop (level broadcast, join roster, bomb blast, baddy
blast, the GS1 host's explosion), and those loops did not agree on either the
membership source or the shape of the affected area. This module owns both.

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

- `Shape.CIRCLE` - euclidean `distance < radius`. What our bomb blast and baddy
  blast have always used.
- `Shape.BOX` - axis-aligned `|dx| < radius and |dy| < radius`. What our arrow
  and firespy hit tests have always used, and the shape GServer-v2 itself uses
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
authoritative for damage instead, so there is no reference shape to copy for its
player/baddy blast and the historical circle is kept as an explicit policy
below. The plus shape is documented rather than implemented because no call site
uses it yet.
"""

import math
from enum import Enum, auto
from typing import Callable, Iterable, List, Optional, Set, TYPE_CHECKING

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

BOMB_BLAST_PLAYERS = Shape.CIRCLE
BOMB_BLAST_NPCS = Shape.CIRCLE
BOMB_BLAST_BADDIES = Shape.CIRCLE
ARROW_HIT_PLAYERS = Shape.BOX
FIRESPY_HIT_PLAYERS = Shape.BOX
# gs1_host's putexplosion (gs1_host._explode) damages players in a BOX while the
# bomb path above uses a CIRCLE. That disagreement is real and unresolved; it is
# recorded here so nothing quietly picks one. The boundary IS inclusive: at
# radius 3 the edge and corner players are in range, and reading this as a
# strict box drops the blast's entire outer ring.
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

    # -- spatial -----------------------------------------------------------

    def players_near(self, level_name: str, x: float, y: float, radius: float,
                     shape: Shape,
                     exclude: Optional[Set[int]] = None) -> List['Player']:
        """Players on `level_name` inside `shape` of `radius` around (x, y)."""
        return [
            p for p in self.players_on_level(level_name, exclude)
            if contains(shape, x, y, radius, p.x, p.y)
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
