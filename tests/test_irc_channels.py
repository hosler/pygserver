"""Serverlist-chat / IRC channel system (pygserver/irc.py + the rewritten
PLI_REQUESTTEXT/PLI_SENDTEXT handlers): wire format, channel lifecycle
(login/join/part/privmsg/PM), prop-81 flags, and the part-removes-membership
divergence from GServer-v2.
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))

from pygserver.irc import (  # noqa: E402
    CATEGORY_CHANNEL,
    CATEGORY_CHANNELOPEN,
    CATEGORY_CHANNELUSER,
    CATEGORY_EXTERNAL,
    EXTERNAL_ID_BASE,
    IrcManager,
    MAX_CHANNELS_PER_PLAYER,
    MAX_CHANNEL_NAME_LENGTH,
    gtokenize,
    guntokenize,
)
from pygserver.protocol.constants import PLO  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# =============================================================================
# Fakes
# =============================================================================

class FakePlayer:
    def __init__(self, player_id, account, nickname=None):
        self.id = player_id
        self.account_name = account
        self.nickname = nickname or account
        self.packets = []

    async def send_raw(self, data: bytes):
        self.packets.append(bytes(data))

    def packets_by_id(self, packet_id):
        return [p for p in self.packets if p and p[0] - 32 == packet_id]

    def server_texts(self):
        """Decoded PLO_SERVERTEXT payload fields."""
        out = []
        for p in self.packets_by_id(PLO.SERVERTEXT):
            out.append(guntokenize(
                p[1:].rstrip(b'\n').decode('latin-1')))
        return out


class FakeServer:
    def __init__(self):
        self.players = {}
        self.rc_manager = None
        self.config = SimpleNamespace(name="TestServer")

    def get_player(self, player_id):
        return self.players.get(player_id)

    def get_player_count(self):
        return len(self.players)

    def add(self, player):
        self.players[player.id] = player
        return player


def make_world():
    server = FakeServer()
    mgr = IrcManager(server)
    server.irc_manager = mgr
    return server, mgr


def parse_props_packet(packet: bytes):
    """Decode a PLO_OTHERPLPROPS pseudo-player packet into (id, props)."""
    assert packet[0] - 32 == PLO.OTHERPLPROPS
    body = packet[1:].rstrip(b'\n')
    pid = ((body[0] - 32) << 7) | (body[1] - 32)
    i = 2
    props = {}
    while i < len(body):
        prop_id = body[i] - 32
        i += 1
        if prop_id in (0, 34):        # NICKNAME / ACCOUNTNAME strings
            slen = body[i] - 32
            props[prop_id] = body[i + 1:i + 1 + slen].decode('latin-1')
            i += 1 + slen
        elif prop_id == 81:           # PLAYERLISTCATEGORY gbyte
            props[prop_id] = body[i] - 32
            i += 1
        elif prop_id == 51:           # DISCONNECT, void payload
            props[prop_id] = True
        else:
            raise AssertionError(f"unexpected prop {prop_id}")
    return pid, props


# =============================================================================
# gtokenize wire format
# =============================================================================

class TestTokenize:
    def test_spec_privmsg_example(self):
        # spec §1.1: the client engine produces exactly this comma-text
        fields = ["-Serverlist_Chat", "irc", "privmsg", "#pyreborn",
                  "protocol test - ignore"]
        wire = gtokenize(fields)
        assert wire == ('-Serverlist_Chat,irc,privmsg,#pyreborn,'
                        '"protocol test - ignore"')
        assert guntokenize(wire) == fields

    def test_round_trip_quotes_commas_backslashes(self):
        fields = ["-Serverlist_Chat", "irc", "privmsg", "#ch",
                  'she said "hi, there" C:\\path']
        assert guntokenize(gtokenize(fields)) == fields

    def test_round_trip_empty_and_slash(self):
        fields = ["w", "", "a/b"]
        wire = gtokenize(fields)
        assert wire == 'w,,"a/b"'
        assert guntokenize(wire) == fields


# =============================================================================
# irc,login - channel pseudo-players
# =============================================================================

class TestIrcLogin:
    def test_login_materializes_default_channel(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))

        run(mgr.handle_send_text(
            p, gtokenize(["-Serverlist_Chat", "irc", "login", "-"])))

        assert p.id in mgr.sessions
        packets = p.packets_by_id(PLO.OTHERPLPROPS)
        assert len(packets) == 1
        pid, props = parse_props_packet(packets[0])
        assert pid >= EXTERNAL_ID_BASE
        assert props[34] == "irc:#reborn"
        assert props[0] == "#reborn (0,0)"
        # not joined: external|channel = 3 (live Login reference value; the
        # GServer-v2 EXTERNAL-only byte is the documented divergence)
        assert props[81] == CATEGORY_EXTERNAL | CATEGORY_CHANNEL == 3

    def test_rc_login_gets_addplayer(self):
        server, mgr = make_world()
        server.rc_manager = MagicMock()
        server.rc_manager.is_rc = lambda pid: pid == 9
        rc = server.add(FakePlayer(9, "admin"))

        run(mgr.handle_send_text(
            rc, gtokenize(["GraalEngine", "irc", "login", "-"])))

        packets = rc.packets_by_id(PLO.ADDPLAYER)
        assert len(packets) == 1
        body = packets[0][1:].rstrip(b'\n')
        pid = ((body[0] - 32) << 7) | (body[1] - 32)
        assert pid >= EXTERNAL_ID_BASE
        alen = body[2] - 32
        assert body[3:3 + alen].decode('latin-1') == "irc:#reborn"

    def test_late_login_sees_existing_members(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(a, "#dev"))

        run(mgr.handle_send_text(
            b, gtokenize(["-Serverlist_Chat", "irc", "login", "-"])))

        parsed = [parse_props_packet(p)
                  for p in b.packets_by_id(PLO.OTHERPLPROPS)]
        accounts = {props[34] for _, props in parsed}
        assert accounts == {"irc:#reborn", "irc:#dev", "alice"}
        member = next(props for _, props in parsed if props[34] == "alice")
        assert member[0] == "alice (on #dev)"
        assert member[81] == CATEGORY_EXTERNAL | CATEGORY_CHANNELUSER == 5


# =============================================================================
# join / part
# =============================================================================

class TestJoinPart:
    def test_join_confirm_and_membership(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))

        run(mgr.handle_send_text(
            p, gtokenize(["-Serverlist_Chat", "irc", "join", "#mychannel"])))

        assert ["-Serverlist_Chat", "irc", "join", "#mychannel"] \
            in p.server_texts()
        channel = mgr.channels["#mychannel"]
        assert p.id in channel.members
        assert channel.pseudo_id >= EXTERNAL_ID_BASE

        # joined viewer sees channel|open|external = 11 and the real count
        updates = [parse_props_packet(pkt)
                   for pkt in p.packets_by_id(PLO.OTHERPLPROPS)]
        ch_props = next(props for pid, props in updates
                        if pid == channel.pseudo_id and 81 in props)
        assert ch_props[81] == (CATEGORY_EXTERNAL | CATEGORY_CHANNEL
                                | CATEGORY_CHANNELOPEN) == 11
        assert ch_props[0] == "#mychannel (1,0)"

    def test_duplicate_join_not_reconfirmed(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))
        run(mgr.join_channel(p, "#x"))
        confirms = p.server_texts().count(
            ["-Serverlist_Chat", "irc", "join", "#x"])
        run(mgr.join_channel(p, "#x"))
        assert p.server_texts().count(
            ["-Serverlist_Chat", "irc", "join", "#x"]) == confirms == 1

    def test_part_removes_membership(self):
        """The GServer-v2 part-leak (removeChatChannel never called) must NOT
        be replicated: after part, no membership and no further relays."""
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(a, "#x"))
        run(mgr.join_channel(b, "#x"))

        run(mgr.handle_send_text(
            a, gtokenize(["-Serverlist_Chat", "irc", "part", "#x"])))

        assert ["-Serverlist_Chat", "irc", "part", "#x"] in a.server_texts()
        assert a.id not in mgr.channels["#x"].members

        a.packets.clear()
        run(mgr.channel_privmsg(b, "#x", "still there?"))
        assert a.server_texts() == []       # parted player receives nothing

    def test_part_removes_member_pseudo_and_last_part_destroys_channel(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(a, "#x"))
        run(mgr.join_channel(b, "#x"))
        channel_pseudo = mgr.channels["#x"].pseudo_id
        a_pseudo = mgr.channels["#x"].members[a.id]

        b.packets.clear()
        run(mgr.part_channel(a, "#x"))
        removed = [parse_props_packet(p)
                   for p in b.packets_by_id(PLO.OTHERPLPROPS)]
        assert any(pid == a_pseudo and props.get(51)
                   for pid, props in removed)

        run(mgr.part_channel(b, "#x"))
        assert "#x" not in mgr.channels
        removed_b = [parse_props_packet(p)
                     for p in b.packets_by_id(PLO.OTHERPLPROPS)]
        assert any(pid == channel_pseudo and props.get(51)
                   for pid, props in removed_b)

    def test_default_channel_survives_last_part(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))
        run(mgr.join_channel(p, "#reborn"))
        run(mgr.part_channel(p, "#reborn"))
        assert "#reborn" in mgr.channels

    def test_per_player_limit_refuses_and_default_still_joins(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))
        for index in range(MAX_CHANNELS_PER_PLAYER):
            run(mgr.join_channel(p, f"#room{index}"))

        run(mgr.join_channel(p, "#one-too-many"))
        run(mgr.join_channel(p, "#reborn"))

        assert "#one-too-many" not in mgr.channels
        assert p.id in mgr.channels["#reborn"].members
        assert len([channel for channel in mgr.channels.values()
                    if p.id in channel.members]) == MAX_CHANNELS_PER_PLAYER + 1

    def test_invalid_channel_names_are_refused(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))
        invalid = ["room", "#" + "x" * MAX_CHANNEL_NAME_LENGTH, "#bad\nname"]

        for name in invalid:
            run(mgr.join_channel(p, name))

        assert set(mgr.channels) == {"#reborn"}
        assert not mgr.channels["#reborn"].members

    def test_failed_member_allocation_leaves_no_orphan_channel(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))
        original_allocate = mgr._allocate_external_id
        calls = 0

        def fail_member(entry):
            nonlocal calls
            calls += 1
            return original_allocate(entry) if calls == 1 else None

        mgr._allocate_external_id = fail_member
        run(mgr.join_channel(p, "#transaction"))

        assert "#transaction" not in mgr.channels
        assert all(entry[1] != "#transaction"
                   for entry in mgr._external_index.values())


# =============================================================================
# privmsg relay
# =============================================================================

class TestPrivmsg:
    def test_relay_includes_sender_echo(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(a, "#x"))
        run(mgr.join_channel(b, "#x"))
        a.packets.clear()
        b.packets.clear()

        run(mgr.handle_send_text(a, gtokenize(
            ["-Serverlist_Chat", "irc", "privmsg", "#x", "hello, world"])))

        expected = ["-Serverlist_Chat", "irc", "privmsg", "alice", "#x",
                    "hello, world"]
        assert expected in a.server_texts()    # sender echo
        assert expected in b.server_texts()

    def test_non_member_sender_reaches_members_but_no_echo(self):
        # ServerList.cpp handleText: delivery is inChatChannel-gated,
        # sender membership is not required to send
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(b, "#x"))
        a.packets.clear()
        b.packets.clear()

        run(mgr.channel_privmsg(a, "#x", "drive-by"))

        assert a.server_texts() == []
        assert ["-Serverlist_Chat", "irc", "privmsg", "alice", "#x",
                "drive-by"] in b.server_texts()

    def test_unknown_channel_dropped(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        run(mgr.channel_privmsg(a, "#nowhere", "hi"))
        assert a.server_texts() == []


# =============================================================================
# PMs to pseudo-player ids
# =============================================================================

class TestExternalPM:
    def test_pm_to_member_pseudo_reaches_real_player(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(b, "#x"))
        b_pseudo = mgr.channels["#x"].members[b.id]

        assert run(mgr.route_external_pm(a, b_pseudo, "psst"))
        pms = b.packets_by_id(PLO.PRIVATEMESSAGE)
        assert len(pms) == 1
        body = pms[0][1:].rstrip(b'\n')
        from_id = ((body[0] - 32) << 7) | (body[1] - 32)
        assert from_id == a.id
        assert b'"psst"' in body

    def test_pm_to_channel_pseudo_relays_to_members(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(b, "#x"))

        assert run(mgr.route_external_pm(
            a, mgr.channels["#x"].pseudo_id, "to the room"))
        assert ["-Serverlist_Chat", "irc", "privmsg", "alice", "#x",
                "to the room"] in b.server_texts()

    def test_pm_to_unknown_pseudo_returns_false(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        assert not run(mgr.route_external_pm(a, 27000, "hello?"))


# =============================================================================
# lister,simplelist
# =============================================================================

class TestSimpleList:
    def test_simplelist_reply_describes_this_server(self):
        server, mgr = make_world()
        p = server.add(FakePlayer(3, "alice"))

        run(mgr.handle_request_text(
            p, gtokenize(["-Serverlist", "lister", "simplelist"])))

        replies = p.server_texts()
        assert len(replies) == 1
        fields = replies[0]
        assert fields[:3] == ["-Serverlist", "lister", "simpleserverlist"]
        entry = guntokenize(fields[3])
        assert entry == ["TestServer", "U TestServer", "1"]


# =============================================================================
# disconnect cleanup
# =============================================================================

class TestDisconnectCleanup:
    def test_remove_player_parts_and_notifies(self):
        server, mgr = make_world()
        a = server.add(FakePlayer(3, "alice"))
        b = server.add(FakePlayer(4, "bob"))
        run(mgr.join_channel(a, "#x"))
        run(mgr.join_channel(b, "#x"))
        a_pseudo = mgr.channels["#x"].members[a.id]

        del server.players[a.id]
        b.packets.clear()
        run(mgr.remove_player(a))

        assert a.id not in mgr.sessions
        assert a.id not in mgr.channels["#x"].members
        removed = [parse_props_packet(p)
                   for p in b.packets_by_id(PLO.OTHERPLPROPS)]
        assert any(pid == a_pseudo and props.get(51)
                   for pid, props in removed)


# =============================================================================
# Player handler integration (real Player, mocked transport)
# =============================================================================

class TestHandlerDispatch:
    def _player(self, server_mock):
        from pygserver.player import Player
        player = Player(server_mock, 3, AsyncMock(), MagicMock())
        player.account_name = "alice"
        return player

    def test_sendtext_reaches_irc_manager(self):
        mock_server = MagicMock()
        fake_server = FakeServer()
        mgr = IrcManager(fake_server)
        mock_server.irc_manager = mgr

        player = self._player(mock_server)
        player.send_raw = AsyncMock()
        fake_server.players[player.id] = player

        payload = gtokenize(
            ["-Serverlist_Chat", "irc", "join", "#viahandler"]
        ).encode('latin-1')
        run(player._handle_send_text(payload))

        assert player.id in mgr.channels["#viahandler"].members

    def test_requesttext_simplelist_via_handler(self):
        mock_server = MagicMock()
        fake_server = FakeServer()
        mgr = IrcManager(fake_server)
        mock_server.irc_manager = mgr

        player = self._player(mock_server)
        player.send_raw = AsyncMock()

        payload = gtokenize(
            ["-Serverlist", "lister", "simplelist"]).encode('latin-1')
        run(player._handle_request_text(payload))

        assert player.send_raw.await_count == 1
        packet = player.send_raw.await_args.args[0]
        assert packet[0] - 32 == PLO.SERVERTEXT
        assert b"simpleserverlist" in packet
