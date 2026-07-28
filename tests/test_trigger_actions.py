import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))

from pygserver.gs2 import GS2ScriptManager, to_csv
from pygserver.npc import NPC, NPCManager
from pygserver.protocol.constants import PLO
from pygserver.protocol.packet_codec import PacketReader
from pygserver.server import GameServer


def run(coro):
    return asyncio.run(coro)


class FakeLevel:
    def __init__(self):
        self._npcs = {}

    def get_npcs(self):
        return list(self._npcs.values())

    def add_npc(self, npc):
        self._npcs[npc.id] = npc
        npc.level = self


class FakePlayer:
    def __init__(self, level):
        self.level = level
        self.flags = {}
        self.packets = []

    async def send_raw(self, packet):
        self.packets.append(bytes(packet))


def make_server(tmp_path):
    weapons = tmp_path / "weapons"
    weapons.mkdir()
    server = GameServer.__new__(GameServer)
    server.config = SimpleNamespace(weapons_dir=str(weapons))
    server.npc_manager = NPCManager(server)
    server.gs2_manager = GS2ScriptManager(server, str(tmp_path))
    return server, weapons


def add_classic_weapon(server, weapons, name, source):
    path = weapons / f"weapon{name}.txt"
    path.write_text(
        f"GRAWP001\nREALNAME {name}\nIMAGE\nSCRIPT\n{source}\n"
        "SCRIPTEND\n",
        encoding="latin-1",
    )
    cache = path.with_suffix(".gs2bc")
    cache.write_bytes(b"")
    os.utime(cache, (path.stat().st_mtime + 1,) * 2)
    server.gs2_manager.load()


def test_trigger_action_hits_level_npc_without_raising(tmp_path):
    server, _ = make_server(tmp_path)
    level = FakeLevel()
    player = FakePlayer(level)
    npc = NPC(10001, "target")
    npc.x, npc.y = 4, 5
    level.add_npc(npc)
    server.npc_manager._npcs[npc.id] = npc
    server.npc_manager.attach_gs1(
        npc, "if (actionpoke) { this.triggered = 1; }")

    run(server.npc_manager.on_trigger_action(player, 5, 6, "poke,value"))

    assert npc.gs1_scopes["this"]["triggered"] == 1.0


def test_serverside_routes_to_named_weapon_with_params(tmp_path):
    server, weapons = make_server(tmp_path)
    level = FakeLevel()
    player = FakePlayer(level)
    add_classic_weapon(
        server, weapons, "-Echo",
        "if (actionserverside) { this.value = strtofloat(#p(0)); }"
        "\n//#CLIENTSIDE\n",
    )

    handled = run(server.handle_trigger_action(
        player, 0, 0, "serverside,-Echo,42"))

    weapon = server.gs2_manager.get_weapon("-Echo")
    assert handled is True
    assert weapon.server_runtime.gs1_scopes["this"]["value"] == 42.0


def test_serverside_params_round_trip_wire_csv(tmp_path):
    server, weapons = make_server(tmp_path)
    player = FakePlayer(FakeLevel())
    add_classic_weapon(
        server, weapons, "-Echo",
        'if (actionserverside) { this.value = #p(0) @ "|" @ #p(1); }'
        "\n//#CLIENTSIDE\n",
    )
    params = ['comma,quote"slash\\', "plain"]

    handled = run(server.handle_trigger_action(
        player, 0, 0, "serverside,-Echo," + to_csv(params)))

    runtime = server.gs2_manager.get_weapon("-Echo").server_runtime
    assert handled is True
    assert runtime.gs1_scopes["this"]["value"] == '|'.join(params)


def test_malformed_csv_still_reaches_named_weapon_as_raw_text(tmp_path):
    server, weapons = make_server(tmp_path)
    player = FakePlayer(FakeLevel())
    add_classic_weapon(
        server, weapons, "-Echo",
        "if (actionserverside) { setstring this.value,#p(0); }"
        "\n//#CLIENTSIDE\n",
    )

    handled = run(server.handle_trigger_action(
        player, 0, 0, 'serverside,-Echo,"unfinished'))

    runtime = server.gs2_manager.get_weapon("-Echo").server_runtime
    assert handled is True
    assert runtime.gs1_scopes["this"]["value"] == '"unfinished'


def test_weapon_sleep_resumes_with_parked_player_and_level(tmp_path):
    server, weapons = make_server(tmp_path)
    first_level, second_level = FakeLevel(), FakeLevel()
    first, second = FakePlayer(first_level), FakePlayer(second_level)
    first.x, second.x = 11, 29
    add_classic_weapon(
        server, weapons, "-Sleep",
        'if (actionserverside) { if (strtofloat(#p(0)) == 1) { sleep .01;'
        " this.resumedx = playerx; } }\n//#CLIENTSIDE\n",
    )

    assert run(server.handle_trigger_action(
        first, 0, 0, "serverside,-Sleep,1")) is True
    runtime = server.gs2_manager.get_weapon("-Sleep").server_runtime
    assert runtime.id in server.npc_manager._weapon_runtimes

    # A later trigger repoints the shared runtime, but does not replace the
    # parked sleep because this firing completes without sleeping.
    assert run(server.handle_trigger_action(
        second, 0, 0, "serverside,-Sleep,0")) is True
    runtime._timer_end = 1
    run(server.npc_manager.tick())

    assert runtime.gs1_scopes["this"]["resumedx"] == 11.0
    assert runtime.level is first_level


def test_unknown_and_malformed_triggers_are_refused(tmp_path):
    server, _ = make_server(tmp_path)
    player = FakePlayer(FakeLevel())

    assert run(server.handle_trigger_action(
        player, 0, 0, "serverside,-Missing,ping")) is False
    assert run(server.handle_trigger_action(
        player, 0, 0, "serverside,giverupees,invalid")) is False


def test_trigger_action_requires_strict_containment(tmp_path):
    # The reference uses triggerDistance only as a candidate-search radius;
    # the hit test is strict containment in the unexpanded NPC rect
    # (GServer-v2 NPCServer.h addEventToLevelNPCsAtPosition, :258).
    server, _ = make_server(tmp_path)
    level = FakeLevel()
    player = FakePlayer(level)
    npc = NPC(10001, "target")
    npc.x, npc.y = 20, 20
    level.add_npc(npc)
    server.npc_manager._npcs[npc.id] = npc
    server.npc_manager.attach_gs1(
        npc, "if (actionpoke) { this.hits++; }")

    run(server.npc_manager.on_trigger_action(player, 21, 20, "poke"))
    run(server.npc_manager.on_trigger_action(player, 19.5, 20, "poke"))

    assert npc.gs1_scopes["this"]["hits"] == 1.0


def test_triggerclient_packet_layout_uses_three_byte_npc_id(tmp_path):
    server, weapons = make_server(tmp_path)
    player = FakePlayer(FakeLevel())
    add_classic_weapon(
        server, weapons, "-Echo",
        'if (actionserverside) { triggerclient("-Echo","pong",#p(0)); }'
        "\n//#CLIENTSIDE\n",
    )

    async def dispatch():
        handled = await server.handle_trigger_action(
            player, 0, 0, "serverside,-Echo,42")
        await asyncio.sleep(0)
        return handled

    assert run(dispatch()) is True
    assert len(player.packets) == 1

    packet = player.packets[0]
    assert packet[0] - 32 == PLO.TRIGGERACTION
    reader = PacketReader(packet[1:-1])
    assert reader.read_gshort() == 0
    assert reader.read_gint3() == 0
    assert reader.read_gchar() == 0
    assert reader.read_gchar() == 0
    assert reader.remaining() == b"clientside,-Echo,pong,42"
