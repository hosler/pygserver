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

from ..attachments import absolute_position
from reborn_protocol.coords import (
    LEVEL_SIZE, segment_index,
)
from reborn_protocol.gs1.runtime import Host, NAMESPACES, UNSET
from reborn_protocol.gs1.values import to_num, to_str
from reborn_protocol.gs1.host_shared import (
    A_CLASS_NPC_ATTR, A_CLASS_PLAYER_ATTR, host_value,
)

from .. import collision, npc_motion, tiletypes
from .player_flags import flag_table
from .players import players_on_level_for, leader_player_for_level

logger = logging.getLogger(__name__)

_DBL_EPSILON = 2.220446049250313e-16  # DoubleIsZero threshold

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


class _GS1ObjectRef:
    """A script object handle (getplayer()/getnpc()/players[i]/bombs[i]/...).

    `kind` is player, npc, baddy, bomb, arrow, item, explosion, horse or sign
    (see objects.py). Used as `with (...)` target and for `array[i].field`
    access; `owner` is the NPC running the script.
    """
    gs1_with_members = True

    def __init__(self, host, kind, target, owner, ctx):
        self.host = host
        self.kind = kind
        self.target = target
        self.owner = owner
        self.ctx = ctx

    def get(self, name, indices=()):
        # The reference property table first (objects.py), then the legacy
        # unprefixed spellings (`playerhearts`, `nick`, `message`, ...).
        value = objects.read_field(self, name, tuple(indices or ()))
        if value is not UNSET:
            return value
        table = {"npc": NPC_ATTR, "player": PLAYER_WITH_ATTR}.get(self.kind)
        attr = table.get(name) if table else None
        if attr is None:
            return UNSET
        if self.kind == "player" and attr in ('x', 'y'):
            return host_value(absolute_position(self.target)[attr == 'y'])
        return host_value(getattr(self.target, attr, 0))

    def set(self, name, value, indices=()):
        if objects.write_field(self, name, value, tuple(indices or ())):
            return True
        table = {"npc": NPC_ATTR, "player": PLAYER_WITH_ATTR}.get(self.kind)
        attr = table.get(name) if table else None
        if attr is None:
            return False
        if self.kind == "player":
            self.host._set_player_attr(self.target, attr, value, self.ctx)
        else:
            clean = objects.sanitize(self.host, self.target, attr,
                                     self.host._num_or_str(value))
            if clean is not objects.DROP:
                setattr(self.target, attr, clean)
                if attr in ("x", "y"):
                    npc_motion.cancel_moves(self.target)  # NPC.cpp:1879,1910
                self.host._dirty(self.target)
        return True


# A player with-block exposes both the normal player-prefixed spellings and
# their explicit unprefixed aliases.  Building this once avoids accidentally
# claiming arbitrary names (which must fall through to flags/builtins).
PLAYER_WITH_ATTR = dict(PLAYER_ATTR)
PLAYER_WITH_ATTR.update({
    name[len("player"):]: attr
    for name, attr in PLAYER_ATTR.items()
    if name.startswith("player") and len(name) > len("player")
})


def _source_player(ctx):
    """The player a bare `#a` refers to: a with-target player handle, else
    the acting player."""
    this = ctx.this_obj
    if isinstance(this, _GS1ObjectRef) and this.kind == "player":
        return this.target
    return ctx.player


def _source_character(ctx):
    """(character, nickname attr, chat attr) for bare `#n`/`#c`."""
    this = ctx.this_obj
    if isinstance(this, _GS1ObjectRef):
        if this.kind == "player":
            return this.target, "nickname", "chat"
        if this.kind == "npc":
            return this.target, "nickname", "message"
    return ctx.player, "nickname", "chat"


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
        if isinstance(ctx.this_obj, _GS1ObjectRef):
            value = ctx.this_obj.get(name, indices)
            if value is not UNSET:
                return value
        player = ctx.player
        npc = (ctx.this_obj.owner
               if isinstance(ctx.this_obj, _GS1ObjectRef) else ctx.this_obj)
        value = objects.get(self, name, indices, ctx)
        if value is not UNSET:
            return value
        if name == "params":
            params = getattr(ctx, "action_params", [])
            if not indices:
                return params
            index = indices[0]
            return params[index] if 0 <= index < len(params) else ""
        reader = _BUILTINS.get(name)
        if reader is not None:
            return reader(self, name, indices, ctx, npc, player)
        if name in PLAYER_ATTR and player is not None:
            attr = PLAYER_ATTR[name]
            value = (absolute_position(player)[attr == 'y'] if attr in ('x', 'y')
                     else getattr(player, attr, 0))
            # The player wire/state scale is 0/2/3; NPC glove power is 0/1/2.
            return self._coerce(value)
        if name in NPC_ATTR and npc is not None:
            return self._coerce(getattr(npc, NPC_ATTR[name], 0))
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        if isinstance(ctx.this_obj, _GS1ObjectRef):
            if ctx.this_obj.set(name, value, indices):
                return True
        player = ctx.player
        npc = (ctx.this_obj.owner
               if isinstance(ctx.this_obj, _GS1ObjectRef) else ctx.this_obj)
        if objects.put(self, name, value, indices, ctx):
            return True
        if name == "save" and indices and npc is not None:
            slots = ctx.vars.scopes["this"].setdefault("save", [0.0] * 10)
            index = int(to_num(indices[0]))
            if 0 <= index < len(slots):
                slots[index] = float(min(220, max(0, int(to_num(value)))))
            return True
        if name == "tiles":
            resolved = self._resolve_tile(indices, ctx)
            if resolved is not None:
                level, x, y = resolved
                self._write_tile(level, x, y, int(to_num(value)))
            return True
        if name == "board":
            # 1641f88b (GS1Variables.cpp:41-59): board[i] = n writes the NPC's
            # own level board. Ignored without an index, outside 0..4095,
            # without a level, or for a non-number. The reference writes the
            # tile array itself, records no board change and sends no
            # board-modify packet, so neither do we: a player who already has
            # the board keeps their copy, a first-time entrant gets the tile.
            level = getattr(npc, "level", None) if npc is not None else None
            if (indices and level is not None and hasattr(level, "set_tile")
                    and not isinstance(value, str)):
                slot, number = to_num(indices[0]), to_num(value)
                if (math.isfinite(slot) and math.isfinite(number)
                        and 0 <= int(slot) < LEVEL_SIZE * LEVEL_SIZE):
                    level.set_tile(int(slot) % LEVEL_SIZE, int(slot) // LEVEL_SIZE,
                                   int(number) & 0xFFFF)
            return True
        if name in PLAYER_ATTR and player is not None:
            self._set_player_attr(player, PLAYER_ATTR[name], value, ctx)
            return True
        if name in NPC_ATTR and npc is not None:
            clean = objects.sanitize(self, npc, NPC_ATTR[name],
                                     self._num_or_str(value))
            if clean is not objects.DROP:
                setattr(npc, NPC_ATTR[name], clean)
                if NPC_ATTR[name] in ("x", "y"):
                    npc_motion.cancel_moves(npc)  # NPC.cpp:1879,1910
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

    def _write_tile(self, level, x, y, tile):
        """GS1 tiles[x,y] = n: only the level's script tile layer changes.
        Nothing is sent and nothing is recorded until `updateboard`
        (Level::getMapTileForEditing, Level.cpp:3135-3153; GS1Variables.cpp:
        416-442)."""
        if hasattr(level, "write_script_tile"):
            level.write_script_tile(x, y, tile)

    def _publish_board_area(self, level, x, y, width, height, tiles,
                            allow_respawn=True):
        """`updateboard x,y,w,h`: publish `tiles` (row-major, from the script
        layer) as a board change that reverts after `respawntime` seconds
        and send it to the level, on a grid map to the 3x3 around it
        (Level::applyBoardChangeFromScriptTiles -> alterBoard with
        forceRespawn, Level.cpp:1751-1799, :1668-1749; LevelBoardChange.cpp:
        51-62). `allow_respawn` False is the permanent `updateboard2`."""
        if self.server is None or not hasattr(level, "alter_board"):
            return
        from ..board_changes import respawn_context
        now, respawn_seconds = respawn_context(self.server)
        corrections = []
        change = level.alter_board(
            x, y, width, height, tiles, now, respawn_seconds,
            allow_respawn=allow_respawn, force_respawn=allow_respawn,
            corrections=corrections)
        if change is None:
            return
        manager = getattr(self.server, "board_changes", None)
        if manager is not None:
            for sent in [change] + corrections:
                _schedule(manager.send_to_level(level, sent))
            return
        from ..protocol.packets import (
            PacketBuilder, build_board_modify, build_board_modify2)
        payload = PacketBuilder()
        for tile in tiles:
            payload.write_gshort(tile)
        world = getattr(self.server, "world", None)
        gmap_info = (world.get_gmap_for_level(level.name)
                     if world and hasattr(world, "get_gmap_for_level") else None)
        if gmap_info:
            _, map_x, map_y = gmap_info
            packet = build_board_modify2(map_x, map_y, x, y, width, height,
                                         payload.build())
        else:
            packet = build_board_modify(x, y, width, height, payload.build())
        _schedule(self.server.broadcast_to_level(level.name, packet))

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
        lowered = name.lower()
        if lowered == "getplayer":
            return self._get_player(args, ctx)
        if lowered == "getnpc":
            return self._get_npc(args, ctx)
        # findplayer/findnpc are not in the GServer-v2 function table
        # (GS1Functions.cpp); kept as the older level-scoped lookup.
        if lowered == "findplayer":
            return self._object_ref(args, ctx, "player")
        if lowered == "findnpc":
            return self._object_ref(args, ctx, "npc")
        if name == "makevar":
            dynamic = to_str(args[0]).strip('"') if args else ""
            namespace, dot, key = dynamic.partition(".")
            scope = NAMESPACES.get(namespace) if dot else None
            if scope in ("server", "client", None):
                key = dynamic
            value = ctx.vars.get(scope, key)
            return 0.0 if value is UNSET else value
        if name == "degtorad":
            return (to_num(args[0]) if args else 0.0) * math.pi / 180.0
        return UNSET

    def _get_player(self, args, ctx):
        """getplayer(account): any online client on the server, matched on
        the account name case-insensitively (GS1Functions.cpp fn_getplayer ->
        NPCServer::getPlayer(account, PLTYPE_ANYCLIENT), NPCServer.h:185);
        0 when nobody matches."""
        wanted = to_str(args[0]).lower() if args else ""
        if not wanted:
            return 0.0
        players = getattr(self.server, "players", None)
        if isinstance(players, dict):
            candidates = [players[pid] for pid in sorted(players)]
            from ..protocol.constants import PLTYPE
            candidates = [
                p for p in candidates
                if getattr(p, "connection_type", PLTYPE.CLIENT) & PLTYPE.ANYCLIENT
                and getattr(p, "logged_in", True)]
        else:
            candidates = self._players_on_level(ctx)
            if ctx.player is not None and ctx.player not in candidates:
                candidates.insert(0, ctx.player)
            candidates = [p for p in candidates if getattr(p, "logged_in", True)]
        for player in candidates:
            if to_str(getattr(player, "account_name", "")).lower() == wanted:
                return _GS1ObjectRef(self, "player", player,
                                     self._object_owner(ctx), ctx)
        return 0.0

    def _get_npc(self, args, ctx):
        """getnpc(name): any NPC on the server whose name equals `name`
        exactly (fn_getnpc compares `npc->name == npcName`), first by id."""
        wanted = to_str(args[0]) if args else ""
        if not wanted:
            return 0.0
        manager = getattr(self.server, "npc_manager", None)
        pool = getattr(manager, "_npcs", None)
        if isinstance(pool, dict):
            candidates = [pool[nid] for nid in sorted(pool)]
        else:
            candidates = objects._level_npcs(self._level_of(ctx))
        for npc in candidates:
            if getattr(npc, "name", None) == wanted:
                return _GS1ObjectRef(self, "npc", npc,
                                     self._object_owner(ctx), ctx)
        return 0.0

    @staticmethod
    def _object_owner(ctx):
        this = ctx.this_obj
        return this.owner if isinstance(this, _GS1ObjectRef) else this

    def _object_ref(self, args, ctx, kind):
        wanted = to_str(args[0]).lower() if args else ""
        if not wanted:
            return 0.0
        if kind == "player":
            objects = self._players_on_level(ctx)
            if ctx.player is not None and ctx.player not in objects:
                objects.insert(0, ctx.player)
            objects = [p for p in objects if getattr(p, "logged_in", True)]
            fields = ("account_name", "nickname", "id")
        else:
            level = self._level_of(ctx)
            if level is None:
                return 0.0
            direct = (level.get_npcs() if hasattr(level, "get_npcs")
                      else getattr(level, "npcs", getattr(level, "_npcs", [])))
            objects = list(direct.values()) if isinstance(direct, dict) else list(direct)
            fields = ("name", "nickname", "id")
        for obj in objects:
            if any(to_str(getattr(obj, field, "")).lower() == wanted
                   for field in fields):
                return _GS1ObjectRef(self, kind, obj, ctx.this_obj, ctx)
        return 0.0

    def _trigger_client(self, args, ctx):
        player = ctx.player
        if player is None or not args or not hasattr(player, "send_raw"):
            return 0.0
        from ..protocol.packets import build_trigger_action
        from ..gs2 import to_csv
        action = "clientside," + to_csv([to_str(value) for value in args])
        _schedule(player.send_raw(build_trigger_action(0, 0, 0, 0, action)))
        return 1.0

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
        mc_CharacterProperty (GS1MessageCodes.cpp:259):
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
        slot (mc_C -> getClassicColorName, Character.h:119), NOT the raw
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

    def _set_player_attr(self, player, attr, value, ctx=None):
        value = objects.sanitize(self, player, attr, value)
        if value is objects.DROP:
            return  # non-finite: never let it reach state or a packet
        if attr in ('x', 'y') and getattr(player, 'attach_npc', (0, 0))[1]:
            # Script coordinates are absolute; retain offsets in wire state.
            base = absolute_position(player)[attr == 'y'] - getattr(player, attr)
            value = to_num(value) - base
        cur = getattr(player, attr, None)
        if isinstance(cur, str) or attr in ("chat", "nickname", "account_name",
                                            "head_image", "body_image",
                                            "sword_image", "shield_image", "gani"):
            setattr(player, attr, to_str(value))
        else:
            setattr(player, attr, to_num(value))
        # queue the change for propagation (flushed after the event in
        # run_npc_event to the player and to the other clients); chat/account
        # are not client props
        props = objects.player_wire_props(player, attr, PLAYER_PROP_WIRE)
        if props:
            dirty = getattr(player, "_gs1_dirty_props", None)
            if dirty is None:
                dirty = {}
                player._gs1_dirty_props = dirty
            dirty.update(props)
            if ctx is not None:
                written = getattr(ctx, "written_players", None)
                if written is None:
                    written = {}
                    ctx.written_players = written
                written[id(player)] = player

    def _set_timer(self, npc, seconds):
        if not math.isfinite(seconds):
            return
        seconds = min(seconds, 86400.0)
        if hasattr(npc, "set_timer"):
            npc.set_timer(seconds)
        else:
            npc._timer_remaining = seconds

    # -- tiles and walls (collision.py holds the geometry) ------------------

    def _probe(self, args, ctx, rect_form):
        """Parse onwall/onwater style arguments into (level, point|rect).

        x,y are tiles (float32, truncated to pixels with the epsilon nudge,
        GS1Functions.cpp:1116-1188); width/height are floor(v*16) px, uint16.
        None when the call is malformed or there is no level."""
        level = self._level_of(ctx)
        if level is None or len(args) < (4 if rect_form else 2):
            return None
        px = collision.tiles_to_pixels(to_num(args[0]))
        py = collision.tiles_to_pixels(to_num(args[1]))
        if not rect_form:
            return level, (px, py), None
        size = []
        for value in args[2:4]:
            number = to_num(value) * 16
            size.append(max(0, min(0xFFFF, math.floor(number)))
                        if math.isfinite(number) else 0)
        return level, None, (px, py, size[0], size[1])

    def _onwall(self, args, ctx, rect_form=False):
        """onwall(x,y) / onwall2(x,y,w,h): a wall tile, then (only for a
        script owned by an NPC) a player or ANOTHER NPC at the spot. A hit on
        the probing NPC itself never counts (86df2119); see
        collision.find_npc_at for the divergence from the reference's
        first-hit-only scan."""
        probe = self._probe(args, ctx, rect_form)
        if probe is None:
            return 0.0
        level, point, rect = probe
        if point is not None:
            if collision.is_on_wall_pixel(self.server, level, *point):
                return 1.0
        elif collision.is_on_wall_rect(self.server, level, *rect):
            return 1.0
        owner = self._object_owner(ctx)
        if owner is None or not hasattr(owner, "gs1_scopes"):
            return 0.0
        blocked = collision.blocked_by_objects(
            level, owner, self._players_on_level(ctx), point=point, rect=rect)
        return 1.0 if blocked else 0.0

    def _onwater(self, args, ctx, rect_form=False):
        """onwater(x,y) / onwater2(x,y,w,h): water tiles only (not lava)."""
        probe = self._probe(args, ctx, rect_form)
        if probe is None:
            return 0.0
        level, point, rect = probe
        if point is not None:
            wet = collision.is_on_water_pixel(self.server, level, *point)
        else:
            wet = collision.is_on_water_rect(self.server, level, *rect)
        return 1.0 if wet else 0.0

    def _area_npcs(self, args, ctx):
        """getareanpcs(x,y,w,h) (GS1Functions.cpp:489-506): the NPCs of the
        script's level, visible or not, whose bounding box meets the area
        (inclusive edges); width and height are at least 16 px.

        Returns indices into the level NPC list, the values `npcs[]` takes
        (the documented contract). The reference code returns NPC ids, which
        `npcs[]` does not accept, so the doc'd idiom only works with indices."""
        level = self._level_of(ctx)
        if level is None or len(args) < 4:
            return []
        try:
            px = math.floor(to_num(args[0]) * 16)
            py = math.floor(to_num(args[1]) * 16)
            size = [max(16, max(0, min(0xFFFF, math.floor(to_num(v) * 16))))
                    for v in args[2:4]]
        except (OverflowError, ValueError):
            return []
        area = (px, py, size[0], size[1])
        return [float(index)
                for index, npc in enumerate(collision._level_npcs(level))
                if collision.rects_intersect(area, collision.npc_bounding_rect(npc))]

    def _tiletype(self, args, ctx):
        """tiletype(x,y) (351966c1, GS1Functions.cpp:1700-1716): the type of
        the tile at level tile (x,y); 22 off the level, past a gmap, for an id
        >= 4096, or with no level."""
        level = self._level_of(ctx)
        if level is None or len(args) < 2:
            return float(tiletypes.BLOCKING)
        return float(collision.tile_type_at_script_tile(
            self.server, level, to_num(args[0]), to_num(args[1])))

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
        # Inside `with (players[i])` this_obj is the object handle; the script
        # still belongs to the NPC that owns it.
        this = ctx.this_obj
        if isinstance(this, _GS1ObjectRef):
            this = this.owner
        return getattr(this, "level", None) or getattr(ctx.player, "level", None)

    def _object_ref_for(self, kind, target, ctx):
        this = ctx.this_obj
        owner = this.owner if isinstance(this, _GS1ObjectRef) else this
        return _GS1ObjectRef(self, kind, target, owner, ctx)

    def _players_on_level(self, ctx):
        """All logged-in Player objects on the script's level (nearest-* helpers)."""
        return players_on_level_for(self.server, self._level_of(ctx))

    def _player_is_swimming(self, player):
        level = getattr(player, "level", None)
        if level is None:
            return False
        px, py = absolute_position(player)
        x = math.floor(to_num(px) + 1.5)
        y = math.floor(to_num(py) + 2.0)
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
                objects = [p for p in objects if getattr(p, "logged_in", True)]
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
        """Pixel box testnpc/testplayer test a point against, or None.
        NPC rules (invisible, NOBLOCK, character) live in collision.py."""
        rect = (collision.player_collision_rect(obj) if player
                else collision.npc_collision_rect(obj))
        return None if rect is None else tuple(float(v) for v in rect)

    def _sorted_by_distance(self, args, ctx):
        if len(args) < 2:
            return []
        x, y = to_num(args[0]), to_num(args[1])
        players = self._players_on_level(ctx)
        players.sort(key=lambda p: (to_num(absolute_position(p)[0]) - x) ** 2
                     + (to_num(absolute_position(p)[1]) - y) ** 2)
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

    def _nearest_players(self, args, ctx, condition=None):
        """getnearestplayers(x,y[,condition]) -> indices into the level player
        list (the `players[]` order), nearest first (ca160f9e,
        GS1Functions.cpp:597-660).

        `condition` is a zero-argument callable that evaluates the third
        argument's expression (see ServerInterpreter._ex_Call): it is re-run
        once per level player with that player as the current source, so
        `playerhearts>0` reads each candidate's hearts, and a player whose
        result is ~0 (|v| < DBL_EPSILON) is dropped. Without a condition every
        player qualifies (the reference's 2-argument form reads past the
        argument list; ours is the documented optional-condition behaviour).

        Deviation: the reference keys candidates by distance in a std::map, so
        two equidistant players collapse to the first; ours keeps both, in
        level order (stable sort).
        """
        if len(args) < 2:
            return []
        x, y = to_num(args[0]), to_num(args[1])
        players = self._players_on_level(ctx)
        ranked = []
        for index, player in enumerate(players):
            if condition is not None and not self._condition_for(
                    player, condition, ctx):
                continue
            px, py = absolute_position(player)
            ranked.append((math.hypot(to_num(px) - x, to_num(py) - y), index))
        ranked.sort(key=lambda pair: pair[0])
        return [float(index) for _, index in ranked]

    def _condition_for(self, player, condition, ctx):
        """Evaluate `condition()` with `player` as the current source."""
        # pushSource(FromPlayer) per candidate (GS1Functions.cpp:597): the
        # acting player's flags, `client.` scope and vars.player move with it.
        store = ctx.vars
        saved = (ctx.player, ctx.this_obj, getattr(store, "player", None),
                 store.player_flags, store.scopes.get("client"))
        ctx.player = player
        if isinstance(ctx.this_obj, _GS1ObjectRef) and ctx.this_obj.kind == "player":
            ctx.this_obj = self._object_ref_for("player", player, ctx)
        if hasattr(store, "player"):
            store.player = player
        table = flag_table(player)
        store.player_flags = table
        store.scopes["client"] = table
        try:
            value = to_num(condition())
        finally:
            ctx.player, ctx.this_obj = saved[0], saved[1]
            if hasattr(store, "player"):
                store.player = saved[2]
            store.player_flags = saved[3]
            if saved[4] is None:
                store.scopes.pop("client", None)
            else:
                store.scopes["client"] = saved[4]
        return abs(value) >= _DBL_EPSILON

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
        "triggerclient": lambda self, name, args, ctx: self._trigger_client(
            args, ctx),
        "onwall": lambda self, name, args, ctx: self._onwall(args, ctx),
        "onwall2": lambda self, name, args, ctx: self._onwall(args, ctx, True),
        "onwater": lambda self, name, args, ctx: self._onwater(args, ctx),
        "onwater2": lambda self, name, args, ctx: self._onwater(args, ctx, True),
        "tiletype": lambda self, name, args, ctx: self._tiletype(args, ctx),
        "getareanpcs": lambda self, name, args, ctx: self._area_npcs(args, ctx),
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
        "#p": lambda args, ctx: (
            to_str(ctx.action_params[int(to_num(args[0]))])
            if args and 0 <= int(to_num(args[0])) < len(
                getattr(ctx, "action_params", ()))
            else ""
        ),
        # Inside `with (players[i])` / `with (npcs[i])` the message codes read
        # the with-target (GS1MessageCodes.cpp mc_a: nearest PLAYER source;
        # mc_CharacterProperty: current source); elsewhere the acting player.
        "#a": lambda args, ctx: (
            to_str(getattr(_source_player(ctx), "account_name", ""))
            if _source_player(ctx) is not None else ""
        ),
        "#n": lambda args, ctx: (
            to_str(getattr(_source_character(ctx)[0], _source_character(ctx)[1], ""))
            if _source_character(ctx)[0] is not None else ""
        ),
        "#c": lambda args, ctx: (
            to_str(getattr(_source_character(ctx)[0], _source_character(ctx)[2], ""))
            if _source_character(ctx)[0] is not None else ""
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


from . import objects
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
