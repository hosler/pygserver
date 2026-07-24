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

import asyncio
import logging
import math
import re
import time

from reborn_protocol.gs1.runtime import Host, UNSET, VarStore, Context
from reborn_protocol.gs1.interp import Interpreter
from reborn_protocol.gs1.parser import parse
from reborn_protocol.gs1.values import to_num, to_str

from . import tiletypes
from .combat import CarryObjectSprite

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
    from .protocol.constants import PLPROP, NPCPROP

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
    "playerx": "x", "playery": "y", "playerdir": "direction",
    "playersprite": "sprite", "playerrupees": "rupees", "playergralats": "rupees",
    "playerhearts": "hearts", "playerfullhearts": "max_hearts",
    "playerarrows": "arrows", "playerbombs": "bombs",
    "playerswordpower": "sword_power", "playershieldpower": "shield_power",
    "playerglovepower": "glove_power", "playerkills": "kills",
    "playerdeaths": "deaths", "playerchat": "chat", "playernick": "nickname",
    "playeraccount": "account_name", "playerhead": "head_image",
    "playerbody": "body_image", "playersword": "sword_image",
    "playershield": "shield_image", "playerap": "ap", "playergani": "gani",
}
# unprefixed attribute name -> Python attribute on the NPC ("this")
NPC_ATTR = {
    "x": "x", "y": "y", "dir": "direction", "nick": "nickname",
    "hearts": "hearts", "rupees": "rupees", "arrows": "arrows",
    "bombs": "bombs", "glovepower": "glove_power",
    "image": "image", "ani": "gani",
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
        player = ctx.player
        npc = ctx.this_obj
        if name == "tiles":
            resolved = self._resolve_tile(indices, ctx)
            if resolved is None:
                return 0.0
            level, x, y = resolved
            return float(level.get_tile(x, y))
        if name == "board":
            level = getattr(npc, "level", None) if npc is not None else None
            if level is None or not hasattr(level, "get_tile"):
                return 0.0
            if not indices:
                return [float(level.get_tile(i % 64, i // 64)) for i in range(4096)]
            index = int(to_num(indices[0]))
            if index < 0 or index >= 4096:
                return 0.0
            return float(level.get_tile(index % 64, index // 64))
        if name == "tokenscount":   # number of tokens from the last `tokenize`
            # (GS1Commands.cpp:3138 sets this on tokenize; mirrors the
            # client host's implementation in pyreborn.gs1_client)
            return float(len(getattr(ctx, "tokenize_tokens", []) or []))
        if name == "timevar2":
            # Serverside timevar2 is the Unix timestamp (seconds).
            return float(int(time.time()))
        if name == "playerfreezetime":
            if player is None or not getattr(player, "is_frozen", False):
                return -1.0
            deadline = getattr(player, "_gs1_freeze_until", None)
            if deadline is None:  # freezeplayer2 has no timed expiry.
                return 0.0
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                player.is_frozen = False
                player._gs1_freeze_until = None
                return -1.0
            return remaining
        if name in PLAYER_ATTR and player is not None:
            value = getattr(player, PLAYER_ATTR[name], 0)
            # The player wire/state scale is 0/2/3; NPC glove power is 0/1/2.
            return self._coerce(value)
        if name == "playerlevel" and player is not None:
            lvl = getattr(player, "level", None)
            return getattr(lvl, "name", "") if lvl else ""
        if name == "playeronline":
            return 1.0 if player is not None else 0.0
        if name == "isweapon":
            return 0.0
        if name == "playerswimming":
            return 1.0 if player is not None and self._player_is_swimming(player) else 0.0
        if name == "carrying":
            return 1.0 if player is not None and int(getattr(player, "carrysprite", 0) or 0) != 0 else 0.0
        carry_flags = {
            "carriesbush": CarryObjectSprite.BUSH,
            "carriesstone": CarryObjectSprite.STONE,
            "carriesvase": CarryObjectSprite.VASE,
            "carriessign": CarryObjectSprite.SIGN,
            "carriesblackstone": CarryObjectSprite.BLACKSTONE,
        }
        if name in carry_flags:
            sprite = int(getattr(player, "carrysprite", 0) or 0) if player is not None else 0
            return 1.0 if sprite == int(carry_flags[name]) else 0.0
        if name == "carriesnpc":
            carried_npc = (getattr(player, "carryNPC", 0) or
                           getattr(player, "carry_npc", 0) or
                           getattr(player, "npc_id", 0)) if player is not None else 0
            return 1.0 if carried_npc else 0.0
        if name in NPC_ATTR and npc is not None:
            return self._coerce(getattr(npc, NPC_ATTR[name], 0))
        if name == "sprite" and npc is not None:
            return self._coerce(npc.flags.get("sprite", 0)) if hasattr(npc, "flags") else 0.0
        if name == "timeout" and npc is not None:
            end = getattr(npc, "_timer_end", 0.0)
            return max(0.0, end - time.time()) if end else 0.0
        # -- nw* clock variables (Server.cpp:178-185, upstream ac3adf01) --
        if name in _NW_CLOCK_FIELDS:
            return _nw_clock_value(name)
        # -- hit-source flags: WASSHOT only (GS1Flags.cpp:136-138); washit
        # has no equivalent source flags upstream.
        if name in _SHOTBY_SOURCE:
            if ctx.active_event != "wasshot":
                return 0.0
            return 1.0 if getattr(ctx, "hit_source", None) == _SHOTBY_SOURCE[name] else 0.0
        if name in _PELTWITH_TYPE:
            if ctx.active_event != "waspelt":
                return 0.0
            return 1.0 if getattr(ctx, "carryobject_type", None) == _PELTWITH_TYPE[name] else 0.0
        # -- player flags with real pygserver-side backing state
        if name == "weaponsenabled" and player is not None:
            return 0.0 if getattr(player, "weapons_disabled", False) else 1.0
        if name == "playeronhorse" and player is not None:
            hm = getattr(self.server, "horse_manager", None) if self.server is not None else None
            pid = getattr(player, "id", None)
            return 1.0 if hm is not None and pid is not None and hm.is_mounted(pid) else 0.0
        if name in ("playerismale", "playerisfemale") and player is not None:
            # player.gender only ever exists if a GS1 script set it
            # (_c_setgender/_c_setchargender) - pygserver has no other
            # gender source. 0 = male by the same raw-int convention those
            # commands already use (classic GraalScript "sex" 0/1); unset
            # defaults to male, matching upstream's PLSTATUS_MALE-set default.
            is_male = int(to_num(getattr(player, "gender", 0))) == 0
            return 1.0 if is_male == (name == "playerismale") else 0.0
        if name == "isleader" and player is not None:
            leader = self._leader_player(ctx)
            return 1.0 if leader is not None and leader is player else 0.0
        # -- NPC/level flags (GS1Flags.cpp setNPCFlags/setLevelFlags) --
        if name == "visible" and npc is not None:
            return 1.0 if getattr(npc, "visible", True) else 0.0
        if name == "isonmap":
            return 1.0 if self._gmap_info(ctx) is not None else 0.0
        if name == "compsdead":
            return 1.0 if self._all_baddies_dead(ctx) else 0.0
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
        # to the selected level's dimensions.
        world = getattr(self.server, "world", None) if self.server is not None else None
        if world is not None and (x > 64 or y > 64):
            info = world.get_gmap_for_level(getattr(level, "name", ""))
            if info is not None:
                gmap, gx, gy = info
                target_name = gmap.get_level_at(gx + x // 64, gy + y // 64)
                target = world.get_level(target_name) if target_name else None
                if target is not None:
                    level = target

        width = int(getattr(level, "WIDTH", 64))
        height = int(getattr(level, "HEIGHT", 64))
        if width <= 0 or height <= 0:
            return None
        x = max(0, min(width - 1, x % width))
        y = max(0, min(height - 1, y % height))
        return level, x, y

    def _broadcast_tiles(self, level, x, y, width, height):
        if self.server is None or not hasattr(level, "_tiles"):
            return
        tiles = bytearray()
        level_width = int(getattr(level, "WIDTH", 64))
        for row in range(y, y + height):
            start = (row * level_width + x) * 2
            tiles += bytes(level._tiles[start:start + width * 2])
        try:
            from .protocol.packets import build_board_modify, build_board_modify2
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
        if name in ("onwall", "onwall2"):
            return self._onwall(args, ctx)
        if name in ("onwater", "onwater2"):
            return 0.0  # known stub: real level water-tile detection isn't wired server-side
        if name == "testnpc":
            return self._test_at(args, ctx, players=False)
        if name == "testplayer":
            return self._test_at(args, ctx, players=True)
        if name == "playersays":
            return self._playersays(args, ctx, contains=False)
        if name == "playersays2":
            return self._playersays(args, ctx, contains=True)
        if name == "hasweapon":
            player = ctx.player
            if player is not None and args and hasattr(player, "has_weapon"):
                return bool(player.has_weapon(to_str(args[0])))
            return False
        if name in ("getnearestplayer", "findnearestplayer"):
            return self._nearest_player(args, ctx, name == "getnearestplayer")
        if name == "getnearestplayers":
            return self._nearest_players(args, ctx)
        if name in ("onmapx", "onmapy"):
            return self._onmap_pos(args, ctx, 0 if name == "onmapx" else 1)
        # getnpc/getplayer return ScriptObject references that require a
        # script-object member-access model (obj.x / obj.hearts). Deliberately
        # unimplemented: zero usage across the 5732-file GS1 corpus, so it isn't
        # worth the interp rewrite; the nearest-player helpers above cover the
        # real follow/guard idiom by setting ctx.player. -> 0 (falsey).
        return UNSET

    # -- message codes -----------------------------------------------------
    def message_code(self, code, args, ctx) -> str:
        player = ctx.player
        npc = ctx.this_obj
        m = _COLOR_CODE_RE.match(code)
        if m:
            return self._read_color_code(int(m.group(1)), args, ctx)
        if player is not None:
            if code == "#a":
                return to_str(getattr(player, "account_name", ""))
            if code == "#n":
                return to_str(getattr(player, "nickname", ""))
            if code == "#c":
                return to_str(getattr(player, "chat", ""))
        if code == "#N" and npc is not None:
            return to_str(getattr(npc, "name", ""))
        if code == "#f" and npc is not None:
            return to_str(getattr(npc, "image", ""))
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
        if isinstance(v, str):
            return v
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

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
        lvl = self._level_of(ctx)
        if lvl is None or self.server is None or not hasattr(lvl, "get_player_ids"):
            return []
        out = []
        for pid in lvl.get_player_ids():
            p = self.server.get_player(pid)
            if p is not None:
                out.append(p)
        return out

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


# -- command handlers -------------------------------------------------------
def _c_setimg(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.image = to_str(a[0])
        self._dirty(npc)


def _c_setimgpart(self, a, npc, player, ctx):
    # setimgpart filename,x,y,width,height — show only a sub-rect of the
    # sheet (GS1Commands.cpp:2228 fn_setimgpart sets NPCProp::IMAGE +
    # NPCProp::IMAGEPART). The rect flows to clients via
    # NPC.build_props_packet() -> build_npc_props() NPCPROP.IMAGEPART.
    if npc is None or len(a) < 5:
        return
    npc.image = to_str(a[0])
    npc.imagepart = (int(to_num(a[1])), int(to_num(a[2])),
                      int(to_num(a[3])), int(to_num(a[4])))
    self._dirty(npc)


def _c_setani(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.gani = to_str(a[0])
        self._dirty(npc)


def _c_message(self, a, npc, player, ctx):
    if npc is not None:
        npc.message = to_str(a[0]) if a else ""
        self._dirty(npc)


def _c_hide(self, a, npc, player, ctx):
    if npc is not None:
        npc.visible = False
        self._dirty(npc)


def _c_show(self, a, npc, player, ctx):
    if npc is not None:
        npc.visible = True
        self._dirty(npc)


def _c_move(self, a, npc, player, ctx):
    if npc is not None and len(a) >= 2:
        npc.x = to_num(getattr(npc, "x", 0)) + to_num(a[0])
        npc.y = to_num(getattr(npc, "y", 0)) + to_num(a[1])
        self._dirty(npc)


def _c_setnick(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.nickname = to_str(a[0])
        self._dirty(npc)


def _c_setshape(self, a, npc, player, ctx):
    # setshape type,width,height — type 1 is a fully-solid box; other type
    # values are unimplemented in the GServer-v2 C++ oracle too
    # (GS1Commands.cpp:2384 fn_setshape returns early unless type == 1).
    # width/height are stored in tiles, matching the client host's
    # setshape/setshape2 (gs1_client.py). Not a wire prop (no NPCPROP for a
    # shape rect) — this is server-side collision geometry; note for the
    # touch-handling owner: nothing in gs1_host.py currently *reads*
    # npc.shape for collision, so a touch-handler change elsewhere would be
    # needed to make setshape blocking actually take effect.
    if npc is None or len(a) < 3:
        return
    if int(to_num(a[0])) != 1:
        return
    npc.shape = (int(to_num(a[1])), int(to_num(a[2])))


def _gattribs_of(obj):
    ga = getattr(obj, "gattribs", None)
    if ga is None:
        ga = {}
        obj.gattribs = ga
    return ga


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


def _resolve_color(val):
    """Colour value for a #C0-#C7 slot, matching the C++ engine
    (GS1MessageCodes.cpp COLORS write + GS1Visitor::getColorValueFromString):
    a STRING is looked up as a classic-colour name (unknown name -> 0, white),
    a genuine NUMBER is used as a raw palette index. GS1 passes bare tokens as
    strings, so `setplayerprop #C0,red` -> 4 but `setplayerprop #C0,9` -> 0
    ("9" is not a colour name), where treating it as a raw index would give 9."""
    if isinstance(val, str):
        return _CLASSIC_COLOR_INDEX.get(val, 0)
    return int(to_num(val)) & 0xFF


def _apply_charprop(obj, code, val, table):
    """Set the attr / color / gani-attribute an NPC setcharprop code maps to.
    Returns True if the code was recognized and applied."""
    target = _charprop_target(code, table)
    if target is None:
        return False
    if isinstance(target, tuple):
        kind, n = target
        if kind == "color":
            colors = getattr(obj, "colors", None)
            if isinstance(colors, list) and 0 <= n < len(colors):
                colors[n] = _resolve_color(val)
        elif kind == "gattrib" and 1 <= n <= len(_NPC_GATTRIB_PROPS):
            _gattribs_of(obj)[_NPC_GATTRIB_PROPS[n - 1]] = to_str(val)
        return True
    setattr(obj, target, to_str(val))
    return True


def _c_setcharprop(self, a, npc, player, ctx):
    # setcharprop <messagecode>, <value> — set the NPC's appearance/identity
    if npc is None or len(a) < 2:
        return
    if _apply_charprop(npc, to_str(a[0]), a[1], NPC_CHARPROP):
        self._dirty(npc)


def _c_setplayerprop(self, a, npc, player, ctx):
    if player is None or len(a) < 2:
        return
    target = _charprop_target(to_str(a[0]), PLAYER_CHARPROP)
    if target is None:
        return
    if isinstance(target, tuple):
        kind, n = target
        if kind == "color":  # set slot + queue the full COLORS prop
            colors = getattr(player, "colors", None)
            if isinstance(colors, list) and 0 <= n < len(colors):
                colors[n] = _resolve_color(a[1])
                if PLPROP is not None:
                    _queue_player_prop(player, PLPROP.COLORS, list(colors))
        elif kind == "gattrib" and 1 <= n <= len(_PLAYER_GATTRIB_PROPS):
            prop_id = _PLAYER_GATTRIB_PROPS[n - 1]
            _gattribs_of(player)[prop_id] = to_str(a[1])
            _queue_player_prop(player, prop_id, to_str(a[1]))
    elif target in PLAYER_PROP_WIRE:
        self._set_player_attr(player, target, a[1])  # sets + queues wire prop
    else:
        setattr(player, target, to_str(a[1]))  # e.g. sword/shield/horse image
    if hasattr(player, "mark_dirty"):
        player.mark_dirty()


def _c_addweapon(self, a, npc, player, ctx):
    # addweapon <name> — give the acting player a weapon and push it to client
    if player is None or not a:
        return
    name = to_str(a[0])
    if hasattr(player, "add_weapon"):
        player.add_weapon(name)
    wm = getattr(self.server, "weapon_manager", None)
    weapon = wm.get_weapon(name) if wm is not None and hasattr(wm, "get_weapon") else None
    if weapon is None or not hasattr(player, "send_raw"):
        return
    try:
        from .protocol.packets import build_npc_weapon_add
        pkt = build_npc_weapon_add(name, getattr(weapon, "image", ""),
                                   getattr(weapon, "client_script", ""))
        _schedule(player.send_raw(pkt))
    except Exception:
        logger.debug("addweapon send failed for %s", name, exc_info=True)


def _c_triggeraction(self, a, npc, player, ctx):
    # triggeraction x,y,action,params... — dispatch a serverside trigger.
    # handle_trigger_action reads token[1] as the action, so prefix with "gs1".
    if player is None or self.server is None or len(a) < 3:
        return
    if not hasattr(self.server, "handle_trigger_action"):
        return
    x, y = to_num(a[0]), to_num(a[1])
    parts = ["gs1"] + [to_str(v) for v in a[2:]]
    _schedule(self.server.handle_trigger_action(player, x, y, ",".join(parts)))


def _spawn_npc(self, image, script, x, y, ctx):
    lvl = self._level_of(ctx)
    nm = getattr(self.server, "npc_manager", None) if self.server is not None else None
    if lvl is None or nm is None or not hasattr(nm, "create_npc"):
        return None
    npc = nm.create_npc(level=lvl, x=to_num(x), y=to_num(y))
    if image:
        npc.image = to_str(image)
    if script:
        nm.attach_gs1(npc, to_str(script))
    self._dirty(npc)
    if hasattr(self.server, "broadcast_to_level"):
        _schedule(self.server.broadcast_to_level(lvl.name, npc.build_props_packet()))
    return npc


def _c_putnpc(self, a, npc, player, ctx):
    # putnpc image,script,x,y — create a level NPC
    if len(a) < 4:
        return
    _spawn_npc(self, a[0], a[1], a[2], a[3], ctx)


def _c_putnpc2(self, a, npc, player, ctx):
    # putnpc2 x,y,script — create a level NPC running the inline script
    if len(a) < 3:
        return
    _spawn_npc(self, "", a[2], a[0], a[1], ctx)


def _c_sethead(self, a, npc, player, ctx):
    if player is not None and a:
        self._set_player_attr(player, "head_image", a[0])  # wired HEADIMAGE


def _c_setbody(self, a, npc, player, ctx):
    if player is not None and a:
        self._set_player_attr(player, "body_image", a[0])  # wired BODYIMAGE


def _c_setsword(self, a, npc, player, ctx):
    # setsword image,power — acting player's sword image + power
    if player is None or not a:
        return
    img = to_str(a[0])
    power = int(to_num(a[1])) if len(a) > 1 else int(to_num(getattr(player, "sword_power", 0)))
    player.sword_image = img
    player.sword_power = power
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.SWORDPOWER, (power, img))


def _c_setshield(self, a, npc, player, ctx):
    # setshield image,power — acting player's shield image + power
    if player is None or not a:
        return
    img = to_str(a[0])
    power = int(to_num(a[1])) if len(a) > 1 else int(to_num(getattr(player, "shield_power", 0)))
    player.shield_image = img
    player.shield_power = power
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.SHIELDPOWER, (power, img))


def _c_setgender(self, a, npc, player, ctx):
    if player is not None and a:
        try:
            player.gender = int(to_num(a[0]))
        except Exception:
            pass


def _c_showcharacter(self, a, npc, player, ctx):
    # showcharacter — NPC displays as a player-style character (body/head/
    # gani sprite) instead of a raw image sheet. Previously a no-op, which
    # left the NPC with no image and no body/gani, so it rendered as
    # nothing. Fill in classic defaults so any client can show *something*
    # even without client-side inference: default body/gani only if the
    # script hasn't already set its own (setbody/setani may run before or
    # after showcharacter in real scripts), and clear image so clients
    # that key off body_image/gani rather than a raw sheet pick it up.
    if npc is None:
        return
    if not npc.body_image:
        npc.body_image = "body.png"
    if not npc.gani:
        npc.gani = "idle"
    npc.image = ""
    self._dirty(npc)


def _c_destroy(self, a, npc, player, ctx):
    # destroy — the NPC deletes itself from the level
    if npc is None or self.server is None:
        return
    nm = getattr(self.server, "npc_manager", None)
    if nm is not None and hasattr(nm, "destroy_npc"):
        _schedule(nm.destroy_npc(npc))


def _set_player_color(player, slot, value):
    """Set one of the acting player's 5 color slots and queue the COLORS prop."""
    colors = getattr(player, "colors", None)
    if not isinstance(colors, list) or not (0 <= slot < len(colors)):
        return
    colors[slot] = int(to_num(value)) & 0xFF
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.COLORS, list(colors))
    if hasattr(player, "mark_dirty"):
        player.mark_dirty()


def _make_color_cmd(slot):
    def handler(self, a, npc, player, ctx):
        if player is not None and a:
            _set_player_color(player, slot, a[0])
    return handler


# set{skin,coat,sleeve,shoe,belt}color color — player color slots 0..4
_c_setskincolor = _make_color_cmd(0)
_c_setcoatcolor = _make_color_cmd(1)
_c_setsleevecolor = _make_color_cmd(2)
_c_setshoecolor = _make_color_cmd(3)
_c_setbeltcolor = _make_color_cmd(4)


def _c_puthorse(self, a, npc, player, ctx):
    # puthorse imagefile,x,y — drop a horse on the level (bushes=2, dir=0)
    if self.server is None or len(a) < 3:
        return
    hm = getattr(self.server, "horse_manager", None)
    lvl = self._level_of(ctx)
    if hm is None or lvl is None or not hasattr(hm, "add_horse"):
        return
    _schedule(hm.add_horse(lvl, to_num(a[1]), to_num(a[2]),
                           direction=0, bushes=2, image=to_str(a[0])))


def _c_takehorse(self, a, npc, player, ctx):
    # takehorse index — mount the level horse at <index> onto this NPC
    if npc is None or self.server is None or not a:
        return
    hm = getattr(self.server, "horse_manager", None)
    lvl = self._level_of(ctx)
    if hm is None or lvl is None or not hasattr(hm, "get_horses_on_level"):
        return
    horses = hm.get_horses_on_level(lvl.name)
    idx = int(to_num(a[0]))
    if 0 <= idx < len(horses):
        horse = horses[idx]
        npc.horse_image = getattr(horse, "image", "")
        self._dirty(npc)
        _schedule(hm.remove_horse(lvl.name, horse.id))


# -- items ------------------------------------------------------------------
def _spawn_item(self, ctx, code, x, y):
    im = getattr(self.server, "item_manager", None) if self.server is not None else None
    lvl = self._level_of(ctx)
    if im is None or lvl is None or not hasattr(im, "spawn_item"):
        return
    try:
        from .protocol.constants import LevelItemType
        item_type = LevelItemType(int(to_num(code)))
    except Exception:
        return
    _schedule(im.spawn_item(lvl, to_num(x), to_num(y), item_type))


def _c_lay(self, a, npc, player, ctx):
    # lay itemname — drop an item at the NPC's position
    if npc is None or not a:
        return
    _spawn_item(self, ctx, a[0], getattr(npc, "x", 0), getattr(npc, "y", 0))


def _c_lay2(self, a, npc, player, ctx):
    # lay2 itemname,x,y — drop an item at an exact position
    if len(a) >= 3:
        _spawn_item(self, ctx, a[0], a[1], a[2])


def _c_take(self, a, npc, player, ctx):
    # take itemname — remove matching items within ~10 tiles of the NPC
    im = getattr(self.server, "item_manager", None) if self.server is not None else None
    lvl = self._level_of(ctx)
    if im is None or lvl is None or npc is None or not hasattr(im, "get_items_on_level"):
        return
    try:
        from .protocol.constants import LevelItemType
        want = LevelItemType(int(to_num(a[0]))) if a else None
    except Exception:
        return
    nx, ny = to_num(getattr(npc, "x", 0)), to_num(getattr(npc, "y", 0))
    for it in im.get_items_on_level(lvl.name):
        if (want is None or it.item_type == want) and abs(it.x - nx) <= 10 and abs(it.y - ny) <= 10:
            _schedule(im.remove_item(lvl.name, it.x, it.y))


def _c_toweapons(self, a, npc, player, ctx):
    # toweapons name — turn this NPC into a weapon and give it to the player
    if player is None or npc is None or not a:
        return
    name = to_str(a[0])
    if hasattr(player, "add_weapon"):
        player.add_weapon(name)
    if hasattr(player, "send_raw"):
        try:
            from .protocol.packets import build_npc_weapon_add
            _schedule(player.send_raw(build_npc_weapon_add(
                weapon_name=name,
                image=to_str(getattr(npc, "image", "")),
                script="",
            )))
        except Exception:
            logger.debug("toweapons send failed for %s", name, exc_info=True)


# -- board ------------------------------------------------------------------
def _c_updateboard(self, a, npc, player, ctx):
    # updateboard x,y,width,height — re-broadcast a region of the level board
    if len(a) < 4 or self.server is None:
        return
    lvl = self._level_of(ctx)
    if lvl is None or not hasattr(lvl, "_tiles"):
        return
    x = max(0, int(to_num(a[0])))
    y = max(0, int(to_num(a[1])))
    w = max(0, min(64 - x, int(to_num(a[2]))))
    h = max(0, min(64 - y, int(to_num(a[3]))))
    if w == 0 or h == 0:
        return
    tiles = bytearray()
    for row in range(y, y + h):
        start = (row * 64 + x) * 2
        tiles += bytes(lvl._tiles[start:start + w * 2])
    try:
        from .protocol.packets import build_board_modify, build_board_modify2
        world = getattr(self.server, "world", None)
        gmap_info = (
            world.get_gmap_for_level(lvl.name)
            if world and hasattr(world, "get_gmap_for_level")
            else None
        )
        if gmap_info:
            _, map_x, map_y = gmap_info
            packet = build_board_modify2(
                map_x, map_y, x, y, w, h, bytes(tiles)
            )
        else:
            packet = build_board_modify(x, y, w, h, bytes(tiles))
        _schedule(self.server.broadcast_to_level(
            lvl.name, packet))
    except Exception:
        logger.debug("updateboard failed", exc_info=True)


# -- player state -----------------------------------------------------------
def _c_setplayerdir(self, a, npc, player, ctx):
    if player is None or not a:
        return
    d = int(to_num(a[0])) & 3
    player.direction = d
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.DIRECTION, d)


def _c_enableweapons(self, a, npc, player, ctx):
    if player is not None:
        try:
            player.weapons_disabled = False
        except Exception:
            pass


def _c_disableweapons(self, a, npc, player, ctx):
    if player is not None:
        try:
            player.weapons_disabled = True
        except Exception:
            pass


def _c_setchargender(self, a, npc, player, ctx):
    if npc is not None and a:
        try:
            npc.gender = int(to_num(a[0]))
        except Exception:
            pass


# -- carry / push -----------------------------------------------------------
def _c_carryobject(self, a, npc, player, ctx):
    if npc is not None:
        npc.gani = "carrystill"
        self._dirty(npc)


def _c_throwcarry(self, a, npc, player, ctx):
    if npc is not None and to_str(getattr(npc, "gani", "")).startswith("carry"):
        npc.gani = "idle"
        self._dirty(npc)


def _make_blockflag_cmd(bit, on):
    def handler(self, a, npc, player, ctx):
        if npc is not None:
            bf = int(getattr(npc, "block_flags", 0) or 0)
            npc.block_flags = (bf | bit) if on else (bf & ~bit)
    return handler


# NPCBlockFlags: CANBECARRIED=2, CANBEPULLED=4, CANBEPUSHED=8
_c_canbecarried = _make_blockflag_cmd(0x02, True)
_c_cannotbecarried = _make_blockflag_cmd(0x02, False)
_c_canbepulled = _make_blockflag_cmd(0x04, True)
_c_cannotbepulled = _make_blockflag_cmd(0x04, False)
_c_canbepushed = _make_blockflag_cmd(0x08, True)
_c_cannotbepushed = _make_blockflag_cmd(0x08, False)


def _c_takeplayercarry(self, a, npc, player, ctx):
    # force the player to drop a carried object (PLO_THROWCARRIED)
    if player is None or self.server is None or not hasattr(self.server, "broadcast_to_level"):
        return
    lvl = getattr(player, "level", None)
    if lvl is None:
        return
    try:
        from .protocol.packets import PacketBuilder
        from .protocol.constants import PLO
        pkt = (PacketBuilder().write_gchar(PLO.THROWCARRIED)
               .write_gshort(getattr(player, "id", 0)).write_newline().build())
        _schedule(self.server.broadcast_to_level(lvl.name, pkt))
    except Exception:
        logger.debug("takeplayercarry failed", exc_info=True)


# -- combat -----------------------------------------------------------------
def _c_putbomb(self, a, npc, player, ctx):
    # putbomb power,x,y
    if len(a) < 3 or self.server is None:
        return
    lvl = self._level_of(ctx)
    if lvl is None:
        return
    try:
        from .protocol.packets import build_bomb_add
        pid = getattr(player, "id", 0) if player is not None else 0
        _schedule(self.server.broadcast_to_level(lvl.name, build_bomb_add(
            pid, to_num(a[1]), to_num(a[2]), int(to_num(a[0])), 55)))
    except Exception:
        logger.debug("putbomb failed", exc_info=True)


def _explode(self, ctx, radius, power, x, y):
    lvl = self._level_of(ctx)
    if lvl is None or self.server is None:
        return
    try:
        from .protocol.packets import build_explosion
        _schedule(self.server.broadcast_to_level(
            lvl.name, build_explosion(x, y, radius, power)))
    except Exception:
        logger.debug("explosion broadcast failed", exc_info=True)
    cm = getattr(self.server, "combat_manager", None)
    if cm is None or not hasattr(cm, "apply_damage"):
        return
    try:
        from .combat import DamageType
        dtype = DamageType.BOMB
    except Exception:
        dtype = None
    for p in self._players_on_level(ctx):
        if abs(to_num(getattr(p, "x", 0)) - x) <= radius and abs(to_num(getattr(p, "y", 0)) - y) <= radius:
            _schedule(cm.apply_damage(p, power * 2, 0, 0, dtype))


def _c_putexplosion(self, a, npc, player, ctx):
    # putexplosion radius,x,y  (power=1)
    if len(a) < 3:
        return
    _explode(self, ctx, int(to_num(a[0])), 1, to_num(a[1]), to_num(a[2]))


def _c_putexplosion2(self, a, npc, player, ctx):
    # putexplosion2 power,radius,x,y
    if len(a) < 4:
        return
    _explode(self, ctx, int(to_num(a[1])), int(to_num(a[0])), to_num(a[2]), to_num(a[3]))


def _c_shootarrow(self, a, npc, player, ctx):
    # shootarrow dir — fire an arrow from the NPC in a cardinal direction
    if npc is None or self.server is None:
        return
    lvl = self._level_of(ctx)
    if lvl is None:
        return
    try:
        from .protocol.packets import build_arrow_add
        d = (int(to_num(a[0])) & 3) if a else 2
        _schedule(self.server.broadcast_to_level(lvl.name, build_arrow_add(
            0, to_num(getattr(npc, "x", 0)), to_num(getattr(npc, "y", 0)), d)))
    except Exception:
        logger.debug("shootarrow failed", exc_info=True)


def _c_hitplayer(self, a, npc, player, ctx):
    # hitplayer index,halfhearts,fromx,fromy — damage the level player at index
    if len(a) < 2 or self.server is None:
        return
    cm = getattr(self.server, "combat_manager", None)
    if cm is None or not hasattr(cm, "apply_damage"):
        return
    players = self._players_on_level(ctx)
    idx = int(to_num(a[0]))
    if 0 <= idx < len(players):
        try:
            from .combat import DamageType
            dtype = DamageType.OTHER
        except Exception:
            dtype = None
        target = players[idx]
        hurt_dx, hurt_dy = _player_hurt_push(target, to_num(a[2]), to_num(a[3]))
        _schedule(cm.apply_damage(target, math.floor(to_num(a[1])),
                                  hurt_dx, hurt_dy, dtype))


def _normalized_push(target, from_x, from_y, distance=1.0):
    """C++ GS1 hit direction: normalize target tile minus source tile."""
    dx = to_num(getattr(target, "x", 0)) - to_num(from_x)
    dy = to_num(getattr(target, "y", 0)) - to_num(from_y)
    length = math.hypot(dx, dy)
    if length:
        dx /= length
        dy /= length
    return dx * distance, dy * distance


def _player_hurt_push(target, from_x, from_y):
    # Server::hitPlayer pushes four tiles, converts to pixels (*16), then
    # recentres both wire components at 64.
    dx, dy = _normalized_push(target, from_x, from_y, 4.0)
    return int(dx * 16) + 64, int(dy * 16) + 64


def _c_hitobjects(self, a, npc, player, ctx):
    # hitobjects power,x,y — GS1Commands.cpp fn_hitobjects calls
    # Server::hitObjectsAtPoint(pos, power, level, npc) which, for an
    # NPC-sourced call, ONLY broadcasts a PLO_HITOBJECTS notification to
    # nearby clients (Server.cpp:2253-2257 in the GServer-v2 checkout) — it
    # does NOT itself look up or damage any NPC/baddy/player server-side.
    # The real server-side hit detection + washit firing happens in the
    # CLIENT-REPORTED PLI_HITOBJECTS packet handler (msgPLI_HITOBJECTS,
    # PlayerClientPackets.cpp:1017), i.e. combat.handle_hit_objects, which is
    # what actually applies a player's own sword swing to nearby NPCs (see
    # that function's docstring). A serverside NPC script calling
    # `hitobjects` itself (e.g. from a timeout/AI loop) therefore only ever
    # produces a client-side visual/audio hit effect here, matching upstream.
    if npc is None or self.server is None or len(a) < 3:
        return
    lvl = self._level_of(ctx)
    if lvl is None:
        return
    try:
        from .protocol.packets import build_hit_objects
        power = int(to_num(a[0]) * 2)
        pkt = build_hit_objects(0, power, to_num(a[1]), to_num(a[2]), npc_id=npc.id)
        _schedule(self.server.broadcast_to_level(lvl.name, pkt))
    except Exception:
        logger.debug("hitobjects failed", exc_info=True)


def _c_hitnpc(self, a, npc, player, ctx):
    # hitnpc index,halfhearts,fromx,fromy — GS1Commands.cpp fn_hitnpc: hits
    # the NPC at position <index> in the level's NPC list, decrementing its
    # health and firing washit. HURTDXDY stores the normalized target-from
    # source direction at midpoint 32.
    if npc is None or self.server is None or len(a) < 4:
        return
    lvl = self._level_of(ctx)
    if lvl is None or not hasattr(lvl, "get_npcs"):
        return
    npcs = lvl.get_npcs()
    idx = int(to_num(a[0]))
    if not (0 <= idx < len(npcs)):
        return
    target = npcs[idx]
    halfhearts = math.floor(to_num(a[1]))
    dx, dy = _normalized_push(target, a[2], a[3])
    target.hurt_dx = int(max(-1.0, min(1.0, dx)) * 32)
    target.hurt_dy = int(max(-1.0, min(1.0, dy)) * 32)
    target.hearts = max(0.0, to_num(getattr(target, "hearts", 0)) - halfhearts / 2.0)
    self._dirty(target)
    nm = getattr(self.server, "npc_manager", None)
    if nm is not None and hasattr(nm, "on_npc_washit"):
        _schedule(nm.on_npc_washit(target, player))


def _c_hitcompu(self, a, npc, player, ctx):
    # hitcompu index,power,fromx,fromy — GS1Commands.cpp fn_hitcompu.
    # Upstream is a client-trust artifact: it sends a bare PLO_BADDYHURT
    # packet to the level's leader player ONLY and never touches server-side
    # baddy health at all (relying on that one client to self-report the
    # damage back, same as a real sword swing would). pygserver treats baddy
    # health as server-authoritative everywhere else (explosion/arrow/sword
    # all go through BaddyManager.handle_baddy_hurt), so this deliberately
    # applies REAL damage via that same path instead of replicating the
    # leader-only notify quirk — a real hit is strictly more useful than a
    # packet only one player's client happens to see.
    if self.server is None or len(a) < 4:
        return
    lvl = self._level_of(ctx)
    bm = getattr(self.server, "baddy_manager", None)
    if lvl is None or bm is None or not hasattr(bm, "get_baddies_on_level"):
        return
    baddies = bm.get_baddies_on_level(lvl.name)
    idx = int(to_num(a[0]))
    if not (0 <= idx < len(baddies)):
        return
    leader = self._leader_player(ctx)
    if leader is None:
        return
    _schedule(bm.handle_baddy_hurt(
        leader, baddies[idx].id, math.floor(to_num(a[1])),
        to_num(a[2]), to_num(a[3])))


def _c_sendtorc(self, a, npc, player, ctx):
    message = to_str(a[0]) if a else ""
    rc_manager = getattr(self.server, "rc_manager", None) if self.server else None
    if rc_manager is not None and hasattr(rc_manager, "process_chat"):
        _schedule(rc_manager.process_chat(message))


def _queue_player_prop(player, prop_id, value):
    dirty = getattr(player, "_gs1_dirty_props", None)
    if dirty is None:
        dirty = {}
        player._gs1_dirty_props = dirty
    dirty[prop_id] = value


def _c_freezeplayer(self, a, npc, player, ctx):
    # freezeplayer/freezeplayer2 - GServer-v2 PlayerClient::freezePlayer()
    # sends a bare PLO_FREEZEPLAYER2 packet (PlayerClient.cpp:1700-1703).
    if player is None:
        return
    try:
        player.is_frozen = True
        player._gs1_freeze_until = (
            time.monotonic() + max(0.0, to_num(a[0])) if a else None
        )
    except Exception:
        pass
    if hasattr(player, "send_raw"):
        from .protocol.packets import build_freeze_player
        _schedule(player.send_raw(build_freeze_player()))


def _c_unfreezeplayer(self, a, npc, player, ctx):
    # unfreezeplayer/unfreezeplayer2 - GServer-v2 PlayerClient::unfreezePlayer()
    # sends a bare PLO_UNFREEZEPLAYER packet (PlayerClient.cpp:1705-1708).
    if player is None:
        return
    try:
        player.is_frozen = False
        player._gs1_freeze_until = None
    except Exception:
        pass
    if hasattr(player, "send_raw"):
        from .protocol.packets import build_unfreeze_player
        _schedule(player.send_raw(build_unfreeze_player()))


def _c_say2(self, a, npc, player, ctx):
    # say2 <raw text> - GServer-v2 PlayerClient::sendSignMessage() sends
    # PLO_SAY2 with the translated text (PlayerClient.cpp:1717-1721). This is
    # the RPG-style textbox/sign message sent directly to the triggering
    # player, distinct from `message`/`say` which just set the NPC's chat
    # bubble (NPCPROP #c) for everyone on the level to see.
    if player is None or not hasattr(player, "send_raw"):
        return
    text = to_str(a[0]) if a else ""
    from .protocol.packets import build_say2
    _schedule(player.send_raw(build_say2(text)))


def _schedule(coro):
    try:
        asyncio.get_running_loop().create_task(coro)
        return True
    except RuntimeError:
        # Callers construct the coroutine before asking us to schedule it.
        # Close it when invoked from a synchronous unit context so it does
        # not leak a never-awaited coroutine warning.
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        return False


def _c_setlevel2(self, a, npc, player, ctx):
    # warp the acting player to level,x,y (doors/teleports)
    if player is None or not a or not hasattr(player, "warp"):
        return
    lvl = to_str(a[0])
    x = to_num(a[1]) if len(a) > 1 else getattr(player, "x", 30)
    y = to_num(a[2]) if len(a) > 2 else getattr(player, "y", 30)
    _schedule(player.warp(lvl, x, y))


def _c_setlevel(self, a, npc, player, ctx):
    if player is None or not a or not hasattr(player, "warp"):
        return
    _schedule(player.warp(to_str(a[0]), getattr(player, "x", 30),
                          getattr(player, "y", 30)))


def _c_hurt(self, a, npc, player, ctx):
    # hurt <halfhearts> — C++ fn_hurt (GS1Commands.cpp:1346) floors the
    # argument to an int and hits the acting player for that many
    # HALF-hearts (hitPlayer power), so `hurt 1` removes 0.5 hearts.
    #
    # Clamp at 0 and hand off to the death path, matching combat.py's
    # apply_damage (combat.py:522/548-549) — a GS1 hurt must not be able to
    # drive hearts negative or push a garbage negative CURPOWER prop.
    if player is None or not a:
        return
    halfhearts = math.floor(to_num(a[0]))
    new_hearts = max(0.0, to_num(getattr(player, "hearts", 0)) - halfhearts / 2.0)
    self._set_player_attr(player, "hearts", new_hearts)
    if new_hearts <= 0:
        cm = getattr(self.server, "combat_manager", None) if self.server is not None else None
        if cm is not None and hasattr(cm, "handle_player_death"):
            try:
                from .combat import DamageType
                dtype = DamageType.OTHER
            except Exception:
                dtype = None
            _schedule(cm.handle_player_death(player, None, dtype))


def _c_noop(self, a, npc, player, ctx):
    pass


def _showimg_index(args):
    return math.floor(to_num(args[0])) if args else -1


def _broadcast_showimgs(host, npc, images, *, reset=False):
    if host.server is None or npc is None or getattr(npc, "level", None) is None:
        return
    from .protocol.packets import build_npc_showimgs
    packet = build_npc_showimgs(npc.id, images, reset=reset)
    _schedule(host.server.broadcast_to_level(npc.level.name, packet))


def _c_showimg(self, a, npc, player, ctx):
    if npc is None or len(a) < 4:
        return
    index = _showimg_index(a)
    if not 0 <= index <= 199:
        return
    props = {0: to_str(a[1]), 1: int(to_num(a[2]) * 2),
             2: int(to_num(a[3]) * 2)}
    if len(a) >= 5:
        z = int(to_num(a[4]))
        if z != 0:
            props[7] = z
    npc.showimgs[index] = props
    npc._had_showimgs = True
    _broadcast_showimgs(self, npc, {index: props})


def _c_hideimg(self, a, npc, player, ctx):
    if npc is None or not a:
        return
    index = _showimg_index(a)
    if not 0 <= index <= 199:
        return
    end = math.floor(to_num(a[1])) if len(a) > 1 else index
    for layer in range(index, min(end, 199) + 1):
        npc.showimgs.pop(layer, None)
    npc._had_showimgs = True
    _broadcast_showimgs(self, npc, npc.showimgs, reset=True)


def _change_showimg(self, a, npc, prop_id, value):
    if npc is None or not a:
        return
    index = _showimg_index(a)
    if not 0 <= index <= 199 or index not in npc.showimgs:
        return
    npc.showimgs[index][prop_id] = value
    _broadcast_showimgs(self, npc, {index: {prop_id: value}})


def _c_changeimgvis(self, a, npc, player, ctx):
    if len(a) >= 2:
        _change_showimg(self, a, npc, 3, int(to_num(a[1])) & 0xff)


def _c_changeimgpart(self, a, npc, player, ctx):
    if len(a) >= 5:
        value = (int(to_num(a[1])) & 0xffff, int(to_num(a[2])) & 0xffff,
                 int(to_num(a[3])) & 0xff, int(to_num(a[4])) & 0xff)
        _change_showimg(self, a, npc, 4, value)


def _c_changeimgcolors(self, a, npc, player, ctx):
    if len(a) >= 5:
        value = tuple(int(max(0.0, min(1.0, to_num(v))) * 200) for v in a[1:5])
        _change_showimg(self, a, npc, 5, value)


def _c_changeimgzoom(self, a, npc, player, ctx):
    if len(a) >= 2:
        value = int(max(0.0, min(22.0, to_num(a[1]))) * 10)
        _change_showimg(self, a, npc, 6, value)


def _c_changeimgmode(self, a, npc, player, ctx):
    if len(a) >= 2:
        _change_showimg(self, a, npc, 8, int(to_num(a[1])) & 0xff)


_COMMANDS = {
    "setimg": _c_setimg, "setgif": _c_setimg, "seticon": _c_noop,
    "setimgpart": _c_setimgpart,
    "setani": _c_setani, "setcharani": _c_setani,
    "message": _c_message, "say2": _c_say2, "say": _c_message,
    "hide": _c_hide, "show": _c_show,
    "hidelocal": _c_hide, "showlocal": _c_show,
    "move": _c_move,
    "setlevel2": _c_setlevel2, "setlevel": _c_setlevel, "hurt": _c_hurt,
    "setcharprop": _c_setcharprop, "setplayerprop": _c_setplayerprop,
    "addweapon": _c_addweapon, "triggeraction": _c_triggeraction,
    "putnpc": _c_putnpc, "putnpc2": _c_putnpc2,
    "puthorse": _c_puthorse, "takehorse": _c_takehorse,
    "destroy": _c_destroy,
    "sethead": _c_sethead, "setbody": _c_setbody, "setsword": _c_setsword,
    "setshield": _c_setshield, "setgender": _c_setgender,
    "showcharacter": _c_showcharacter,
    "setskincolor": _c_setskincolor, "setcoatcolor": _c_setcoatcolor,
    "setsleevecolor": _c_setsleevecolor, "setshoecolor": _c_setshoecolor,
    "setbeltcolor": _c_setbeltcolor,
    "freezeplayer": _c_freezeplayer, "freezeplayer2": _c_freezeplayer,
    "unfreezeplayer": _c_unfreezeplayer,
    # items
    "lay": _c_lay, "lay2": _c_lay2, "take": _c_take, "toweapons": _c_toweapons,
    # board
    "updateboard": _c_updateboard, "updateboard2": _c_updateboard,
    # player state
    "setplayerdir": _c_setplayerdir, "setchargender": _c_setchargender,
    "enableweapons": _c_enableweapons, "disableweapons": _c_disableweapons,
    # carry / push
    "carryobject": _c_carryobject, "throwcarry": _c_throwcarry,
    "takeplayercarry": _c_takeplayercarry,
    "canbecarried": _c_canbecarried, "cannotbecarried": _c_cannotbecarried,
    "canbepulled": _c_canbepulled, "cannotbepulled": _c_cannotbepulled,
    "canbepushed": _c_canbepushed, "cannotbepushed": _c_cannotbepushed,
    # combat
    "putbomb": _c_putbomb, "putexplosion": _c_putexplosion,
    "putexplosion2": _c_putexplosion2, "shootarrow": _c_shootarrow,
    "hitplayer": _c_hitplayer, "hitobjects": _c_hitobjects,
    "hitnpc": _c_hitnpc, "hitcompu": _c_hitcompu,
    "sendtorc": _c_sendtorc,
    "setshape": _c_setshape,
    "showimg": _c_showimg, "showimg2": _c_showimg,
    "hideimg": _c_hideimg, "hideimgs": _c_hideimg,
    "changeimgvis": _c_changeimgvis, "changeimgpart": _c_changeimgpart,
    "changeimgcolors": _c_changeimgcolors, "changeimgzoom": _c_changeimgzoom,
    "changeimgmode": _c_changeimgmode,
}

# Client-side visual / sound / timing commands. pygserver runs GS1 server-side
# and ships only NPC props (not the script) to clients, so these have no
# server-authoritative effect and are intentionally ignored. `sleep` is NOT
# listed here: it never reaches call_command at all (reborn_protocol's
# interp.py intercepts the "sleep" Command node itself, in coro/resumable
# mode yielding the duration for run_npc_event to drive via the NPC's real
# timer - see run_npc_event's docstring).
_NOOP_COMMANDS = (
    "play", "play2", "playlooped", "playsound", "stopmidi", "stopsound",
    "seteffectmode", "setcoloreffect", "setzoomeffect", "seteffect",
    "timereverywhere", "drawunderplayer", "drawoverplayer",
    "drawaslight", "drawovertrees", "dontblock", "blockagain",
    "dontblocklocal", "blockagainlocal",
    # setimgvis is client-only; the server-owned equivalent is changeimgvis.
    "setimgvis", "putleaps",
    "setbackpal", "setletters", "setmap", "setminimap",
    "showtext", "showtext2", "showstats", "replaceani",
    "setfocus", "centermap", "putcomp", "putnewcomp", "removecompus",
    "setpause", "dontshowtime", "showbomb", "showbow", "showsword", "showani",
    "resetfocus",
    # not implemented in the GServer-v2 C++ oracle either (commented out there),
    # so faithfully no-ops:
    "noplayerkilling", "enabledefmovement", "disabledefmovement",
    "toinventory", "hideplayer", "showplayer",
    # combat projectiles with no pygserver representation (client-side in Reborn)
    "shootball", "shootfireball", "shootfireblast", "shoot",
)
for _name in _NOOP_COMMANDS:
    _COMMANDS.setdefault(_name, _c_noop)


def leader_player_for_level(server, level):
    """First player on `level` (GS1Flags.cpp isleader / Level::isPlayerLeader),
    used as the triggering-player context for NPC events that have no
    natural player of their own - notably `timeout`. GServer-v2 documents
    exactly this: the level leader "can trigger timeout events on NPCs that
    didn't issue the timereverywhere command" (scripting-gs1-flags.md), i.e.
    upstream runs a non-timereverywhere `timeout` in the leader's script
    context, not player-less. Without a player context here, bare (unprefixed)
    `set`/`unset` flags - which run_npc_event stores on player.flags - have
    nowhere to persist and can never be read back by a later event, breaking
    any quest that sets a flag on the player (e.g. a beer-guard NPC) and
    later reads it from an unrelated NPC's `timeout` (e.g. a mountain guard
    that should unblock once `drunkguard` is set).

    Same "first player" lookup as GS1Host._leader_player - Level._players is
    insertion-ordered, so iterating level.get_player_ids() genuinely yields
    join order. Returns None (matching prior behaviour) if the level has no
    players.
    """
    if level is None or server is None or not hasattr(level, "get_player_ids"):
        return None
    for pid in level.get_player_ids():
        p = server.get_player(pid)
        if p is not None:
            return p
    return None


# -- script binding / event firing -----------------------------------------
def compile_gs1(code: str):
    """Parse GS1 source into a Program AST (None on hard failure)."""
    try:
        return parse(code)
    except Exception:
        logger.warning("failed to parse GS1 NPC script", exc_info=True)
        return None


class _PendingGS1Sleep:
    """One suspended GS1 execution parked on an NPC by a mid-script `sleep`.

    Mirrors GServer-v2's m_sleepCallStack/m_sleepCurrentSource
    (GS1Visitor.h/.cpp GS1Visitor::execute): a real NPC keeps exactly one of
    these per script, resumed only by its own next TIMEOUT event
    (ScriptEngineGS1.cpp:314, GS1Visitor.cpp:719-727 - "Sleeping scripts use
    the timeout event to resume themselves").

    `resumable`/`ctx` are shared with the NPC's persistent GS1 Context (see
    run_npc_event); the rest of the fields are a SNAPSHOT of the ctx bindings
    this particular execution was suspended with (player/this_obj/source/
    scopes), captured at suspend time and reapplied verbatim before every
    `.resume()` - because in between resumes, the SAME shared ctx may have
    been reused (and its bindings temporarily repointed) by unrelated fresh
    events firing on this NPC (playerchats, playertouchsme, ...; see
    run_npc_event). Without this snapshot/restore, a fresh event's player
    would leak into the next resume instead of "one execution, one player"
    (task requirement: resume with the SAME player the suspended execution
    started with, even if the level leader changed mid-sleep).
    """
    __slots__ = ("resumable", "ctx", "vars", "player", "this_obj",
                 "hit_source", "charprop_source", "tokenize_tokens")

    def __init__(self, resumable, ctx):
        self.resumable = resumable
        self.ctx = ctx
        self.vars = ctx.vars
        self.player = ctx.player
        self.this_obj = ctx.this_obj
        self.hit_source = ctx.hit_source
        self.charprop_source = ctx.charprop_source
        self.tokenize_tokens = ctx.tokenize_tokens

    def apply(self):
        ctx = self.ctx
        ctx.vars = self.vars
        ctx.player = self.player
        ctx.this_obj = self.this_obj
        ctx.hit_source = self.hit_source
        ctx.charprop_source = self.charprop_source
        ctx.tokenize_tokens = self.tokenize_tokens
        ctx.active_event = "timeout"


def _ensure_gs1_ctx(npc, host):
    """The NPC's single persistent GS1 Context, created lazily and reused for
    every event on this NPC's whole life (`ctx.vars` is swapped out per fresh
    call - see _bind_fresh_gs1_call - but the Context object itself, and
    hence `ctx.sleep_cancelled`, stays the same identity forever). This is
    what makes a bare `timeout = x;` from ANY handler able to cancel a sleep
    left pending by a different, earlier execution: reborn_protocol's
    ResumableExecution.resume() only ever consults `sleep_cancelled` on the
    ctx object it was constructed with (Context.sleep_cancelled), so that
    cancellation signal only crosses executions if they share one Context -
    exactly like GServer-v2 keeping one GS1Visitor per NPC script for its
    whole lifetime (ScriptEngineGS1.cpp: `context->script` holds the wrapper
    with `wrapper->visitor` reused across every execute() call)."""
    ctx = getattr(npc, "_gs1_ctx", None)
    if ctx is None:
        ctx = Context(host, VarStore(), this_obj=npc, player=None)
        npc._gs1_ctx = ctx
    return ctx


def _bind_fresh_gs1_call(ctx, npc, server, player, event, source,
                         carryobject_type=None):
    """(Re)configure the NPC's persistent ctx for a brand-new top-level
    firing (as opposed to resuming a pending sleep). Rebuilds `ctx.vars` from
    scratch exactly like the pre-resumable code used to build a whole new
    Context every call: this./thiso. persist on the NPC (npc.gs1_scopes),
    `local.` is TEMPORARY like `temp.` (GS1Variables.h; gets a fresh dict
    every call, not npc-owned - upstream d6c78ef3), client./server./level./
    global. persist on the player/server/level, bare flags persist on the
    player. `source` records who/what initiated this event ("player"/"baddy"/
    "npc") for flags that expose it (wasshot's shotbyplayer/shotbybaddy/
    shotbynpc, GS1Flags.cpp) - stashed as `hit_source`, read by
    GS1Host.get_builtin."""
    sc = npc.gs1_scopes
    level = getattr(npc, "level", None)
    scopes = {
        "this": sc["this"], "thiso": sc["thiso"],
        "local": {}, "temp": {},
        "client": _lazy(player, "_gs1_client"),
        "server": _lazy(server, "_gs1_server"),
        "level": _lazy(level, "_gs1_vars"),
        "global": _lazy(server, "_gs1_global"),
    }
    player_flags = getattr(player, "flags", None)
    if player_flags is None:
        player_flags = _lazy(player, "_gs1_flags")
    ctx.vars = VarStore(scopes=scopes, player_flags=player_flags)
    ctx.this_obj = npc
    ctx.player = player
    ctx.hit_source = source
    ctx.carryobject_type = (int(carryobject_type)
                            if carryobject_type is not None else None)
    ctx.active_event = event
    ctx.tokenize_tokens = []
    ctx.charprop_source = None
    ctx.steps = 0


def run_npc_event(npc, event: str, server=None, player=None, source=None,
                  carryobject_type=None):
    """Fire a GS1 event handler (`if (<event>) {...}`) on an NPC.

    Always runs through reborn_protocol's resumable API (Interpreter.
    run_event_resumable / ResumableExecution): a `sleep` mid-script suspends
    the execution instead of breaking its enclosing loop, and is resumed by
    a later NPC timer tick - see NPCManager.tick's on_timeout firing and
    _PendingGS1Sleep above. `sleep`'s own duration is what schedules that
    tick: reborn_protocol's interpreter is host-agnostic and never touches a
    timer itself (interp.py's sleep handling just yields the seconds), so
    THIS function wires `resumable.pending_sleep` into npc.set_timer(...),
    mirroring GS1Commands.cpp fn_sleep, which sets `npc->timeout = duration`
    itself right before throwing its sleep_exception.

    Dispatch rule (mirrors GS1Visitor::execute, ScriptEngineGS1.cpp:314 +
    GS1Visitor.cpp:719-727, and cross-checked against the real oracle binary
    by reborn-protocol/tests/test_gs1_sleep_resume.py's `_drive_resumable`,
    the TestOracleSleepResume class): ONLY a "timeout" event, while a sleep
    from an earlier execution is still pending, resumes that pending
    execution (picking up exactly where it suspended, loops/scopes intact).
    Every other event - including this NPC's own `created`/`playerchats`/
    `playertouchsme`/etc., AND a "timeout" event when nothing is pending -
    fires a brand-new execution instead. That fresh execution runs
    "alongside" any still-pending sleep rather than being queued, merged, or
    dropped: confirmed against the real GServer-v2 binary, firing the SAME
    non-timeout event repeatedly does NOT resume a pending sleep - each
    firing reruns its `if (event) {...}` block from scratch (see the oracle
    class's docstring). If that fresh execution itself suspends on a sleep,
    it REPLACES whatever was previously pending (GS1Visitor.cpp:757
    `m_sleepCallStack = std::move(m_callStack)` - an unconditional
    overwrite, no queueing). A bare (non-compound) `timeout = x;` assignment
    from ANY execution - fresh or resumed - cancels a pending sleep via
    Context.sleep_cancelled, consumed the next time that pending execution's
    `.resume()` is called (see _ensure_gs1_ctx for why this requires one
    shared Context per NPC).

    Returns the Context (or None if the NPC has no GS1 program).
    """
    prog = getattr(npc, "gs1_program", None)
    if prog is None:
        return None

    host = getattr(server, "gs1_host", None) or GS1Host(server)
    pending = getattr(npc, "_gs1_pending", None)
    if pending is not None and pending.resumable.done:
        pending = None
        npc._gs1_pending = None

    if event == "timeout" and pending is not None:
        ctx = pending.ctx
        pending.apply()
        try:
            pending.resumable.resume()
        except Exception as e:
            _report_gs1_error(f"event timeout resume on npc {getattr(npc, 'id', '?')}", e)
            npc._gs1_pending = None
            _flush_player_props(pending.player)
            return ctx
        if pending.resumable.done:
            npc._gs1_pending = None
        elif hasattr(npc, "set_timer"):
            npc.set_timer(pending.resumable.pending_sleep)
        _flush_player_props(pending.player)
        return ctx

    ctx = _ensure_gs1_ctx(npc, host)
    _bind_fresh_gs1_call(ctx, npc, server, player, event, source,
                         carryobject_type)
    try:
        resumable = Interpreter(ctx).run_event_resumable(prog, event)
    except Exception as e:
        _report_gs1_error(f"event {event} on npc {getattr(npc, 'id', '?')}", e)
        _flush_player_props(player)
        return ctx
    if not resumable.done:
        npc._gs1_pending = _PendingGS1Sleep(resumable, ctx)
        if hasattr(npc, "set_timer"):
            npc.set_timer(resumable.pending_sleep)
    _flush_player_props(player)
    return ctx


def _flush_player_props(player):
    """Send any player stat changes a script made to that player's client."""
    if player is None:
        return
    dirty = getattr(player, "_gs1_dirty_props", None)
    if not dirty:
        return
    player._gs1_dirty_props = {}
    if not hasattr(player, "send_props"):
        return
    try:
        import asyncio
        asyncio.get_running_loop().create_task(player.send_props(dirty))
    except RuntimeError:
        pass  # no running loop (e.g. unit test) — state is set, just not pushed


def _lazy(obj, attr):
    """Return obj.attr, creating it as a fresh dict if missing (None -> {})."""
    if obj is None:
        return {}
    d = getattr(obj, attr, None)
    if d is None:
        d = {}
        try:
            setattr(obj, attr, d)
        except Exception:
            pass
    return d
