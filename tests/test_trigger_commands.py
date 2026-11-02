"""Builtin triggeractions using fake transports and real player inventory."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pygserver.account import Account, AccountManager
from pygserver.audience import Audience
from pygserver.config import ServerConfig
from pygserver.gs2 import GS2ScriptManager, to_csv
from pygserver.level import Level
from pygserver.npc import NPC, NPCManager
from pygserver.player import Player
from pygserver.protocol.constants import NPCPROP, PLPROP, PLO
from pygserver.protocol.packets import (
    PacketBuilder,
    build_npc_weapon_add,
    build_other_player_props,
    build_player_props,
    build_trigger_action,
)
from pygserver.server import GameServer
from pygserver.trigger_commands import dispatch_trigger_command
from pygserver.world import World


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def world(tmp_path):
    server = GameServer.__new__(GameServer)
    server.config = ServerConfig(weapons_dir=str(tmp_path / "weapons"))
    level = Level("lobby.nw")
    server.world = SimpleNamespace(
        get_level=lambda name: level if name == level.name else None,
        get_all_levels=lambda: [level],
        reload_level=AsyncMock(return_value=level),
    )
    server.players = {}
    server.audience = Audience(server)
    server.npc_manager = NPCManager(server)
    server.npc_manager.on_trigger_action = AsyncMock()
    server.gs2_manager = GS2ScriptManager(server, str(tmp_path))
    server.broadcast_to_level = AsyncMock()
    server.broadcast_to_all = AsyncMock()
    server.resend_level = AsyncMock()
    server.rc_manager = SimpleNamespace(broadcast_chat=AsyncMock())
    player = Player(server, 1, AsyncMock(), MagicMock())
    player.account_name = "tester"
    player.nickname = "Tester (Old)"
    player.level = level
    player.send_raw = AsyncMock()
    server.players[player.id] = player
    level.add_player(player)
    return server, player, level


def trigger_payload(action):
    return (PacketBuilder().write_gint3(0).write_gchar(0).write_gchar(0)
            .write_string(action).build())


def test_addweapon_sends_script_and_persists_only_known_names(world, tmp_path):
    server, player, _ = world
    server.config.triggerhack_weapons = True
    weapon = server.gs2_manager.upsert_classic_weapon(
        "-validation", "check.png", "//#CLIENTSIDE\nshow();")
    action = "gr.addweapon, -validation ,unknown,,"

    run(player._handle_trigger_action(trigger_payload(action)))

    assert player.weapons == ["-validation"]
    player.send_raw.assert_awaited_once_with(build_npc_weapon_add(
        weapon.name, weapon.image, weapon.clientside))
    server.broadcast_to_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()

    # Exercise the same account-save path used on disconnect, then reload.
    accounts = AccountManager(server, str(tmp_path / "accounts"))
    try:
        accounts.save_player_to_account(player, Account(player.account_name))
    finally:
        accounts._save_executor.shutdown(wait=True)
    saved = json.loads((tmp_path / "accounts" / "tester.json").read_text())
    assert saved["weapons"] == ["-validation"]


def test_addweapon_csv_names_and_repeated_grants(world):
    server, player, _ = world
    server.config.triggerhack_weapons = True
    server.gs2_manager.upsert_classic_weapon("one,two", "", "show();")
    action = "gr.addweapon," + to_csv(["one,two", "one,two"])
    assert run(dispatch_trigger_command(server, player, action)) is True
    assert player.weapons == ["one,two"]
    # The reference re-announces even when the player already owns a weapon.
    assert player.send_raw.await_count == 2


def test_deleteweapon_removes_and_sends_raw_name(world):
    server, player, _ = world
    server.config.triggerhack_weapons = True
    server.gs2_manager.upsert_classic_weapon("-validation", "", "show();")
    player.add_weapon("-validation")
    assert run(dispatch_trigger_command(
        server, player, "gr.deleteweapon, -validation ,unknown,-validation"))
    assert player.weapons == []
    player.send_raw.assert_awaited_once_with(
        bytes([PLO.NPCWEAPONDEL + 32]) + b"-validation\n")


@pytest.mark.parametrize("command", ["gr.addweapon", "gr.deleteweapon"])
def test_missing_weapon_name_is_silently_consumed(world, command):
    server, player, _ = world
    server.config.triggerhack_weapons = True
    assert run(dispatch_trigger_command(server, player, command)) is True
    player.send_raw.assert_not_awaited()


@pytest.mark.parametrize("action", [
    "gr.addweapon,-validation", "gr.deleteweapon,-validation",
    "gr.setguild,Staff", "gr.rcchat,hello", "gr.npc.move,1,1,1,1,0",
    "gr.npc.setpos,1,1,1", "gr.addguildmember,Staff,tester",
    "gr.removeguildmember,Staff,tester", "gr.removeguild,Staff",
    "gr.setgroup,lobby", "gr.setlevelgroup,lobby", "gr.setplayergroup,tester,lobby",
    "gr.attr,1,hat.png", "gr.fullhearts,8", "gr.updatelevel",
    "gr.es", "gr.es_clear", "gr.es_set,value", "gr.es_append,value",
    "gr.appendfile,test.log,value", "gr.writefile,test.log,value",
    "gr.readfile,test.log,1",
])
def test_disabled_option_returns_false_and_relays(world, action):
    server, player, level = world
    server.config.triggerhack_groups = False
    assert run(dispatch_trigger_command(server, player, action)) is False

    run(player._handle_trigger_action(trigger_payload(action)))

    server.broadcast_to_level.assert_awaited_once_with(
        level.name, build_trigger_action(player.id, 0, 0, 0, action),
        exclude={player.id})
    server.npc_manager.on_trigger_action.assert_awaited_once_with(
        player, 0, 0, action)
    server.world.reload_level.assert_not_awaited()
    server.resend_level.assert_not_awaited()
    player.send_raw.assert_not_awaited()


@pytest.mark.parametrize("action", ["gr.setgroupExtra,lobby", "gr.unknown,lobby"])
def test_dispatch_requires_a_complete_command_name(world, action):
    server, player, _ = world
    assert run(dispatch_trigger_command(server, player, action)) is False
    assert player.group == ""


@pytest.mark.parametrize("command", [
    "GR.setgroup", "  Gr.SeTgRoUp  ", '"  GR.SETGROUP  "',
])
def test_dispatch_normalizes_first_field_and_preserves_arguments(world, command):
    server, player, _ = world
    run(player._handle_trigger_action(trigger_payload(command + ",MiXeD Group")))
    assert player.group == "MiXeD Group"
    server.broadcast_to_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_group_commands_store_only_group_attribute(world):
    server, player, level = world
    other = SimpleNamespace(id=2, account_name="Other", group="")
    server.players[other.id] = other
    level.add_player(other)
    assert run(dispatch_trigger_command(server, player, "gr.setgroup,first"))
    assert player.group == "first"
    assert run(dispatch_trigger_command(server, player, "gr.setlevelgroup,all"))
    assert player.group == other.group == "all"
    assert run(dispatch_trigger_command(
        server, player, "gr.setplayergroup,OTHER,second"))
    assert player.group == "all"
    assert other.group == "second"
    assert run(dispatch_trigger_command(server, player, "gr.setgroup"))
    assert player.group == ""


@pytest.mark.parametrize("x,y,stored_x,stored_y", [
    (12.75, -3.25, 12.75, -3.25),
    (111.5, 0, 111.5, 0),
    (1.1, 2.2, 1.0625, 2.1875),
])
def test_npc_setpos_emits_exact_xy_bytes(world, x, y, stored_x, stored_y):
    server, player, level = world
    server.config.triggerhack_levels = True
    npc = NPC(257)
    level.add_npc(npc)
    server.npc_manager._npcs[npc.id] = npc
    action = f"gr.npc.setpos,{npc.id},{x},{y}"

    run(player._handle_trigger_action(trigger_payload(action)))

    expected = (bytes([PLO.NPCPROPS + 32]) + b' "!'
                + bytes([NPCPROP.X + 32, (int(x * 2) + 32) & 255,
                         NPCPROP.Y + 32, (int(y * 2) + 32) & 255]) + b"\n")
    server.broadcast_to_level.assert_awaited_once_with(level.name, expected)
    assert (npc.x, npc.y) == (stored_x, stored_y)
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_npc_move_uses_old_position_signed_deltas_and_50ms_ticks(world):
    server, player, level = world
    server.config.triggerhack_levels = True
    npc = NPC(257)
    npc.x, npc.y = 10, 20
    level.add_npc(npc)
    server.npc_manager._npcs[npc.id] = npc
    assert run(dispatch_trigger_command(
        server, player, "gr.npc.move,257,-2,3,0.15,7")) is True
    expected = (bytes([PLO.MOVE + 32]) + b' "!'
                + bytes([52, 72, 128, 138, 32, 35, 39, 10]))
    server.broadcast_to_level.assert_awaited_once_with(level.name, expected)
    assert (npc.x, npc.y) == (8, 23)


@pytest.mark.parametrize("action", [
    "gr.npc.setpos,257,1", "gr.npc.move,257,1,2,3",
    "gr.npc.setpos,257,nan,0", "gr.npc.move,257,1,2,inf,0",
])
def test_invalid_npc_arguments_are_refused_without_crashing(world, action):
    server, player, _ = world
    server.config.triggerhack_levels = True
    assert run(dispatch_trigger_command(server, player, action)) is False
    server.broadcast_to_level.assert_not_awaited()


@pytest.mark.parametrize("action,message", [
    ('gr.rcchat,"hello,all",ok', "hello,all,ok,"),
    ("gr.rcchat,hello,", "hello,,"),
    ("gr.rcchat", ""),
])
def test_rcchat_uses_trailing_commas_and_rc_broadcast(world, action, message):
    server, player, _ = world
    server.config.triggerhack_rc = True
    assert run(dispatch_trigger_command(server, player, action))
    server.rc_manager.broadcast_chat.assert_awaited_once_with(
        message, exclude={player.id})


@pytest.mark.parametrize("number", [1, 6, 10, 30])
def test_attr_sets_correct_property_id_and_preserves_raw_csv_remainder(world, number):
    server, player, level = world
    server.config.triggerhack_props = True
    value = '"Hat,MiXeD.png",tail  '
    action = f" GR.ATTR ,{number},{value}"
    prop_id = getattr(PLPROP, f"GATTRIB{number}")

    run(player._handle_trigger_action(trigger_payload(action)))

    assert player.gattribs == {prop_id: value}
    player.send_raw.assert_awaited_once_with(build_player_props({prop_id: value}))
    server.broadcast_to_level.assert_awaited_once_with(
        level.name, build_other_player_props(player.id, {prop_id: value}),
        exclude={player.id})
    server.npc_manager.on_trigger_action.assert_not_awaited()


@pytest.mark.parametrize("action", [
    "gr.attr", "gr.attr,1", "gr.attr,0,hat.png", "gr.attr,31,hat.png",
    "gr.attr,invalid,hat.png",
])
def test_invalid_attr_is_consumed_without_mutation_or_relay(world, action):
    server, player, _ = world
    server.config.triggerhack_props = True
    run(player._handle_trigger_action(trigger_payload(action)))
    assert player.gattribs == {}
    player.send_raw.assert_not_awaited()
    server.broadcast_to_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_attr_can_clear_a_value_and_skips_unchanged_values(world):
    server, player, _ = world
    server.config.triggerhack_props = True
    player.gattribs[PLPROP.GATTRIB1] = "hat.png"
    assert run(dispatch_trigger_command(server, player, "gr.attr,1,"))
    assert player.gattribs[PLPROP.GATTRIB1] == ""
    assert run(dispatch_trigger_command(server, player, "gr.attr,1,"))
    assert player.send_raw.await_count == 1


@pytest.mark.parametrize("requested,limit,maximum,current", [
    (8, 20, 8, 8), (8, 3, 3, 8), (24, 30, 20, 24),
    (260, 20, 4, 4), (-1, 20, 20, 127),
])
def test_fullhearts_sets_maximum_refills_and_sends_props(
        world, requested, limit, maximum, current):
    server, player, level = world
    server.config.triggerhack_props = True
    server.config.heart_limit = limit
    player.hearts = 1
    run(player._handle_trigger_action(trigger_payload(f"gr.fullhearts, {requested} ")))
    assert player.max_hearts == maximum
    assert player.hearts == current
    # The reference sends the requested FULLHEARTS value and stored HALFHEARTS.
    props = {PLPROP.MAXPOWER: requested & 255, PLPROP.CURPOWER: current * 2}
    player.send_raw.assert_awaited_once_with(build_player_props(props))
    server.broadcast_to_level.assert_awaited_once_with(
        level.name, build_other_player_props(player.id, props), exclude={player.id})
    server.npc_manager.on_trigger_action.assert_not_awaited()


@pytest.mark.parametrize("action", ["gr.fullhearts", "gr.fullhearts,3"])
def test_fullhearts_missing_or_unchanged_is_consumed_without_refilling(world, action):
    server, player, _ = world
    server.config.triggerhack_props = True
    player.hearts = 1
    run(player._handle_trigger_action(trigger_payload(action)))
    assert player.max_hearts == 3
    assert player.hearts == 1
    player.send_raw.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


@pytest.mark.parametrize("suffix", ["", ",", ",   ", ",lobby.nw"])
def test_updatelevel_reloads_current_level_and_resends_retired_npcs(world, suffix):
    server, player, level = world
    server.config.triggerhack_levels = True
    npc = NPC(257)
    npc.file_defined = True
    level.add_npc(npc)
    run(player._handle_trigger_action(trigger_payload("gr.updatelevel" + suffix)))
    server.world.reload_level.assert_awaited_once_with(level.name)
    server.resend_level.assert_awaited_once_with(level.name, retired_npc_ids=(257,))
    server.broadcast_to_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_updatelevel_can_reload_another_loaded_level(world):
    server, player, level = world
    server.config.triggerhack_levels = True
    target = Level("other.nw")
    server.world.get_all_levels = lambda: [level, target]
    server.world.reload_level.return_value = target
    assert run(dispatch_trigger_command(server, player, "gr.updatelevel, other.nw "))
    server.world.reload_level.assert_awaited_once_with(target.name)
    server.resend_level.assert_awaited_once_with(target.name, retired_npc_ids=())
    assert player.level is level


def test_updatelevel_does_not_load_an_uncached_file(world):
    server, player, _ = world
    server.config.triggerhack_levels = True
    server.world.get_level = MagicMock(side_effect=AssertionError("must not load"))
    run(player._handle_trigger_action(trigger_payload("gr.updatelevel,uncached.nw")))
    server.world.get_level.assert_not_called()
    server.world.reload_level.assert_not_awaited()
    server.resend_level.assert_not_awaited()
    server.broadcast_to_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_updatelevel_without_current_level_is_consumed(world):
    server, player, _ = world
    server.config.triggerhack_levels = True
    player.level = None
    assert run(dispatch_trigger_command(server, player, "gr.updatelevel,lobby.nw"))
    server.world.reload_level.assert_not_awaited()


def test_updatelevel_reload_failure_is_logged_and_consumed(world, caplog):
    server, player, _ = world
    server.config.triggerhack_levels = True
    server.world.reload_level.side_effect = OSError("disk unavailable")
    run(player._handle_trigger_action(trigger_payload("gr.updatelevel")))
    assert "disk unavailable" in caplog.text
    server.resend_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_updatelevel_uses_real_world_reload_path(world, tmp_path):
    server, player, level = world
    server.config.triggerhack_levels = True
    server.world = World(server)
    server.world.add_level(level)
    server.item_manager = None
    server.baddy_manager = None
    server._register_level_features = AsyncMock()
    level.file_path = str(tmp_path / level.name)
    changed = Level(level.name)
    changed.file_path = level.file_path
    changed.set_tile(2, 3, 123)
    changed.save_to_file()

    assert run(dispatch_trigger_command(server, player, "gr.updatelevel"))

    assert server.world.get_level(level.name) is level
    assert player.level is level
    assert level.get_tile(2, 3) == 123
    assert level.get_player_ids() == [player.id]
    server._register_level_features.assert_awaited_once_with(level)
    server.resend_level.assert_awaited_once_with(level.name, retired_npc_ids=())


@pytest.mark.parametrize("account", ["", ",tester"])
def test_setguild_replaces_suffix_and_sends_self_and_global_props(world, account):
    server, player, _ = world
    server.config.triggerhack_guilds = True
    assert run(dispatch_trigger_command(
        server, player, "gr.setguild,Builders" + account))
    assert player.nickname == "Tester (Builders)"
    assert player.guild_name == "Builders"
    props = {PLPROP.NICKNAME: player.nickname}
    player.send_raw.assert_awaited_once_with(build_player_props(props))
    server.broadcast_to_all.assert_awaited_once_with(
        build_other_player_props(player.id, props), exclude={player.id})


@pytest.mark.parametrize("command", [
    "gr.serverlist", "gr.addguildmember", "gr.removeguildmember", "gr.removeguild",
    "gr.es", "gr.es_clear", "gr.es_set", "gr.es_append",
    "gr.appendfile", "gr.writefile", "gr.readfile",
])
def test_unsupported_builtins_are_logged_and_consumed(world, command, caplog):
    server, player, _ = world
    server.config.triggerhack_guilds = True
    server.config.triggerhack_execscript = True
    server.config.triggerhack_files = True
    with caplog.at_level("INFO"):
        run(player._handle_trigger_action(trigger_payload(command)))
    assert command in caplog.text
    server.broadcast_to_level.assert_not_awaited()
    server.npc_manager.on_trigger_action.assert_not_awaited()


def test_config_defaults_and_serveroptions_round_trip(tmp_path):
    defaults = ServerConfig()
    options = {
        "triggerhack_weapons": False, "triggerhack_groups": True,
        "triggerhack_guilds": False, "triggerhack_rc": False,
        "triggerhack_levels": False,
        "triggerhack_props": False, "triggerhack_execscript": False,
        "triggerhack_files": False,
    }
    path = tmp_path / "serveroptions.txt"
    assert defaults.heart_limit == 3
    defaults.heart_limit = 20
    for key, value in options.items():
        assert getattr(defaults, key) is value
        setattr(defaults, key, not value)
    defaults.to_file(str(path))
    loaded = ServerConfig.from_file(str(path))
    assert loaded.heart_limit == 20
    for key, value in options.items():
        assert getattr(loaded, key) is not value
