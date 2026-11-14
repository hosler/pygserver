"""PLAYERLISTSTATUS (player prop 53) and PLO_STATUSLIST against the reference
server (GServer-v2 beta4).

The status byte is stored per connection, sent to every other connection when a
loaded player changes it (PlayerProps.cpp:991-1003), and is part of the props
other players get when a player joins (PlayerProps.h:153-165). The label list
the byte indexes is the `playerlisticons` option, sent once at login
(PlayerClient.cpp:529-536, PlayerRC.cpp:263-267).
"""

import pytest
from unittest.mock import AsyncMock

from pygserver.config import ServerConfig
from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import (
    PacketBuilder, build_status_list, parse_player_props)

from tests.test_control_login import make_login_player, make_server as control_server
from tests.test_gmap_player_combat_audience import (
    FAR, NEIGHBOUR, SAME, SENDER, arena, audience, clock, gmap_arena, received, run)

DEFAULT_LABELS = b"Online,Away,DND,Eating,Hiding,No PMs,RPing,Sparring,PKing"


def loaded_arena(tmp_path):
    server, players = gmap_arena(tmp_path)
    for player in players.values():
        player.account_loaded = True
    return server, players


def status_payload(value):
    return PacketBuilder().write_gchar(PLPROP.PSTATUSMSG).write_gchar(value).build()


# -- option -----------------------------------------------------------------

def test_a_config_without_the_option_has_the_reference_default_list(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("name = x\n", encoding="latin-1")
    config = ServerConfig.from_file(str(path))
    assert ",".join(config.playerlisticons).encode() == DEFAULT_LABELS


def test_the_option_is_split_on_commas_and_written_back(tmp_path):
    path = tmp_path / "serveroptions.txt"
    path.write_text("playerlisticons = Here, Gone ,Busy\n", encoding="latin-1")
    config = ServerConfig.from_file(str(path))
    assert config.playerlisticons == ["Here", "Gone", "Busy"]
    assert "playerlisticons = Here,Gone,Busy" in config.option_lines()
    assert not any(line.startswith("playerlisticons")
                   for line in ServerConfig().option_lines())


# -- PLO_STATUSLIST ---------------------------------------------------------

def test_the_status_list_is_one_raw_comma_joined_string():
    assert build_status_list(["Online", " Away ", "DND"]) == (
        bytes([PLO.STATUSLIST + 32]) + b"Online,Away,DND\n")


def real_login_stream(client_type, account="someone", staff=(), version=None):
    """Packet ids in the order login sends them, with the real
    send_login_response: the frame it writes is split on the packet
    terminator and put in line with the send_raw calls."""
    import asyncio
    server = control_server(staff=staff)
    server.config.playerlisticons = ["A", "B"]
    player = make_login_player(server, 2, account, client_type)
    if version is not None:
        player.client_version = version
    login = player.login_service
    login.send_login_response = type(login).send_login_response.__get__(login)
    stream = []

    async def frame(data):
        stream.extend(p for p in data.split(b"\n") if p)

    async def raw(data):
        stream.append(data.rstrip(b"\n"))

    player.session.send_login_response = frame
    player.send_raw = raw
    assert asyncio.run(player._handle_login()) is True
    return [p[0] - 32 for p in stream]


@pytest.mark.parametrize("version,sent", [(2.0, False), (2.1, True), (6.037, True)])
def test_a_client_gets_the_list_from_version_2_1(version, sent):
    ids = real_login_stream(5, version=version)
    assert (PLO.STATUSLIST in ids) is sent


def test_a_client_gets_guilds_and_list_before_the_props():
    """PlayerClient.cpp:524-536 (stage 0) precede :550 (the props)."""
    ids = real_login_stream(5, version=6.037)
    assert ids.index(PLO.STAFFGUILDS) + 1 == ids.index(PLO.STATUSLIST)
    assert ids.index(PLO.STATUSLIST) < ids.index(PLO.PLAYERPROPS)


def test_an_rc_gets_guilds_list_then_the_upload_size():
    """PlayerRC.cpp:259-270."""
    ids = real_login_stream(6, account="admin", staff=("admin",))
    guilds = ids.index(PLO.STAFFGUILDS)
    assert ids[guilds + 1] == PLO.STATUSLIST
    assert ids[guilds + 2] == PLO.RC_MAXUPLOADFILESIZE


# -- the prop ---------------------------------------------------------------

def test_a_changed_status_reaches_everyone_but_the_sender(tmp_path, clock):
    """sendPacketToAll, sender excluded: every segment, not the 3x3
    (PlayerProps.cpp:1240-1241)."""
    server, players = loaded_arena(tmp_path)
    run(players[SENDER]._handle_player_props(status_payload(2)))
    expected = (bytes([PLO.OTHERPLPROPS + 32, 32, 32 + SENDER, PLPROP.PSTATUSMSG + 32, 2 + 32])
                + b"\n")
    assert audience(players) == {SAME, NEIGHBOUR, FAR}
    for pid in (SAME, NEIGHBOUR, FAR):
        assert received(players[pid]) == expected
    assert players[SENDER].status_msg == 2


def test_an_unchanged_status_is_ignored(tmp_path, clock):
    server, players = loaded_arena(tmp_path)
    run(players[SENDER]._handle_player_props(status_payload(0)))
    assert audience(players) == set()
    run(players[SENDER]._handle_player_props(status_payload(3)))
    for player in players.values():
        player.send_raw.reset_mock()
    run(players[SENDER]._handle_player_props(status_payload(3)))
    assert audience(players) == set()


def test_a_player_that_is_not_loaded_stores_without_sending(tmp_path, clock):
    """`m_id == 0 || !m_loaded` stops before sendToAll (PlayerProps.cpp:998)."""
    server, players = gmap_arena(tmp_path)
    players[SENDER].account_loaded = False
    run(players[SENDER]._handle_player_props(status_payload(1)))
    assert players[SENDER].status_msg == 1
    assert audience(players) == set()


def test_a_status_change_does_not_resend_the_other_props_to_everyone(tmp_path, clock):
    """One packet, but a nearby prop stays in the nearby packet
    (PropertySerializers.cpp:726-737)."""
    server, players = loaded_arena(tmp_path)
    payload = (PacketBuilder().write_gchar(PLPROP.PSTATUSMSG).write_gchar(1)
               .write_gchar(PLPROP.GANI).write_gchar(4).write_bytes(b"walk").build())
    run(players[SENDER]._handle_player_props(payload))
    everyone = parse_player_props(received(players[FAR])[3:-1])
    assert everyone == {PLPROP.PSTATUSMSG: 1}
    # The cell neighbour hears the status in the everyone packet and the gani
    # in the nearby packet, as two packets.
    got = [parse_player_props(chunk) for chunk in
           received(players[SAME]).split(b"\n")[:-1]
           for chunk in [chunk[3:]]]
    assert {PLPROP.PSTATUSMSG: 1} in got and {PLPROP.GANI: "walk"} in got


def test_the_status_is_in_the_join_props_other_players_get(tmp_path, clock):
    server, players = gmap_arena(tmp_path)
    players[SENDER].status_msg = 4
    props = parse_player_props(players[SENDER].build_props_packet()[3:-1])
    assert props[PLPROP.PSTATUSMSG] == 4
