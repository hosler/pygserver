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
import time

from .gs1.runtime import Host, UNSET, VarStore, Context
from .gs1.interp import Interpreter
from .gs1.parser import parse
from .gs1.values import to_num, to_str

logger = logging.getLogger(__name__)

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
            return 0.0
        # getnpc/getplayer/nearest-player/etc not yet modelled -> 0
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
        if hasattr(player, "mark_dirty"):
            player.mark_dirty()

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


def _c_setcharprop(self, a, npc, player, ctx):
    # setcharprop <messagecode>, <value> — map the common chat/nick codes
    if npc is None or len(a) < 2:
        return
    code, val = to_str(a[0]), to_str(a[1])
    if code == "#c":
        npc.message = val
        self._dirty(npc)
    elif code in ("#n", "#N"):
        npc.nickname = val
        self._dirty(npc)


def _c_setplayerprop(self, a, npc, player, ctx):
    if player is None or len(a) < 2:
        return
    code, val = to_str(a[0]), to_str(a[1])
    if code == "#c" and hasattr(player, "chat"):
        player.chat = val
    elif code == "#n":
        player.nickname = val
    if hasattr(player, "mark_dirty"):
        player.mark_dirty()


def _c_freezeplayer(self, a, npc, player, ctx):
    if player is not None:
        try:
            player.is_frozen = True
        except Exception:
            pass


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
    "setcharprop": _c_setcharprop, "setplayerprop": _c_setplayerprop,
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
    return ctx


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
