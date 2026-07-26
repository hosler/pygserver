"""pygserver Host for the GS1 interpreter.

Bridges GS1 scripts to live game objects: built-in attributes (playerx, x,
hearts, ...) read/write the Player and NPC, commands (setimg, hide, move, ...)
mutate them and mark them dirty for prop broadcast, message codes (#a, #n, #c)
expand from the player, and game functions (onwall, ...) query the level.

The interpreter calls into this via the runtime.Host interface. NPC-scoped
state (this./local.) persists on the NPC; bare player flags persist on the
player; server/level scopes persist on the server/level.

See memory: gs1-python-port. Conventions: unprefixed x/y/dir/sprite refer to
the NPC running the script; player* attributes refer to the acting player.
"""
from __future__ import annotations

import logging
import math
import re
import time

from reborn_protocol.coords import (
    LEVEL_SIZE, segment_index,
)
from reborn_protocol.gs1.runtime import Host, UNSET
from reborn_protocol.gs1.values import to_num, to_str
from reborn_protocol.gs1.host_shared import (
    A_CLASS_NPC_ATTR, A_CLASS_PLAYER_ATTR, host_value,
)

from .. import tiletypes
from .players import players_on_level_for, leader_player_for_level

logger = logging.getLogger(__name__)

# Surface GS1 script/command failures (they used to be swallowed at DEBUG,
# invisible by default) without spamming: dedup per (site, exception type,
# message) signature, mirroring pyreborn.gs1_client's _report_gs1_error.
_GS1_ERR_SEEN: set = set()


def _report_gs1_error(site: str, exc: Exception) -> None:
    sig = (site, type(exc).__name__, str(exc)[:160])
    if sig in _GS1_ERR_SEEN:
        return
    _GS1_ERR_SEEN.add(sig)
    logger.warning("GS1 %s: %s: %s", site, type(exc).__name__, exc, exc_info=True)

try:
    from ..protocol.constants import PLPROP, NPCPROP

    # gani-attribute slot N (1-30) -> wire prop id. Player GATTRIBs are
    # contiguous (37-74); NPC GATTRIBs follow NPCGaniAttrPackets (sparse).
    _PLAYER_GATTRIB_PROPS = [getattr(PLPROP, f"GATTRIB{n}") for n in range(1, 31)]
    _NPC_GATTRIB_PROPS = [getattr(NPCPROP, f"GATTRIB{n}") for n in range(1, 31)]

    # player Python attr -> (wire prop id, value encoder) for change propagation
    PLAYER_PROP_WIRE = {
        "rupees": (PLPROP.RUPEESCOUNT, lambda v: int(to_num(v))),
        "hearts": (PLPROP.CURPOWER, lambda v: int(to_num(v) * 2)),
        "max_hearts": (PLPROP.MAXPOWER, lambda v: int(to_num(v))),
        "arrows": (PLPROP.ARROWSCOUNT, lambda v: int(to_num(v))),
        "bombs": (PLPROP.BOMBSCOUNT, lambda v: int(to_num(v))),
        "glove_power": (PLPROP.GLOVEPOWER, lambda v: int(to_num(v))),
        "sword_power": (PLPROP.SWORDPOWER, lambda v: int(to_num(v))),
        "shield_power": (PLPROP.SHIELDPOWER, lambda v: int(to_num(v))),
        "nickname": (PLPROP.NICKNAME, to_str),
        "head_image": (PLPROP.HEADIMAGE, to_str),
        "body_image": (PLPROP.BODYIMAGE, to_str),
        "gani": (PLPROP.GANI, to_str),
        "chat": (PLPROP.CURCHAT, to_str),
    }
except Exception:  # constants unavailable (e.g. isolated unit context)
    PLPROP = None
    NPCPROP = None
    PLAYER_PROP_WIRE = {}
    _PLAYER_GATTRIB_PROPS = []
    _NPC_GATTRIB_PROPS = []

# player-prefixed attribute name -> Python attribute on Player
PLAYER_ATTR = {
    **A_CLASS_PLAYER_ATTR,
    "playerx": "x", "playery": "y",
    "playerglovepower": "glove_power", "playerkills": "kills",
    "playerdeaths": "deaths", "playerchat": "chat",
    "playeraccount": "account_name",
    "playerap": "ap", "playergani": "gani",
}
# unprefixed attribute name -> Python attribute on the NPC ("this")
NPC_ATTR = {
    **A_CLASS_NPC_ATTR,
    "hearts": "hearts", "rupees": "rupees", "arrows": "arrows",
    "bombs": "bombs",
}

# setcharprop / setplayerprop message codes -> target. Mirrors the C++
# GS1MessageCodes GetNPCPropFromIndex / GetPlayerPropFromIndex tables, keyed by
# the raw codes GS1 actually lexes: #1-8 equipment, #m gani, #n nick, #c chat,
# #C0-#C7 color slots (indices 20-27), and #P1-#P30 gani-attribute slots
# (handled dynamically by _charprop_target). A ("color", n) / ("gattrib", n)
# value targets that slot; otherwise it's a Python attr. NPCs store chat in
# `message`, players in `chat`. (#9/#10/#20 are not valid GS1 codes.)
_CHARPROP_CODES = {
    "#1": "sword_image", "#2": "shield_image", "#3": "head_image",
    "#5": "horse_image", "#7": "gani", "#8": "body_image",
    "#m": "gani", "#n": "nickname",
    **{f"#C{n}": ("color", n) for n in range(8)},
}
NPC_CHARPROP = {**_CHARPROP_CODES, "#c": "message"}
PLAYER_CHARPROP = {**_CHARPROP_CODES, "#c": "chat"}

# #P1..#P30 -> gani attribute slot 1..30 (C++ mc_P: index N uses prop 30+N-1)
_GANI_ATTR_RE = re.compile(r"#P(\d+)$")
# #C0..#C7 colour slots as READ values (write side is _CHARPROP_CODES)
_COLOR_CODE_RE = re.compile(r"#C([0-7])$")

# nw* clock variables (GServer-v2 Server.cpp:178-185, epoch/formula fixed
# upstream in ac3adf01). This is a synthetic in-game clock derived from real
# time, not wall-clock minutes: despite the inline C++ comments calling the
# base unit "minutes", the actual tick is (unix_time - _NW_EPOCH) // 5 -
# nwtime/nwmin/nwhour/nwday/nwweekday/nwweek/nwmonth/nwyear are all just that
# single counter divided/wrapped at different scales (60/1440/10080/40320/
# 403200 ticks respectively). day/weekday/week/month are 1-indexed; year
# starts at 1000. Distinct from `timevar`, which stays an unimplemented
# builtin here (see the comment on call_function).
_NW_EPOCH = 981048814.0  # Thu Feb 01 2001 17:33:34 GMT
_NW_CLOCK_FIELDS = (
    "nwtime", "nwmin", "nwhour", "nwday", "nwweekday", "nwweek",
    "nwmonth", "nwyear",
)


def _nw_clock_value(name):
    ticks = int((time.time() - _NW_EPOCH) / 5)
    if name == "nwtime":
        return float(ticks % 1440)
    if name == "nwmin":
        return float(ticks % 60)
    if name == "nwhour":
        return float((ticks // 60) % 24)
    if name == "nwday":
        return float((ticks // 1440) % 28 + 1)
    if name == "nwweekday":
        return float((ticks // 1440) % 7 + 1)
    if name == "nwweek":
        return float((ticks // 10080) % 40 + 1)
    if name == "nwmonth":
        return float((ticks // 40320) % 10 + 1)
    return float((ticks // 403200) + 1000)  # nwyear


# wasshot's initiator-source flags (GS1Flags.cpp:136-138) -> the `source`
# string run_npc_event/_fire_gs1 stash as ctx.hit_source.
_SHOTBY_SOURCE = {
    "shotbyplayer": "player", "shotbybaddy": "baddy", "shotbynpc": "npc",
}

_PELTWITH_TYPE = {
    "peltwithbush": 2, "peltwithstone": 3, "peltwithvase": 4,
    "peltwithsign": 5, "peltwithblackstone": 10,
    "peltwithnpc": 11, "peltwithperson": 11, "peltwithplayer": 12,
}


def _charprop_target(code, table):
    """Resolve a setcharprop/setplayerprop message code to its target.
    Static codes come from `table`; #P<n> maps to ("gattrib", n)."""
    target = table.get(code)
    if target is not None:
        return target
    m = _GANI_ATTR_RE.match(code)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 30:
            return ("gattrib", n)
    return None


class GS1Host(Host):
    def __init__(self, server=None):
        self.server = server

    # -- built-in attribute access ----------------------------------------
    def get_builtin(self, name, indices, ctx):
        """Read a GS1 built-in variable.

        Registry-driven like call_command: `_BUILTINS` maps every name to its
        reader, and the two plain attribute tables (PLAYER_ATTR / NPC_ATTR) are
        consulted afterwards. UNSET means "not a built-in here" and sends the
        interpreter on to the ordinary flag/var lookup -- which is also what a
        reader returns when its gate (a player / an NPC) is missing.
        """
        player = ctx.player
        npc = ctx.this_obj
        reader = _BUILTINS.get(name)
        if reader is not None:
            return reader(self, name, indices, ctx, npc, player)
        if name in PLAYER_ATTR and player is not None:
            value = getattr(player, PLAYER_ATTR[name], 0)
            # The player wire/state scale is 0/2/3; NPC glove power is 0/1/2.
            return self._coerce(value)
        if name in NPC_ATTR and npc is not None:
            return self._coerce(getattr(npc, NPC_ATTR[name], 0))
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        player = ctx.player
        npc = ctx.this_obj
        if name == "tiles":
            resolved = self._resolve_tile(indices, ctx)
            if resolved is not None:
                level, x, y = resolved
                level.set_tile(x, y, int(to_num(value)))
                self._broadcast_tiles(level, x, y, 1, 1)
            return True
        if name in PLAYER_ATTR and player is not None:
            self._set_player_attr(player, PLAYER_ATTR[name], value)
            return True
        if name in NPC_ATTR and npc is not None:
            setattr(npc, NPC_ATTR[name], self._num_or_str(value))
            self._dirty(npc)
            return True
        if name == "sprite" and npc is not None and hasattr(npc, "flags"):
            npc.flags["sprite"] = to_num(value)
            self._dirty(npc)
            return True
        if name == "timeout" and npc is not None:
            self._set_timer(npc, to_num(value))
            return True
        return False

    def _resolve_tile(self, indices, ctx):
        """Resolve tiles[x,y], including classic bigmap segment overflow."""
        if len(indices) < 2:
            return None
        level = getattr(ctx.this_obj, "level", None) if ctx.this_obj is not None else None
        if level is None or not hasattr(level, "get_tile"):
            return None
        x = max(0, int(to_num(indices[0])))
        y = max(0, int(to_num(indices[1])))

        # GServer checks the adjacent segment before reducing the coordinates
        # to the selected level's dimensions. The `> LEVEL_SIZE` (not `>=`)
        # bound is upstream's: tilesCheckForAdjacent tests
        # `tileX > subLevelTiles.width()` (GS1Variables.cpp:400), so tiles[64,y]
        # reads column 0 of the CURRENT level rather than the next segment.
        world = getattr(self.server, "world", None) if self.server is not None else None
        if world is not None and (x > LEVEL_SIZE or y > LEVEL_SIZE):
            info = world.get_gmap_for_level(getattr(level, "name", ""))
            if info is not None:
                gmap, gx, gy = info
                target_name = gmap.get_level_at(gx + segment_index(x),
                                                gy + segment_index(y))
                target = world.get_level(target_name) if target_name else None
                if target is not None:
                    level = target

        width = int(getattr(level, "WIDTH", LEVEL_SIZE))
        height = int(getattr(level, "HEIGHT", LEVEL_SIZE))
        if width <= 0 or height <= 0:
            return None
        x = max(0, min(width - 1, x % width))
        y = max(0, min(height - 1, y % height))
        return level, x, y

    def _broadcast_tiles(self, level, x, y, width, height):
        if self.server is None or not hasattr(level, "_tiles"):
            return
        tiles = bytearray()
        level_width = int(getattr(level, "WIDTH", LEVEL_SIZE))
        for row in range(y, y + height):
            start = (row * level_width + x) * 2
            tiles += bytes(level._tiles[start:start + width * 2])
        try:
            from ..protocol.packets import build_board_modify, build_board_modify2
            world = getattr(self.server, "world", None)
            gmap_info = (
                world.get_gmap_for_level(level.name)
                if world and hasattr(world, "get_gmap_for_level")
                else None
            )
            if gmap_info:
                _, map_x, map_y = gmap_info
                packet = build_board_modify2(
                    map_x, map_y, x, y, width, height, bytes(tiles)
                )
            else:
                packet = build_board_modify(
                    x, y, width, height, bytes(tiles)
                )
            _schedule(self.server.broadcast_to_level(
                level.name, packet))
        except Exception:
            logger.debug("tiles assignment broadcast failed", exc_info=True)

    # -- commands ----------------------------------------------------------
    def call_command(self, name, args, ctx) -> None:
        npc = ctx.this_obj
        player = ctx.player
        try:
            handler = _COMMANDS.get(name)
            if handler is not None:
                handler(self, args, npc, player, ctx)
        except Exception as e:  # a bad command must never kill the script/server
            _report_gs1_error(f"command {name} on npc {getattr(npc, 'id', '?')}", e)

    # -- functions ---------------------------------------------------------
    def call_function(self, name, args, ctx):
        # `timevar` (server clock, GServer-v2 Server::calculateNWTime) is a
        # known missing builtin server-side: falls through to UNSET below,
        # unlike the client host (pyreborn.gs1_client) which computes it.
        handler = self._FUNCTIONS.get(name)
        if handler is not None:
            return handler(self, name, args, ctx)
        # getnpc/getplayer return ScriptObject references that require a
        # script-object member-access model (obj.x / obj.hearts). Deliberately
        # unimplemented: zero usage across the 5732-file GS1 corpus, so it isn't
        # worth the interp rewrite; the nearest-player helpers above cover the
        # real follow/guard idiom by setting ctx.player. -> 0 (falsey).
        return UNSET

    # -- message codes -----------------------------------------------------
    def message_code(self, code, args, ctx) -> str:
        handler = self._MESSAGE_CODES.get(code)
        if handler is not None:
            return handler(args, ctx)
        m = _COLOR_CODE_RE.match(code)
        if m:
            return self._read_color_code(int(m.group(1)), args, ctx)
        return ""

    def _color_code_character(self, args, ctx):
        """Which character a #C<n> READ refers to — mirrors the C++
        handleCharacterBasedMessageCode (GS1MessageCodes.cpp:347):
          * #Cn(-1)  -> the source NPC
          * #Cn(0)   -> the acting player itself
          * #Cn(k>0) -> the k-th player on the level (falls back to the
                        acting player when out of range, exactly like
                        getPlayerFromSource's bounds check)
          * bare #Cn -> the CURRENT SOURCE (getCurrentSource(true)). Inside a
                        setcharprop/setplayerprop value argument that is the
                        command's own pushed target (the NPC / the player —
                        processBuiltInCommand pushSource, GS1Commands.cpp:430;
                        verified live vs gs2emu: the copy idiom
                        `setcharprop #C0,#C0` round-trips the NPC's OWN slot,
                        not the player's). Elsewhere the source stack is
                        empty, so it falls back to the initiating player,
                        else the NPC itself.
        """
        if args:
            idx = int(math.floor(to_num(args[0])))
            if idx == -1:
                return ctx.this_obj
            if idx >= 0:
                if ctx.player is None:
                    return None
                if idx >= 1:
                    players = self._players_on_level(ctx)
                    if idx < len(players):
                        return players[idx]
                return ctx.player
            # other negative indices fall through to the bare-code path
            # (the C++ if/else-if chain only special-cases exactly -1 / >=0)
        src = getattr(ctx, "charprop_source", None)
        if src == "npc" and ctx.this_obj is not None:
            return ctx.this_obj
        if src == "player" and ctx.player is not None:
            return ctx.player
        return ctx.player if ctx.player is not None else ctx.this_obj

    def _read_color_code(self, slot, args, ctx) -> str:
        """#C<slot> as a VALUE resolves to the classic colour NAME of that
        slot (mc_C -> getClassicColorName, Character.h:104), NOT the raw
        index and NOT "". This is what makes the real-corpus copy idiom
        `setcharprop #C0,#C0` round-trip through the name-based write side
        (_resolve_color) instead of zeroing the slot."""
        character = self._color_code_character(args, ctx)
        colors = getattr(character, "colors", None) if character is not None else None
        if not isinstance(colors, list) or not (0 <= slot < len(colors)):
            return ""
        idx = int(to_num(colors[slot]))
        # out-of-enum values (HTML colours, 20+) have no classic name -> ""
        return _CLASSIC_COLORS[idx] if 0 <= idx < len(_CLASSIC_COLORS) else ""

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _coerce(v):
        return host_value(v)

    @staticmethod
    def _num_or_str(value):
        return value  # interpreter already produced a float or str

    @staticmethod
    def _dirty(npc):
        if hasattr(npc, "mark_dirty"):
            npc.mark_dirty()

    def _set_player_attr(self, player, attr, value):
        cur = getattr(player, attr, None)
        if isinstance(cur, str) or attr in ("chat", "nickname", "account_name",
                                            "head_image", "body_image",
                                            "sword_image", "shield_image", "gani"):
            setattr(player, attr, to_str(value))
        else:
            setattr(player, attr, to_num(value))
        # queue the change for propagation to the client (flushed after the
        # event in run_npc_event); chat/account are not client props
        wire = PLAYER_PROP_WIRE.get(attr)
        if wire is not None:
            prop_id, enc = wire
            dirty = getattr(player, "_gs1_dirty_props", None)
            if dirty is None:
                dirty = {}
                player._gs1_dirty_props = dirty
            dirty[prop_id] = enc(getattr(player, attr))

    def _set_timer(self, npc, seconds):
        if hasattr(npc, "set_timer"):
            npc.set_timer(seconds)
        else:
            npc._timer_remaining = seconds

    def _onwall(self, args, ctx):
        lvl = getattr(ctx.this_obj, "level", None) or getattr(ctx.player, "level", None)
        if lvl is None or len(args) < 2 or not hasattr(lvl, "is_blocking"):
            return 0.0
        try:
            return 1.0 if lvl.is_blocking(int(to_num(args[0])), int(to_num(args[1]))) else 0.0
        except Exception:
            return 0.0

    def _playersays(self, args, ctx, contains):
        # playersays(text) / playersays(index,text) — GS1Functions.cpp:963.
        # playersays: case-insensitive EXACT match (string::equalsi).
        # playersays2: case-insensitive CONTAINS (string::findi). An optional
        # leading index selects a level player by position instead of the
        # acting player.
        if not args:
            return 0.0
        if len(args) >= 2:
            idx = int(to_num(args[0]))
            text = to_str(args[1])
            players = self._players_on_level(ctx)
            player = players[idx] if 0 <= idx < len(players) else None
        else:
            text = to_str(args[0])
            player = ctx.player
        if player is None:
            return 0.0
        chat = to_str(getattr(player, "chat", "")).lower()
        text = text.lower()
        if contains:
            return 1.0 if text in chat else 0.0
        return 1.0 if chat == text else 0.0

    # -- world queries -----------------------------------------------------
    def _level_of(self, ctx):
        return getattr(ctx.this_obj, "level", None) or getattr(ctx.player, "level", None)

    def _players_on_level(self, ctx):
        """All logged-in Player objects on the script's level (nearest-* helpers)."""
        return players_on_level_for(self.server, self._level_of(ctx))

    def _player_is_swimming(self, player):
        level = getattr(player, "level", None)
        if level is None:
            return False
        x = math.floor(to_num(getattr(player, "x", 0)) + 1.5)
        y = math.floor(to_num(getattr(player, "y", 0)) + 2.0)
        tile_id = level.get_tile(x, y) if hasattr(level, "get_tile") else 0
        return tiletypes.get_tile_type(tile_id) in (tiletypes.WATER, tiletypes.LAVA)

    def _test_at(self, args, ctx, players):
        miss = -2.0 if players else -1.0
        if len(args) < 2:
            return miss
        px, py = math.floor(to_num(args[0]) * 16), math.floor(to_num(args[1]) * 16)
        objects = self._players_on_level(ctx) if players else []
        level = self._level_of(ctx)
        if players and not objects and level is not None:
            direct = getattr(level, "players", None)
            if direct is not None:
                objects = list(direct.values()) if isinstance(direct, dict) else list(direct)
        if not players:
            if level is not None:
                if hasattr(level, "get_npcs"):
                    objects = level.get_npcs()
                else:
                    direct = getattr(level, "npcs", getattr(level, "_npcs", []))
                    objects = list(direct.values()) if isinstance(direct, dict) else list(direct)
        for index, obj in enumerate(objects):
            rect = self._collision_rect(obj, players)
            if rect is not None:
                x, y, width, height = rect
                if x <= px <= x + width and y <= py <= y + height:
                    return float(index)
        return miss

    @staticmethod
    def _collision_rect(obj, player):
        getter = getattr(obj, "getCollisionBoundingBox", None)
        if getter is None:
            getter = getattr(obj, "get_collision_bounding_box", None)
        if getter is not None:
            rect = getter()
            if isinstance(rect, (tuple, list)) and len(rect) >= 4:
                return tuple(to_num(v) for v in rect[:4])
        x, y = to_num(getattr(obj, "x", 0)) * 16, to_num(getattr(obj, "y", 0)) * 16
        if player:
            return x + 8, y + 16, 32, 32
        shape = getattr(obj, "shape", None)
        if shape and len(shape) >= 2:
            return x, y, to_num(shape[0]), to_num(shape[1])
        # Character NPCs use the same feet-centred 2x2 collision square.
        if getattr(obj, "gani", "") or getattr(obj, "body_image", "") or getattr(obj, "head_image", ""):
            return x + 8, y + 16, 32, 32
        return None

    def _sorted_by_distance(self, args, ctx):
        if len(args) < 2:
            return []
        x, y = to_num(args[0]), to_num(args[1])
        players = self._players_on_level(ctx)
        players.sort(key=lambda p: (to_num(getattr(p, "x", 0)) - x) ** 2
                     + (to_num(getattr(p, "y", 0)) - y) ** 2)
        return players

    def _nearest_player(self, args, ctx, return_id):
        """findnearestplayer -> found flag; getnearestplayer -> player id.

        Both set ctx.player to the nearest player so a subsequent playerx /
        playery / hearts etc. refer to that player (the common follow/guard
        idiom in events that have no triggering player, e.g. timeout).
        """
        ranked = self._sorted_by_distance(args, ctx)
        if not ranked:
            return False
        ctx.player = ranked[0]
        return float(getattr(ranked[0], "id", 0)) if return_id else True

    def _nearest_players(self, args, ctx):
        """getnearestplayers(x,y[,condition]) -> player ids sorted nearest-first.

        Deviations from upstream fn_getnearestplayers (GS1Functions.cpp:597,
        the per-candidate re-evaluation added in 81ec8a13):

        1. The optional 3rd "condition" argument is NOT evaluated per
           candidate. Upstream pushes each candidate player as the current
           script source and re-runs the condition EXPRESSION once per
           player (so e.g. `getnearestplayers(x,y,playerhearts>0)` reads a
           different playerhearts each time), skipping players where it's
           falsy. That requires the interpreter to hand the *unevaluated*
           AST node down to the host so it can be re-run under a different
           ctx.player. reborn_protocol.gs1.interp.Interpreter evaluates all
           call arguments eagerly, exactly once, before call_function() ever
           runs (`[self.eval(a) for a in node.args]`) — there is no hook
           here to re-run args[2] per candidate without changing that
           evaluation strategy, which lives in reborn-protocol (out of scope
           for this host). So the condition argument is silently ignored
           rather than half-applied (a single-evaluation, applied-to-all-or-
           none filter could easily look "correct" for a condition that
           doesn't happen to read per-player state and then quietly do the
           wrong thing for one that does — worse than a documented no-op).
        2. Return semantics: upstream returns INDICES into level->getPlayers()
           (a `players[]`-style array a script would index elsewhere in the
           same script). This host has no players[] array-indexing construct
           (see call_function's getnpc/getplayer note above), so this keeps
           returning player IDs instead, as it already did.
        """
        ranked = self._sorted_by_distance(args, ctx)
        return [float(getattr(p, "id", 0)) for p in ranked]

    def _onmap_pos(self, args, ctx, axis):
        """onmapx(level)/onmapy(level) -> the named level's grid position
        within the CURRENT level's gmap (GS1Functions.cpp fn_onmapx/fn_onmapy,
        upstream 9e759e9d): -1 if the current level has no gmap at all, else
        the target level's (x,y) in that grid, defaulting to (0,0) - not -1 -
        if the named level isn't actually in the grid (matches the C++
        `.value_or(MapPosition{0,0})`)."""
        lvl = self._level_of(ctx)
        if lvl is None or not args:
            return -1.0
        info = self._gmap_info(ctx)
        if info is None:
            return -1.0
        gmap, _, _ = info
        pos = gmap.find_level(to_str(args[0])) if hasattr(gmap, "find_level") else None
        return float((pos or (0, 0))[axis])

    def _gmap_info(self, ctx):
        """(gmap, grid_x, grid_y) for the script's level, or None if it isn't
        on a gmap (backs the `isonmap` flag and onmapx/onmapy)."""
        lvl = self._level_of(ctx)
        world = getattr(self.server, "world", None) if self.server is not None else None
        if lvl is None or world is None or not hasattr(world, "get_gmap_for_level"):
            return None
        return world.get_gmap_for_level(getattr(lvl, "name", ""))

    def _leader_player(self, ctx):
        """First player on the script's level (GS1Flags.cpp isleader /
        Level::isPlayerLeader). Level._players is insertion-ordered, so this
        is genuinely "first to join and still present" (same player PLO_
        ISLEADER is sent to), not just a lowest-id proxy."""
        lvl = self._level_of(ctx)
        return leader_player_for_level(self.server, lvl)

    def _all_baddies_dead(self, ctx):
        """compsdead (GS1Flags.cpp setLevelFlags: !level->hasLivingBaddies()).
        Vacuously true if there's no baddy system to ask, same as "no living
        baddies found" upstream."""
        lvl = self._level_of(ctx)
        bm = getattr(self.server, "baddy_manager", None) if self.server is not None else None
        if lvl is None or bm is None or not hasattr(bm, "get_baddies_on_level"):
            return True
        baddies = bm.get_baddies_on_level(getattr(lvl, "name", ""))
        return all(getattr(b, "dead", False) for b in baddies)


    _FUNCTIONS = {
        "onwall": lambda self, name, args, ctx: self._onwall(args, ctx),
        "onwall2": lambda self, name, args, ctx: self._onwall(args, ctx),
        # known stub: real level water-tile detection isn't wired server-side
        "onwater": lambda self, name, args, ctx: 0.0,
        "onwater2": lambda self, name, args, ctx: 0.0,
        "testnpc": lambda self, name, args, ctx: self._test_at(args, ctx, players=False),
        "testplayer": lambda self, name, args, ctx: self._test_at(args, ctx, players=True),
        "playersays": lambda self, name, args, ctx: self._playersays(args, ctx, contains=False),
        "playersays2": lambda self, name, args, ctx: self._playersays(args, ctx, contains=True),
        "hasweapon": lambda self, name, args, ctx: (
            bool(ctx.player.has_weapon(to_str(args[0])))
            if ctx.player is not None and args and hasattr(ctx.player, "has_weapon")
            else False
        ),
        "getnearestplayer": lambda self, name, args, ctx: self._nearest_player(args, ctx, True),
        "findnearestplayer": lambda self, name, args, ctx: self._nearest_player(args, ctx, False),
        "getnearestplayers": lambda self, name, args, ctx: self._nearest_players(args, ctx),
        "onmapx": lambda self, name, args, ctx: self._onmap_pos(args, ctx, 0),
        "onmapy": lambda self, name, args, ctx: self._onmap_pos(args, ctx, 1),
    }

    _MESSAGE_CODES = {
        "#a": lambda args, ctx: (
            to_str(getattr(ctx.player, "account_name", ""))
            if ctx.player is not None else ""
        ),
        "#n": lambda args, ctx: (
            to_str(getattr(ctx.player, "nickname", ""))
            if ctx.player is not None else ""
        ),
        "#c": lambda args, ctx: (
            to_str(getattr(ctx.player, "chat", ""))
            if ctx.player is not None else ""
        ),
        "#N": lambda args, ctx: (
            to_str(getattr(ctx.this_obj, "name", ""))
            if ctx.this_obj is not None else ""
        ),
        "#f": lambda args, ctx: (
            to_str(getattr(ctx.this_obj, "image", ""))
            if ctx.this_obj is not None else ""
        ),
    }


from .builtins import _BUILTINS
# Classic colour names, index = ClassicColors enum value (GServer-v2
# Character.h / ScriptEngineGS1.h colorNames). The #C0-#C7 message codes read
# and write colour slots by NAME, not raw index.
_CLASSIC_COLORS = (
    "white", "yellow", "orange", "pink", "red",
    "darkred", "lightgreen", "green", "darkgreen", "lightblue",
    "blue", "darkblue", "brown", "cynober", "purple",
    "darkpurple", "lightgray", "gray", "black", "transparent",
)
_CLASSIC_COLOR_INDEX = {name: i for i, name in enumerate(_CLASSIC_COLORS)}


from .commands import _COMMANDS
from .execution import _schedule
from ..audience import GS1_EXPLOSION_PLAYERS, contains  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..combat import CarryObjectSprite  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import asyncio  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.coords import LEVEL_TILE_COUNT, level_index  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.gs1.interp import Interpreter  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.gs1.parser import parse  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.gs1.runtime import Context, VarStore  # noqa: F401  - kept: original import block (star-import consumers rely on it)
