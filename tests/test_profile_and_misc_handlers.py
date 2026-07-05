"""Unit tests for the previously-stubbed handlers: PLI_PROFILEGET/PROFILESET,
PLI_MUTEPLAYER, PLI_PROCESSLIST, PLI_MAPINFO, PLI_SERVERWARP, and
RC_APINCREMENTSET.
"""

import asyncio
import sys
import os
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))


class TestProfileWireFormat:
    """Byte-level parse_profile/build_profile round-trip tests."""

    def test_parse_profile_set_payload(self):
        from pygserver.protocol.packets import parse_profile, PacketBuilder

        builder = PacketBuilder()
        builder.write_gstring("hosler")
        for field in ("Real Name", "25", "male", "US", "icq", "a@b.c",
                      "web.site", "hangout", "a quote"):
            builder.write_gstring(field)

        parsed = parse_profile(builder.build())

        assert parsed["account"] == "hosler"
        assert parsed["name"] == "Real Name"
        assert parsed["age"] == "25"
        assert parsed["gender"] == "male"
        assert parsed["country"] == "US"
        assert parsed["messenger"] == "icq"
        assert parsed["email"] == "a@b.c"
        assert parsed["website"] == "web.site"
        assert parsed["hangout"] == "hangout"
        assert parsed["quote"] == "a quote"

    def test_build_profile_reply(self):
        from pygserver.protocol.packets import build_profile, PacketReader
        from pygserver.protocol.constants import PLO

        profile = {
            "name": "Real Name", "age": "25", "gender": "male",
            "country": "US", "messenger": "icq", "email": "a@b.c",
            "website": "web.site", "hangout": "hangout", "quote": "a quote",
        }
        packet = build_profile("hosler", profile, "1 hrs 2 mins 3 secs")

        reader = PacketReader(packet)
        packet_id = reader.read_gchar()
        assert packet_id == PLO.PROFILE

        assert reader.read_gstring() == "hosler"
        assert reader.read_gstring() == "Real Name"
        assert reader.read_gstring() == "25"
        assert reader.read_gstring() == "male"
        assert reader.read_gstring() == "US"
        assert reader.read_gstring() == "icq"
        assert reader.read_gstring() == "a@b.c"
        assert reader.read_gstring() == "web.site"
        assert reader.read_gstring() == "hangout"
        assert reader.read_gstring() == "a quote"
        assert reader.read_gstring() == "1 hrs 2 mins 3 secs"

    def test_parse_profile_missing_fields_defaults_empty(self):
        """A short/partial PROFILESET payload shouldn't raise."""
        from pygserver.protocol.packets import parse_profile, PacketBuilder

        builder = PacketBuilder().write_gstring("hosler").write_gstring("Real Name")
        parsed = parse_profile(builder.build())

        assert parsed["account"] == "hosler"
        assert parsed["name"] == "Real Name"
        assert "quote" not in parsed


class TestProfileManager:
    """ProfileManager local persistence, independent of the packet layer."""

    def _account_manager(self, tmp_path):
        from pygserver.account import AccountManager
        mock_server = MagicMock()
        return AccountManager(mock_server, accounts_dir=str(tmp_path))

    def test_get_profile_unknown_account(self, tmp_path):
        from pygserver.account import ProfileManager
        mock_server = MagicMock()
        mock_server.account_manager = self._account_manager(tmp_path)
        pm = ProfileManager(mock_server)

        assert pm.get_profile("nobody") == {}

    def test_set_then_get_profile_roundtrip(self, tmp_path):
        from pygserver.account import ProfileManager

        mock_server = MagicMock()
        account_manager = self._account_manager(tmp_path)
        mock_server.account_manager = account_manager
        account_manager.create_account("hosler", "pw")

        pm = ProfileManager(mock_server)
        fake_player = MagicMock()
        fake_player.account_name = "hosler"

        pm.set_profile(fake_player, {
            "account": "hosler", "name": "Real Name", "age": "25",
            "gender": "male", "country": "US", "messenger": "icq",
            "email": "a@b.c", "website": "web.site", "hangout": "hangout",
            "quote": "a quote",
        })

        profile = pm.get_profile("hosler")
        assert profile["account"] == "hosler"
        assert profile["name"] == "Real Name"
        assert profile["age"] == "25"
        assert profile["quote"] == "a quote"
        assert profile["online_time"] == "0 hrs 0 mins 0 secs"

    def test_format_online_time(self):
        from pygserver.account import ProfileManager
        assert ProfileManager._format_online_time(3723) == "1 hrs 2 mins 3 secs"


def _make_player(mock_server):
    from pygserver.player import Player
    mock_reader = AsyncMock()
    mock_writer = MagicMock()
    player = Player(mock_server, 1, mock_reader, mock_writer)
    player.account_name = "hosler"
    player.send_raw = AsyncMock()
    return player


class TestProfileHandlers:
    """Player._handle_profile_get / _handle_profile_set integration."""

    def _account_manager(self, tmp_path):
        from pygserver.account import AccountManager
        mock_server = MagicMock()
        return AccountManager(mock_server, accounts_dir=str(tmp_path))

    def test_profile_set_rejects_mismatched_account(self, tmp_path):
        from pygserver.protocol.packets import PacketBuilder
        from pygserver.account import ProfileManager

        mock_server = MagicMock()
        account_manager = self._account_manager(tmp_path)
        mock_server.account_manager = account_manager
        mock_server.profile_manager = ProfileManager(mock_server)
        account_manager.create_account("hosler", "pw")

        player = _make_player(mock_server)

        payload = PacketBuilder().write_gstring("someoneelse").write_gstring("Fake Name").build()

        async def main():
            await player._handle_profile_set(payload)
        asyncio.run(main())

        # Nothing should have been persisted under our own account.
        profile = mock_server.profile_manager.get_profile("hosler")
        assert profile["name"] == ""

    def test_profile_get_set_roundtrip_via_handlers(self, tmp_path):
        from pygserver.protocol.packets import PacketBuilder, parse_profile
        from pygserver.protocol.constants import PLO
        from pygserver.account import ProfileManager

        mock_server = MagicMock()
        account_manager = self._account_manager(tmp_path)
        mock_server.account_manager = account_manager
        mock_server.profile_manager = ProfileManager(mock_server)
        account_manager.create_account("hosler", "pw")

        player = _make_player(mock_server)

        set_payload = PacketBuilder().write_gstring("hosler")
        for field in ("Real Name", "25", "male", "US", "icq", "a@b.c",
                      "web.site", "hangout", "a quote"):
            set_payload.write_gstring(field)

        async def main():
            await player._handle_profile_set(set_payload.build())
            await player._handle_profile_get(b"hosler")
        asyncio.run(main())

        assert player.send_raw.await_count == 1
        sent = player.send_raw.await_args.args[0]
        assert sent[0] == PLO.PROFILE + 32  # write_gchar encoding
        from pygserver.protocol.packets import PacketReader
        reader = PacketReader(sent)
        reader.read_gchar()  # packet id
        assert reader.read_gstring() == "hosler"
        assert reader.read_gstring() == "Real Name"


class TestMiscNoOpHandlers:
    """PLI_MUTEPLAYER / PLI_PROCESSLIST / PLI_MAPINFO stay safe no-ops."""

    def test_mute_player_does_not_send_or_raise(self):
        mock_server = MagicMock()
        player = _make_player(mock_server)

        from pygserver.protocol.packets import PacketBuilder
        payload = PacketBuilder().write_gshort(2).write_gchar(1).build()

        async def main():
            await player._handle_mute_player(payload)
        asyncio.run(main())

        player.send_raw.assert_not_awaited()

    def test_process_list_does_not_send_or_raise(self):
        mock_server = MagicMock()
        player = _make_player(mock_server)

        async def main():
            await player._handle_process_list(b"explorer.exe\nsomething.exe")
        asyncio.run(main())

        player.send_raw.assert_not_awaited()

    def test_map_info_does_not_send_or_raise(self):
        mock_server = MagicMock()
        player = _make_player(mock_server)

        async def main():
            await player._handle_map_info(b"")
        asyncio.run(main())

        player.send_raw.assert_not_awaited()


class TestServerWarp:
    """PLI_SERVERWARP forwards through the listserver connection, when one exists."""

    def test_server_warp_no_listserver_logs_and_drops(self):
        mock_server = MagicMock()
        mock_server.listserver = None
        player = _make_player(mock_server)

        async def main():
            await player._handle_server_warp(b"My Server")
        asyncio.run(main())

        player.send_raw.assert_not_awaited()

    def test_server_warp_disconnected_listserver_drops(self):
        mock_server = MagicMock()
        mock_server.listserver.connected = False
        mock_server.listserver.request_server_info = AsyncMock()
        player = _make_player(mock_server)

        async def main():
            await player._handle_server_warp(b"My Server")
        asyncio.run(main())

        mock_server.listserver.request_server_info.assert_not_awaited()

    def test_server_warp_forwards_when_connected(self):
        mock_server = MagicMock()
        mock_server.listserver.connected = True
        mock_server.listserver.request_server_info = AsyncMock()
        player = _make_player(mock_server)

        async def main():
            await player._handle_server_warp(b"My Server")
        asyncio.run(main())

        mock_server.listserver.request_server_info.assert_awaited_once_with(1, "My Server")

    def test_server_info_reply_relayed_as_serverwarp(self):
        from pygserver.listserver import ServerListClient
        from pygserver.protocol.packets import PacketBuilder, PacketReader
        from pygserver.protocol.constants import PLO

        mock_server = MagicMock()
        target_player = MagicMock()
        target_player.send_raw = AsyncMock()
        mock_server.players = {7: target_player}

        client = ServerListClient(mock_server)

        payload = PacketBuilder().write_gshort(7).write_bytes(b"raw-serverwarp-payload").build()
        reader = PacketReader(payload)

        async def main():
            await client._handle_server_info(reader)
        asyncio.run(main())

        target_player.send_raw.assert_awaited_once()
        sent = target_player.send_raw.await_args.args[0]
        pkt_reader = PacketReader(sent)
        assert pkt_reader.read_gchar() == PLO.SERVERWARP
        assert pkt_reader.remaining() == b"raw-serverwarp-payload"

    def test_request_server_info_sends_svo_serverinfo(self):
        from pygserver.listserver import ServerListClient
        from reborn_protocol import SVO
        from pygserver.protocol.packets import PacketReader

        mock_server = MagicMock()
        client = ServerListClient(mock_server)
        client._send_packet = AsyncMock()

        async def main():
            await client.request_server_info(3, "My Server")
        asyncio.run(main())

        client._send_packet.assert_awaited_once()
        sent = client._send_packet.await_args.args[0]
        reader = PacketReader(sent)
        assert reader.read_gchar() == SVO.SERVERINFO
        assert reader.read_gshort() == 3
        assert reader.remaining() == b"My Server"


class TestRCApIncrementSet:
    """RC_APINCREMENTSET stays a documented no-op but parses safely."""

    def test_requires_setattributes_right(self):
        from pygserver.rc import RCManager, RCSession

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        session = RCSession(player=player, rights=0)

        async def main():
            await rc._handle_ap_increment_set(session, b"")
        asyncio.run(main())  # must not raise even with no rights

    def test_parses_value_without_raising(self):
        from pygserver.rc import RCManager, RCSession
        from pygserver.protocol.constants import PLPERM
        from pygserver.protocol.packets import PacketBuilder

        mock_server = MagicMock()
        rc = RCManager(mock_server)
        player = MagicMock()
        session = RCSession(player=player, rights=PLPERM.SETATTRIBUTES)

        payload = PacketBuilder().write_gchar(5).build()

        async def main():
            await rc._handle_ap_increment_set(session, payload)
        asyncio.run(main())
