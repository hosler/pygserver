"""NC NPC handlers against hand-built reference bytes.

Reference: GServer-v2 server/src/player/packets/PlayerNCPackets.cpp. These
handlers used to raise on every call (a dict passed to a string builder, a
missing `read_string` length, calls into methods the NPC manager never had),
which the packet loop logged and swallowed.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pygserver.nc import NCManager, NCSession
from pygserver.npc import NPC, NPCManager
from pygserver.protocol.constants import PLO, PLTYPE, NPCPROP


def run(coro):
    return asyncio.run(coro)


def gint3(value):
    return bytes((((value >> 14) & 0x7F) + 32, ((value >> 7) & 0x7F) + 32,
                  (value & 0x7F) + 32))


def gchar(value):
    return bytes([value + 32])


class FakeLevel:
    def __init__(self, name):
        self.name = name
        self.npcs = []

    def add_npc(self, npc):
        npc.level = self
        self.npcs.append(npc)

    def remove_npc(self, npc):
        self.npcs.remove(npc)
        npc.level = None

    def get_npcs(self):
        return list(self.npcs)


connections = []


def make_nc():
    connections.clear()
    levels = {name: FakeLevel(name) for name in ("town.nw", "cave.nw")}
    server = SimpleNamespace(
        config=SimpleNamespace(serverside=True),
        world=SimpleNamespace(get_level=levels.get),
        broadcast_to_level=AsyncMock(),
        broadcast_to_all=AsyncMock(),
        get_all_players=lambda: list(connections),
    )
    server.npc_manager = NPCManager(server)
    nc = NCManager(server)
    player = SimpleNamespace(id=7, account_name="editor", send_raw=AsyncMock(),
                             connection_type=PLTYPE.NC)
    connections.append(player)
    session = nc.register_session(player)
    return nc, session, server, levels


def sent(session):
    return [call.args[0] for call in session.player.send_raw.await_args_list]


def chat(text):
    return gchar(PLO.RC_CHAT) + text.encode() + b"\n"


def add_npc(nc, session, row=b"Guard,10050,Soldier,me,town.nw,10.5,20"):
    run(nc._handle_npc_add(session, row))
    return nc.server.npc_manager.get_npc(10050)


def test_npc_add_parses_the_csv_row_and_announces_it():
    nc, session, server, levels = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,10.5,20")

    run(go())

    npc = server.npc_manager.get_npc(10050)
    assert (npc.name, npc.x, npc.y, npc.level) == ("Guard", 10.5, 20.0, levels["town.nw"])
    assert npc.script_type == "Soldier" and npc.scripter == "me"
    assert sent(session) == [
        gchar(PLO.NC_NPCADD) + gint3(10050)
        + gchar(50) + gchar(5) + b"Guard"
        + gchar(51) + gchar(7) + b"Soldier"
        + gchar(52) + gchar(7) + b"town.nw" + b"\n",
        chat("NPC Guard added by editor"),
    ]


def test_npc_add_refusals_match_the_reference_messages():
    nc, session, server, _ = make_nc()
    for row, message in [
        (b",10050,t,s,town.nw,1,1", "Error adding database npc: NPC name cannot be empty"),
        (b"Low,2,t,s,town.nw,1,1", "Error adding database npc: NPC ID must be greater than 3"),
    ]:
        session.player.send_raw.reset_mock()
        run(nc._handle_npc_add(session, row))
        assert sent(session) == [chat(message)]
    assert server.npc_manager._npcs == {}

    async def twice():
        await nc._handle_npc_add(session, b"Guard,10050,t,s,town.nw,1,1")
        session.player.send_raw.reset_mock()
        await nc._handle_npc_add(session, b"Guard,10051,t,s,town.nw,1,1")
        await nc._handle_npc_add(session, b"Other,10050,t,s,town.nw,1,1")

    run(twice())
    assert sent(session) == [
        chat("Error adding database npc: NPC name already exists"),
        chat("Error adding database npc: NPC ID already exists"),
    ]


def test_nc_chosen_id_is_reserved_against_later_auto_creates():
    nc, session, server, levels = make_nc()
    manager = server.npc_manager

    async def go():
        await nc._handle_npc_add(session, b"Guard,10001,t,s,town.nw,1,1")
        first = manager.create_npc(name="auto1", level=levels["cave.nw"])
        # A chosen id ahead of the counter, and one that was taken by hand.
        await nc._handle_npc_add(session, b"Far,10005,t,s,town.nw,1,1")
        manager._next_id = 10003
        manager._npcs[10003] = NPC(10003, "manual")
        ids = [manager.create_npc(name=f"auto{i}").id for i in range(2, 4)]
        return first, ids

    first, ids = run(go())
    assert manager.get_npc(10001).name == "Guard"
    assert first.id not in (10001, 10005) and first.id == 10002
    assert ids == [10004, 10006]
    assert sorted(manager._npcs) == [10001, 10002, 10003, 10004, 10005, 10006]
    assert len(levels["town.nw"].npcs) == 2 and len(levels["cave.nw"].npcs) == 1


def test_npc_add_refuses_an_id_the_wire_cannot_carry():
    nc, session, server, _ = make_nc()
    for row in (b"Big,3682400,t,s,town.nw,1,1", b"Huge,99999999999,t,s,town.nw,1,1"):
        session.player.send_raw.reset_mock()
        run(nc._handle_npc_add(session, row))
        assert sent(session) == [
            chat("Error adding database npc: NPC ID must be less than 3682400")]
    assert server.npc_manager._npcs == {}
    run(nc._handle_npc_add(session, b"Edge,3682399,t,s,town.nw,1,1"))
    assert server.npc_manager.get_npc(3682399).name == "Edge"


def test_npc_get_replies_with_a_csv_variable_dump_and_no_id():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,10.5,20")
        session.player.send_raw.reset_mock()
        await nc._handle_npc_get(session, gint3(10050))
        await nc._handle_npc_get(session, b"")      # the keep-alive poll

    run(go())

    (reply,) = sent(session)
    assert reply.startswith(gchar(PLO.NC_NPCATTRIBUTES) + b'"Variables dump from npc Guard",,')
    assert b"Guard.level: town.nw" in reply
    assert b'"Guard.pos: 10.50, 20.00, 0"' in reply
    assert reply.endswith(b"\n")


def test_npc_delete_removes_it_and_tells_the_ncs():
    nc, session, server, levels = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        session.player.send_raw.reset_mock()
        await nc._handle_npc_delete(session, gint3(10050))

    run(go())

    assert server.npc_manager.get_npc(10050) is None
    assert levels["town.nw"].npcs == []
    assert sent(session)[0] == gchar(PLO.NC_NPCDELETE) + gint3(10050) + b"\n"
    assert sent(session)[1] == chat("NPC Guard deleted by editor")


def test_npc_warp_moves_within_and_across_levels():
    nc, session, server, levels = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        npc = server.npc_manager.get_npc(10050)
        # x = 7.5 tiles, y = 9 tiles, same level.
        await nc._handle_npc_warp(session, gint3(10050) + gchar(15) + gchar(18) + b"town.nw")
        assert (npc.x, npc.y, npc.level) == (7.5, 9.0, levels["town.nw"])
        await nc._handle_npc_warp(session, gint3(10050) + gchar(4) + gchar(6) + b"cave.nw")
        assert (npc.x, npc.y, npc.level) == (2.0, 3.0, levels["cave.nw"])
        # An unknown level must not strand the NPC outside every level.
        await nc._handle_npc_warp(session, gint3(10050) + gchar(4) + gchar(6) + b"nowhere.nw")
        assert npc.level is levels["cave.nw"]

    run(go())


def test_npc_flags_round_trip_through_csv():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        await nc._handle_npc_flags_set(
            session, gint3(10050) + b'alert,mood=angry,"note=a b"')
        npc = server.npc_manager.get_npc(10050)
        assert npc.flags == {"alert": "", "mood": "angry", "note": "a b"}
        session.player.send_raw.reset_mock()
        await nc._handle_npc_flags_get(session, gint3(10050))

    run(go())
    assert sent(session) == [
        gchar(PLO.NC_NPCFLAGS) + gint3(10050) + b'alert,mood=angry,"note=a b"\n']


def test_npc_script_set_decodes_the_csv_and_get_encodes_it():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        await nc._handle_npc_script_set(
            session, gint3(10050) + b'if (created) {,"  setimg a.png;",}')
        npc = server.npc_manager.get_npc(10050)
        assert npc.gs1_source == "if (created) {\n  setimg a.png;\n}"
        session.player.send_raw.reset_mock()
        await nc._handle_npc_script_get(session, gint3(10050))

    run(go())
    assert sent(session) == [
        gchar(PLO.NC_NPCSCRIPT) + gint3(10050)
        + b'"if (created) {","  setimg a.png;",}\n']


def test_npc_reset_clears_state_and_rebroadcasts():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        npc = server.npc_manager.get_npc(10050)
        npc.flags["alert"] = "1"
        npc.gs1_scopes["this"]["hp"] = 5
        session.player.send_raw.reset_mock()
        await nc._handle_npc_reset(session, gint3(10050))
        return npc

    npc = run(go())
    assert npc.flags == {} and npc.gs1_scopes["this"] == {}
    assert npc._dirty
    # Server.cpp:2329-2341: the delete reaches every connection, not the level.
    server.broadcast_to_all.assert_awaited_once()
    assert sent(session) == [chat("NPC script of Guard reset by editor")]


def test_local_npcs_get_sends_a_level_dump():
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        session.player.send_raw.reset_mock()
        await nc._handle_local_npcs_get(session, b"town.nw")
        await nc._handle_local_npcs_get(session, b"nowhere.nw")

    run(go())

    (reply,) = sent(session)
    assert reply.startswith(gchar(PLO.NC_LEVELDUMP) + b'"Variables dump from level town.nw",,')
    assert b'"Variables dump from npc Guard"' in reply


def test_hurt_dxdy_rides_the_npc_props_packet():
    # PropertyHurtDxDy::serialize (PropertySerializers.h:636-644) writes
    # (char)(v + 32) through writeGChar (CString.cpp:1519), which adds 32 again:
    # the wire byte is v + 64, so "no push" is 0x40 0x40 and full push is
    # 0x20 / 0x60. The stored v is signed half pixels in [-32, 32].
    npc = NPC(10050, "Guard")
    assert gchar(NPCPROP.HURTDXDY) not in npc.build_props_packet()
    for stored, wire in (((0, 0), b"\x40\x40"), ((-32, 32), b"\x20\x60"),
                         ((19, -22), bytes((83, 42)))):
        npc.hurt_dxdy = stored
        assert gchar(NPCPROP.HURTDXDY) + wire in npc.build_props_packet()


def test_hurt_dxdy_client_prop_decodes_the_reference_layout():
    npc = NPC(10050, "Guard")
    assert npc.apply_client_prop(NPCPROP.HURTDXDY, b"\x53\x2a") == {
        NPCPROP.HURTDXDY: b"\x53\x2a"}
    assert npc.hurt_dxdy == (19, -22)
    # Out-of-range bytes are clamped to the reference's [-32, 32] and a
    # short payload is dropped.
    assert npc.apply_client_prop(NPCPROP.HURTDXDY, b"\x00\xff") == {
        NPCPROP.HURTDXDY: b"\x20\x60"}
    assert npc.hurt_dxdy == (-32, 32)
    assert npc.apply_client_prop(NPCPROP.HURTDXDY, b"\x40") == {}


def test_hurt_dxdy_never_puts_a_control_byte_in_the_props_packet():
    from pygserver.gs1.objects import set_npc_hurt
    values = [None, 0, 0.5, -0.6875, -1, 1, 2, -2, 1e300, -1e300,
              float("nan"), float("inf"), float("-inf"), -0.0001, 0.99999]
    for dx in values:
        for dy in values:
            npc = NPC(10050, "Guard")
            set_npc_hurt(npc, dx, dy)
            packet = npc.build_props_packet()
            assert b"\n" not in packet[:-1], (dx, dy)
            assert min(packet[:-1]) >= 32, (dx, dy)
            wire = packet.split(gchar(NPCPROP.HURTDXDY), 1)
            if npc.hurt_dxdy is not None:
                assert all(32 <= b <= 96 for b in wire[1][:2]), (dx, dy)
    # Even a raw out-of-range store is clamped when it is encoded.
    for stored in ((-128, 127), (-1000, 1000), (-33, 33)):
        npc = NPC(10050, "Guard")
        npc.hurt_dxdy = stored
        packet = npc.build_props_packet()
        assert b"\n" not in packet[:-1] and min(packet[:-1]) >= 32


def test_npc_script_set_queues_created_like_the_reference():
    # PlayerNCPackets.cpp:226-228 (origin/beta4): setScript, then CREATED.
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        await nc._handle_npc_script_set(
            session, gint3(10050) + b'if (created) {,"  setimg a.png;",}')
        return server.npc_manager.get_npc(10050)

    assert run(go()).image == "a.png"


def test_npc_reset_restores_every_modelled_character_field():
    # NPC.cpp:117-162 (`character = m_initialCharacter`) over Character.h.
    nc, session, server, _ = make_nc()

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        npc = server.npc_manager.get_npc(10050)
        initial = dict(hearts=5.5, rupees=7, bombs=3, arrows=2, bomb_power=2,
                       ap=10, sprite=4, glove_power=1, message="hi",
                       horse_image="horse.png")
        for field, value in initial.items():
            setattr(npc, field, value)
        npc.record_initial_state("town.nw")
        for field, value in dict(hearts=0.5, rupees=999, bombs=99, arrows=99,
                                 bomb_power=3, ap=100, sprite=9, glove_power=2,
                                 message="changed", horse_image="").items():
            setattr(npc, field, value)
        npc.nickname = "changed"
        await nc._handle_npc_reset(session, gint3(10050))
        return npc, initial

    npc, initial = run(go())
    for field, value in initial.items():
        assert getattr(npc, field) == value, field
    assert npc.nickname == ""


def test_a_destroy_tells_the_nc_once_and_the_rc_nothing():
    """Server::deleteNPC (Server.cpp:1636-1639): an NC connection gets
    PLO_NC_NPCDELETE, an RC connection nothing; the NC-initiated delete sends
    it once too."""
    nc, session, server, levels = make_nc()
    rc_player = SimpleNamespace(id=8, connection_type=PLTYPE.RC, send_raw=AsyncMock())
    connections.append(rc_player)
    packet = gchar(PLO.NC_NPCDELETE) + gint3(10050) + b"\n"

    async def go():
        await nc._handle_npc_add(session, b"Guard,10050,Soldier,me,town.nw,1,1")
        await nc._handle_npc_add(session, b"Other,10051,Soldier,me,town.nw,1,1")
        session.player.send_raw.reset_mock()
        await server.npc_manager.destroy_npc(server.npc_manager.get_npc(10050))
        assert sent(session).count(packet) == 1
        session.player.send_raw.reset_mock()
        await nc._handle_npc_delete(session, gint3(10051))

    run(go())

    assert sent(session).count(gchar(PLO.NC_NPCDELETE) + gint3(10051) + b"\n") == 1
    assert rc_player.send_raw.await_count == 0
