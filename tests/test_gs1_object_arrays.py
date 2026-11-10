"""GS1 object arrays (players[], npcs[], compus[], bombs[], ...), their
`...count` variables, and the commands that act on them (join, callnpc,
removebomb, takeplayerhorse, timershow, shootfire*/shootnuke).

Reference: GServer-v2 GS1Variables.cpp, GS1Commands.cpp, level/Level*.h."""
import asyncio
import os
import re
import time
from types import SimpleNamespace

import pytest

from pygserver.combat import Arrow, Bomb
from pygserver.gs1_host import GS1Host, compile_gs1, run_npc_event
from pygserver.horse import Horse
from pygserver.items import LevelItem
from pygserver.npc import NPC
from pygserver.protocol.constants import LevelItemType

CORPUS = os.path.join(os.path.dirname(__file__), "gs1_corpus")


def FakePlayer(pid, account, x, y, hearts=3.0, level=None):
    """A REAL pygserver Player (so attribute names are the real ones) whose
    outbound packets are recorded in `.sent`."""
    from unittest.mock import MagicMock
    from pygserver.player import Player
    player = Player(MagicMock(), pid, MagicMock(), MagicMock())
    player.account_name = account
    player.nickname = account.title()
    player.x, player.y = x, y
    player.hearts = hearts
    player.level = level
    player.logged_in = True
    player.sent = []

    async def send_raw(packet):
        player.sent.append(packet)

    player.send_raw = send_raw
    return player


class FakeLevel:
    def __init__(self, name="lvl"):
        self.name = name
        self._npcs = []
        self.player_ids = []
        self.signs = {}

    def add_npc(self, npc):
        npc.level = self
        self._npcs.append(npc)

    def remove_npc(self, npc):
        npc.level = None

    def get_npcs(self):
        return list(self._npcs)

    def get_player_ids(self):
        return list(self.player_ids)

    def get_signs(self):
        return dict(self.signs)


class Recorder:
    def __init__(self):
        self.broadcasts = []
        self.excluded = []

    async def broadcast_to_level(self, level_name, packet, exclude=None):
        self.broadcasts.append((level_name, packet))
        self.excluded.append(exclude)


class FakeServer(Recorder):
    def __init__(self):
        super().__init__()
        self.players = {}
        self.world = None
        self.combat_manager = None
        self.item_manager = None
        self.horse_manager = None
        self.baddy_manager = None
        self.npc_manager = None
        self.gs2_manager = None

    def get_player(self, pid):
        return self.players.get(pid)


def make_world(*accounts, level=None):
    """A server with a level holding one player per (account, x, y)."""
    level = level or FakeLevel()
    server = FakeServer()
    for pid, (account, x, y) in enumerate(accounts, start=1):
        player = FakePlayer(pid, account, x, y, level=level)
        server.players[pid] = player
        level.player_ids.append(pid)
    return server, level


def npc_with(code, level, name="host", npc_id=1):
    npc = NPC(npc_id, name)
    level.add_npc(npc)
    npc.gs1_program = compile_gs1(code)
    npc.gs1_source = code
    return npc


def run(npc, event="created", server=None, player=None, **kw):
    return run_npc_event(npc, event, server, player, **kw)


def this_vars(npc):
    return npc.gs1_scopes["this"]


# -- players[] ----------------------------------------------------------------

def test_players_fields_read_in_level_audience_order():
    server, level = make_world(("alice", 10, 12), ("bob", 30, 40))
    server.players[2].hearts = 1.5
    server.players[1].rupees = 7
    server.players[2].sword_power = 2
    server.players[2].head_image = "head12.png"
    npc = npc_with(
        "if (created) { this.n = playerscount; this.x0 = players[0].x;"
        " this.x1 = players[1].x; this.y1 = players[1].y;"
        " this.h1 = players[1].hearts; this.id1 = players[1].id;"
        " this.ap1 = players[1].ap; this.dir = players[1].dir;"
        " this.rup = players[0].rupees; this.dart = players[1].darts;"
        " this.hp = players[1].hp; this.full = players[1].fullhearts;"
        " this.sw = players[1].swordpower; this.head = players[1].headset; }",
        level)
    run(npc, server=server, player=server.players[1])
    v = this_vars(npc)
    assert (v["n"], v["x0"], v["x1"], v["y1"]) == (2.0, 10.0, 30.0, 40.0)
    assert (v["h1"], v["hp"], v["id1"], v["ap1"]) == (1.5, 1.5, 2.0, 50.0)
    assert (v["dir"], v["rup"], v["dart"], v["full"]) == (2.0, 7.0, 10.0, 3.0)
    assert (v["sw"], v["head"]) == (2.0, 12.0)


def test_players_index_matches_testplayer_and_hitplayer_order():
    server, level = make_world(("alice", 10, 12), ("bob", 30, 40))
    npc = npc_with(
        "if (created) { this.i = testplayer(31, 42);"
        " this.pid = players[this.i].id; this.miss = testplayer(0, 0); }",
        level)
    run(npc, server=server, player=server.players[1])
    v = this_vars(npc)
    assert v["i"] == 1.0 and v["pid"] == 2.0 and v["miss"] == -2.0


def test_players_minus_one_is_the_initiator_and_other_negatives_are_nothing():
    server, level = make_world(("alice", 10, 12), ("bob", 30, 40))
    npc = npc_with(
        "if (created) { this.me = players[-1].id; this.other = players[-2].id; }",
        level)
    run(npc, server=server, player=server.players[2])
    assert this_vars(npc)["me"] == 2.0
    assert this_vars(npc)["other"] == 0.0


def test_players_out_of_range_reads_zero_and_skips_with():
    server, level = make_world(("alice", 10, 12))
    npc = npc_with(
        "if (created) { this.far = players[9].x; this.hit = 0;"
        " with (players[9]) { this.hit = 1; hearts = 0; } }", level)
    run(npc, server=server, player=server.players[1])
    assert this_vars(npc)["far"] == 0.0
    assert this_vars(npc)["hit"] == 0.0
    assert server.players[1].hearts == 3.0


def test_players_field_write_updates_player_and_queues_prop_flush():
    server, level = make_world(("alice", 10, 12), ("bob", 30, 40))
    npc = npc_with(
        "if (created) { players[1].hearts = 1; players[1].rupees = 99;"
        " players[1].x = 33; }", level)
    run(npc, server=server, player=server.players[1])
    bob = server.players[2]
    assert (bob.hearts, bob.rupees, bob.x) == (1.0, 99.0, 33.0)


def test_with_players_element_reads_message_codes_of_that_player():
    server, level = make_world(("alice", 10, 12), ("bob", 30, 40))
    npc = npc_with(
        "if (created) { with (players[1]) { setstring this.acct,#a;"
        " setstring this.nick,#n; this.h = hearts; this.px = x; } }", level)
    run(npc, server=server, player=server.players[1])
    v = this_vars(npc)
    assert (v["acct"], v["nick"], v["h"], v["px"]) == ("bob", "Bob", 3.0, 30.0)


def test_players_client_flag_reads_the_players_flag_store():
    server, level = make_world(("alice", 10, 12), ("bob", 30, 40))
    server.players[2].flags["client.nopk"] = "1"
    npc = npc_with(
        "if (created) { this.a = players[1].client.nopk;"
        " this.b = players[0].client.nopk; }", level)
    run(npc, server=server, player=server.players[1])
    assert (this_vars(npc)["a"], this_vars(npc)["b"]) == (1.0, 0.0)


# -- allplayers[] -------------------------------------------------------------

def test_allplayers_spans_levels_and_counts():
    server, level = make_world(("alice", 10, 12))
    other = FakeLevel("elsewhere")
    far = FakePlayer(5, "carol", 3, 4, level=other)
    server.players[5] = far
    ghost = FakePlayer(6, "ghost", 0, 0)
    ghost.logged_in = False
    server.players[6] = ghost
    npc = npc_with(
        "if (created) { this.n = allplayerscount; this.local = playerscount;"
        " this.x = allplayers[1].x; this.i = allplayers[1].id; }", level)
    run(npc, server=server, player=server.players[1])
    v = this_vars(npc)
    assert (v["n"], v["local"], v["x"], v["i"]) == (2.0, 1.0, 3.0, 5.0)


# -- npcs[] -------------------------------------------------------------------

def test_npcs_array_count_fields_and_self_reference():
    server, level = make_world(("alice", 10, 12))
    host = npc_with(
        "if (created) { this.n = npcscount; this.bx = npcs[1].x;"
        " this.by = npcs[1].y; this.w = npcs[1].width; this.h = npcs[1].height;"
        " this.sv = npcs[1].save[2]; this.me = npcs[-1].id; this.nid = npcs[1].id;"
        " this.hp = npcs[1].hearts; this.gone = npcs[7].x;"
        " this.cw = npcs[2].width; this.plain = npcs[0].width; }", level)
    other = npc_with("", level, name="other", npc_id=2)
    other.x, other.y, other.hearts = 5.5, 6.25, 2.0
    other.shape = (3, 4)
    third = npc_with("", level, name="third", npc_id=3)
    third.image = "#c#"
    other.gs1_scopes["this"]["save"] = [0, 0, 9, 0, 0, 0, 0, 0, 0, 0]
    run(host, server=server, player=server.players[1])
    v = this_vars(host)
    assert (v["n"], v["bx"], v["by"], v["w"], v["h"]) == (3.0, 5.5, 6.25, 3.0, 4.0)
    assert (v["cw"], v["plain"]) == (3.0, 0.0)  # character NPC 3x3, none otherwise
    assert (v["sv"], v["me"], v["nid"], v["hp"], v["gone"]) == (9.0, 1.0, 2.0, 2.0, 0.0)


def test_npcs_element_writes_and_with_block():
    server, level = make_world(("alice", 10, 12))
    host = npc_with(
        "if (created) { npcs[1].x = 20; npcs[1].save[3] = 300;"
        " with (npcs[1]) { y = 8; dir = 1; } }", level)
    other = npc_with("", level, name="other", npc_id=2)
    run(host, server=server, player=server.players[1])
    assert (other.x, other.y, other.direction) == (20.0, 8.0, 1.0)
    assert other.gs1_scopes["this"]["save"][3] == 220.0  # clamped like save[]
    assert host.x != 20.0


# -- level object arrays ------------------------------------------------------

def object_world():
    server, level = make_world(("alice", 10, 12))
    now = time.time()
    server.combat_manager = SimpleNamespace(
        _bombs={"lvl": {
            7: Bomb(7, 1, "lvl", 5.5, 6.0, 2, 3.0, created_at=now),
            8: Bomb(8, 1, "lvl", 9.0, 9.5, 1, 3.0, created_at=now)}},
        get_bombs_on_level=lambda n: list(
            server.combat_manager._bombs.get(n, {}).values()),
        get_arrows_on_level=lambda n: [
            Arrow(3, 1, n, 20.0, 21.0, 3, created_at=now)] if n == "lvl" else [])
    server.item_manager = SimpleNamespace(get_items_on_level=lambda n: [
        LevelItem(1, n, 3.5, 4.5, LevelItemType.HEART, created_at=now,
                  despawn_time=30.0)])
    server.horse_manager = SimpleNamespace(get_horses_on_level=lambda n: [
        Horse(1, n, 12.0, 13.0, direction=1, bushes=2)])
    server.baddy_manager = SimpleNamespace(get_baddies_on_level=lambda n: [
        SimpleNamespace(id=0, x=7.0, y=8.0, direction=2, baddy_type=3,
                        health=4, mode=2, dead=False),
        SimpleNamespace(id=1, x=1.0, y=1.0, direction=0, baddy_type=0,
                        health=0, mode=9, dead=True)])
    level.signs = {(4, 5): "hi", (6, 7): "yo"}
    return server, level


def test_level_object_arrays_counts_and_fields():
    server, level = object_world()
    npc = npc_with(
        "if (created) { this.nb = bombscount; this.bx = bombs[1].x;"
        " this.bp = bombs[0].power; this.bt = bombs[0].time;"
        " this.na = arrowscount; this.ax = arrows[0].x; this.adx = arrows[0].dx;"
        " this.ady = arrows[0].dy; this.ad = arrows[0].dir; this.at = arrows[0].type;"
        " this.ni = itemscount; this.ix = items[0].x; this.it = items[0].type;"
        " this.nh = horsescount; this.hb = horses[0].bushes; this.hd = horses[0].dir;"
        " this.nc = compuscount; this.cx = compus[0].x; this.cm = compus[0].mode;"
        " this.cp = compus[0].power; this.cd = compus[1].x;"
        " this.ns = signscount; this.sx = signs[1].x; this.sy = signs[1].y;"
        " this.ne = exploscount; this.ex = explos[0].x; this.nope = bombs[5].x; }",
        level)
    run(npc, server=server, player=server.players[1])
    v = this_vars(npc)
    assert (v["nb"], v["bx"], v["bp"]) == (2.0, 9.0, 2.0)
    assert 2.5 < v["bt"] <= 3.0
    assert (v["na"], v["ax"], v["adx"], v["ady"], v["ad"], v["at"]) == (
        1.0, 20.0, 1.0, 0.0, 3.0, 1.0)
    assert (v["ni"], v["ix"], v["it"]) == (1.0, 3.5, float(LevelItemType.HEART))
    assert (v["nh"], v["hb"], v["hd"]) == (1.0, 2.0, 1.0)
    # compuscount counts every baddy, compus[] lists only the living
    assert (v["nc"], v["cx"], v["cm"], v["cp"]) == (2.0, 7.0, 2.0, 4.0)
    assert v["cd"] == 0.0  # compus[1] is past the living list: no object
    assert (v["ns"], v["sx"], v["sy"]) == (2.0, 6.0, 7.0)
    assert (v["ne"], v["ex"], v["nope"]) == (0.0, 0.0, 0.0)


def test_with_level_object_exposes_its_fields():
    server, level = object_world()
    npc = npc_with(
        "if (created) { this.sum = 0; for (i = 0; i < bombscount; i++) {"
        " with (bombs[i]) { this.sum += x; } } }", level)
    run(npc, server=server, player=server.players[1])
    assert this_vars(npc)["sum"] == 14.5


def test_bare_bombs_and_arrows_stay_the_npcs_own_counters():
    server, level = object_world()
    npc = npc_with("if (created) { bombs = 3; arrows = 9; this.b = bombs; }", level)
    run(npc, server=server, player=server.players[1])
    assert (npc.bombs, npc.arrows, this_vars(npc)["b"]) == (3, 9, 3.0)


# -- getplayer / getnpc -------------------------------------------------------

def test_getplayer_searches_every_level_case_insensitively():
    server, level = make_world(("alice", 10, 12))
    server.players[5] = FakePlayer(5, "Carol", 3, 4, level=FakeLevel("elsewhere"))
    npc = npc_with(
        "if (created) { with (getplayer(carol)) { hearts = 1; }"
        " this.found = getplayer(nobody); }", level)
    run(npc, server=server, player=server.players[1])
    assert server.players[5].hearts == 1.0
    assert this_vars(npc)["found"] == 0.0


def test_getnpc_searches_the_whole_server_with_an_exact_name():
    server, level = make_world(("alice", 10, 12))
    elsewhere = FakeLevel("elsewhere")
    host = npc_with("if (created) { with (getnpc(Target)) { this.t = 1; x = 9; }"
                    " with (getnpc(target)) { y = 9; } }", level)
    target = NPC(2, "Target")
    elsewhere.add_npc(target)
    server.npc_manager = SimpleNamespace(_npcs={1: host, 2: target})
    run(host, server=server, player=server.players[1])
    assert target.x == 9.0
    assert target.y != 9.0  # the name compare is case-sensitive
    assert target.gs1_scopes["this"].get("t") is None  # `this.` stays the caller's


# -- join -----------------------------------------------------------------------

class FakeClasses:
    def __init__(self, **classes):
        self.classes = {k.lower(): SimpleNamespace(source=v, name=k)
                        for k, v in classes.items()}

    def get_class(self, name):
        return self.classes.get(name.lower())


def test_join_makes_class_functions_callable_and_runs_class_events():
    server, level = make_world(("alice", 10, 12))
    server.gs2_manager = FakeClasses(
        door="function open() { this.opened = this.opened + 1; }\n"
             "if (created) { this.class_created = 1; }\n"
             "if (playertouchsme) { open(); }\n")
    npc = npc_with(
        "if (created) { join door; this.joined_once = 1; }"
        " if (playerchats) { open(); }", level)
    run(npc, server=server, player=server.players[1])
    v = this_vars(npc)
    assert v["joined_once"] == 1.0
    assert v["class_created"] == 1.0  # class saw the `created` that joined it
    run(npc, "playertouchsme", server, server.players[1])
    assert this_vars(npc)["opened"] == 1.0
    run(npc, "playerchats", server, server.players[1])  # fn from the class
    assert this_vars(npc)["opened"] == 2.0
    run(npc, "created", server, server.players[1])  # re-join is a no-op
    assert npc._gs1_joined == ["door"]


def test_join_prefers_the_npcs_own_function_and_ignores_unknown_classes():
    server, level = make_world(("alice", 10, 12))
    server.gs2_manager = FakeClasses(
        lib="function shared() { this.who = 1; }")
    npc = npc_with(
        "function shared() { this.who = 2; }"
        " if (created) { join lib; join missing; shared(); }", level)
    run(npc, server=server, player=server.players[1])
    assert this_vars(npc)["who"] == 2.0
    assert npc._gs1_joined == ["lib"]


def test_join_inside_with_target_joins_that_npc():
    server, level = make_world(("alice", 10, 12))
    server.gs2_manager = FakeClasses(lib="if (playerchats) { this.hit = 1; }")
    host = npc_with("if (created) { with (npcs[1]) { join lib; } }", level)
    other = npc_with("if (created) { this.own = 1; }", level, name="o", npc_id=2)
    run(host, server=server, player=server.players[1])
    assert getattr(other, "_gs1_joined", []) == ["lib"]
    assert not getattr(host, "_gs1_joined", [])
    run(other, "playerchats", server, server.players[1])
    assert this_vars(other)["hit"] == 1.0


# -- callnpc (real corpus fragment: treasurecave2.nw:249) -------------------------

def test_callnpc_fires_custom_event_on_indexed_npc_with_params_and_not_created():
    server, level = make_world(("alice", 10, 12))
    manager = SimpleNamespace()
    server.npc_manager = manager
    fired = []

    def fire(target, event, player, params=None, **kw):
        fired.append((target.id, event, player, params))
        run_npc_event(target, event, server, player, params=params)

    manager._fire_gs1 = fire
    caller = npc_with(
        "if (created) { callnpc 1,ping,a,b; callnpc 1,created; callnpc 9,ping;"
        " callnpc -1,ping; callnpc 1,PING; }", level)
    target = npc_with("if (ping) { setstring this.p0,#p(0); setstring this.p1,#p(1); }", level, name="t", npc_id=2)
    run(caller, server=server, player=server.players[1])
    assert fired == [(2, "ping", None, ["a", "b"]), (2, "PING", None, [])]
    assert this_vars(target)["p0"] == "a"
    assert this_vars(target)["p1"] == "b"


def test_callnpc_loop_from_real_corpus_script_reaches_every_npc():
    # treasurecave2.nw:249 `for (i=0; i<npcscount; i++) callnpc i,getdigmagic;`
    corpus = os.path.join(CORPUS, "treasurecave2.nw")
    text = open(corpus, encoding="latin-1").read()
    assert "for (i=0; i<npcscount; i++) callnpc i,getdigmagic;" in text
    server, level = make_world(("alice", 10, 12))
    manager = SimpleNamespace()
    server.npc_manager = manager
    manager._fire_gs1 = lambda target, event, player, params=None, **kw: (
        run_npc_event(target, event, server, player, params=params))
    caller = npc_with(
        "if (playerchats) { for (i=0; i<npcscount; i++) callnpc i,getdigmagic; }",
        level)
    listeners = [npc_with("if (getdigmagic) { this.magic = 1; }", level,
                          name=f"n{i}", npc_id=10 + i) for i in range(2)]
    run(caller, "playerchats", server, server.players[1])
    assert [this_vars(n).get("magic") for n in listeners] == [1.0, 1.0]


def test_callnpc_is_queued_on_a_running_loop():
    server, level = make_world(("alice", 10, 12))
    server.npc_manager = SimpleNamespace()
    server.npc_manager._fire_gs1 = lambda target, event, player, params=None, **kw: (
        run_npc_event(target, event, server, player, params=params))
    caller = npc_with("if (created) { callnpc 0,again; this.after = 1; }"
                      " if (again) { this.again = 1; }", level)

    async def scenario():
        run(caller, server=server, player=server.players[1])
        assert "again" not in this_vars(caller)  # not re-entered mid-script
        await asyncio.sleep(0.12)  # one server tick later

    asyncio.run(scenario())
    assert this_vars(caller)["after"] == 1.0
    assert this_vars(caller)["again"] == 1.0


# -- removebomb -----------------------------------------------------------------

def test_removebomb_drops_the_indexed_bomb_and_broadcasts_its_removal():
    server, level = object_world()
    npc = npc_with("if (created) { removebomb 0; removebomb 9; removebomb -1; }",
                   level)

    async def scenario():
        run(npc, server=server, player=server.players[1])
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert list(server.combat_manager._bombs["lvl"]) == [8]
    assert len(server.broadcasts) == 1
    name, packet = server.broadcasts[0]
    from pygserver.protocol.packets import build_bomb_del
    assert name == "lvl" and packet == build_bomb_del(5.5, 6.0)


# -- takeplayerhorse ------------------------------------------------------------

def test_takeplayerhorse_clears_the_horse_prop_and_the_mount():
    server, level = make_world(("alice", 10, 12))
    horse = Horse(4, "lvl", 1.0, 1.0)
    horse.rider_id = 1
    removed = []

    async def remove_horse(level_name, horse_id):
        removed.append((level_name, horse_id))

    server.horse_manager = SimpleNamespace(_mounted={1: horse},
                                           remove_horse=remove_horse)
    npc = npc_with("if (created) { takeplayerhorse; }", level)

    async def scenario():
        run(npc, server=server, player=server.players[1])
        await asyncio.sleep(0)

    asyncio.run(scenario())
    from pygserver.protocol.constants import PLPROP
    from pygserver.protocol.packets import build_player_props
    assert server.players[1].sent == [build_player_props({PLPROP.HORSEGIF: ""})]
    assert server.horse_manager._mounted == {}
    assert removed == [("lvl", 4)] and horse.rider_id is None


# -- timershow ------------------------------------------------------------------

def test_timershow_sets_the_flag_once_and_sends_visflags():
    server, level = make_world(("alice", 10, 12))
    npc = npc_with("if (created) { timershow; timershow; }", level)
    npc.visible = True

    async def scenario():
        run(npc, server=server, player=server.players[1])
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert npc.vis_flags & 0b1000
    assert len(server.broadcasts) == 1
    from pygserver.protocol.builders.core import build_npc_props
    from pygserver.protocol.constants import NPCPROP
    assert server.broadcasts[0][1] == build_npc_props(
        npc.id, {NPCPROP.VISFLAGS: 0b1001})


# -- shootfireball / shootfireblast / shootnuke ---------------------------------

@pytest.mark.parametrize("command,arrow_type", [
    ("shootfireball", 2), ("shootfireblast", 3), ("shootnuke", 4)])
def test_shoot_commands_broadcast_typed_arrow_from_the_npc(command, arrow_type):
    server, level = make_world(("alice", 10, 12))
    npc = npc_with(f"if (created) {{ {command} 3; }}", level)
    npc.x, npc.y = 20.0, 30.0

    async def scenario():
        run(npc, server=server, player=server.players[1])
        await asyncio.sleep(0)

    asyncio.run(scenario())
    from pygserver.protocol.packets import build_arrow_add
    assert server.broadcasts == [
        ("lvl", build_arrow_add(0, 20.0, 30.0, 3, 107 + 3, arrow_type))]


def test_shoot_from_character_npc_offsets_position():
    server, level = make_world(("alice", 10, 12))
    npc = npc_with("if (created) { shootnuke 1; }", level)
    npc.x, npc.y, npc.image = 20.0, 30.0, "#c#"

    async def scenario():
        run(npc, server=server, player=server.players[1])
        await asyncio.sleep(0)

    asyncio.run(scenario())
    from pygserver.protocol.packets import build_arrow_add
    assert server.broadcasts == [
        ("lvl", build_arrow_add(0, 19.5, 31.5, 1, 108, 4))]


# -- real corpus fragment: gcarena8.nw players[this.i] loop ----------------------

def test_corpus_arena_switch_counts_foes_through_players_array():
    text = open(os.path.join(CORPUS, "gcarena8.nw"), encoding="latin-1").read()
    block = re.search(r"NPC - 18 20\n(.*?)\nNPCEND", text, re.S).group(1)
    assert "players[this.i].hearts>0" in block
    server, level = make_world(("alice", 20, 25), ("bob", 30, 30), ("cat", 3, 3))
    server.players[3].hearts = 3.0
    npc = npc_with(block, level)
    run(npc, "playerenters", server, server.players[1])
    # bob stands in the arena rect and is alive, cat is outside it
    assert this_vars(npc)["foes"] == 1.0
    assert this_vars(npc)["on"] == 1.0
    server.players[2].hearts = 0.0
    run(npc, "playerenters", server, server.players[1])
    assert this_vars(npc)["foes"] == 0.0


# -- review regressions ---------------------------------------------------------

HUGE = "*".join(["1000000"] * 60)           # overflows a double to inf
BIG = "1000000*1000000*1000000"             # finite, far past any wire range
NAN = f"(({HUGE}) - ({HUGE}))"


def drain(coro_factory, delay=0.0):
    async def scenario():
        coro_factory()
        await asyncio.sleep(delay)
    asyncio.run(scenario())


def test_non_finite_and_out_of_range_writes_never_reach_a_packet_builder():
    from pygserver.protocol.builders.core import (
        build_npc_props, build_other_player_props, build_player_props)
    from pygserver.protocol.constants import NPCPROP, PLPROP
    server, level = make_world(("alice", 10, 12), ("bob", 5, 5))
    bob = server.players[2]
    host = npc_with(
        f"if (created) {{ players[1].x = {BIG}; players[1].y = {NAN};"
        f" players[1].hearts = 999999; players[1].rupees = 0-5;"
        f" players[1].darts = 0-1; players[1].swordpower = 9999;"
        f" players[1].dir = 77; players[1].fullhearts = 0-4;"
        f" players[1].mp = 0-3; players[1].kills = 0-3;"
        f" players[1].deaths = {HUGE}; npcs[1].x = {BIG}; npcs[1].hearts = {HUGE};"
        f" npcs[1].timeout = {HUGE}; }}", level)
    target = NPC(2, "t")
    level.add_npc(target)
    target.x, target.hearts = 3.0, 2.0
    run(host, server=server, player=server.players[1])
    assert (bob.x, bob.y) == (64.0, 5)              # huge clamps, nan drops
    assert (bob.hearts, bob.rupees, bob.arrows) == (111.5, 0, 0)
    assert (bob.sword_power, bob.direction, bob.max_hearts) == (20, 3, 0)
    assert (bob.mp, bob.kills, bob.deaths) == (0, 0, 0)
    assert (target.x, target.hearts) == (64.0, 2.0)
    build_other_player_props(2, {PLPROP.X2: bob.x, PLPROP.Y2: bob.y})
    build_player_props(bob._gs1_dirty_props)
    target.build_props_packet()
    # the builders also survive a non-finite value on their own: the prop is
    # skipped, the rest of the packet is intact
    assert (build_other_player_props(2, {PLPROP.X2: float("inf"), PLPROP.Y2: 3.0})
            == build_other_player_props(2, {PLPROP.Y2: 3.0}))
    assert (build_other_player_props(2, {PLPROP.X2: float("nan")})
            == build_other_player_props(2, {}))
    assert (build_npc_props(2, {NPCPROP.X2: float("inf"), NPCPROP.Y2: 3.0})
            == build_npc_props(2, {NPCPROP.Y2: 3.0}))


def test_bare_player_and_npc_attribute_writes_are_clamped_too():
    server, level = make_world(("alice", 10, 12))
    host = npc_with(
        f"if (created) {{ playerx = {BIG}; playerhearts = {NAN};"
        f" x = {BIG}; timeout = {NAN}; }}", level)
    run(host, server=server, player=server.players[1])
    assert server.players[1].x == 64.0 and server.players[1].hearts == 3.0
    assert host.x == 64.0 and host._timer_end == 0


def test_player_field_writes_reach_the_owner_and_the_other_clients():
    from pygserver.protocol.constants import PLPROP
    from pygserver.protocol.packets import (
        build_other_player_props, build_player_props)
    server, level = make_world(("alice", 10, 12), ("bob", 5, 5))
    bob = server.players[2]
    bob.sword_image = "sword2.png"
    host = npc_with(
        "if (created) { players[1].x = 33; players[1].y = 34; players[1].z = 2;"
        " players[1].hearts = 1; players[1].mp = 7; players[1].ap = 60;"
        " players[1].dir = 1; players[1].rupees = 99; players[1].swordpower = 2;"
        " players[1].kills = 4; }", level)
    drain(lambda: run(host, server=server, player=server.players[1]), 0.02)
    expected = {
        PLPROP.X2: 33.0, PLPROP.Y2: 34.0, PLPROP.Z: 2, PLPROP.Z2: 2.0,
        PLPROP.CURPOWER: 2, PLPROP.MAGICPOINTS: 7, PLPROP.ALIGNMENT: 60,
        PLPROP.SPRITE: 1, PLPROP.RUPEESCOUNT: 99, PLPROP.SWORDPOWER: 2,
        PLPROP.KILLSCOUNT: 4}
    assert bob._gs1_dirty_props == {} and len(bob.sent) == 1
    assert bob.sent == [build_player_props(expected)]
    # others get the same props minus the owner-only inventory counters, with
    # gear in its (power, image) form
    public = {k: v for k, v in expected.items() if k != PLPROP.RUPEESCOUNT}
    public[PLPROP.SWORDPOWER] = (2, "sword2.png")
    assert server.broadcasts == [("lvl", build_other_player_props(2, public))]
    assert server.excluded == [{2}]


def test_script_nickname_change_goes_to_everyone_not_just_the_level():
    from pygserver.protocol.constants import PLPROP
    from pygserver.protocol.packets import build_other_player_props
    server, level = make_world(("alice", 10, 12))
    everyone = []

    async def broadcast_to_all(packet, exclude=None):
        everyone.append((packet, exclude))

    server.broadcast_to_all = broadcast_to_all
    host = npc_with("if (created) { setplayerprop #n,Renamed; }", level)
    drain(lambda: run(host, server=server, player=server.players[1]), 0.02)
    assert everyone == [(build_other_player_props(
        1, {PLPROP.NICKNAME: "Renamed"}), {1})]


def test_allplayers_write_broadcasts_on_the_targets_own_level():
    server, level = make_world(("alice", 10, 12))
    carol = FakePlayer(5, "Carol", 3, 4, level=FakeLevel("elsewhere"))
    server.players[5] = carol
    host = npc_with("if (created) { allplayers[1].x = 50; allplayers[1].hearts = 1; }",
                    level)
    drain(lambda: run(host, server=server, player=server.players[1]), 0.02)
    assert (carol.x, carol.hearts) == (50.0, 1.0)
    assert [name for name, _ in server.broadcasts] == ["elsewhere"]


def test_object_field_tables_only_name_attributes_the_real_objects_have():
    from pygserver.gs1 import objects as gs1_objects
    player = FakePlayer(1, "alice", 1, 1)
    for field, attr in gs1_objects._PLAYER_ATTR.items():
        assert hasattr(player, attr), (field, attr)
    npc = NPC(1, "n")
    dynamic = {"ap", "z"}   # set by NPC.apply_client_prop, not in __init__
    for field, attr in gs1_objects._NPC_ATTR.items():
        assert hasattr(npc, attr) or attr in dynamic, (field, attr)
    # stateless reference properties read 0 and swallow writes
    server, level = make_world(("alice", 10, 12))
    host = npc_with(
        "if (created) { players[0].bombpower = 3; this.a = players[0].bombpower;"
        " this.b = players[0].lastdead; this.c = players[0].shootpower; }", level)
    run(host, server=server, player=server.players[0 + 1])
    assert not hasattr(server.players[1], "bomb_power")
    assert [this_vars(host)[k] for k in "abc"] == [0.0, 0.0, 0.0]


def test_npc_hurt_push_round_trips_through_the_real_hurt_bytes():
    server, level = make_world(("alice", 10, 12))
    host = npc_with("if (created) { npcs[1].hurtdx = 0.5; npcs[1].hurtdy = 0-2;"
                    " this.a = npcs[1].hurtdx; this.b = npcs[1].hurtdy; }", level)
    target = NPC(2, "t")
    level.add_npc(target)
    run(host, server=server, player=server.players[1])
    assert target.hurt_dxdy == (16, -32)  # dy clamped to -1.0
    assert (this_vars(host)["a"], this_vars(host)["b"]) == (0.5, -1.0)
    assert not hasattr(target, "hurt_dx")


def test_callnpc_self_recursion_is_limited_to_the_server_tick():
    server, level = make_world(("alice", 10, 12))
    count = [0]
    server.npc_manager = SimpleNamespace()

    def fire(target, event, player, params=None, **kw):
        count[0] += 1
        run_npc_event(target, event, server, player, params=params)

    server.npc_manager._fire_gs1 = fire
    a = npc_with("if (ping) { callnpc 0,ping; }", level)

    async def scenario():
        run_npc_event(a, "ping", server, None)
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert 5 <= count[0] <= 12          # ~20/s, not tens of thousands


def test_callnpc_backlog_is_capped_and_destroyed_targets_are_skipped():
    server, level = make_world(("alice", 10, 12))
    fired = []
    server.npc_manager = SimpleNamespace(_npcs={})
    server.npc_manager._fire_gs1 = lambda t, e, p, params=None, **kw: fired.append(t.id)
    caller = npc_with("if (created) { callnpc 1,die; }", level)
    victim = npc_with("if (die) { this.ran = 1; }", level, name="v", npc_id=2)
    server.npc_manager._npcs = {1: caller, 2: victim}

    async def scenario():
        run_npc_event(caller, "created", server, None)
        level.remove_npc(victim)            # destroyed before the queued event
        await asyncio.sleep(0.12)
        # a flood of calls from one frame queues at most the cap
        level.add_npc(victim)
        for _ in range(50):
            run_npc_event(caller, "created", server, None)
        await asyncio.sleep(0.12)

    asyncio.run(scenario())
    assert fired.count(2) == 8


def test_not_logged_in_connections_are_invisible_to_scripts():
    server, level = make_world(("alice", 10, 12), ("bob", 5, 5))
    server.players[2].logged_in = False
    host = npc_with(
        "if (created) { this.n = playerscount; this.p = players[1].x;"
        " this.a = allplayerscount; with (getplayer(bob)) { hearts = 1; }"
        " with (findplayer(bob)) { rupees = 5; } players[1].hearts = 1; }", level)
    run(host, server=server, player=server.players[1])
    v = this_vars(host)
    assert (v["n"], v["p"], v["a"]) == (1.0, 0.0, 1.0)
    bob = server.players[2]
    assert (bob.hearts, bob.rupees) == (3.0, 0)


def test_array_syntax_smoke_never_raises_and_keeps_bare_names_as_flags():
    server, level = make_world(("alice", 10, 12))
    alice = server.players[1]
    host = npc_with(
        "if (created) { this.a = players[-1].x; with (players[-1]) { this.b = 1;"
        " hearts = 2; } players = 5; this.p = players; items[2] = 7;"
        " this.i = items[2]; signs = 3; this.s = signs; this.z = players[0];"
        " this.y = players[0] + 1; }", level)
    run(host, server=server, player=alice)
    v = this_vars(host)
    assert (v["a"], v["b"], alice.hearts) == (10.0, 1.0, 2.0)
    assert (v["p"], v["i"], v["s"], v["z"], v["y"]) == (5.0, 0.0, 3.0, 0.0, 1.0)
    assert alice.flags["players"] == 5.0 and alice.flags["signs"] == 3.0
    # no triggering player (timeout): handles resolve to nothing, no error
    idle = npc_with("if (timeout) { this.a = players[-1].x;"
                    " with (players[-1]) { this.b = 1; } this.c = npcs[-1].x; }",
                    level, name="idle", npc_id=7)
    run(idle, "timeout", server, None)
    assert this_vars(idle) == {"a": 0.0, "c": 0.0}
