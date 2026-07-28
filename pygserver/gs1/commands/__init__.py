from . import character
from . import world
from . import combat
from . import visual
import logging


_COMMANDS = {}
for _module in (character, world, combat, visual):
    _COMMANDS.update(_module._COMMANDS)

_c_noop = character._c_noop
_COMMANDS["seticon"] = _c_noop


def _c_savelog2(self, args, npc, player, ctx):
    """Write the script-selected channel and message to the server log."""
    channel = str(args[0]) if args else "script"
    message = str(args[1]) if len(args) > 1 else ""
    logging.getLogger("pygserver.gs1.script").info("[%s] %s", channel, message)


_COMMANDS["savelog2"] = _c_savelog2
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
    "setpause", "dontshowtime", "timershow", "showbomb", "showbow", "showsword", "showani",
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
