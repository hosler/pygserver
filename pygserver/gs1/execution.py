from __future__ import annotations

import asyncio
import logging

from reborn_protocol.gs1 import ast as gs1_ast
from reborn_protocol.gs1.runtime import Context, VarStore
from reborn_protocol.gs1.lexer import tokenize
from reborn_protocol.gs1.parser import Parser, parse

from .interpreter import ServerInterpreter
from .player_flags import PlayerVarStore

logger = logging.getLogger(__name__)


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


def compile_gs1(code: str, raise_errors: bool = False):
    """Parse GS1 source into a Program AST (None on hard failure)."""
    try:
        if raise_errors:
            parser = Parser(tokenize(code))
            program = parser.parse_program()
            if parser.errors:
                raise parser.errors[0]
            return program
        return parse(code)
    except Exception:
        if raise_errors:
            raise
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
    __slots__ = ("resumable", "ctx", "vars", "player", "this_obj", "level",
                 "hit_source", "charprop_source", "tokenize_tokens",
                 "functions")

    def __init__(self, resumable, ctx):
        self.resumable = resumable
        self.ctx = ctx
        self.vars = ctx.vars
        self.player = ctx.player
        self.this_obj = ctx.this_obj
        self.level = getattr(ctx.this_obj, "level", None)
        self.hit_source = ctx.hit_source
        self.charprop_source = ctx.charprop_source
        self.tokenize_tokens = ctx.tokenize_tokens
        # A joined class runs against its own function table (see
        # _run_joined_classes), so a resume must put that table back.
        self.functions = ctx.functions

    def apply(self):
        ctx = self.ctx
        ctx.functions = self.functions
        ctx.vars = self.vars
        ctx.player = self.player
        ctx.this_obj = self.this_obj
        if self.this_obj is not None:
            self.this_obj.level = self.level
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
                         carryobject_type=None, params=None):
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
    player_flags = getattr(player, "flags", None)
    if player_flags is None:
        player_flags = _lazy(player, "_gs1_flags")
    scopes = {
        "this": sc["this"], "thiso": sc["thiso"],
        "local": {}, "temp": {},
        "client": player_flags,
        "server": (server.server_flags if hasattr(server, "server_flags")
                   else _lazy(server, "_gs1_server")),
        "level": _lazy(level, "_gs1_vars"),
        "global": _lazy(server, "_gs1_global"),
    }
    ctx.vars = PlayerVarStore(player, scopes=scopes, player_flags=player_flags)
    ctx.this_obj = npc
    ctx.player = player
    ctx.hit_source = source
    ctx.carryobject_type = (int(carryobject_type)
                            if carryobject_type is not None else None)
    ctx.active_event = event
    ctx.action_params = list(params or ())
    ctx.tokenize_tokens = []
    ctx.charprop_source = None
    ctx.steps = 0
    ctx.written_players = {}


def run_npc_event(npc, event: str, server=None, player=None, source=None,
                  carryobject_type=None, params=None):
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

    Classes the script `join`ed run the same event after the NPC's own script
    (NPC.cpp:678-693 NPC::executeEvents), see _run_joined_classes.
    """
    prog = getattr(npc, "gs1_program", None)
    if prog is None:
        return None
    ctx = _run_npc_program(npc, prog, event, server, player, source,
                           carryobject_type, params)
    if getattr(npc, "_gs1_joined", None):
        _run_joined_classes(npc, ctx, event, server, player, source,
                            carryobject_type, params)
    return ctx


def _run_npc_program(npc, prog, event, server, player, source,
                     carryobject_type, params):
    """The NPC's own script for one event (the body of run_npc_event)."""

    from .host import GS1Host
    host = getattr(server, "gs1_host", None) or GS1Host(server)
    pending = getattr(npc, "_gs1_pending", None)
    if pending is not None and pending.resumable.done:
        pending = None
        npc._gs1_pending = None

    if event == "timeout" and pending is not None:
        ctx = pending.ctx
        pending.apply()
        ctx.written_players = {}
        try:
            pending.resumable.resume()
        except Exception as e:
            from .host import _report_gs1_error
            _report_gs1_error(f"event timeout resume on npc {getattr(npc, 'id', '?')}", e)
            npc._gs1_pending = None
            _flush_event_players(ctx, pending.player)
            return ctx
        if pending.resumable.done:
            npc._gs1_pending = None
        elif hasattr(npc, "set_timer"):
            npc.set_timer(pending.resumable.pending_sleep)
        _flush_event_players(ctx, pending.player)
        return ctx

    ctx = _ensure_gs1_ctx(npc, host)
    _bind_fresh_gs1_call(ctx, npc, server, player, event, source,
                         carryobject_type, params)
    ctx.functions = _main_functions(ctx)
    merge_joined_functions(npc, server, ctx.functions)
    try:
        resumable = ServerInterpreter(ctx).run_event_resumable(prog, event)
    except Exception as e:
        from .host import _report_gs1_error
        _report_gs1_error(f"event {event} on npc {getattr(npc, 'id', '?')}", e)
        error_handler = getattr(npc, "_gs1_error_handler", None)
        if error_handler is not None:
            error_handler(str(e))
        _flush_event_players(ctx, player)
        return ctx
    if not resumable.done:
        npc._gs1_pending = _PendingGS1Sleep(resumable, ctx)
        if hasattr(npc, "set_timer"):
            npc.set_timer(resumable.pending_sleep)
    _flush_event_players(ctx, player)
    return ctx


# -- joined script classes ---------------------------------------------------
# `join <class>` (GS1Commands.cpp:1597 fn_join -> NPC::joinClass,
# NPC.cpp:854) attaches a server script class to the NPC. The reference then
# (a) runs every event through each joined class's script after the NPC's own
# script (NPC::executeEvents, NPC.cpp:678-693) and (b) resolves a user
# function the NPC's script does not define in its joined classes
# (GS1Visitor::visitStatementUserFunctionCall, GS1Visitor.cpp:1033-1044).

def class_program(server, name):
    """Compiled serverside GS1 half of the script class `name`, or None.

    The program is cached on the class's script object (a reloaded class is a
    new object, so edits take effect). False marks "not GS1 / empty"."""
    manager = getattr(server, "gs2_manager", None)
    getter = getattr(manager, "get_class", None)
    script = getter(name) if getter is not None else None
    if script is None:
        return None
    cached = getattr(script, "_gs1_class_program", None)
    if cached is not None:
        return cached or None
    from reborn_protocol.gs2.detect import looks_like_gs2
    from ..npc import split_gs1_script
    server_half = split_gs1_script(getattr(script, "source", "") or "", True)[0]
    program = None
    if server_half.strip() and not looks_like_gs2(server_half):
        program = compile_gs1(server_half)
    script._gs1_class_program = program if program is not None else False
    return program


def _main_functions(ctx):
    """The NPC script's own user-function table (stable per Context)."""
    table = getattr(ctx, "_gs1_main_functions", None)
    if table is None:
        table = ctx.functions
        ctx._gs1_main_functions = table
    return table


def merge_joined_functions(npc, server, table):
    """Add the joined classes' functions to `table` without overriding a
    function that is already defined there (own script wins)."""
    for name in getattr(npc, "_gs1_joined", None) or ():
        program = class_program(server, name)
        if program is None:
            continue
        for stmt in program.body:
            if isinstance(stmt, gs1_ast.FuncDef):
                table.setdefault(stmt.name, stmt)


def _run_joined_classes(npc, ctx, event, server, player, source,
                        carryobject_type, params):
    pending = npc.__dict__.setdefault("_gs1_class_pending", {})
    main = _main_functions(ctx)
    try:
        for name in list(npc._gs1_joined):
            program = class_program(server, name)
            if program is None:
                continue
            held = pending.get(name)
            if held is not None and held.resumable.done:
                pending.pop(name, None)
                held = None
            try:
                if event == "timeout" and held is not None:
                    ctx.written_players = {}
                    held.apply()
                    held.resumable.resume()
                    if held.resumable.done:
                        pending.pop(name, None)
                    elif hasattr(npc, "set_timer"):
                        npc.set_timer(held.resumable.pending_sleep)
                    _flush_event_players(ctx, held.player)
                    continue
                _bind_fresh_gs1_call(ctx, npc, server, player, event, source,
                                     carryobject_type, params)
                table = {}
                merge_joined_functions(npc, server, table)
                ctx.functions = table
                resumable = ServerInterpreter(ctx).run_event_resumable(
                    program, event)
                if not resumable.done:
                    pending[name] = _PendingGS1Sleep(resumable, ctx)
                    if hasattr(npc, "set_timer"):
                        npc.set_timer(resumable.pending_sleep)
                _flush_event_players(ctx, player)
            except Exception as e:
                from .host import _report_gs1_error
                _report_gs1_error(
                    f"event {event} in class {name} on npc "
                    f"{getattr(npc, 'id', '?')}", e)
                pending.pop(name, None)
    finally:
        ctx.functions = main


def _flush_event_players(ctx, acting_player):
    """Flush every player mutated during this execution slice."""
    players = dict(getattr(ctx, "written_players", {}) or {})
    if acting_player is not None:
        players[id(acting_player)] = acting_player
    ctx.written_players = {}
    server = getattr(ctx.host, "server", None)
    for player in players.values():
        _flush_player_props(player, server)


# Inventory counters stay with the owner; everything else a script
# changes is also relayed to the other clients, as the reference does for each
# modified prop (PlayerProps.cpp:1217-1239 sendPropsFromResults: NICKNAME to
# everyone, the rest to the nearby level).

def _other_form(player, prop_id, value):
    """The OTHERPLPROPS value for `prop_id` (gear carries its image)."""
    from ..protocol.constants import PLPROP
    if prop_id == PLPROP.SWORDPOWER:
        return (getattr(player, "sword_power", 0), getattr(player, "sword_image", ""))
    if prop_id == PLPROP.SHIELDPOWER:
        return (getattr(player, "shield_power", 0), getattr(player, "shield_image", ""))
    return value


async def _send_player_props(player, server, dirty):
    from ..protocol.constants import PLPROP
    from ..protocol.packets import build_other_player_props
    await player.send_props(dirty)
    if server is None:
        server = getattr(player, "server", None)
    private = {PLPROP.RUPEESCOUNT, PLPROP.ARROWSCOUNT, PLPROP.BOMBSCOUNT}
    others = {pid: _other_form(player, pid, value)
              for pid, value in dirty.items() if pid not in private}
    if not others or server is None:
        return
    me = getattr(player, "id", None)
    exclude = {me} if me is not None else None
    if PLPROP.NICKNAME in others and hasattr(server, "broadcast_to_all"):
        await server.broadcast_to_all(build_other_player_props(
            me, {PLPROP.NICKNAME: others.pop(PLPROP.NICKNAME)}), exclude=exclude)
    level = getattr(player, "level", None)
    if not others or level is None:
        return
    packet = build_other_player_props(me, others)
    cast = (getattr(server, "broadcast_to_world", None)
            or getattr(server, "broadcast_to_level", None))
    if cast is not None:
        await cast(level.name, packet, exclude=exclude)


def _flush_player_props(player, server=None):
    """Send any player stat changes a script made: to that player's client and
    to the other clients that can see the player."""
    if player is None:
        return
    dirty = getattr(player, "_gs1_dirty_props", None)
    if not dirty:
        return
    player._gs1_dirty_props = {}
    if not hasattr(player, "send_props"):
        return
    try:
        asyncio.get_running_loop().create_task(
            _send_player_props(player, server, dirty))
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
