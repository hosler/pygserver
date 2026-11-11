"""Level persistence, live reload, and control-chat command tests."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pygserver.level import Level
from pygserver.handlers.misc import MiscHandlers
from pygserver.npc import NPC, NPCManager
from pygserver.protocol.constants import PLO, PLPERM
from pygserver.protocol.packets import PacketBuilder
from pygserver.rc import RCManager, RCSession
from pygserver.nc import NCManager, NCSession
from pygserver.server import GameServer
from pygserver.world import World


def _sample_level(path: Path, tile_id: int = 7) -> Level:
    level = Level(path.name)
    level.file_path = str(path)
    level.set_tile(2, 3, tile_id)
    level._links.append({
        'dest_level': 'next room.nw', 'x': 1, 'y': 2,
        'width': 3, 'height': 4, 'dest_x': '5.5', 'dest_y': '6.5',
    })
    level._signs[(8, 9)] = 'first line\nsecond line'
    level._chests.append({'x': 10, 'y': 11, 'item': 0, 'sign': 2})
    level._npc_defs.append({
        'x': 12.5, 'y': 13.0, 'image': 'person.png',
        'code': 'if (created) setcharani idle,;',
    })
    level._baddies.append({
        'x': 14.0, 'y': 15.5, 'type': 5, 'verses': ['one', 'two'],
    })
    return level


def _chat_text(packet: bytes) -> str:
    assert packet[0] == PLO.RC_CHAT + 32
    return packet[1:-1].decode('latin-1')


def test_level_serializer_round_trip(tmp_path):
    first = _sample_level(tmp_path / 'room.nw')
    second = Level('room.nw')
    second._parse_nw_file(first.serialize().encode('latin-1'))

    assert second.get_board_packet() == first.get_board_packet()
    assert second.get_links() == first.get_links()
    assert second.get_signs() == first.get_signs()
    assert second.get_chest_defs() == first.get_chest_defs()
    assert second.get_npc_defs() == first.get_npc_defs()
    assert second.get_baddy_defs() == first.get_baddy_defs()


def test_all_reachable_level_fixtures_round_trip():
    roots = [Path(__file__).parent]
    optional_world = Path(__file__).parents[2] / 'funtimes' / 'world'
    if optional_world.is_dir():
        roots.append(optional_world)
    paths = sorted(path for root in roots for path in root.rglob('*.nw'))
    assert paths

    for path in paths:
        first = Level.load(str(path))
        second = Level(path.name)
        second._parse_nw_file(first.serialize().encode('latin-1'))
        assert second.get_board_packet() == first.get_board_packet(), path
        assert second.get_links() == first.get_links(), path
        assert second.get_signs() == first.get_signs(), path
        assert second.get_chest_defs() == first.get_chest_defs(), path
        assert second.get_npc_defs() == first.get_npc_defs(), path
        assert second.get_baddy_defs() == first.get_baddy_defs(), path


def test_atomic_save_failure_leaves_original_file(tmp_path):
    path = tmp_path / 'room.nw'
    path.write_text('original\n', encoding='latin-1')
    level = _sample_level(path)

    with patch.object(os, 'replace', side_effect=OSError('replace failed')):
        with pytest.raises(OSError, match='replace failed'):
            level.save_to_file()

    assert path.read_text(encoding='latin-1') == 'original\n'
    assert list(tmp_path.iterdir()) == [path]


def test_serializer_rejects_unsupported_feature():
    level = Level('room.nw')
    level._parse_nw_file(b'GLEVNW01\nUNSUPPORTED value\n')

    with pytest.raises(ValueError, match='unsupported level feature'):
        level.serialize()


def test_reload_level_keeps_player_and_level_identity(tmp_path):
    path = tmp_path / 'room.nw'
    original = _sample_level(path, tile_id=7)
    original.save_to_file()
    player = SimpleNamespace(id=41, level=original, x=22.5, y=18.0)
    original.add_player(player)

    server = SimpleNamespace(
        config=SimpleNamespace(levels_dir=str(tmp_path)),
        npc_manager=SimpleNamespace(_npcs={}),
        item_manager=None,
        baddy_manager=None,
        _register_level_features=AsyncMock(),
    )
    world = World(server)
    world.add_level(original)
    changed = _sample_level(path, tile_id=123)
    changed.save_to_file()

    reloaded = asyncio.run(world.reload_level('room.nw'))

    assert reloaded is original
    assert player.level is original
    assert original.get_player_ids() == [41]
    assert (player.x, player.y) == (22.5, 18.0)
    assert original.get_tile(2, 3) == 123


@pytest.mark.parametrize('command, expected', [
    ('/savelevel room.nw', 'Saved level room.nw'),
    ('/reloadlevel room.nw', 'Reloaded level room.nw and resent it to players'),
    ('/updatelevel room.nw', 'Reloaded level room.nw and resent it to players'),
])
def test_level_control_commands_reply(command, expected, tmp_path):
    level = _sample_level(tmp_path / 'room.nw')
    world = MagicMock()
    world.get_level.return_value = level
    world.reload_level = AsyncMock(return_value=level)
    server = SimpleNamespace(world=world, resend_level=AsyncMock())
    manager = RCManager(server)
    player = SimpleNamespace(account_name='editor', send_raw=AsyncMock())
    session = RCSession(player=player, rights=PLPERM.UPDATELEVEL)

    asyncio.run(manager.process_chat(command, session))

    assert expected in _chat_text(player.send_raw.await_args.args[0])
    if not command.startswith('/savelevel'):
        server.resend_level.assert_awaited_once_with(
            'room.nw', retired_npc_ids=())
    else:
        assert (tmp_path / 'room.nw').exists()


def test_level_control_command_refuses_missing_right():
    server = SimpleNamespace(world=MagicMock(), resend_level=AsyncMock())
    manager = RCManager(server)
    player = SimpleNamespace(account_name='viewer', send_raw=AsyncMock())
    session = RCSession(player=player, rights=PLPERM.VIEWATTRIBUTES)

    asyncio.run(manager.process_chat('/reloadlevel room.nw', session))

    assert _chat_text(player.send_raw.await_args.args[0]) == (
        '/reloadlevel: permission denied (UPDATELEVEL required)'
    )
    server.world.reload_level.assert_not_called()


def test_level_control_command_reports_failure_text():
    world = MagicMock()
    world.reload_level = AsyncMock(side_effect=OSError('disk unavailable'))
    server = SimpleNamespace(world=world, resend_level=AsyncMock())
    manager = RCManager(server)
    player = SimpleNamespace(account_name='editor', send_raw=AsyncMock())
    session = RCSession(player=player, rights=PLPERM.UPDATELEVEL)

    asyncio.run(manager.process_chat('/reloadlevel room.nw', session))

    assert _chat_text(player.send_raw.await_args.args[0]) == (
        '/reloadlevel room.nw failed: disk unavailable'
    )


def test_reload_deletes_retired_npcs_before_replacement_props(tmp_path):
    path = tmp_path / 'room.nw'
    level = _sample_level(path)
    level.save_to_file()

    packets = []

    class RecordingPlayer:
        id = 41
        x = 22.5
        y = 18.0

        async def send_raw(self, packet):
            packets.append(packet)

        async def warp(self, level_name, x, y):
            replacement = server.world.get_level(level_name)
            for npc in replacement.get_npcs():
                await self.send_raw(npc.build_props_packet())

    player = RecordingPlayer()
    server = SimpleNamespace(
        config=SimpleNamespace(levels_dir=str(tmp_path)),
        item_manager=None,
        baddy_manager=None,
    )
    server.world = World(server)
    server.world.add_level(level)
    server.npc_manager = NPCManager(server)
    server._register_level_features = (
        GameServer._register_level_features.__get__(server))
    server.get_players_on_level = (
        lambda level_name: [player] if level_name == level.name else [])
    server.resend_level = GameServer.resend_level.__get__(server)
    old_npcs = [NPC(10000 + offset, 'levelnpc') for offset in range(2)]
    for npc in old_npcs:
        npc.file_defined = True
        level.add_npc(npc)
        server.npc_manager._npcs[npc.id] = npc
    server.npc_manager._next_id = 10002

    manager = RCManager(server)
    control_player = SimpleNamespace(
        account_name='editor', send_raw=AsyncMock())
    session = RCSession(
        player=control_player, rights=PLPERM.UPDATELEVEL)

    asyncio.run(manager.process_chat('/reloadlevel room.nw', session))

    replacement_ids = [npc.id for npc in level.get_npcs()]
    assert replacement_ids
    old_ids = [npc.id for npc in old_npcs]
    assert not set(old_ids) & set(replacement_ids)
    delete_packets = [
        (index, packet) for index, packet in enumerate(packets)
        if packet[0] == PLO.NPCDEL + 32]
    prop_indexes = [
        index for index, packet in enumerate(packets)
        if packet[0] == PLO.NPCPROPS + 32]
    deleted_ids = [packet[1:4] for _, packet in delete_packets]
    assert deleted_ids == [
        PacketBuilder().write_gint3(npc_id).build()
        for npc_id in old_ids]
    assert prop_indexes
    assert max(index for index, _ in delete_packets) < min(prop_indexes)


def _editor_manager(level, rights=PLPERM.UPDATELEVEL):
    world = MagicMock()
    world.get_level.side_effect = (
        lambda name: level if name == level.name else None)
    server = SimpleNamespace(world=world, resend_level=AsyncMock())
    manager = RCManager(server)
    player = SimpleNamespace(
        id=51, account_name='editor', send_raw=AsyncMock())
    return manager, RCSession(player=player, rights=rights), server


@pytest.mark.parametrize('command, collection, expected', [
    ('/sign add room.nw 2 3 hello world', '_signs', {(2, 3): 'hello world'}),
    ('/chest add room.nw 4 5 bombs 7', '_chests',
     [{'x': 4, 'y': 5, 'item': 3, 'sign': 7}]),
    ('/link add room.nw 6 7 2 3 next.nw 8 9', '_links', [{
        'dest_level': 'next.nw', 'x': 6, 'y': 7, 'width': 2, 'height': 3,
        'dest_x': '8', 'dest_y': '9',
    }]),
])
def test_level_object_add_commands(command, collection, expected):
    level = Level('room.nw')
    manager, session, server = _editor_manager(level)

    asyncio.run(manager.process_chat(command, session))

    assert getattr(level, collection) == expected
    server.resend_level.assert_awaited_once_with('room.nw')
    assert 'Added' in _chat_text(session.player.send_raw.await_args.args[0])


@pytest.mark.parametrize('kind, setup, collection', [
    ('sign', lambda level: level._signs.update({(2, 3): 'old'}), '_signs'),
    ('chest', lambda level: level._chests.append(
        {'x': 2, 'y': 3, 'item': 0, 'sign': 0}), '_chests'),
    ('link', lambda level: level._links.append({
        'x': 2, 'y': 3, 'width': 1, 'height': 1,
        'dest_level': 'next.nw', 'dest_x': '4', 'dest_y': '5'}), '_links'),
])
def test_level_object_delete_commands(kind, setup, collection):
    level = Level('room.nw')
    setup(level)
    manager, session, server = _editor_manager(level)

    asyncio.run(manager.process_chat(f'/{kind} del room.nw 2 3', session))

    assert not getattr(level, collection)
    server.resend_level.assert_awaited_once_with('room.nw')
    assert 'Deleted' in _chat_text(session.player.send_raw.await_args.args[0])


@pytest.mark.parametrize('command', [
    '/sign add room.nw 2 3 text',
    '/chest add room.nw 2 3 bombs',
    '/link add room.nw 2 3 1 1 next.nw 4 5',
    '/eval room.nw if (created) { sendtorc "hello"; }',
])
def test_editor_commands_require_updatelevel(command):
    level = Level('room.nw')
    manager, session, server = _editor_manager(level, PLPERM.VIEWATTRIBUTES)

    asyncio.run(manager.process_chat(command, session))

    assert 'permission denied (UPDATELEVEL required)' in _chat_text(
        session.player.send_raw.await_args.args[0])
    server.resend_level.assert_not_awaited()


@pytest.mark.parametrize('command, expected', [
    ('/sign add missing.nw 2 3 text', 'unknown level: missing.nw'),
    ('/chest add room.nw 64 3 bombs', 'coordinates out of range: 64 3'),
    ('/link add room.nw 63 3 2 1 next.nw 4 5',
     'link area out of range: 63 3 2 1'),
    ('/sign del room.nw 2 3', 'no sign at 2 3'),
    ('/chest del room.nw 2 3', 'no chest at 2 3'),
    ('/link del room.nw 2 3', 'no link at 2 3'),
])
def test_level_object_commands_report_distinct_errors(command, expected):
    level = Level('room.nw')
    manager, session, server = _editor_manager(level)

    asyncio.run(manager.process_chat(command, session))

    assert expected in _chat_text(session.player.send_raw.await_args.args[0])
    server.resend_level.assert_not_awaited()


def test_save_after_sign_add_writes_sign(tmp_path):
    level = Level('room.nw')
    level.file_path = str(tmp_path / 'room.nw')
    manager, session, _server = _editor_manager(level)

    asyncio.run(manager.process_chat('/sign add room.nw 2 3 saved text', session))
    asyncio.run(manager.process_chat('/savelevel room.nw', session))

    assert 'SIGN 2 3\nsaved text\nSIGNEND' in (
        tmp_path / 'room.nw').read_text(encoding='latin-1')


def test_board_modify_is_not_written_by_savelevel(tmp_path):
    """A client PLI_BOARDMODIFY only appends a board change (Level.cpp:1743);
    Level::saveLevel writes the tile array (Level.cpp:999-1030), so the edit
    shows live but never reaches the file. A server-side tile write does."""
    level = Level('room.nw')
    level.file_path = str(tmp_path / 'room.nw')
    manager, session, server = _editor_manager(level)
    server.world.get_gmap_for_level.return_value = None

    class EditingPlayer(MiscHandlers):
        pass

    player = EditingPlayer()
    player.id = 51
    player.level = level
    player.server = SimpleNamespace(
        world=server.world, broadcast_to_level=AsyncMock())
    tile_id = 314
    payload = bytes((12 + 32, 12 + 32, 1 + 32, 1 + 32,
                     (tile_id >> 7) + 32, (tile_id & 0x7f) + 32))

    asyncio.run(player._handle_board_modify(payload))
    level.set_tile(20, 20, 271)
    asyncio.run(manager.process_chat('/savelevel room.nw', session))
    reloaded = Level.load(str(tmp_path / 'room.nw'))

    assert level.get_tile(12, 12) == tile_id
    assert reloaded.get_tile(12, 12) == 0
    assert reloaded.get_tile(20, 20) == 271


def test_eval_scratch_is_reused_hidden_and_never_serialized():
    async def run():
        level = Level('room.nw')
        manager, session, server = _editor_manager(level)
        server.npc_manager = NPCManager(server)
        server.rc_manager = manager
        await manager.process_chat('/eval room.nw if (created) { this.a = 1; }', session)
        manager._eval_times.clear()
        await manager.process_chat('/eval room.nw if (created) { this.a = 2; }', session)
        assert len(manager._eval_npcs) == 1
        npc = manager._eval_npcs['editor']
        assert npc.visible is False
        assert 'NPC ' not in level.serialize()
        manager._sessions[session.player.id] = session
        manager.unregister_session(session.player.id)
        assert npc not in level.get_npcs()

    asyncio.run(run())


def test_eval_restores_mangled_newlines():
    async def run():
        level = Level('room.nw')
        manager, session, server = _editor_manager(level)
        server.npc_manager = NPCManager(server)
        server.rc_manager = manager
        await manager.process_chat(
            '/eval room.nw if (created) { this.a = 1;\xa7this.b = 2; }',
            session)
        assert manager._eval_npcs['editor'].gs1_source == (
            'if (created) { this.a = 1;\nthis.b = 2; }')

    asyncio.run(run())


def test_eval_syntax_error_replies_to_caller():
    async def run():
        level = Level('room.nw')
        manager, session, server = _editor_manager(level)
        server.npc_manager = NPCManager(server)
        server.rc_manager = manager
        await manager.process_chat(
            '/eval room.nw this is not valid script(((;', session)
        assert _chat_text(session.player.send_raw.await_args.args[0]).startswith(
            '[eval compile error]')

    asyncio.run(run())


def test_nc_npc_script_syntax_error_replies_to_saving_session():
    npc = NPC(10001)
    server = SimpleNamespace()
    server.npc_manager = NPCManager(server)
    server.npc_manager._npcs[npc.id] = npc
    manager = NCManager(server)
    player = SimpleNamespace(account_name='editor', send_raw=AsyncMock())
    session = NCSession(player=player)
    data = (PacketBuilder().write_gint3(npc.id).build() + b'function ( {')

    asyncio.run(manager._handle_npc_script_set(session, data))

    assert _chat_text(player.send_raw.await_args.args[0]).startswith(
        '[NC NPC compile error]')
    assert npc.gs1_program is None


@pytest.mark.parametrize('kind', ['class', 'weapon'])
def test_nc_asset_syntax_error_replies_to_saving_session(kind):
    asset_manager = MagicMock()
    server = SimpleNamespace(gs2_manager=asset_manager)
    manager = NCManager(server)
    player = SimpleNamespace(account_name='editor', send_raw=AsyncMock())
    session = NCSession(player=player)
    builder = PacketBuilder().write_gstring('asset')
    if kind == 'weapon':
        builder.write_gstring('image.png')
    data = builder.build() + b'function ( {'

    handler = (manager._handle_class_add if kind == 'class'
               else manager._handle_weapon_add)
    asyncio.run(handler(session, data))

    assert _chat_text(player.send_raw.await_args.args[0]).startswith(
        f'[NC {kind} compile error]')
    getattr(asset_manager, f'upsert_{kind}').assert_not_called()
