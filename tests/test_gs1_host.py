"""Tests for the broadened GS1 host commands/functions (setcharprop equipment
codes, setplayerprop, addweapon, triggeraction, putnpc/putnpc2, nearest-player).

These exercise the GS1Host bridge with light fakes — see test_gs1_integration
for the base wiring; this file covers the commands added on top of it.
"""
import asyncio

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
    npc = make_npc("if (created) { setcharprop #C0,3; setcharprop #C4,7; }")
    run_npc_event(npc, "created", None, None)
    assert npc.colors[0] == 3
    assert npc.colors[4] == 7


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
            " setplayerprop #7,dance; setplayerprop #C1,4; }")
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
        "if (timeout) { if (getnearestplayer(10,10)) { message saw #n; } }", level)
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
