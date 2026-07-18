"""Regression tests for the death -> respawn flow: a player who dies must go
through the same old-level leave / new-level arrive plumbing as a normal
PLI_LEVELWARP, not just have its coordinates silently reassigned.

Live 2-bot testing found that for several seconds after a cross-level death
respawn, the surviving player's roster of the old level still contained the
dead/respawned player (no PLO_PLAYERLEFT ever arrived), and the respawned
player's own level/roster stayed stale until an unrelated later warp fixed
it up. combat.py's _respawn_player already calls the real Player.warp() (the
same method PLI_LEVELWARP uses), which does drive the leave/arrive flow
correctly - but the respawn task was fired with a bare
`asyncio.create_task(...)` and no reference kept anywhere, so it was liable
to be garbage collected by the event loop before it completed (asyncio docs:
"save a reference to the result of this function"). That silently dropped
the respawn's warp() call, leaving both players in the stale state seen live.
CombatManager now keeps a strong reference to the respawn task in
`_respawn_tasks` until it completes.

Follows the FakeServer/FakePlayer-via-real-classes idiom used elsewhere in
this suite (test_gs1_hit_events.py's FakeServer wires real
NPCManager/BaddyManager/CombatManager/HorseManager against a fake world;
test_profile_and_misc_handlers.py's _make_player builds a real pygserver.player.Player
against a mocked reader/writer) rather than faking Player itself, since the
whole point here is Player.warp()'s real leave/arrive side effects.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from pygserver.baddy import BaddyManager
from pygserver.combat import CombatManager
from pygserver.horse import HorseManager
from pygserver.level import Level
from pygserver.npc import NPCManager
from pygserver.player import Player


class FakeWorld:
    """Minimal World stand-in: same get_level/get_gmap_for_level contract as
    pygserver.world.World, without needing a real server.config."""

    def __init__(self):
        self._levels = {}

    def get_level(self, name):
        return self._levels.get(name)

    def get_gmap_for_level(self, name):
        return None


class FakeConfig:
    start_level = "chicken1.nw"
    start_x = 30.0
    start_y = 30.5


class FakeServer:
    """Real managers (NPC/Baddy/Horse/Combat) wired against a fake world, so
    Player.warp()'s side effects (npc events, horse-warp handling) run for
    real - only the world/level lookup and socket I/O are faked."""

    def __init__(self):
        self.world = FakeWorld()
        self.config = FakeConfig()
        self.players = {}
        self.npc_manager = NPCManager(self)
        self.baddy_manager = BaddyManager(self)
        self.horse_manager = HorseManager(self)
        self.combat_manager = None  # set by caller once CombatManager exists

    def get_player(self, pid):
        return self.players.get(pid)

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        exclude = exclude or set()
        for p in list(self.players.values()):
            if p.logged_in and p.level and p.level.name == level_name and p.id not in exclude:
                await p.send_raw(packet)


def make_player(server, pid):
    """Build a real Player against a mocked socket, matching
    test_profile_and_misc_handlers.py's _make_player idiom."""
    reader = AsyncMock()
    writer = MagicMock()
    player = Player(server, pid, reader, writer)
    player.logged_in = True
    player.account_name = f"acct{pid}"
    player.nickname = f"nick{pid}"
    player.send_raw = AsyncMock()
    return player


def make_two_player_level(server, level_name="onlinestartlocal.nw"):
    """Two fake players standing on the same level, per the multi-player
    fixture idiom used by test_gs1_hit_events.py's make_server_and_level."""
    level = Level(level_name)
    server.world._levels[level_name] = level

    survivor = make_player(server, 1)
    mover = make_player(server, 2)
    server.players[survivor.id] = survivor
    server.players[mover.id] = mover

    for p in (survivor, mover):
        p.level = level
        level.add_player(p)

    return level, survivor, mover


def test_death_respawn_moves_player_through_leave_arrive_flow():
    async def main():
        server = FakeServer()
        combat = CombatManager(server)
        combat.respawn_time = 0.01  # keep the test fast
        server.combat_manager = combat

        old_level, survivor, mover = make_two_player_level(server, "onlinestartlocal.nw")
        new_level = Level("chicken1.nw")
        server.world._levels[new_level.name] = new_level

        mover.hearts = 0.5  # one hit from death

        await combat.handle_player_death(mover, killer_id=None)
        await asyncio.sleep(0.05)  # let the respawn task's warp() complete

        # Survivor's roster of the old level no longer contains the dead/moved
        # player - the leave broadcast/roster update ran, not just a silent
        # coordinate change.
        assert mover.id not in old_level.get_player_ids()

        # The mover's own visible set is the *new* level's, not a stale mix of
        # both - Player.warp() attached it to the new level and sent it that
        # level's roster.
        assert mover.level is new_level
        assert mover.id in new_level.get_player_ids()
        assert mover.id not in old_level.get_player_ids()

        # Survivor is untouched: still on the old level.
        assert survivor.level is old_level
        assert survivor.id in old_level.get_player_ids()

    asyncio.run(main())


def test_respawn_task_is_kept_alive_against_gc():
    """The respawn task must be referenced somewhere so the event loop's
    weak-reference-only bookkeeping can't collect it mid-flight (asyncio
    docs: tasks not referenced elsewhere may be garbage collected before
    they're done). This is what actually caused the live stale-ghost bug:
    the old bare `asyncio.create_task(...)` held no reference at all.
    """
    async def main():
        server = FakeServer()
        combat = CombatManager(server)
        combat.respawn_time = 0.01
        server.combat_manager = combat

        old_level, survivor, mover = make_two_player_level(server, "onlinestartlocal.nw")
        new_level = Level("chicken1.nw")
        server.world._levels[new_level.name] = new_level

        mover.hearts = 0.5
        await combat.handle_player_death(mover, killer_id=None)

        # The task must be tracked (not just fire-and-forget) while it's pending.
        assert len(combat._respawn_tasks) == 1

        await asyncio.sleep(0.05)

        # ... and dropped from tracking once it completes, so it doesn't stick
        # around forever.
        assert len(combat._respawn_tasks) == 0
        assert mover.level is new_level

    asyncio.run(main())
