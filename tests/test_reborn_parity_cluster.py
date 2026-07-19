import asyncio
from unittest.mock import AsyncMock, MagicMock

from pygserver.baddy import Baddy, BaddyManager, BaddyType
from pygserver.level import Level
from pygserver.player import Player
from pygserver.protocol.constants import BDPROP, BDMODE, PLO
from pygserver.protocol.packets import PacketBuilder, PacketReader, build_private_message


def make_player(server, player_id, level=None):
    player = Player(server, player_id, AsyncMock(), MagicMock())
    player.nickname = f"player{player_id}"
    player.send_raw = AsyncMock()
    player.level = level
    if level:
        level.add_player(player)
    return player


def packet_body(packet):
    reader = PacketReader(packet)
    assert reader.read_gchar() == PLO.PRIVATEMESSAGE
    reader.read_gshort()
    return reader.remaining()[:-1].decode("latin-1")


class TestPrivateMessages:
    def test_lines_are_quoted_without_losing_content(self):
        message = 'comma, quote" and \\slash\n\nlast,'
        packet = build_private_message(1, "sender", message)
        assert packet_body(packet) == (
            '"","Private message:",'
            '"comma, quote"" and \\\\slash","","last,"'
        )

    def test_multiple_targets_use_mass_label(self):
        class Server:
            def __init__(self):
                self.players = {}

            def get_player(self, player_id):
                return self.players.get(player_id)

        server = Server()
        sender = make_player(server, 1)
        server.players = {2: make_player(server, 2), 3: make_player(server, 3)}
        payload = (PacketBuilder().write_gshort(2).write_gshort(2)
                   .write_gshort(3).write_string("hello").build())
        asyncio.run(sender._handle_private_message(payload))
        for target in server.players.values():
            assert '"Mass message:"' in packet_body(target.send_raw.await_args.args[0])


class TestValuelessFlags:
    def test_bare_flag_sets_true(self):
        player = make_player(MagicMock(), 1)
        asyncio.run(player._handle_flag_set(b"flagname"))
        assert player.flags["flagname"] is True

    def test_empty_value_deletes_flag(self):
        player = make_player(MagicMock(), 1)
        player.flags["flagname"] = "old"
        asyncio.run(player._handle_flag_set(b"flagname="))
        assert "flagname" not in player.flags


class World:
    def __init__(self, levels, gmap_levels=()):
        self.levels = {level.name: level for level in levels}
        self.gmap_levels = set(gmap_levels)

    def get_level(self, name):
        return self.levels.get(name)

    def get_gmap_for_level(self, name):
        if name in self.gmap_levels:
            gmap = MagicMock()
            gmap.name = "world"
            return gmap, 0, 0
        return None


class LevelServer:
    def __init__(self, levels, gmap_levels=()):
        self.world = World(levels, gmap_levels)
        self.players = {}
        self.broadcast_to_level = AsyncMock()
        self.npc_manager = MagicMock()
        self.npc_manager.on_player_enters = AsyncMock()
        self.npc_manager.on_player_leaves = AsyncMock()

    def get_player(self, player_id):
        return self.players.get(player_id)


def is_leader_packets(player):
    return [call.args[0] for call in player.send_raw.await_args_list
            if call.args[0] == bytes((PLO.ISLEADER + 32, 10))]


class TestLeaderPackets:
    def test_entry_only_first_player_receives_packet(self):
        level = Level("one.nw")
        server = LevelServer([level])
        first = make_player(server, 1, level)
        second = make_player(server, 2, level)
        server.players = {1: first, 2: second}
        asyncio.run(first._send_level(level))
        asyncio.run(second._send_level(level))
        assert len(is_leader_packets(first)) == 1
        assert not is_leader_packets(second)

    def test_warp_hands_authority_to_remaining_player(self):
        old, new = Level("old.nw"), Level("new.nw")
        server = LevelServer([old, new])
        leaving = make_player(server, 1, old)
        remaining = make_player(server, 2, old)
        server.players = {1: leaving, 2: remaining}
        asyncio.run(leaving.warp(new.name, 1, 1))
        assert len(is_leader_packets(remaining)) == 1

    def test_disconnect_hands_authority_to_remaining_player(self):
        level = Level("old.nw")
        server = LevelServer([level])
        leaving = make_player(server, 1, level)
        remaining = make_player(server, 2, level)
        server.players = {1: leaving, 2: remaining}
        asyncio.run(leaving._cleanup())
        assert len(is_leader_packets(remaining)) == 1

    def test_all_players_in_segment_receive_packet(self):
        level = Level("segment.nw")
        server = LevelServer([level], {level.name})
        first = make_player(server, 1, level)
        second = make_player(server, 2, level)
        server.players = {1: first, 2: second}
        asyncio.run(first._send_level(level))
        asyncio.run(second._send_level(level))
        assert len(is_leader_packets(first)) == 1
        assert len(is_leader_packets(second)) == 1


def baddy_payload(baddy_id, props):
    packet = PacketBuilder().write_gchar(baddy_id)
    for prop_id, value in props.items():
        packet.write_gchar(prop_id)
        if prop_id == BDPROP.POWERIMAGE:
            packet.write_gchar(value[0]).write_gstring(value[1])
        else:
            packet.write_gchar(value)
    return packet.build()


class TestLeaderBaddyProps:
    def setup_method(self):
        self.level = Level("baddies.nw")
        self.server = LevelServer([self.level])
        self.manager = BaddyManager(self.server)
        self.server.baddy_manager = self.manager
        self.baddy = Baddy(1, self.level.name, BaddyType.GRAYBALL, 2, 2)
        self.manager._baddies[self.level.name] = {1: self.baddy}
        self.leader = make_player(self.server, 1, self.level)
        self.other = make_player(self.server, 2, self.level)
        self.server.players = {1: self.leader, 2: self.other}

    def test_only_leader_can_apply_props(self):
        payload = baddy_payload(1, {BDPROP.POWERIMAGE: (7, "new.png")})
        asyncio.run(self.other._handle_baddy_props(payload))
        assert self.baddy.health != 7
        self.server.broadcast_to_level.assert_not_awaited()

    def test_powerimage_updates_health(self):
        payload = baddy_payload(1, {BDPROP.POWERIMAGE: (7, "new.png")})
        asyncio.run(self.leader._handle_baddy_props(payload))
        assert self.baddy.health == 7

    def test_dead_mode_uses_death_workflow(self):
        self.manager.handle_baddy_death = AsyncMock()
        payload = baddy_payload(1, {BDPROP.MODE: BDMODE.DEAD})
        asyncio.run(self.leader._handle_baddy_props(payload))
        self.manager.handle_baddy_death.assert_awaited_once_with(
            self.baddy, self.leader, exclude={self.leader.id}
        )

    def test_rebroadcast_excludes_sender(self):
        payload = baddy_payload(1, {BDPROP.MODE: BDMODE.HUNT})
        asyncio.run(self.leader._handle_baddy_props(payload))
        call = self.server.broadcast_to_level.await_args
        assert call.kwargs["exclude"] == {self.leader.id}
        assert call.args[1][0] == PLO.BADDYPROPS + 32

