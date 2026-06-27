"""Phase-5 integration: GS1 scripts driving real pygserver NPC/player objects.

Exercises the GS1Host bridge and the NPC event-firing path without needing the
full async server — a fake player/level is enough to verify attribute mapping,
command effects, prop-dirtying, event flags, and per-NPC state persistence.
"""
from pygserver.npc import NPC
from pygserver.gs1_host import compile_gs1, run_npc_event


class FakePlayer:
    def __init__(self):
        self.x = 10.0
        self.y = 12.0
        self.direction = 2
        self.rupees = 5
        self.hearts = 3.0
        self.account_name = "hosler"
        self.nickname = "Hos"
        self.chat = ""
        self.flags = {}

    def mark_dirty(self):
        pass


class FakeLevel:
    name = "testlevel"

    def is_blocking(self, x, y):
        return False

    def add_npc(self, npc):
        npc.level = self

    def remove_npc(self, npc):
        npc.level = None


def make_npc(code):
    npc = NPC(1, "t")
    npc.level = FakeLevel()
    npc.gs1_program = compile_gs1(code)
    return npc


def test_created_initialises_npc_state():
    npc = make_npc("if (created) { this.hits = 0; setimg statue.png; }")
    run_npc_event(npc, "created", None, None)
    assert npc.gs1_scopes["this"]["hits"] == 0.0
    assert npc.image == "statue.png"
    assert npc._dirty is True


def test_touch_mutates_npc_and_player():
    npc = make_npc(
        "if (playertouchsme) { this.hits += 1; setimg hit.png; "
        "message touched #v(this.hits); playerrupees = playerrupees + 10; }")
    run_npc_event(npc, "created", None, None)
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.image == "hit.png"
    assert npc.message == "touched 1"
    assert p.rupees == 15.0
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "touched 2"     # this.* persisted across events
    assert p.rupees == 25.0


def test_compound_event_condition():
    npc = make_npc("if (playerchats && strequals(#c,hello)) { message hi back; }")
    p = FakePlayer()
    p.chat = "hello"
    run_npc_event(npc, "playerchats", None, p)
    assert npc.message == "hi back"
    npc.message = "(none)"
    p.chat = "goodbye"
    run_npc_event(npc, "playerchats", None, p)
    assert npc.message == "(none)"        # condition false -> skipped


def test_event_flag_isolation():
    # created block must not run when a different event fires
    npc = make_npc("if (created) { this.v = 99; } if (playerenters) { this.v = 1; }")
    run_npc_event(npc, "created", None, None)
    assert npc.gs1_scopes["this"]["v"] == 99.0
    run_npc_event(npc, "playerenters", None, FakePlayer())
    assert npc.gs1_scopes["this"]["v"] == 1.0


def test_player_flags_persist_on_player():
    npc = make_npc("if (playertouchsme) { set talkedto; setstring questname,dragon; }")
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.flags.get("talkedto") == 1.0
    assert p.flags.get("questname") == "dragon"


def test_message_codes_from_player():
    npc = make_npc("if (playertouchsme) { message Hello #n from #a; }")
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "Hello Hos from hosler"


def test_timeout_attribute_sets_timer():
    npc = make_npc("if (created) { timeout = 5; }")
    run_npc_event(npc, "created", None, None)
    assert npc._timer_end > 0   # set_timer scheduled a timeout


def test_bad_command_does_not_crash():
    npc = make_npc("if (created) { nonexistentcommand 1,2,3; setimg ok.png; }")
    run_npc_event(npc, "created", None, None)
    assert npc.image == "ok.png"  # execution continued past the unknown command


# -- NPCManager wiring (the server-side path added in Phase 5) ---------------
import asyncio

from pygserver.npc import NPCManager


class StubServer:
    async def broadcast_to_level(self, *a, **k):
        pass


def test_npcmanager_attach_and_touch_and_chat():
    async def main():
        level = FakeLevel()
        mgr = NPCManager(StubServer())
        npc = mgr.create_npc(name="levelnpc", level=level, x=20.0, y=20.0)
        mgr.attach_gs1(npc, (
            "if (created) { this.hits = 0; }"
            "if (playertouchsme) { this.hits += 1; message touched; }"
            "if (playerchats && strequals(#c,hi)) { message heard hi; }"))
        assert npc.gs1_scopes["this"]["hits"] == 0.0  # created fired

        p = FakePlayer()
        p.level = level
        p.x, p.y = 20.0, 20.0  # standing on the NPC

        await mgr.check_touches(p)
        assert npc.message == "touched" and npc.gs1_scopes["this"]["hits"] == 1.0
        # standing still on it must not re-fire
        await mgr.check_touches(p)
        assert npc.gs1_scopes["this"]["hits"] == 1.0
        # walk away then back -> fires again
        p.x = 40.0
        await mgr.check_touches(p)
        p.x = 20.0
        await mgr.check_touches(p)
        assert npc.gs1_scopes["this"]["hits"] == 2.0

        p.chat = "hi"
        await mgr.on_player_chats(p, "hi")
        assert npc.message == "heard hi"

    asyncio.run(main())
