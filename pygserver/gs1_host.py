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
import re
import time

from .gs1.runtime import Host, UNSET, VarStore, Context
from .gs1.interp import Interpreter
from .gs1.parser import parse
from .gs1.values import to_num, to_str

logger = logging.getLogger(__name__)

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
        "max_hearts": (PLPROP.MAXPOWER, lambda v: int(to_num(v) * 2)),
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
    "bombs": "bombs", "image": "image", "ani": "gani",
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
        if name in PLAYER_ATTR and player is not None:
            return self._coerce(getattr(player, PLAYER_ATTR[name], 0))
        if name == "playerlevel" and player is not None:
            lvl = getattr(player, "level", None)
            return getattr(lvl, "name", "") if lvl else ""
        if name == "playeronline":
            return 1.0 if player is not None else 0.0
        if name in NPC_ATTR and npc is not None:
            return self._coerce(getattr(npc, NPC_ATTR[name], 0))
        if name == "sprite" and npc is not None:
            return self._coerce(npc.flags.get("sprite", 0)) if hasattr(npc, "flags") else 0.0
        if name == "timeout" and npc is not None:
            end = getattr(npc, "_timer_end", 0.0)
            return max(0.0, end - time.time()) if end else 0.0
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        player = ctx.player
        npc = ctx.this_obj
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

    # -- commands ----------------------------------------------------------
    def call_command(self, name, args, ctx) -> None:
        npc = ctx.this_obj
        player = ctx.player
        try:
            handler = _COMMANDS.get(name)
            if handler is not None:
                handler(self, args, npc, player, ctx)
        except Exception:  # a bad command must never kill the script/server
            logger.debug("gs1 command %s failed", name, exc_info=True)

    # -- functions ---------------------------------------------------------
    def call_function(self, name, args, ctx):
        if name in ("onwall", "onwall2"):
            return self._onwall(args, ctx)
        if name in ("onwater", "onwater2"):
            return 0.0
        if name in ("playersays", "playersays2"):
            return self._playersays(args, ctx)
        if name == "hasweapon":
            player = ctx.player
            if player is not None and args and hasattr(player, "has_weapon"):
                return 1.0 if player.has_weapon(to_str(args[0])) else 0.0
            return 0.0
        if name in ("getnearestplayer", "findnearestplayer"):
            return self._nearest_player(args, ctx, name == "getnearestplayer")
        if name == "getnearestplayers":
            return self._nearest_players(args, ctx)
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

    def _playersays(self, args, ctx):
        player = ctx.player
        if player is None or not args:
            return 0.0
        return 1.0 if to_str(getattr(player, "chat", "")).startswith(to_str(args[0])) else 0.0

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
            return 0.0
        ctx.player = ranked[0]
        return float(getattr(ranked[0], "id", 0)) if return_id else 1.0

    def _nearest_players(self, args, ctx):
        """getnearestplayers(x,y) -> player ids sorted nearest-first.

        The C++ optional flag-filter arg is not supported: GS1 lexes that arg
        as an expression, so the flag *name* isn't recoverable here.
        """
        ranked = self._sorted_by_distance(args, ctx)
        return [float(getattr(p, "id", 0)) for p in ranked]


# -- command handlers -------------------------------------------------------
def _c_setimg(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.image = to_str(a[0])
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


def _gattribs_of(obj):
    ga = getattr(obj, "gattribs", None)
    if ga is None:
        ga = {}
        obj.gattribs = ga
    return ga


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
                colors[n] = int(to_num(val)) & 0xFF
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
                colors[n] = int(to_num(a[1])) & 0xFF
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


def _queue_player_prop(player, prop_id, value):
    dirty = getattr(player, "_gs1_dirty_props", None)
    if dirty is None:
        dirty = {}
        player._gs1_dirty_props = dirty
    dirty[prop_id] = value


def _c_freezeplayer(self, a, npc, player, ctx):
    if player is not None:
        try:
            player.is_frozen = True
        except Exception:
            pass


def _schedule(coro):
    try:
        asyncio.get_running_loop().create_task(coro)
        return True
    except RuntimeError:
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
    if player is None or not a:
        return
    self._set_player_attr(player, "hearts",
                          to_num(getattr(player, "hearts", 0)) - to_num(a[0]))


def _c_noop(self, a, npc, player, ctx):
    pass


_COMMANDS = {
    "setimg": _c_setimg, "setgif": _c_setimg, "seticon": _c_noop,
    "setimgpart": _c_setimg,
    "setani": _c_setani, "setcharani": _c_setani,
    "message": _c_message, "say2": _c_message, "say": _c_message,
    "hide": _c_hide, "show": _c_show,
    "hidelocal": _c_hide, "showlocal": _c_show,
    "move": _c_move,
    "setlevel2": _c_setlevel2, "setlevel": _c_setlevel, "hurt": _c_hurt,
    "setcharprop": _c_setcharprop, "setplayerprop": _c_setplayerprop,
    "addweapon": _c_addweapon, "triggeraction": _c_triggeraction,
    "putnpc": _c_putnpc, "putnpc2": _c_putnpc2,
    "puthorse": _c_puthorse, "takehorse": _c_takehorse,
    "freezeplayer": _c_freezeplayer, "freezeplayer2": _c_freezeplayer,
    # known visual/sound commands we intentionally ignore for now
    "play": _c_noop, "play2": _c_noop, "playlooped": _c_noop,
    "seteffectmode": _c_noop, "setcoloreffect": _c_noop, "setzoomeffect": _c_noop,
    "timereverywhere": _c_noop, "showcharacter": _c_noop,
    "drawunderplayer": _c_noop, "drawoverplayer": _c_noop, "drawaslight": _c_noop,
    "dontblock": _c_noop, "blockagain": _c_noop,
}


# -- script binding / event firing -----------------------------------------
def compile_gs1(code: str):
    """Parse GS1 source into a Program AST (None on hard failure)."""
    try:
        return parse(code)
    except Exception:
        logger.warning("failed to parse GS1 NPC script", exc_info=True)
        return None


def run_npc_event(npc, event: str, server=None, player=None):
    """Fire a GS1 event handler (`if (<event>) {...}`) on an NPC.

    Wires variable scopes so this./thiso./local. persist on the NPC, bare flags
    persist on the player, and server/level/global persist on the server/level.
    Returns the Context (or None if the NPC has no GS1 program).
    """
    prog = getattr(npc, "gs1_program", None)
    if prog is None:
        return None

    host = getattr(server, "gs1_host", None) or GS1Host(server)
    sc = npc.gs1_scopes
    level = getattr(npc, "level", None)
    scopes = {
        "this": sc["this"], "thiso": sc["thiso"], "local": sc["local"],
        "temp": {},
        "client": _lazy(player, "_gs1_client"),
        "server": _lazy(server, "_gs1_server"),
        "level": _lazy(level, "_gs1_vars"),
        "global": _lazy(server, "_gs1_global"),
    }
    player_flags = getattr(player, "flags", None)
    if player_flags is None:
        player_flags = _lazy(player, "_gs1_flags")
    vs = VarStore(scopes=scopes, player_flags=player_flags)
    ctx = Context(host, vs, this_obj=npc, player=player)
    try:
        Interpreter(ctx).run_event(prog, event)
    except Exception:
        logger.debug("GS1 event %s on NPC %s failed", event,
                     getattr(npc, "id", "?"), exc_info=True)
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
