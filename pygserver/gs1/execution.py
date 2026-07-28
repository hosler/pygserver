from __future__ import annotations

import asyncio
import logging

from reborn_protocol.gs1.runtime import Context, VarStore
from reborn_protocol.gs1.interp import Interpreter
from reborn_protocol.gs1.parser import parse

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
    __slots__ = ("resumable", "ctx", "vars", "player", "this_obj", "level",
                 "hit_source", "charprop_source", "tokenize_tokens")

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

    def apply(self):
        ctx = self.ctx
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
    """
    prog = getattr(npc, "gs1_program", None)
    if prog is None:
        return None

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
    try:
        resumable = Interpreter(ctx).run_event_resumable(prog, event)
    except Exception as e:
        from .host import _report_gs1_error
        _report_gs1_error(f"event {event} on npc {getattr(npc, 'id', '?')}", e)
        _flush_event_players(ctx, player)
        return ctx
    if not resumable.done:
        npc._gs1_pending = _PendingGS1Sleep(resumable, ctx)
        if hasattr(npc, "set_timer"):
            npc.set_timer(resumable.pending_sleep)
    _flush_event_players(ctx, player)
    return ctx


def _flush_event_players(ctx, acting_player):
    """Flush every player mutated during this execution slice."""
    players = dict(getattr(ctx, "written_players", {}) or {})
    if acting_player is not None:
        players[id(acting_player)] = acting_player
    ctx.written_players = {}
    for player in players.values():
        _flush_player_props(player)


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
