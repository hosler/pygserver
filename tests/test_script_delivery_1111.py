"""Level-NPC bytecode delivery, engine routing and PLO 176/177 (batch 1111).

Reference: GServer-v2 origin/beta4 (25396a18): Level.cpp:1473-1480 (NPC props
then PLO_NPCBYTECODE), PropertySerializers.cpp:554-555 (zeroed SCRIPT),
Script.cpp:504-538 (compile pipeline), ScriptSystem.cpp:82-172 (engine choice),
Player.cpp:746-752 (server-mode packets).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from reborn_protocol.gs2.compiler import find_compiler

from pygserver.config import ServerConfig
from pygserver.gs1.execution import class_program
from pygserver.gs2 import (
    GS2ScriptManager, engine_header, explicit_engine, server_half_is_gs2)
from pygserver.level import Level
from pygserver.npc import NPC, NPCManager
from pygserver.protocol.constants import PLO
from pygserver.protocol.packets import (
    build_kingdoms_mode, build_new_world_mode, build_npc_bytecode_raw,
    build_server_mode)

from .test_control_login import (
    flat_ids, framed_login, make_server, make_wire_player)
from .test_reborn_parity_cluster import LevelServer, make_player

GS2_CLIENT = "function onCreated() {\n  this.speed = 3;\n}"
GS1_CLIENT = "if (playerenters) {\n  message hi;\n}"
BYTECODE = bytes([0xDE, 0xAD, 0xBE, 0x0A, 0x01])


class FakeCompiler:
    """Stands in for gs2test: counts calls, returns canned bytecode."""

    def __init__(self, result=BYTECODE, error="", available=True):
        self.result = result
        self.last_error = error
        self.available = available
        self.calls = []

    def compile(self, source, name="script"):
        self.calls.append((source, name))
        return self.result


def make_manager(compiler=None, **options):
    config = ServerConfig(**options)
    server = SimpleNamespace(config=config, nc_manager=None)
    gs2 = GS2ScriptManager(server)
    gs2.compiler = compiler or FakeCompiler()
    server.gs2_manager = gs2
    return NPCManager(server), server, gs2.compiler


def attach(manager, client_half, server_half="", npc=None):
    npc = npc or NPC(5, "guard")
    manager.attach_gs1(npc, f"{server_half}\n//#CLIENTSIDE\n{client_half}")
    return npc


# -- wire vectors ------------------------------------------------------------

def test_raw_wrapped_bytecode_packet_matches_the_reference_vector():
    # Level.cpp:1477-1478 inside PLO_RAWDATA (spec 1111 section 1).
    assert build_npc_bytecode_raw(1234, b"\xde\xad\xbe") == bytes.fromhex(
        "84202028" "0a" "a3202972" "deadbe" "0a")


def test_server_mode_packets_match_the_reference_vectors():
    assert build_new_world_mode() == bytes.fromhex("d10a")
    assert build_kingdoms_mode() == bytes.fromhex("d00a")
    assert build_server_mode("NewWorld") == bytes.fromhex("d10a")
    assert build_server_mode("kingdoms") == bytes.fromhex("d00a")
    assert build_server_mode("normal") == b""
    assert build_server_mode("") == b""
    assert (PLO.KINGDOMSMODE, PLO.NEWWORLDMODE) == (176, 177)
    assert PLO.DISABLECLASSICMODE == 176 and PLO.FULLSTOP2 == 177


def test_bytecode_npc_zeroes_script_and_lists_joined_classes():
    npc = NPC(1234, "guard")
    npc.client_script = "text that must not be sent"
    npc.client_bytecode = BYTECODE
    npc._static_joins = ["foo", "bar"]
    props = npc.build_props_packet()
    assert bytes.fromhex("212020") in props          # SCRIPT = GSHORT 0
    assert bytes.fromhex("6a2027666f6f2c626172") in props   # CLASS "foo,bar"
    assert b"text that must not be sent" not in props
    # No joins: no CLASS prop at all (modTime unset in the reference).
    npc._static_joins = []
    assert bytes.fromhex("6a20") not in npc.build_props_packet()


def test_text_npc_keeps_its_script_prop():
    npc = NPC(9, "sign")
    npc.client_script = "setimg door.png;"
    props = npc.build_props_packet()
    assert b"setimg door.png;" in props
    assert npc.build_bytecode_packet() == b""


# -- engine resolution -------------------------------------------------------

@pytest.mark.parametrize("half, expected", [
    ("//#GS2\nfunction f(){}", "gs2"),
    ("//#gs1\nsetani a;", "gs1"),
    ("// license\n\n//#Gs2\nx;", "gs2"),
    ("//#BLOCKPOSITIONUPDATES\nx;", None),
    ("x;\n//#GS2", None),
    ("", None),
])
def test_engine_header_is_the_first_line_only(half, expected):
    assert engine_header(half) == expected


def test_explicit_engine_precedence_header_then_specific_then_generic():
    both = SimpleNamespace(default_scriptengine="gs1",
                           default_scriptengine_client="gs2",
                           default_scriptengine_server="")
    assert explicit_engine("x;", "client", both) == "gs2"
    assert explicit_engine("x;", "server", both) == "gs1"      # generic
    assert explicit_engine("//#GS1\nx;", "client", both) == "gs1"  # header wins
    assert explicit_engine("x;", "client", ServerConfig()) is None
    junk = SimpleNamespace(default_scriptengine_client="v8")
    assert explicit_engine("x;", "client", junk) is None


def test_server_half_routing_keeps_todays_defaults():
    config = ServerConfig()
    gs2_class = "function onCreated() { this.a = 1; }"
    assert server_half_is_gs2(gs2_class, "class", config) is True
    assert server_half_is_gs2(gs2_class, "npc", config) is False
    assert server_half_is_gs2("//#GS1\n" + gs2_class, "class", config) is False
    assert server_half_is_gs2("//#GS2\nx;", "npc", config) is True


def test_class_program_obeys_a_gs1_header_on_gs2_shaped_source():
    source = "//#GS1\nfunction onCreated() { this.a = 1; }"
    script = SimpleNamespace(source=source)
    server = SimpleNamespace(
        config=ServerConfig(),
        gs2_manager=SimpleNamespace(get_class=lambda name: script))
    assert class_program(server, "k") is not None
    plain = SimpleNamespace(source=source.split("\n", 1)[1])
    server.gs2_manager = SimpleNamespace(get_class=lambda name: plain)
    assert class_program(server, "k") is None


def test_config_reads_the_new_options(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("servermode = NewWorld\ngeneration = modern # x\n"
                    "default_scriptengine = gs1\n"
                    "default_scriptengine_client = GS2\n")
    config = ServerConfig.from_file(str(path))
    assert (config.servermode, config.generation) == ("newworld", "modern")
    assert config.default_scriptengine == "gs1"
    assert config.default_scriptengine_client == "gs2"
    assert ServerConfig().servermode == "normal"
    assert ServerConfig().generation == ""


# -- attach_gs1: choose text or bytecode --------------------------------------

def test_gs2_client_half_compiles_and_replaces_the_text():
    manager, _, compiler = make_manager()
    npc = attach(manager, GS2_CLIENT)
    assert npc.client_bytecode == BYTECODE
    assert len(compiler.calls) == 1
    assert npc.client_script == GS2_CLIENT          # kept, just not sent
    assert npc.build_bytecode_packet() == build_npc_bytecode_raw(5, BYTECODE)


def test_gs1_client_half_stays_text_and_never_compiles():
    manager, _, compiler = make_manager()
    npc = attach(manager, GS1_CLIENT)
    assert npc.client_bytecode == b"" and compiler.calls == []
    assert GS1_CLIENT.encode().replace(b"\n", b"\xa7") in npc.build_props_packet()


def test_no_client_half_means_no_bytecode():
    manager, _, compiler = make_manager()
    npc = NPC(5, "guard")
    manager.attach_gs1(npc, "if (created) { this.a = 1; }")
    assert npc.client_bytecode == b"" and compiler.calls == []


def test_gs1_header_on_a_gs2_shaped_half_is_not_compiled():
    manager, _, compiler = make_manager()
    npc = attach(manager, "//#GS1\n" + GS2_CLIENT)
    assert npc.client_bytecode == b"" and compiler.calls == []


def test_gs2_header_on_an_unclear_half_compiles():
    manager, _, compiler = make_manager()
    npc = attach(manager, "//#GS2\nthis.x = 1;")
    assert npc.client_bytecode == BYTECODE and len(compiler.calls) == 1


def test_option_picks_the_engine_and_the_header_still_wins():
    manager, _, compiler = make_manager(default_scriptengine_client="gs2")
    assert attach(manager, "this.x = 1;").client_bytecode == BYTECODE
    assert attach(manager, "//#GS1\nthis.x = 1;").client_bytecode == b""
    manager, _, compiler = make_manager(default_scriptengine="gs1")
    assert attach(manager, GS2_CLIENT).client_bytecode == b""
    assert compiler.calls == []


@pytest.mark.parametrize("options", [
    {"generation": "classic"}, {"generation": "newmain"},
    {"serverside": False}])
def test_generation_and_serverside_gates_keep_text_only(options):
    manager, _, compiler = make_manager(**options)
    npc = attach(manager, GS2_CLIENT)
    assert npc.client_bytecode == b"" and compiler.calls == []
    assert npc.build_bytecode_packet() == b""


def test_generation_modern_is_accepted():
    manager, _, _ = make_manager(generation="modern")
    assert attach(manager, GS2_CLIENT).client_bytecode == BYTECODE


def test_missing_compiler_keeps_text_without_complaint(caplog):
    manager, _, compiler = make_manager(FakeCompiler(available=False))
    with caplog.at_level("WARNING"):
        npc = attach(manager, GS2_CLIENT)
    assert npc.client_bytecode == b"" and compiler.calls == []
    assert not caplog.records


async def test_compile_failure_sends_text_logs_and_tells_the_ncs(caplog):
    manager, server, compiler = make_manager(
        FakeCompiler(result=None, error="line 2: parse error"))
    nc = SimpleNamespace(_send_to_ncs=AsyncMock())
    server.nc_manager = nc
    with caplog.at_level("WARNING"):
        npc = attach(manager, GS2_CLIENT)
        await asyncio.sleep(0)
    assert npc.client_bytecode == b""
    assert npc.build_bytecode_packet() == b""            # no 131
    assert GS2_CLIENT.replace("\n", "\xa7").encode("latin-1") in npc.build_props_packet()
    assert any("failed to compile" in r.message for r in caplog.records)
    nc._send_to_ncs.assert_awaited_once()
    assert "[NC NPC compile error]" in nc._send_to_ncs.await_args.args[0]
    assert "parse error" in nc._send_to_ncs.await_args.args[0]


def test_script_replacement_clears_the_old_bytecode():
    manager, _, _ = make_manager()
    npc = attach(manager, GS2_CLIENT)
    assert npc.client_bytecode
    manager.attach_gs1(npc, "if (created) { this.a = 1; }")
    assert npc.client_bytecode == b""


@pytest.mark.skipif(find_compiler() is None, reason="gs2test not built")
def test_real_compiler_produces_deliverable_bytecode():
    server = SimpleNamespace(config=ServerConfig())
    bytecode, error = GS2ScriptManager(server).compile_npc_client(
        "function onCreated() {\n  this.speed = 3;\n}", "t")
    assert error == "" and bytecode


# -- delivery ------------------------------------------------------------------

def test_level_entry_sends_bytecode_right_after_that_npcs_props():
    level = Level("one.nw")
    server = LevelServer([level])
    player = make_player(server, 1, level)
    scripted, plain = NPC(21, "a"), NPC(22, "b")
    scripted.client_bytecode = BYTECODE
    plain.client_script = "setimg x.png;"
    for npc in (scripted, plain):
        npc.x = npc.y = 5
        level.add_npc(npc)

    asyncio.run(player._send_level(level))

    sent = [call.args[0] for call in player.send_raw.await_args_list]
    raw = build_npc_bytecode_raw(21, BYTECODE)
    assert sent.count(raw) == 1
    assert sent[sent.index(raw) - 1] == scripted.build_props_packet()
    assert build_npc_bytecode_raw(22, BYTECODE) not in sent
    assert not any(p.startswith(bytes([PLO.RAWDATA + 32])) and b"\xde" in p
                   for p in sent if p != raw)


def make_tick_manager(level):
    server = SimpleNamespace(
        config=ServerConfig(), broadcast_to_level=AsyncMock(),
        world=SimpleNamespace(get_gmap_for_level=lambda name: None),
        audience=SimpleNamespace(players_on_level=lambda name: []),
        get_player=lambda pid: None)
    return NPCManager(server), server


async def test_script_update_rebroadcasts_props_then_bytecode():
    level = Level("one.nw")
    manager, server = make_tick_manager(level)
    npc = manager.create_npc(level=level, x=3, y=3)
    await asyncio.sleep(0)
    npc.client_bytecode = BYTECODE
    npc._script_dirty = True
    npc.mark_dirty()

    await manager.tick()

    sent = [call.args[1] for call in server.broadcast_to_level.await_args_list]
    assert sent[0].startswith(bytes([PLO.NPCPROPS + 32]))
    assert bytes.fromhex("212020") in sent[0]
    assert sent[1] == build_npc_bytecode_raw(npc.id, BYTECODE)
    # A plain movement update carries neither the script nor the bytecode.
    server.broadcast_to_level.reset_mock()
    npc.x = 4
    npc.mark_dirty()
    await manager.tick()
    assert len(server.broadcast_to_level.await_args_list) == 1


async def test_class_change_marks_only_bytecode_npcs_that_joined_it():
    level = Level("one.nw")
    manager, _ = make_tick_manager(level)
    joined = manager.create_npc(level=level)
    other = manager.create_npc(level=level)
    text = manager.create_npc(level=level)
    await asyncio.sleep(0)
    for npc in (joined, other, text):
        npc._dirty = npc._script_dirty = False
    joined.client_bytecode = other.client_bytecode = BYTECODE
    joined._static_joins = ["Shared"]
    text._static_joins = ["shared"]
    other._static_joins = ["unrelated"]

    manager.mark_class_users_dirty("shared")

    assert joined._script_dirty and joined._dirty
    assert not other._script_dirty and not text._script_dirty


def test_class_upsert_and_delete_notify_the_npc_manager(tmp_path):
    config = ServerConfig(base_dir=str(tmp_path))
    server = SimpleNamespace(config=config, npc_manager=Mock())
    gs2 = GS2ScriptManager(server, str(tmp_path))
    gs2.compiler = FakeCompiler()
    gs2.upsert_class("Shared", "if (created) { this.a = 1; }")
    server.npc_manager.mark_class_users_dirty.assert_called_with("Shared")
    server.npc_manager.mark_class_users_dirty.reset_mock()
    assert gs2.delete_class("Shared")
    server.npc_manager.mark_class_users_dirty.assert_called_with("Shared")


# -- PLO 176/177 at login -------------------------------------------------------

def first_frame(servermode, client_type=5, account="player", staff=()):
    server = make_server(staff=staff)
    server.server_mode = servermode
    player, trace = make_wire_player(server, client_type, False, account)
    if client_type in (1, 3):
        player.session.read.return_value = framed_login(
            client_type, include_key=False)
    assert asyncio.run(player._handle_login()) is True
    assert trace[0][0] == "login-frame"
    return trace[0][1], flat_ids(trace)


def test_normal_mode_sends_neither_176_nor_177():
    ids, _ = first_frame("normal")
    assert 176 not in ids and 177 not in ids
    assert ids[:2] == [194, PLO.PLAYERPROPS]


@pytest.mark.parametrize("mode, packet_id", [("newworld", 177), ("kingdoms", 176)])
def test_mode_packet_leads_the_game_login_and_is_exclusive(mode, packet_id):
    ids, all_ids = first_frame(mode)
    other = 176 if packet_id == 177 else 177
    assert ids[:3] == [packet_id, 194, PLO.PLAYERPROPS]
    assert other not in all_ids and all_ids.count(packet_id) == 1


def test_mode_packet_also_leads_rc_and_nc_logins():
    ids, _ = first_frame("newworld", client_type=1, account="hosler",
                         staff=("hosler",))
    assert ids[:3] == [177, 194, PLO.PLAYERPROPS]
    server = make_server(staff=("hosler",))
    server.server_mode = "kingdoms"
    from pygserver.nc import NCManager
    server.nc_manager = NCManager(server)
    player, trace = make_wire_player(server, 3, False, "hosler")
    player.session.read.return_value = framed_login(3, include_key=False)
    assert asyncio.run(player._handle_login()) is True
    assert trace[0][1][:2] == [176, PLO.PLAYERPROPS]


# -- fix round: CLASS list (static scan) ---------------------------------------

from pygserver.gs2 import GS2Script  # noqa: E402
from pygserver.npc import MAX_JOINED_CLASSES, scan_gs2_joins  # noqa: E402
from pygserver.protocol.packets import build_npc_del, build_npc_del2  # noqa: E402

CLASS_FOO_BAR = bytes.fromhex("6a2027666f6f2c626172")


def add_classes(manager, *names, source="if (created) { this.k = 1; }"):
    gs2 = manager.server.gs2_manager
    for name in names:
        gs2.classes[name.lower()] = GS2Script(
            kind="class", name=name, source=source)


GS2_JOINER = 'function onCreated() {\n  this.join("foo");\n}'


def test_scan_reads_only_literal_gs2_join_forms():
    code = ('join("a"); join ("b");\nthis.join("c"); // join("e")\n'
            '"join(\\"f\\")"; /* join("g") */ x = 1; rejoin("h"); joined("i");\n'
            'obj.join("j"); join(this.k); join("A"); join("q,r"); join("#s(c)");\n'
            'join("x(y)"); join(""); temp.join = 1; join foo;')
    assert scan_gs2_joins(code) == ["a", "b", "c"]
    assert scan_gs2_joins('this . join("z")') == []


def test_scan_is_bounded_and_rejects_bad_names():
    many = "".join(f'join("c{i}");' for i in range(MAX_JOINED_CLASSES + 10))
    assert len(scan_gs2_joins(many)) == MAX_JOINED_CLASSES
    assert scan_gs2_joins(f'join("{"x" * 129}");join("ok");') == ["ok"]
    assert scan_gs2_joins('join("a\x01b");join("c\xa7d");') == []


def test_scan_follows_joined_classes_own_joins():
    sources = {"outer": 'join("inner");', "inner": 'join("outer"); join("deep");'}
    assert scan_gs2_joins('join("outer");', sources.get) == [
        "outer", "inner", "deep"]


ADVERSARIAL = {
    "join + spaces, no semicolon": "join " + " " * 65536,
    "join + spaces + name": "join(" + " " * 32768 + "a" * 32768,
    "repeated join + spaces": ("join " + " " * 200) * 320,
    "unterminated string": '"' + "a" * 65536,
    "escaped quotes": '"' + '\\"' * 32768,
    "unterminated comment": "/*" + "join(" * 13000,
    "join in braces": "if (created) { join" + " " * 65000 + " foo }",
    "many joins": 'join("a") ' * 6500,
    # Fix round 3: backslash parity and CR-only line ends.
    "even backslashes before quote": '"\\\\"' * 16000,
    "odd then even backslashes": '"\\"\\\\"' * 10000,
    "backslash run then quotes": '"' + "\\" * 30000 + '"' * 30000,
    "CR-only comments": "// c\r" * 13000,
    "CR-only strings": '"abc\r' * 16000,
    "CR-only with joins": 'x; // c\rjoin("a");\r' * 3000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_scanner_stays_inside_a_time_budget_on_adversarial_input(name):
    import time
    code = ADVERSARIAL[name]
    start = time.perf_counter()
    scan_gs2_joins(code)
    assert time.perf_counter() - start < 0.05, name


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_scanner_is_linear(name):
    import time
    code = ADVERSARIAL[name][:16384]
    def best(text):
        times = []
        for _ in range(5):
            start = time.perf_counter()
            scan_gs2_joins(text)
            times.append(time.perf_counter() - start)
        return min(times)
    small, double = best(code), best(code + code)
    assert double < 2.5 * small + 0.002, (name, small, double)


def test_a_gs1_script_joins_as_it_runs_and_attach_clears_the_list():
    manager, _, _ = make_manager()
    add_classes(manager, "foo", "bar")
    npc = attach(manager, GS2_CLIENT, "if (created) { join foo; }")
    assert npc.joined_class_names() == ["foo"] and b"foo" in npc.build_props_packet()
    # An attach that does not run `created` (only tests ask for that): the
    # old list is gone, the new script has not run yet, and a later run
    # re-joins and notifies.
    manager.attach_gs1(
        npc, f"if (playerenters) {{ join bar; }}\n//#CLIENTSIDE\n{GS2_CLIENT}",
        raise_errors=True, fire_created=False)
    assert npc.joined_class_names() == [] and b"foo" not in npc.build_props_packet()
    npc._script_dirty = npc._dirty = False
    npc.__dict__.setdefault("_gs1_joined", []).append("bar")
    npc.note_join("bar")
    assert npc._script_dirty and b"bar" in npc.build_props_packet()


def test_a_re_attached_script_can_join_the_same_class_again(monkeypatch):
    import pygserver.gs1.commands.objects as objects
    manager, server, _ = make_manager()
    add_classes(manager, "foo")
    npc = attach(manager, GS2_CLIENT, "if (created) { join foo; }")
    monkeypatch.setattr(objects, "_source_npc", lambda ctx: npc)
    monkeypatch.setattr(objects, "_running_npc", lambda ctx: None)
    manager.attach_gs1(
        npc, f"if (created) {{ join foo; }}\n//#CLIENTSIDE\n{GS2_CLIENT}\n//x",
        raise_errors=True, fire_created=False)
    npc._script_dirty = npc._dirty = False
    objects._c_join(SimpleNamespace(server=server), ["foo"], npc, None, None)
    assert npc._script_dirty and npc.joined_class_names() == ["foo"]


def test_gs2_routed_server_half_lists_its_literal_joins():
    manager, _, _ = make_manager()
    add_classes(manager, "foo")
    npc = attach(manager, GS2_CLIENT, "//#GS2\n" + GS2_JOINER)
    assert npc.gs1_program is None
    assert npc.joined_class_names() == ["foo"]
    assert b"foo" in npc.build_props_packet()
    # Header only: GS1 syntax under //#GS2 lists nothing.
    plain = attach(manager, GS2_CLIENT, "//#GS2\nif (created) { join foo; }")
    assert plain.joined_class_names() == []


def test_class_edit_rescans_a_static_list_through_the_class_joins():
    manager, server, _ = make_manager(default_scriptengine_server="gs2")
    add_classes(manager, "foo", source='function onCreated() { x = 1; }')
    npc = attach(manager, GS2_CLIENT, GS2_JOINER)
    manager._npcs[npc.id] = npc
    assert npc.joined_class_names() == ["foo"]
    npc._script_dirty = npc._dirty = False
    add_classes(manager, "foo", source='function onCreated() { this.join("baz"); }')
    manager.mark_class_users_dirty("foo")
    assert npc.joined_class_names() == ["foo", "baz"]
    assert npc._script_dirty and b"baz" in npc.build_props_packet()
    # An edit that changes nothing in the list still refreshes the clients.
    npc._script_dirty = npc._dirty = False
    manager.mark_class_users_dirty("foo")
    assert npc._script_dirty
    # An unrelated class leaves it alone.
    npc._script_dirty = npc._dirty = False
    manager.mark_class_users_dirty("other")
    assert not npc._script_dirty


# -- fix round: generation changed at runtime ------------------------------------

def test_generation_flip_falls_back_to_text_at_send_time():
    manager, server, _ = make_manager()
    npc = attach(manager, GS2_CLIENT)
    assert npc.build_bytecode_packet()
    server.config.generation = "classic"
    props = npc.build_props_packet()
    assert npc.build_bytecode_packet() == b""
    assert bytes.fromhex("212020") not in props
    assert GS2_CLIENT.replace("\n", "\xa7").encode("latin-1") in props
    server.config.generation = ""
    assert npc.build_bytecode_packet()


# -- fix round: bytecode -> text drops the client's NPC ------------------------------

async def tick_sent(manager, server):
    server.broadcast_to_level.reset_mock()
    await manager.tick()
    return [call.args[1] for call in server.broadcast_to_level.await_args_list]


async def make_sent_npc(source_half=GS2_CLIENT):
    level = Level("one.nw")
    manager, server = make_tick_manager(level)
    server.gs2_manager = GS2ScriptManager(server)
    server.gs2_manager.compiler = FakeCompiler()
    npc = manager.create_npc(level=level, x=3, y=3)
    await asyncio.sleep(0)
    manager.attach_gs1(npc, f"\n//#CLIENTSIDE\n{source_half}")
    npc.build_props_packet()             # a level entry: clients now have it
    await tick_sent(manager, server)     # flush the attach
    return manager, server, npc


async def test_nc_edit_from_gs2_to_gs1_deletes_before_resending():
    manager, server, npc = await make_sent_npc()
    manager.attach_gs1(npc, f"\n//#CLIENTSIDE\n{GS1_CLIENT}",
                       raise_errors=True, fire_created=False)
    sent = await tick_sent(manager, server)
    assert sent[0] == build_npc_del2("one.nw", npc.id)
    assert sent[1].startswith(bytes([PLO.NPCPROPS + 32]))
    assert GS1_CLIENT.replace("\n", "\xa7").encode("latin-1") in sent[1]
    assert not any(p.startswith(bytes([PLO.RAWDATA + 32])) for p in sent)


async def test_generation_flip_at_runtime_deletes_and_resends_text():
    manager, server, npc = await make_sent_npc()
    server.config.generation = "classic"
    sent = await tick_sent(manager, server)
    assert sent[0] == build_npc_del2("one.nw", npc.id)
    assert GS2_CLIENT.replace("\n", "\xa7").encode("latin-1") in sent[1]
    # Settled: the next tick sends nothing.
    assert await tick_sent(manager, server) == []


async def test_gs2_to_gs2_update_and_text_to_text_do_not_delete():
    manager, server, npc = await make_sent_npc()
    manager.attach_gs1(npc, f"\n//#CLIENTSIDE\n{GS2_CLIENT}\n// x")
    sent = await tick_sent(manager, server)
    assert build_npc_del2("one.nw", npc.id) not in sent
    manager, server, npc = await make_sent_npc(GS1_CLIENT)
    manager.attach_gs1(npc, f"\n//#CLIENTSIDE\n{GS1_CLIENT}\nx;")
    assert build_npc_del2("one.nw", npc.id) not in await tick_sent(manager, server)


# -- fix round: GS2-routed server halves warn -------------------------------------------

def test_gs2_routed_server_half_warns_once_per_script(caplog):
    import pygserver.gs2 as gs2_module
    gs2_module._warned_server_halves.clear()
    manager, _, _ = make_manager()
    with caplog.at_level("WARNING"):
        attach(manager, GS2_CLIENT, "//#GS2\nthis.a = 1;")
        attach(manager, GS2_CLIENT, "//#GS2\nthis.a = 1;")
    warned = [r for r in caplog.records if "no GS2 server engine" in r.message]
    assert len(warned) == 1


def test_gs2_default_engine_option_warns_at_startup(caplog):
    for key in ("default_scriptengine", "default_scriptengine_server"):
        caplog.clear()
        with caplog.at_level("WARNING"):
            make_manager(**{key: "gs2"})
        assert any(key in r.message and "will NOT run" in r.message
                   for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING"):
        make_manager(default_scriptengine_client="gs2")
        make_manager()
    assert not caplog.records


# -- NC_NPCRESET to the recorded initial state ----------------------------------------

from .test_nc_npc_handlers import gint3, make_nc, run  # noqa: E402


def test_reset_restores_the_added_state_and_warps_back():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(
            session, b"Guard,10050,Soldier,me,town.nw,4,6")
        npc = server.npc_manager.get_npc(10050)
        assert (npc.initial_level, npc.initial_x, npc.initial_y) == (
            "town.nw", 4.0, 6.0)
        npc.image = "x.png"
        npc.x, npc.y = 20.0, 21.0
        npc.nickname, npc.vis_flags, npc.block_flags = "n", 9, 3
        npc.saves = [1] * 10
        npc._timer_end = 99
        npc.hurt_dxdy = (1, 1)
        npc.no_player_on_wall = True
        npc.flags["a"] = "1"
        server.broadcast_to_all.reset_mock()
        await nc._handle_npc_reset(session, gint3(10050))
        return npc

    npc = run(go())
    assert (npc.x, npc.y) == (4.0, 6.0) and npc.image == ""
    assert (npc.nickname, npc.vis_flags, npc.block_flags) == ("", 1, 0)
    assert npc.saves == [0] * 10 and npc._timer_end == 0
    assert npc.hurt_dxdy is None and not npc.no_player_on_wall
    assert npc.flags == {} and npc._script_dirty
    server.broadcast_to_all.assert_awaited_once_with(build_npc_del(10050))


def test_reset_relocates_an_npc_that_was_warped_elsewhere():
    nc, session, server, levels = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,4,6")
        npc = server.npc_manager.get_npc(10050)
        levels["town.nw"].remove_npc(npc)
        levels["cave.nw"].add_npc(npc)
        npc.x, npc.y = 30.0, 31.0
        await nc._handle_npc_reset(session, gint3(10050))
        return npc

    npc = run(go())
    assert npc.level is levels["town.nw"] and (npc.x, npc.y) == (4.0, 6.0)


def test_reset_queues_created_and_keeps_the_notice():
    from pygserver.protocol.packets import build_rc_chat
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        npc = server.npc_manager.get_npc(10050)
        server.npc_manager.attach_gs1(
            npc, "if (created) { this.runs = this.runs + 1; }")
        npc.gs1_scopes["this"]["runs"] = 7      # wiped by the reset
        session.player.send_raw.reset_mock()
        await nc._handle_npc_reset(session, gint3(10050))
        return npc

    npc = run(go())
    assert npc.gs1_scopes["this"].get("runs") == 1
    assert session.player.send_raw.await_args_list[-1].args[0] == build_rc_chat(
        "NPC script of Guard reset by editor")


async def test_first_warp_records_the_initial_state_of_an_unattached_npc():
    level = Level("one.nw")
    manager, _ = make_tick_manager(level)
    npc = manager.create_npc(x=1, y=2)
    await asyncio.sleep(0)
    assert npc.initial_level is None
    npc.x, npc.y = 8, 9
    await manager.relocate_npc(npc, level)
    assert (npc.initial_level, npc.initial_x, npc.initial_y) == ("one.nw", 8, 9)


# -- fix round 2: appearance, showimgs, warning set -----------------------------------

def test_reset_restores_the_initial_character():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,4,6")
        npc = server.npc_manager.get_npc(10050)
        import copy
        initial = {f: copy.copy(getattr(npc, f)) for f in npc._CHARACTER_FIELDS}
        npc.gani, npc.message = "walk", "hello"
        npc.head_image, npc.body_image = "h.png", "b.png"
        npc.sword_image, npc.shield_image, npc.horse_image = "s", "sh", "ho"
        npc.direction, npc.colors, npc.glove_power = 1, [9] * 5, 2
        npc.gattribs[36] = "x"
        await nc._handle_npc_reset(session, gint3(10050))
        return npc, initial

    npc, initial = run(go())
    for field, value in initial.items():
        assert getattr(npc, field) == value, field
    assert npc.gattribs == {} and npc._script_dirty


async def test_reset_replays_the_kept_showimgs_after_the_props():
    level = Level("one.nw")
    manager, server = make_tick_manager(level)
    npc = manager.create_npc(level=level, x=3, y=3)
    await asyncio.sleep(0)
    npc.showimgs[3] = {0: "x.png"}
    npc._resend_showimgs = True
    npc._script_dirty = True
    npc.mark_dirty()
    sent = await tick_sent(manager, server)
    assert sent[0].startswith(bytes([PLO.NPCPROPS + 32]))
    assert sent[-1] == npc.build_showimgs_packet()
    assert npc._resend_showimgs is False


def test_nc_reset_keeps_the_showimgs_and_flags_them_for_replay():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,4,6")
        npc = server.npc_manager.get_npc(10050)
        npc.showimgs[3] = {0: "x.png"}
        await nc._handle_npc_reset(session, gint3(10050))
        return npc

    npc = run(go())
    assert npc.showimgs == {3: {0: "x.png"}} and npc._resend_showimgs


def test_server_half_warning_set_is_keyed_by_script_and_capped(caplog):
    import pygserver.gs2 as gs2_module
    gs2_module._warned_server_halves.clear()
    config = ServerConfig(default_scriptengine_server="gs2")
    with caplog.at_level("WARNING"):
        for npc_id in range(50):       # a putnpc loop: same source, new ids
            gs2_module.note_gs2_server_half(
                "NPC", f"npc (id {npc_id})", "this.a = 1;", config)
    assert len(gs2_module._warned_server_halves) == 1
    assert len(caplog.records) == 1
    for index in range(gs2_module._MAX_WARNED_SERVER_HALVES + 40):
        gs2_module.note_gs2_server_half(
            "NPC", "x", f"this.a = {index};", config)
    assert len(gs2_module._warned_server_halves) <= gs2_module._MAX_WARNED_SERVER_HALVES


# -- fix round 3: scanner shapes -------------------------------------------------

def test_scan_closes_a_string_ending_in_an_escaped_backslash():
    # `"\\"` is a string holding one backslash: the quote after an even run
    # of backslashes closes it, so the join that follows is real.
    assert scan_gs2_joins('a = "\\\\"; join("q");') == ["q"]
    assert scan_gs2_joins('a = "x\\\\\\\\"; join("q");') == ["q"]
    # An odd run escapes the quote: the string runs to the line's end, so a
    # join on the same line is inside it.
    assert scan_gs2_joins('a = "\\"; join("q");') == []
    assert scan_gs2_joins('a = "\\\\\\" join("q");\njoin("r");') == ["r"]


def test_scan_ends_a_comment_or_string_at_a_lone_carriage_return():
    assert scan_gs2_joins('// note\rjoin("x");\r') == ["x"]
    assert scan_gs2_joins('a = "open\rjoin("x");') == ["x"]
    assert scan_gs2_joins('// note\r\njoin("x");') == ["x"]
    assert scan_gs2_joins('// note without any line end join("x");') == []


# -- fix round 3: class edits and attaches do bounded work ------------------------

CLASS_FILLER = ' this.a = "x"; // CLASSBODY\n' * 400


class WorkCounter:
    """Counts direct scans and the minify calls that see a class body."""

    def __init__(self, monkeypatch):
        import pygserver.gs2 as gs2_module
        import pygserver.npc as npc_module
        self.scans = 0
        self.class_minifies = 0
        real_scan, real_minify = npc_module.scan_direct_joins, gs2_module.minify

        def scan(code):
            self.scans += 1
            return real_scan(code)

        def minify(text, *args, **kwargs):
            if "CLASSBODY" in text:
                self.class_minifies += 1
            return real_minify(text, *args, **kwargs)

        monkeypatch.setattr(npc_module, "scan_direct_joins", scan)
        monkeypatch.setattr(gs2_module, "minify", minify)


def gs2_fleet(monkeypatch, classes=64, npcs=20):
    manager, server, _ = make_manager(default_scriptengine_server="gs2")
    names = [f"c{i}" for i in range(classes)]
    add_classes(manager, *names, "other", source=CLASS_FILLER)
    root = "".join(f'join("{name}");' for name in names)
    members = []
    for index in range(npcs):
        npc = attach(manager, GS2_CLIENT, root, NPC(1000 + index, "n"))
        manager._npcs[npc.id] = npc
        members.append(npc)
    return manager, server, names, root, members


def test_editing_a_class_nobody_joins_scans_and_minifies_nothing(monkeypatch):
    manager, _, _, _, members = gs2_fleet(monkeypatch)
    work = WorkCounter(monkeypatch)
    refreshed = []
    real = manager._refresh_static_joins
    monkeypatch.setattr(manager, "_refresh_static_joins",
                        lambda npc, **kw: refreshed.append(npc) or real(npc, **kw))
    for npc in members:
        npc.client_bytecode = BYTECODE
        npc._script_dirty = npc._dirty = False
    manager.mark_class_users_dirty("unrelated")
    assert (refreshed, work.scans, work.class_minifies) == ([], 0, 0)
    assert not any(npc._script_dirty for npc in members)


def test_editing_a_joined_class_rescans_that_class_once_for_all_npcs(monkeypatch):
    manager, _, _, _, members = gs2_fleet(monkeypatch, npcs=50)
    gs2 = manager.server.gs2_manager
    for npc in members:
        npc.client_bytecode = BYTECODE
        npc._script_dirty = npc._dirty = False
    work = WorkCounter(monkeypatch)
    gs2.classes["c7"] = GS2Script(
        kind="class", name="c7", source='this.join("fresh");' + CLASS_FILLER)
    manager.mark_class_users_dirty("c7")
    assert work.scans == 1 and work.class_minifies == 1
    assert all(npc._script_dirty for npc in members)
    # Depth first: the new join sits right after the class that made it, and
    # the 64-name cap drops the tail.
    for npc in members:
        names = npc.joined_class_names()
        assert names[names.index("c7") + 1] == "fresh" and len(names) == 64
    # The fresh entry is cached: an attach afterwards scans only its own half.
    attach(manager, GS2_CLIENT, "".join(f'join("c{i}");' for i in range(64)),
           NPC(3000, "late"))
    assert work.scans == 2 and work.class_minifies == 1


def test_attach_with_cached_classes_never_rescans_or_reminifies_them(monkeypatch):
    manager, _, names, root, _ = gs2_fleet(monkeypatch, npcs=1)
    work = WorkCounter(monkeypatch)
    npc = attach(manager, GS2_CLIENT, root, NPC(2000, "fresh"))
    assert npc.joined_class_names() == names
    assert work.scans == 1                     # the NPC's own half only
    assert work.class_minifies == 0


def test_cache_follows_a_changed_class_even_without_the_notification(monkeypatch):
    manager, _, _, _, _ = gs2_fleet(monkeypatch, classes=2, npcs=0)
    npc = attach(manager, GS2_CLIENT, 'join("c0");', NPC(2000, "n"))
    assert npc.joined_class_names() == ["c0"]
    add_classes(manager, "c0", source='this.join("viaedit");')
    manager._refresh_static_joins(npc)
    assert npc.joined_class_names() == ["c0", "viaedit"]
    manager.server.gs2_manager.classes.pop("c0")
    manager._refresh_static_joins(npc)
    assert npc.joined_class_names() == ["c0"]
    assert "c0" not in manager._class_join_cache


def test_option_change_re_routes_cached_classes(monkeypatch):
    manager, server, _, _, _ = gs2_fleet(monkeypatch, classes=1, npcs=0)
    add_classes(manager, "c0", source='this.join("inner");')
    npc = attach(manager, GS2_CLIENT, 'join("c0");', NPC(2000, "n"))
    assert npc.joined_class_names() == ["c0", "inner"]
    server.config.default_scriptengine_server = "gs1"
    # The class still looks like GS2 by shape; an explicit option wins.
    manager._refresh_static_joins(npc)
    assert npc.joined_class_names() == ["c0"]


def test_collect_joins_reads_at_most_the_documented_bound():
    from pygserver.npc import MAX_DIRECT_JOINS, collect_joins
    calls = []

    def wide(name):
        calls.append(name)
        return [f"{name}.{i}" for i in range(MAX_DIRECT_JOINS)]

    found = collect_joins([f"r{i}" for i in range(500)], wide)
    assert len(found) == MAX_JOINED_CLASSES
    assert len(calls) <= MAX_JOINED_CLASSES


def test_cold_then_warm_attach_and_unjoined_edit_stay_inside_a_time_budget():
    import time
    config = ServerConfig(default_scriptengine_server="gs2")
    server = SimpleNamespace(config=config, nc_manager=None)
    gs2 = GS2ScriptManager(server)
    gs2.compiler = FakeCompiler()
    server.gs2_manager = gs2
    manager = NPCManager(server)
    names = [f"c{i}" for i in range(64)]
    add_classes(manager, *names, "other", source=CLASS_FILLER * 8)
    root = "".join(f'join("{name}");' for name in names)
    for index in range(100):
        npc = attach(manager, GS2_CLIENT, root, NPC(1000 + index, "n"))
        manager._npcs[npc.id] = npc
    start = time.perf_counter()
    manager.mark_class_users_dirty("other")
    assert time.perf_counter() - start < 0.05
    start = time.perf_counter()
    attach(manager, GS2_CLIENT, root, NPC(3000, "n"))
    assert time.perf_counter() - start < 0.05
    start = time.perf_counter()
    manager.mark_class_users_dirty("c3")
    assert time.perf_counter() - start < 0.25


# -- fix round 3: NC script set runs `created` -------------------------------------

def test_nc_script_set_runs_created_so_joins_survive_the_attach():
    from .test_nc_npc_handlers import gint3, make_nc, run
    nc, session, server, _ = make_nc()
    server.gs2_manager = GS2ScriptManager(server)
    server.gs2_manager.compiler = FakeCompiler()
    manager = server.npc_manager
    add_classes(manager, "foo")
    script = (b'if (created) { join foo; },//#CLIENTSIDE,'
              b'function onCreated() {,"  this.speed = 3;",}')

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        npc = manager.get_npc(10050)
        await nc._handle_npc_script_set(session, gint3(10050) + script)
        assert npc.client_bytecode and npc.joined_class_names() == ["foo"]
        assert b"foo" in npc.build_props_packet()
        # A second set replaces the list and runs created again.
        await nc._handle_npc_script_set(session, gint3(10050) + script)
        assert npc.joined_class_names() == ["foo"]
        # Reset re-attaches the stored source and keeps the join too.
        await nc._handle_npc_reset(session, gint3(10050))
        assert npc.joined_class_names() == ["foo"]

    run(go())


def test_mode_announcement_follows_the_start_up_mode_not_a_later_option_write():
    """Server.cpp:194-195: read once; an RC write only takes effect at restart."""
    server = make_server()
    server.config.servermode = "newworld"        # as an RC option write does
    player, trace = make_wire_player(server, 5, False, "player")
    assert asyncio.run(player._handle_login()) is True
    ids = flat_ids(trace)
    assert 176 not in ids and 177 not in ids
    restarted = make_server()
    restarted.server_mode = "newworld"           # what a start with the option gives
    player, trace = make_wire_player(restarted, 5, False, "player")
    assert asyncio.run(player._handle_login()) is True
    assert trace[0][1][0] == 177
