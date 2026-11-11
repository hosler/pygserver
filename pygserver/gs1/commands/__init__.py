from . import character
from . import world
from . import combat
from . import visual
from . import objects
import logging


_COMMANDS = {}
for _module in (character, world, combat, visual, objects):
    _COMMANDS.update(_module._COMMANDS)

_c_noop = character._c_noop
_COMMANDS["seticon"] = _c_noop


def _c_savelog2(self, args, npc, player, ctx):
    """Write the script-selected channel and message to the server log."""
    channel = str(args[0]) if args else "script"
    message = str(args[1]) if len(args) > 1 else ""
    logging.getLogger("pygserver.gs1.script").info("[%s] %s", channel, message)


_COMMANDS["savelog2"] = _c_savelog2


def _map_command(build):
    """setmap / setminimap imgfile,levelsfile,x,y: PLO_BIGMAP / PLO_MINIMAP to
    the calling player only, x and y printed with "%f" (GS1Commands.cpp:
    2614-2652, origin/beta4). The reference rejects any other argument count."""
    def command(self, args, npc, player, ctx):
        if len(args) != 4 or player is None or not hasattr(player, "send_raw"):
            return
        from reborn_protocol.gs1.values import to_num, to_str
        from ..execution import _schedule
        text = ",".join((to_str(args[0]), to_str(args[1]),
                         f"{float(to_num(args[2])):f}", f"{float(to_num(args[3])):f}"))
        packet = build(text)
        if packet:      # empty: refused for a control byte (builders/world.py)
            _schedule(player.send_raw(packet))
    return command


def _build_bigmap(text):
    from ...protocol.packets import build_bigmap
    return build_bigmap(text)


def _build_minimap(text):
    from ...protocol.packets import build_minimap
    return build_minimap(text)


_COMMANDS["setmap"] = _map_command(_build_bigmap)
_COMMANDS["setminimap"] = _map_command(_build_minimap)
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
    "drawaslight", "drawovertrees",
    "dontblocklocal", "blockagainlocal",
    # setimgvis is client-only; the server-owned equivalent is changeimgvis.
    "setimgvis", "putleaps",
    "setbackpal", "setletters",
    "showtext", "showtext2", "showstats", "replaceani",
    "setfocus", "centermap", "putcomp", "putnewcomp", "removecompus",
    "setpause", "dontshowtime", "showbomb", "showbow", "showsword", "showani",
    "resetfocus",
    # not implemented in the GServer-v2 C++ oracle either (commented out there),
    # so faithfully no-ops:
    "noplayerkilling", "enabledefmovement", "disabledefmovement",
    "toinventory", "hideplayer", "showplayer",
    # combat projectiles with no pygserver representation (client-side in Reborn)
    "shootball", "shoot",
)
for _name in _NOOP_COMMANDS:
    _COMMANDS.setdefault(_name, _c_noop)
