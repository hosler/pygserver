"""Level-object and cross-NPC commands: join, callnpc, removebomb,
takeplayerhorse, timershow, shootfireball/shootfireblast/shootnuke.

Each handler cites the GServer-v2 fn_* it mirrors (GS1Commands.cpp)."""
from __future__ import annotations

import asyncio
import logging
import math

from reborn_protocol.gs1.values import to_num, to_str

from ...protocol.constants import NPCPROP, PLPROP
from ..execution import _schedule, merge_joined_functions
from .. import objects as gs1_objects
from ..host import _GS1ObjectRef

logger = logging.getLogger(__name__)


def _source_npc(ctx):
    """Nearest NPC source: a `with (npcs[i])` target, else the running NPC."""
    this = ctx.this_obj
    if isinstance(this, _GS1ObjectRef):
        return this.target if this.kind == "npc" else this.owner
    return this


def _running_npc(ctx):
    """The script's own NPC (getOriginalSource), ignoring with-targets."""
    this = ctx.this_obj
    return this.owner if isinstance(this, _GS1ObjectRef) else this


def _source_player(ctx):
    this = ctx.this_obj
    if isinstance(this, _GS1ObjectRef) and this.kind == "player":
        return this.target
    return ctx.player


def _floor_index(value):
    """DoubleAsIntegralFloor<size_t>: a negative index wraps to a huge
    value, i.e. never in range."""
    number = math.floor(to_num(value))
    return number if number >= 0 else None


# -- join -------------------------------------------------------------------

def _c_join(self, a, npc, player, ctx):
    # join class; (GS1Commands.cpp:1597 fn_join -> NPC::joinClass,
    # NPC.cpp:854). Idempotent; an unknown class is only logged
    # (NPC.cpp:872 "tried to join class ... but it does not exist").
    if len(a) != 1:
        return
    target = _source_npc(ctx)
    if target is None:
        return
    name = to_str(a[0])
    joined = target.__dict__.setdefault("_gs1_joined", [])
    if name in joined:
        return
    manager = getattr(self.server, "gs2_manager", None)
    if manager is None or manager.get_class(name) is None:
        logger.warning("NPC %s '%s' tried to join class '%s', but it does "
                       "not exist.", getattr(target, "id", "?"),
                       getattr(target, "name", ""), name)
        return
    joined.append(name)
    # The functions are callable straight away, even later in this run.
    if target is _running_npc(ctx):
        merge_joined_functions(target, self.server, ctx.functions)


# -- callnpc ----------------------------------------------------------------

def _c_callnpc(self, a, npc, player, ctx):
    # callnpc index,eventname,params; (GS1Commands.cpp:760 fn_callnpc):
    # queue a custom event on the index-th NPC of the script's level. `created`
    # may not be called ("Disabled in client version 2.10"). The initiator is
    # the calling NPC, so the target runs with no acting player.
    global _inline_depth
    if len(a) < 2:
        return
    level = self._level_of(ctx)
    manager = getattr(self.server, "npc_manager", None)
    if level is None or manager is None:
        return
    index = _floor_index(a[0])
    event = to_str(a[1])
    params_csv = to_str(a[2]) if len(a) > 2 else None
    if params_csv is None and "," in event:
        # The prototype is "RS" and a trailing string keeps its commas
        # (GS1Lexer.g4 "Last string? Commas are included!"), so the
        # documented `callnpc i,event,p1,p2` form reaches us as one string.
        # fn_callnpc reads the params from a third argument; split the
        # documented form the same way (scripting-gs1-commands.md:139-161).
        event, _, params_csv = event.partition(",")
    event = event.strip()
    if event.lower() == "created":
        return
    params = []
    if params_csv is not None:
        from ...gs2 import parse_csv_tolerant
        params = parse_csv_tolerant(params_csv)
    npcs = gs1_objects._level_npcs(level)
    if index is None or index >= len(npcs):
        return
    target = npcs[index]
    if getattr(target, "gs1_program", None) is None:
        return
    # The reference queues the event and runs it with the target's next
    # server frame (NPC::executeEvents), i.e. at most once per tick. A script
    # that calls itself (`if (ping) { callnpc 0,ping; }`) is therefore rate
    # limited to the tick, and the per-NPC backlog is capped.
    if getattr(target, "_gs1_callnpc_pending", 0) >= _CALLNPC_QUEUE_CAP:
        return

    def fire():
        target._gs1_callnpc_pending = max(
            0, getattr(target, "_gs1_callnpc_pending", 1) - 1)
        # destroyed / unloaded while queued
        if getattr(target, "level", None) is None:
            return
        registry = getattr(manager, "_npcs", None)
        if isinstance(registry, dict) and registry.get(target.id) is not target:
            return
        manager._fire_gs1(target, event, None, params=params)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop (unit contexts): the event queue is the loop, so run it now
        # unless it would re-enter the script that is executing, with a
        # nesting bound against two NPCs calling each other.
        if target is _running_npc(ctx) or _inline_depth >= _CALLNPC_QUEUE_CAP:
            return
        _inline_depth += 1
        try:
            target._gs1_callnpc_pending = getattr(
                target, "_gs1_callnpc_pending", 0) + 1
            fire()
        finally:
            _inline_depth -= 1
        return
    target._gs1_callnpc_pending = getattr(target, "_gs1_callnpc_pending", 0) + 1
    loop.call_later(_SERVER_TICK, fire)


_CALLNPC_QUEUE_CAP = 8
_SERVER_TICK = 0.05  # the 20 Hz tick the combat/NPC loops run on
_inline_depth = 0


# -- removebomb ---------------------------------------------------------------

def _c_removebomb(self, a, npc, player, ctx):
    # removebomb index; (GS1Commands.cpp:1933 -> Level::removeBomb
    # (inform_client), Level.cpp:2119): drop the index-th level bomb and send
    # PLO_BOMBDEL for its position. Out of range is a no-op.
    if len(a) != 1:
        return
    level = self._level_of(ctx)
    cm = getattr(self.server, "combat_manager", None)
    index = _floor_index(a[0])
    if level is None or cm is None or index is None:
        return
    bombs = cm.get_bombs_on_level(level.name)
    if index >= len(bombs):
        return
    bomb = bombs[index]
    cm._bombs.get(level.name, {}).pop(bomb.id, None)
    from ...protocol.packets import build_bomb_del
    _schedule(self.server.broadcast_to_level(
        level.name, build_bomb_del(bomb.x, bomb.y)))


# -- takeplayerhorse ----------------------------------------------------------

def _c_takeplayerhorse(self, a, npc, player, ctx):
    # takeplayerhorse; (GS1Commands.cpp:3527): set the player's HORSEIMAGE
    # prop to "" so the client stops drawing the horse. pygserver also keeps
    # the mount in HorseManager (playeronhorse), so release that too.
    target = _source_player(ctx)
    if target is None:
        return
    dirty = getattr(target, "_gs1_dirty_props", None)
    if dirty is None:
        dirty = {}
        target._gs1_dirty_props = dirty
    dirty[PLPROP.HORSEGIF] = ""
    written = getattr(ctx, "written_players", None)
    if written is None:
        written = ctx.written_players = {}
    written[id(target)] = target  # flushed with the rest of the event
    hm = getattr(self.server, "horse_manager", None)
    mounted = getattr(hm, "_mounted", None)
    if isinstance(mounted, dict):
        horse = mounted.pop(getattr(target, "id", None), None)
        if horse is not None:
            horse.rider_id = None
            _schedule(hm.remove_horse(horse.level_name, horse.id))


# -- timershow ----------------------------------------------------------------

_TIMERSHOW = 0b1000  # NPCVisFlags::TIMERSHOW (NPC.h:163)


def _c_timershow(self, a, npc, player, ctx):
    # timershow; (GS1Commands.cpp:3555): OR TIMERSHOW into the NPC's VISFLAGS
    # and send the prop, so the client shows the NPC's timeout counter.
    target = _source_npc(ctx)
    if target is None or not hasattr(target, "vis_flags"):
        return
    if target.vis_flags & _TIMERSHOW:
        return
    target.vis_flags |= _TIMERSHOW
    level = getattr(target, "level", None)
    if level is None or self.server is None:
        return
    from ...protocol.packets import build_npc_props
    wire = (target.vis_flags & ~1) | (1 if getattr(target, "visible", True) else 0)
    _schedule(self.server.broadcast_to_level(
        level.name, build_npc_props(target.id, {NPCPROP.VISFLAGS: wire})))


# -- shootfireball / shootfireblast / shootnuke ---------------------------------

_ARROW_FIREBALL, _ARROW_FIREBLAST, _ARROW_NUKE = 2, 3, 4  # LevelArrow.h:41-45
_ARROW_SPRITE = 107  # arrowSpriteIndex (LevelArrow.h:39)


def _shoot_arrow(self, a, ctx, arrow_type):
    # fn_shootfireball/fireblast/nuke (GS1Commands.cpp:3058-3125): addArrow
    # (inform_client) from the running NPC, PLO_ARROWADD with owner 0,
    # flags = dir, sprite = arrowSpriteIndex + dir, trailing byte = type
    # (Level.cpp:1866-1885). Only the broadcast is modelled; pygserver keeps
    # no server-side flight for script arrows, same as shootarrow.
    if len(a) != 1:
        return
    npc = _running_npc(ctx)
    level = self._level_of(ctx)
    if npc is None or level is None or self.server is None:
        return
    direction = math.floor(to_num(a[0])) & 0xFF
    x, y = to_num(getattr(npc, "x", 0)), to_num(getattr(npc, "y", 0))
    # getPositionForArrow (GS1Commands.cpp:621): character NPCs (image
    # "#c#", NPC::isCharacter) fire from (1 + dX, 1.5 + dY) tiles past their
    # origin.
    if getattr(npc, "image", "") in ("#c#", "#c"):
        x += 1.0 + (-1.5 if direction == 1 else 1.5 if direction == 3 else 0.0)
        y += 1.5 + (-1.5 if direction == 0 else 1.5 if direction == 2 else 0.0)
    from ...protocol.packets import build_arrow_add
    _schedule(self.server.broadcast_to_level(level.name, build_arrow_add(
        0, math.floor(x * 2) / 2, math.floor(y * 2) / 2, direction & 3,
        _ARROW_SPRITE + (direction & 3), arrow_type)))


def _c_shootfireball(self, a, npc, player, ctx):
    _shoot_arrow(self, a, ctx, _ARROW_FIREBALL)


def _c_shootfireblast(self, a, npc, player, ctx):
    _shoot_arrow(self, a, ctx, _ARROW_FIREBLAST)


def _c_shootnuke(self, a, npc, player, ctx):
    _shoot_arrow(self, a, ctx, _ARROW_NUKE)


_COMMANDS = {
    "join": _c_join,
    "callnpc": _c_callnpc,
    "removebomb": _c_removebomb,
    "takeplayerhorse": _c_takeplayerhorse,
    "timershow": _c_timershow,
    "shootfireball": _c_shootfireball,
    "shootfireblast": _c_shootfireblast,
    "shootnuke": _c_shootnuke,
}
