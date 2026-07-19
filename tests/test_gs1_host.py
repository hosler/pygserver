"""Tests for the broadened GS1 host commands/functions (setcharprop equipment
codes, setplayerprop, addweapon, triggeraction, putnpc/putnpc2, nearest-player).

These exercise the GS1Host bridge with light fakes — see test_gs1_integration
for the base wiring; this file covers the commands added on top of it.
"""
import asyncio
import time

from pygserver.npc import NPC, NPCManager
from pygserver.gs1_host import compile_gs1, run_npc_event


class FakePlayer:
    def __init__(self, pid=1):
        self.id = pid
        self.x = 10.0
        self.y = 12.0
        self.direction = 2
        self.rupees = 5
        self.hearts = 3.0
        self.account_name = "hosler"
        self.nickname = "Hos"
        self.chat = ""
        self.gani = "idle"
        self.head_image = "head0.png"
        self.body_image = "body.png"
        self.sword_image = ""
        self.shield_image = ""
        self.colors = [0, 0, 0, 0, 0]
        self.weapons = []
        self.flags = {}
        self.sent = []

    def add_weapon(self, name):
        if name not in self.weapons:
            self.weapons.append(name)

    def has_weapon(self, name):
        return name in self.weapons

    async def send_raw(self, packet):
        self.sent.append(packet)

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

    def get_player_ids(self):
        return self._player_ids

    def get_npcs(self):
        return list(self._npcs)


def make_npc(code, level=None):
    npc = NPC(1, "t")
    npc.level = level or FakeLevel()
    npc.gs1_program = compile_gs1(code)
    return npc


# -- setcharprop (NPC appearance) ------------------------------------------
def test_setcharprop_equipment_codes():
    npc = make_npc(
        "if (created) {"
        " setcharprop #1,sword3.png; setcharprop #2,shield2.png;"
        " setcharprop #3,head5.png; setcharprop #8,body2.png;"
        " setcharprop #7,walk; setcharprop #n,Guard; setcharprop #c,Halt!; }")
    run_npc_event(npc, "created", None, None)
    assert npc.sword_image == "sword3.png"
    assert npc.shield_image == "shield2.png"
    assert npc.head_image == "head5.png"
    assert npc.body_image == "body2.png"
    assert npc.gani == "walk"
    assert npc.nickname == "Guard"
    assert npc.message == "Halt!"
    assert npc._dirty is True


def test_setcharprop_colors():
    # #C0-#C7 take classic colour NAMES, not raw indices (matches the C++
    # engine, verified by game_tester --gs1): "red" -> 4, "green" -> 7, and a
    # value that isn't a colour name resolves to 0.
    npc = make_npc("if (created) { setcharprop #C0,red; setcharprop #C4,green;"
                   " setcharprop #C1,9; }")
    run_npc_event(npc, "created", None, None)
    assert npc.colors[0] == 4   # red
    assert npc.colors[4] == 7   # green
    assert npc.colors[1] == 0   # "9" is not a colour name -> 0


# -- #C0-#C7 READ side (GS1MessageCodes.cpp handleCharacterBasedMessageCode +
#    mc_C: the value of a #C code is the classic colour NAME of the slot) ----
def test_color_read_bare_copy_idiom_is_npc_self_roundtrip():
    # the real-corpus idiom `setcharprop #C0,#C0`: setcharprop pushes the NPC
    # as the current source BEFORE its value args are evaluated
    # (processBuiltInCommand, GS1Commands.cpp:430), so bare #C0 reads the
    # NPC's OWN slot as its colour name and the write round-trips — it must
    # NOT read the player's slot, and must not zero the slot ("" -> 0/white
    # was the old bug). Verified live vs gs2emu (game_tester --gs1).
    npc = make_npc("if (playertouchsme) { setplayerprop #C0,green;"
                   " setcharprop #C0,brown; setcharprop #C0,#C0; }")
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.colors[0] == 7       # green written to the player
    assert npc.colors[0] == 12    # brown preserved (not 7, not 0)


def test_color_read_bare_in_setplayerprop_reads_player():
    # symmetric push: setplayerprop pushes the acting player, so bare #C1 in
    # its value arg reads the PLAYER's slot
    npc = make_npc("if (playertouchsme) {"
                   " setplayerprop #C1,darkred; setplayerprop #C3,#C1; }")
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.colors[1] == 5
    assert p.colors[3] == 5


def test_color_read_bare_outside_charprop_reads_initiating_player():
    # outside setcharprop/setplayerprop the source stack is empty, so bare
    # #C0 resolves to the initiating player (getCurrentSource(true))
    npc = make_npc("if (playertouchsme) { message #C0; }")
    p = FakePlayer()
    p.colors = [13, 0, 0, 0, 0]
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "cynober"


def test_color_read_bare_falls_back_to_npc_without_player():
    # no acting player -> the NPC itself (both via the setcharprop push and
    # the original-source fallback)
    npc = make_npc("if (created) { setcharprop #C2,blue; setcharprop #c,#C2; }")
    run_npc_event(npc, "created", None, None)
    assert npc.colors[2] == 10
    assert npc.message == "blue"


def test_color_read_index_minus1_is_source_npc():
    npc = make_npc("if (playertouchsme) {"
                   " setcharprop #C0,pink; setcharprop #c,#C0(-1); }")
    p = FakePlayer()
    p.colors = [4, 0, 0, 0, 0]   # player slot 0 is red; must NOT be read
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "pink"


def test_color_read_index_zero_is_acting_player():
    npc = make_npc("if (playertouchsme) {"
                   " setplayerprop #C1,red; setcharprop #c,#C1(0); }")
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "red"


def test_color_read_out_of_enum_value_is_empty():
    # getClassicColorName returns "" for values outside the classic enum.
    # (bare #C0 inside setcharprop reads the NPC's own slot, so poison that.)
    npc = make_npc("if (playertouchsme) { setcharprop #c,X#C0Y; }")
    npc.colors[0] = 25           # 20+ = HTML colours, no classic name
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "XY"


# -- hurt (argument is HALF-hearts: GS1Commands.cpp fn_hurt) -----------------
def test_hurt_subtracts_halfhearts():
    npc = make_npc("if (playertouchsme) { hurt 1; }")
    p = FakePlayer()
    p.hearts = 3.0
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.hearts == 2.5


def test_hurt_floors_argument():
    # DoubleAsIntegralFloor: hurt 1.9 == hurt 1 == half a heart
    npc = make_npc("if (playertouchsme) { hurt 1.9; }")
    p = FakePlayer()
    p.hearts = 3.0
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.hearts == 2.5


def test_hurt_clamps_at_zero_instead_of_going_negative():
    # A `hurt` for more halfhearts than the player has left must not drive
    # hearts negative (previously `hurt 20` on 3 hearts left player.hearts
    # at -7.0). No server/combat_manager here, so death-handling is simply
    # skipped, but the clamp itself must not depend on it.
    npc = make_npc("if (playertouchsme) { hurt 20; }")
    p = FakePlayer()
    p.hearts = 3.0
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.hearts == 0.0


def test_hurt_clamps_and_triggers_death():
    from pygserver.combat import DamageType
    from pygserver.protocol.constants import PLPROP

    async def main():
        sent = []

        class RichPlayer(FakePlayer):
            async def send_props(self, props):
                sent.append(props)

        cm = _FakeCombatMgr()

        class Server:
            combat_manager = cm

        npc = make_npc("if (playertouchsme) { hurt 20; }")
        p = RichPlayer()
        p.hearts = 3.0
        run_npc_event(npc, "playertouchsme", Server(), p)
        await asyncio.sleep(0)  # let the scheduled send_props/death run

        assert p.hearts == 0.0
        merged = {}
        for d in sent:
            merged.update(d)
        assert merged[PLPROP.CURPOWER] == 0  # never negative
        assert cm.died == [(p.id, None, DamageType.OTHER)]

    asyncio.run(main())


def test_setcharprop_gani_attributes():
    from pygserver.protocol.constants import NPCPROP

    npc = make_npc(
        "if (created) { setcharprop #P1,sword; setcharprop #P10,bow;"
        " setcharprop #P30,last; }")
    run_npc_event(npc, "created", None, None)
    assert npc.gattribs[NPCPROP.GATTRIB1] == "sword"
    assert npc.gattribs[NPCPROP.GATTRIB10] == "bow"
    assert npc.gattribs[NPCPROP.GATTRIB30] == "last"
    assert npc._dirty is True
    assert isinstance(npc.build_props_packet(), bytes)  # gani attrs serialize ok


def test_setplayerprop_gani_attributes():
    from pygserver.protocol.constants import PLPROP

    async def main():
        sent = []

        class RichPlayer(FakePlayer):
            async def send_props(self, props):
                sent.append(props)

        npc = make_npc(
            "if (playertouchsme) { setplayerprop #P1,walk; setplayerprop #P10,run; }")
        p = RichPlayer()
        run_npc_event(npc, "playertouchsme", None, p)
        await asyncio.sleep(0)
        assert p.gattribs[PLPROP.GATTRIB1] == "walk"
        assert p.gattribs[PLPROP.GATTRIB10] == "run"
        merged = {}
        for d in sent:
            merged.update(d)
        assert merged[PLPROP.GATTRIB1] == "walk"
        assert merged[PLPROP.GATTRIB10] == "run"

    asyncio.run(main())


# -- setplayerprop (player appearance, propagated to client) ---------------
def test_setplayerprop_codes_and_propagation():
    from pygserver.protocol.constants import PLPROP

    async def main():
        sent = []

        class RichPlayer(FakePlayer):
            async def send_props(self, props):
                sent.append(props)

        npc = make_npc(
            "if (playertouchsme) {"
            " setplayerprop #n,NewNick; setplayerprop #c,hi there;"
            " setplayerprop #7,dance; setplayerprop #C1,red; }")  # red = index 4
        p = RichPlayer()
        run_npc_event(npc, "playertouchsme", None, p)
        await asyncio.sleep(0)  # let the scheduled send_props run
        assert p.nickname == "NewNick"
        assert p.chat == "hi there"
        assert p.gani == "dance"
        assert p.colors[1] == 4
        merged = {}
        for d in sent:
            merged.update(d)
        assert merged[PLPROP.NICKNAME] == "NewNick"
        assert merged[PLPROP.CURCHAT] == "hi there"
        assert merged[PLPROP.GANI] == "dance"
        assert merged[PLPROP.COLORS] == [0, 4, 0, 0, 0]

    asyncio.run(main())


# -- addweapon -------------------------------------------------------------
def test_addweapon_adds_and_sends_packet():
    class Weapon:
        image = "bow.png"
        client_script = "//bow"

    class WM:
        def get_weapon(self, name):
            return Weapon() if name == "bow" else None

    class Server:
        weapon_manager = WM()

    async def main():
        npc = make_npc("if (playertouchsme) { addweapon bow; }")
        p = FakePlayer()
        run_npc_event(npc, "playertouchsme", Server(), p)
        await asyncio.sleep(0)  # let the scheduled send_raw run
        assert "bow" in p.weapons
        assert len(p.sent) == 1  # weapon packet pushed to client

    asyncio.run(main())


def test_hasweapon_function():
    npc = make_npc("if (playertouchsme) { if (hasweapon(bow)) { message armed; } }")
    p = FakePlayer()
    p.weapons = ["bow"]
    run_npc_event(npc, "playertouchsme", None, p)
    assert npc.message == "armed"


# -- triggeraction ---------------------------------------------------------
def test_triggeraction_dispatches_to_server():
    calls = []

    class Server:
        async def handle_trigger_action(self, player, x, y, action):
            calls.append((player, x, y, action))

    async def main():
        npc = make_npc("if (playertouchsme) { triggeraction 30,31,warp,cave.nw,5,6; }")
        p = FakePlayer()
        run_npc_event(npc, "playertouchsme", Server(), p)
        await asyncio.sleep(0)
        assert len(calls) == 1
        _, x, y, action = calls[0]
        assert (x, y) == (30.0, 31.0)
        # token[0] is a synthetic prefix; handle_trigger_action reads token[1]
        assert action == "gs1,warp,cave.nw,5,6"

    asyncio.run(main())


# -- putnpc / putnpc2 ------------------------------------------------------
class SpawnServer:
    def __init__(self):
        self.npc_manager = NPCManager(self)
        self.broadcasts = []

    async def broadcast_to_level(self, name, packet, *a, **k):
        self.broadcasts.append((name, packet))


def test_putnpc_creates_level_npc():
    async def main():
        server = SpawnServer()
        level = FakeLevel()
        npc = make_npc("if (playertouchsme) { putnpc guard.png,,40,41; }", level)
        p = FakePlayer()
        run_npc_event(npc, "playertouchsme", server, p)
        await asyncio.sleep(0)
        spawned = [n for n in server.npc_manager._npcs.values() if n.image == "guard.png"]
        assert len(spawned) == 1
        assert (spawned[0].x, spawned[0].y) == (40.0, 41.0)
        assert spawned[0].level is level

    asyncio.run(main())


def test_putnpc2_attaches_inline_script():
    async def main():
        server = SpawnServer()
        level = FakeLevel()
        # putnpc2 takes a braced script block; a bare command runs on every
        # event, so attach_gs1 firing 'created' executes the setimg.
        npc = make_npc(
            "if (playertouchsme) { putnpc2 12,13,{ setimg born.png; }; }", level)
        p = FakePlayer()
        run_npc_event(npc, "playertouchsme", server, p)
        await asyncio.sleep(0)
        spawned = [n for n in server.npc_manager._npcs.values() if n.image == "born.png"]
        assert len(spawned) == 1  # inline script ran

    asyncio.run(main())


# -- nearest-player functions ----------------------------------------------
class NearestServer:
    def __init__(self, players):
        self._players = {p.id: p for p in players}

    def get_player(self, pid):
        return self._players.get(pid)


def _level_with_players(*players):
    level = FakeLevel()
    for p in players:
        p.level = level
        level._player_ids.add(p.id)
    return level


def test_getnearestplayer_sets_context_player():
    far = FakePlayer(pid=1)
    far.x, far.y = 50.0, 50.0
    near = FakePlayer(pid=2)
    near.x, near.y = 11.0, 11.0
    level = _level_with_players(far, near)
    server = NearestServer([far, near])
    npc = make_npc(
        "if (timeout) { if (getnearestplayer(10,10) > 0) { message saw #n; } }", level)
    run_npc_event(npc, "timeout", server, None)
    # nearest is pid 2; ctx.player got set so #n resolves to that player
    assert npc.message == "saw Hos"


def test_findnearestplayer_returns_flag():
    near = FakePlayer(pid=2)
    near.x, near.y = 11.0, 11.0
    level = _level_with_players(near)
    server = NearestServer([near])
    npc = make_npc(
        "if (timeout) { this.found = findnearestplayer(10,10); }", level)
    run_npc_event(npc, "timeout", server, None)
    assert npc.gs1_scopes["this"]["found"] == 1.0


def test_getnearestplayers_returns_sorted_ids():
    a = FakePlayer(pid=1)
    a.x, a.y = 30.0, 30.0
    b = FakePlayer(pid=2)
    b.x, b.y = 11.0, 11.0
    level = _level_with_players(a, b)
    server = NearestServer([a, b])
    npc = make_npc(
        "if (timeout) { temp.ids = getnearestplayers(10,10); "
        "this.n = arraylen(temp.ids); }", level)
    run_npc_event(npc, "timeout", server, None)
    assert npc.gs1_scopes["this"]["n"] == 2.0


# -- color setters + destroy -----------------------------------------------
def test_color_setters_set_player_slots():
    from pygserver.protocol.constants import PLPROP

    async def main():
        sent = []

        class RichPlayer(FakePlayer):
            async def send_props(self, props):
                sent.append(props)

        npc = make_npc(
            "if (playertouchsme) { setskincolor 1; setcoatcolor 2; setsleevecolor 3;"
            " setshoecolor 4; setbeltcolor 5; }")
        p = RichPlayer()
        run_npc_event(npc, "playertouchsme", None, p)
        await asyncio.sleep(0)
        assert p.colors == [1, 2, 3, 4, 5]
        merged = {}
        for d in sent:
            merged.update(d)
        assert merged[PLPROP.COLORS] == [1, 2, 3, 4, 5]

    asyncio.run(main())


def test_appearance_setters():
    from pygserver.protocol.constants import PLPROP

    async def main():
        sent = []

        class RichPlayer(FakePlayer):
            async def send_props(self, props):
                sent.append(props)

        npc = make_npc(
            "if (playertouchsme) { sethead head9.png; setbody body3.png;"
            " setsword blade.png,4; setshield guard.png,2; }")
        p = RichPlayer()
        run_npc_event(npc, "playertouchsme", None, p)
        await asyncio.sleep(0)
        assert p.head_image == "head9.png"
        assert p.body_image == "body3.png"
        assert (p.sword_image, p.sword_power) == ("blade.png", 4)
        assert (p.shield_image, p.shield_power) == ("guard.png", 2)
        merged = {}
        for d in sent:
            merged.update(d)
        assert merged[PLPROP.HEADIMAGE] == "head9.png"
        assert merged[PLPROP.SWORDPOWER] == (4, "blade.png")

    asyncio.run(main())


def test_destroy_removes_npc():
    async def main():
        server = SpawnServer()
        level = FakeLevel()
        npc = server.npc_manager.create_npc(level=level, x=5.0, y=5.0)
        server.npc_manager.attach_gs1(npc, "if (playertouchsme) { destroy; }")
        assert npc.id in server.npc_manager._npcs
        p = FakePlayer()
        p.level = level
        from pygserver.gs1_host import run_npc_event as rne
        rne(npc, "playertouchsme", server, p)
        await asyncio.sleep(0)
        assert npc.id not in server.npc_manager._npcs

    asyncio.run(main())


# -- items / board / state / carry / combat --------------------------------
class _FakeItemMgr:
    def __init__(self):
        self.spawned = []
        self.removed = []
        self._items = []

    async def spawn_item(self, level, x, y, item_type):
        self.spawned.append((level.name, x, y, item_type))

    def get_items_on_level(self, name):
        return list(self._items)

    async def remove_item(self, name, x, y):
        self.removed.append((name, x, y))


class _FakeCombatMgr:
    def __init__(self):
        self.damaged = []
        self.died = []

    async def apply_damage(self, player, dmg, kx, ky, dtype=None, attacker=None):
        self.damaged.append((player.id, dmg, kx, ky))

    async def handle_player_death(self, player, killer_id=None, damage_type=None):
        self.died.append((player.id, killer_id, damage_type))


def test_lay_spawns_item():
    from pygserver.protocol.constants import LevelItemType

    async def main():
        class Server:
            item_manager = _FakeItemMgr()
            async def broadcast_to_level(self, *a, **k): pass
        server = Server()
        level = FakeLevel()
        npc = make_npc("if (created) { lay 5; }", level)  # 5 = HEART
        npc.x, npc.y = 12.0, 13.0
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        assert server.item_manager.spawned == [("testlevel", 12.0, 13.0, LevelItemType(5))]

    asyncio.run(main())


def test_take_removes_nearby_items():
    from pygserver.protocol.constants import LevelItemType
    from types import SimpleNamespace

    async def main():
        im = _FakeItemMgr()
        im._items = [SimpleNamespace(x=11.0, y=11.0, item_type=LevelItemType(5)),
                     SimpleNamespace(x=40.0, y=40.0, item_type=LevelItemType(5))]

        class Server:
            item_manager = im
            async def broadcast_to_level(self, *a, **k): pass
        npc = make_npc("if (created) { take 5; }", FakeLevel())
        npc.x, npc.y = 10.0, 10.0
        run_npc_event(npc, "created", Server(), None)
        await asyncio.sleep(0)
        assert im.removed == [("testlevel", 11.0, 11.0)]  # only the nearby one

    asyncio.run(main())


def test_setplayerdir():
    npc = make_npc("if (playertouchsme) { setplayerdir 3; }")
    p = FakePlayer()
    run_npc_event(npc, "playertouchsme", None, p)
    assert p.direction == 3


def test_carry_and_blockflags():
    npc = make_npc("if (created) { carryobject; canbecarried; canbepushed; }")
    run_npc_event(npc, "created", None, None)
    assert npc.gani == "carrystill"
    assert npc.block_flags == 0x02 | 0x08
    # throwcarry resets a carry gani
    npc2 = make_npc("if (created) { throwcarry; }")
    npc2.gani = "carrypeople"
    run_npc_event(npc2, "created", None, None)
    assert npc2.gani == "idle"


def test_updateboard_broadcasts_region():
    async def main():
        sent = []

        class Server:
            async def broadcast_to_level(self, name, packet, *a, **k):
                sent.append(packet)
        level = FakeLevel()
        level._tiles = bytearray(8192)
        npc = make_npc("if (created) { updateboard 0,0,4,4; }", level)
        run_npc_event(npc, "created", Server(), None)
        await asyncio.sleep(0)
        assert len(sent) == 1 and isinstance(sent[0], bytes)

    asyncio.run(main())


def test_putexplosion_damages_players_in_radius():
    async def main():
        cm = _FakeCombatMgr()
        near = FakePlayer(pid=1)
        near.x, near.y = 30.0, 30.0
        far = FakePlayer(pid=2)
        far.x, far.y = 50.0, 50.0
        level = _level_with_players(near, far)

        class Server:
            combat_manager = cm
            def get_player(self, pid):
                return {1: near, 2: far}.get(pid)
            async def broadcast_to_level(self, *a, **k): pass
        npc = make_npc("if (created) { putexplosion 3,30,30; }", level)
        run_npc_event(npc, "created", Server(), None)
        await asyncio.sleep(0)
        assert [d[0] for d in cm.damaged] == [1]  # only the near player

    asyncio.run(main())


def test_sendtorc_uses_shared_rc_chat_processor():
    async def main():
        class RC:
            def __init__(self):
                self.messages = []

            async def process_chat(self, message, session=None):
                self.messages.append((message, session))

        rc = RC()

        class Server:
            rc_manager = rc

        npc = make_npc('if (created) { sendtorc "hello staff"; sendtorc /version; }')
        run_npc_event(npc, "created", Server(), None)
        await asyncio.sleep(0)
        assert rc.messages == [('\"hello staff\"', None), ("/version", None)]

    asyncio.run(main())


def test_hitplayer_uses_halfhearts_and_cpp_push_encoding():
    async def main():
        target = FakePlayer()
        target.x, target.y = 10.0, 10.0
        level = _level_with_players(target)
        cm = _FakeCombatMgr()

        class Server:
            combat_manager = cm

            def get_player(self, pid):
                return target if pid == target.id else None

        npc = make_npc("if (created) { hitplayer 0,3.9,9,10; }", level)
        run_npc_event(npc, "created", Server(), None)
        await asyncio.sleep(0)
        # normalized (target-from)=(1,0), push 4 tiles, *16, midpoint 64
        assert cm.damaged == [(target.id, 3, 128, 64)]

    asyncio.run(main())


def test_hitnpc_uses_halfhearts_and_normalized_push_direction():
    async def main():
        level = FakeLevel()
        source = make_npc("if (created) { hitnpc 1,3.9,5,8; }", level)
        target = NPC(2, "target")
        target.x, target.y, target.hearts = 8.0, 12.0, 3.0
        level._npcs = [source, target]

        class Manager:
            async def on_npc_washit(self, *args):
                pass

        class Server:
            npc_manager = Manager()

        run_npc_event(source, "created", Server(), None)
        await asyncio.sleep(0)
        assert target.hearts == 1.5
        assert (target.hurt_dx, target.hurt_dy) == (19, 25)

    asyncio.run(main())


# -- horse commands --------------------------------------------------------
class HorseServer:
    def __init__(self):
        from pygserver.horse import HorseManager
        self.horse_manager = HorseManager(self)

    async def broadcast_to_level(self, *a, **k):
        pass


def test_puthorse_adds_horse_to_level():
    async def main():
        server = HorseServer()
        level = FakeLevel()
        npc = make_npc("if (created) { puthorse ride.png,33,34; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        horses = server.horse_manager.get_horses_on_level(level.name)
        assert len(horses) == 1
        assert horses[0].image == "ride.png"
        assert (horses[0].x, horses[0].y) == (33.0, 34.0)

    asyncio.run(main())


def test_takehorse_mounts_npc_and_removes_horse():
    async def main():
        server = HorseServer()
        level = FakeLevel()
        await server.horse_manager.add_horse(level, 10.0, 10.0, image="brown.png")
        npc = make_npc("if (created) { takehorse 0; }", level)
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        assert npc.horse_image == "brown.png"
        assert server.horse_manager.get_horses_on_level(level.name) == []

    asyncio.run(main())


# -- server-owned showimg / clock / freeze / glove semantics ---------------
def test_showimg_initial_packet_is_minimal_and_stateful():
    async def main():
        server = SpawnServer()
        npc = make_npc(
            "if (created) { showimg 1,pic.png,3,4; changeimgzoom 1,1.5; }"
        )
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        assert npc.showimgs == {
            1: {0: "pic.png", 1: 6, 2: 8, 6: 15}
        }
        initial = server.broadcasts[0][1]
        assert initial == bytes([
            198, 32, 32, 33, 43,       # packet 166, NPC 1, image index 1
            32, 39,                    # prop 0, string length 7
            *b"pic.png",
            33, 38, 34, 40,           # x=6, y=8
            10,
        ])
        # The change packet contains only selector + changed zoom property.
        assert server.broadcasts[1][1] == bytes(
            [198, 32, 32, 33, 43, 38, 47, 10]
        )

    asyncio.run(main())


def test_hideimgs_resets_and_replays_remaining_layers_and_ignores_local_range():
    async def main():
        server = SpawnServer()
        npc = make_npc(
            "if (created) { showimg 1,a.png,1,2; showimg 2,b.png,3,4;"
            " showimg 200,local.png,5,6; hideimgs 1,1; }"
        )
        run_npc_event(npc, "created", server, None)
        await asyncio.sleep(0)
        assert set(npc.showimgs) == {2}
        assert len(server.broadcasts) == 3  # index 200 never broadcasts
        hide = server.broadcasts[-1][1]
        assert hide[4] == 41               # selector 9: clear all
        assert hide[5] == 44               # then replay index 2

    asyncio.run(main())


def test_timevar2_is_unix_seconds_and_playerfreezetime_counts_down():
    npc = make_npc(
        "if (created) { this.now=timevar2; freezeplayer 2;"
        " this.left=playerfreezetime; }"
    )
    player = FakePlayer()
    before = int(time.time())
    run_npc_event(npc, "created", None, player)
    assert before <= npc.gs1_scopes["this"]["now"] <= int(time.time())
    assert 0 < npc.gs1_scopes["this"]["left"] <= 2
    assert player.is_frozen is True

    npc.gs1_program = compile_gs1(
        "if (created) { unfreezeplayer; this.left=playerfreezetime; }"
    )
    run_npc_event(npc, "created", None, player)
    assert npc.gs1_scopes["this"]["left"] == -1


def test_player_glovepower_uses_player_wire_scale():
    npc = make_npc(
        "if (created) { glovepower=1; this.playerpower=playerglovepower;"
        " this.npcpower=glovepower; }"
    )
    player = FakePlayer()
    player.glove_power = 2
    run_npc_event(npc, "created", None, player)
    assert npc.gs1_scopes["this"]["playerpower"] == 2
    assert npc.gs1_scopes["this"]["npcpower"] == 1
    assert npc.glove_power == 1
