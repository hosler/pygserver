"""Focused unit tests for gs1_host.py's adoption of reborn_protocol's
resumable-sleep API (Interpreter.run_event_resumable / ResumableExecution).

The end-to-end scenario (a real level's `sleep`-driven walk-away NPC) is
covered by tests/test_beer_quest_unblock.py; these tests isolate the three
pieces of the design called out in run_npc_event's docstring:

  1. a `sleep` inside a loop suspends and genuinely progresses across
     multiple NPC timer ticks (not a one-shot loop-break);
  2. a bare `timeout = x;` assignment from ANY handler cancels a pending
     sleep (Context.sleep_cancelled, consumed by ResumableExecution.resume());
  3. a second, unrelated event firing while a sleep is pending runs
     "alongside" it (fresh, immediately) rather than being queued or
     dropped, and does not disturb the pending sleep UNLESS it suspends on
     its own sleep, which replaces the pending one - both confirmed against
     the real GServer-v2 oracle by reborn-protocol/tests/test_gs1_sleep_resume.py's
     `_drive_resumable`/TestOracleSleepResume (see run_npc_event's docstring
     for the exact citation).

Uses the same FakeServer/FakePlayer/FakeLevel idiom as test_gs1_host.py /
test_beer_quest_unblock.py, driven through the real NPCManager.tick() path
(not gs1_host.run_npc_event called directly) so the NPC-timer scheduling
that actually makes sleep-resume work is exercised end to end.
"""
import asyncio

from pygserver.npc import NPCManager


class FakePlayer:
    def __init__(self, pid=1):
        self.id = pid
        self.x = 0.0
        self.y = 0.0
        self.direction = 2
        self.flags = {}
        self.chat = ""

    def mark_dirty(self):
        pass


class FakeLevel:
    name = "testlevel"

    def __init__(self):
        self._npcs = []
        self._player_ids = set()

    def add_npc(self, npc):
        npc.level = self
        self._npcs.append(npc)

    def remove_npc(self, npc):
        npc.level = None
        if npc in self._npcs:
            self._npcs.remove(npc)

    def get_player_ids(self):
        return self._player_ids


class FakeServer:
    def __init__(self):
        self.npc_manager = NPCManager(self)
        self._players = {}
        self.broadcasts = []

    def get_player(self, pid):
        return self._players.get(pid)

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet, exclude))


def _make_server_npc(code, x=10.0, y=10.0, with_player=True):
    server = FakeServer()
    level = FakeLevel()
    npc = server.npc_manager.create_npc(name="t", level=level, x=x, y=y)
    if with_player:
        player = FakePlayer()
        player.level = level
        level._player_ids.add(player.id)
        server._players[player.id] = player
    server.npc_manager.attach_gs1(npc, code)
    return server, level, npc


async def _force_tick(server):
    """Fire every NPC's overdue timer once, through the real tick() path."""
    for npc in list(server.npc_manager._npcs.values()):
        npc.set_timer(-1.0)
    await server.npc_manager.tick()
    await asyncio.sleep(0)


def _this(npc, key, default=0.0):
    return npc.gs1_scopes["this"].get(key, default)


# -- 1: sleep-in-loop progresses across ticks --------------------------------
def test_sleep_in_loop_progresses_across_ticks():
    code = """
        if (created) { timeout = .05; }
        if (timeout) {
          if (this.walked != 1) {
            this.n = 0;
            while (this.n < 3) {
              this.n = this.n + 1;
              x = x + 1;
              sleep .05;
            }
            this.walked = 1;
          }
        }
    """

    async def main():
        server, level, npc = _make_server_npc(code, x=10.0, y=10.0)
        start_x = npc.x

        # 1st timeout tick: fresh execution, suspends at the FIRST sleep
        # after the first loop iteration (this.n == 1, x already bumped once).
        await _force_tick(server)
        assert _this(npc, "n") == 1.0
        assert npc.x == start_x + 1
        assert npc._gs1_pending is not None and not npc._gs1_pending.resumable.done

        # 2nd/3rd timeout ticks: RESUME the same suspended execution, each
        # progressing exactly one more loop iteration - this is the
        # "multiple distinct positions over time" behaviour, not a one-tile
        # jump.
        await _force_tick(server)
        assert _this(npc, "n") == 2.0
        assert npc.x == start_x + 2

        await _force_tick(server)
        assert _this(npc, "n") == 3.0
        assert npc.x == start_x + 3

        # 4th tick: the loop condition is now false, falls through to
        # `this.walked = 1;` with no further sleep -> the execution
        # completes and the pending sleep is cleared.
        await _force_tick(server)
        assert _this(npc, "walked") == 1.0
        assert npc._gs1_pending is None

    asyncio.run(main())


# -- 2: `timeout = x;` cancels a pending sleep -------------------------------
def test_bare_timeout_assignment_cancels_pending_sleep():
    code = """
        if (created) { timeout = .05; }
        if (timeout) {
          if (this.walked != 1) {
            this.n = 0;
            while (this.n < 3) {
              this.n = this.n + 1;
              sleep .05;
            }
            this.walked = 1;
          }
        }
        if (playerchats) {
          timeout = 5;
        }
    """

    async def main():
        server, level, npc = _make_server_npc(code)
        player = next(iter(server._players.values()))
        player.level = level

        await _force_tick(server)  # suspends with this.n == 1
        assert _this(npc, "n") == 1.0
        assert npc._gs1_pending is not None and not npc._gs1_pending.resumable.done

        # An UNRELATED event (`playerchats`) reprograms the NPC's timer via
        # a bare, plain `timeout = 5;` assignment - this must cancel the
        # pending sleep (Context.sleep_cancelled), not just coexist with it.
        player.chat = "hi"
        await server.npc_manager.on_player_chats(player, "hi")

        # The next `timeout` tick must NOT resume the old walk - the
        # cancellation is consumed as soon as .resume() is called, and the
        # execution is marked done without running the loop's remaining
        # iterations.
        await _force_tick(server)
        assert npc._gs1_pending is None
        assert _this(npc, "n") == 1.0, "cancelled sleep must not have resumed"
        assert _this(npc, "walked") == 0.0, "walked must never be set - loop never finished"

    asyncio.run(main())


def test_timeout_compound_assignment_does_not_cancel_pending_sleep():
    # Upstream gates the cancellation on OP_ASSIGN only (GS1Visitor.cpp
    # visitStatementAssignment / reborn_protocol's Interpreter._is_bare_timeout);
    # `timeout += x` must NOT cancel a pending sleep.
    code = """
        if (created) { timeout = .05; }
        if (timeout) {
          if (this.walked != 1) {
            this.n = 0;
            while (this.n < 3) {
              this.n = this.n + 1;
              sleep .05;
            }
            this.walked = 1;
          }
        }
        if (playerchats) {
          timeout += 5;
        }
    """

    async def main():
        server, level, npc = _make_server_npc(code)
        player = next(iter(server._players.values()))
        player.level = level

        await _force_tick(server)  # this.n == 1, suspended
        player.chat = "hi"
        await server.npc_manager.on_player_chats(player, "hi")

        await _force_tick(server)
        assert _this(npc, "n") == 2.0, "compound += must not have cancelled the sleep"
        assert npc._gs1_pending is not None

    asyncio.run(main())


# -- 3: a second event during a pending sleep runs fresh alongside it -------
def test_unrelated_event_during_pending_sleep_runs_fresh_and_leaves_it_intact():
    code = """
        if (created) { timeout = .05; }
        if (timeout) {
          if (this.walked != 1) {
            this.n = 0;
            while (this.n < 3) {
              this.n = this.n + 1;
              sleep .05;
            }
            this.walked = 1;
          }
        }
        if (playertouchsme) {
          this.touches = this.touches + 1;
        }
    """

    async def main():
        server, level, npc = _make_server_npc(code)
        toucher = FakePlayer(pid=2)
        toucher.level = level

        await _force_tick(server)  # this.n == 1, suspended (pending sleep)
        assert _this(npc, "n") == 1.0
        pending_before = npc._gs1_pending
        assert pending_before is not None

        # A completely unrelated event on the SAME NPC, while the sleep is
        # still pending, must run immediately (not queued/dropped) - and
        # must NOT disturb the pending walk.
        await server.npc_manager.on_player_touches(toucher, npc)
        assert _this(npc, "touches") == 1.0, "unrelated event must run fresh, right away"
        assert npc._gs1_pending is pending_before, "pending sleep must be untouched"

        # The pending sleep keeps progressing normally afterwards.
        await _force_tick(server)
        assert _this(npc, "n") == 2.0
        await _force_tick(server)
        assert _this(npc, "n") == 3.0
        await _force_tick(server)
        assert _this(npc, "walked") == 1.0
        assert npc._gs1_pending is None

        # Touching again after the walk finished still works (fresh event,
        # no pending left to interfere with).
        await server.npc_manager.on_player_touches(toucher, npc)
        assert _this(npc, "touches") == 2.0

    asyncio.run(main())


def test_unrelated_sleeping_event_replaces_the_pending_sleep():
    # GS1Visitor.cpp:757 (`m_sleepCallStack = std::move(m_callStack)`) is an
    # UNCONDITIONAL overwrite, not a queue: if a second, unrelated event that
    # ALSO sleeps fires while a sleep is already pending, it replaces the old
    # one outright - the old suspended execution is simply abandoned and
    # never resumes.
    code = """
        if (created) { timeout = .05; }
        if (timeout) {
          if (this.walked != 1) {
            this.n = 0;
            while (this.n < 3) {
              this.n = this.n + 1;
              sleep .05;
            }
            this.walked = 1;
          }
        }
        if (playerchats) {
          this.m = 0;
          while (this.m < 2) {
            this.m = this.m + 1;
            sleep .05;
          }
          this.replaced = 1;
        }
    """

    async def main():
        server, level, npc = _make_server_npc(code)
        player = next(iter(server._players.values()))
        player.level = level

        await _force_tick(server)  # this.n == 1, pending A (the walk)
        assert _this(npc, "n") == 1.0
        pending_a = npc._gs1_pending

        player.chat = "hi"
        await server.npc_manager.on_player_chats(player, "hi")  # this.m == 1, pending B (replaces A)
        assert _this(npc, "m") == 1.0
        assert npc._gs1_pending is not None
        assert npc._gs1_pending is not pending_a, "the new sleeping execution must replace the old one"

        # Further `timeout` ticks only ever resume the NEW pending (B) -
        # the walk (A, this.n) is abandoned and never progresses past 1.
        await _force_tick(server)
        assert _this(npc, "m") == 2.0
        assert _this(npc, "n") == 1.0, "the replaced execution must never resume"

        await _force_tick(server)
        assert _this(npc, "replaced") == 1.0
        assert _this(npc, "n") == 1.0, "the replaced execution must never resume"
        assert _this(npc, "walked") == 0.0, "the replaced execution never got to set this"

    asyncio.run(main())


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
