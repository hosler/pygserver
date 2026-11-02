"""Server builtins for client triggeractions.

Groups only store ``player.group``; level instancing and visibility isolation
are not implemented. Weapon ownership uses the normal player inventory, which
AccountManager.save_player_to_account persists on save/disconnect.

Reference: GServer-v2/server/src/TriggerCommandHandlers.cpp. A handled command
must bypass both the player relay and NPC events (GServer-v2/server/src/player/
packets/PlayerClientPackets.cpp:1303-1330).
"""

import logging
import struct

from .gs2 import parse_csv_tolerant
from .protocol.constants import NPCPROP, PLPROP, PLO
from .protocol.packets import (
    PacketBuilder,
    build_npc_weapon_del,
    build_other_player_props,
    build_player_props,
)

logger = logging.getLogger(__name__)


async def _addweapon(server, player, fields, action):
    manager = server.gs2_manager
    if manager is not None:
        for field in fields[1:]:
            name = field.strip()
            weapon = manager.get_weapon(name) if name else None
            if weapon is not None:
                player.add_weapon(weapon.name)
                await manager.announce_weapon(player, weapon.name)
    return True


async def _deleteweapon(server, player, fields, action):
    manager = server.gs2_manager
    if manager is not None:
        for field in fields[1:]:
            name = field.strip()
            weapon = manager.get_weapon(name) if name else None
            if weapon is not None and player.has_weapon(weapon.name):
                player.remove_weapon(weapon.name)
                await player.send_raw(build_npc_weapon_del(weapon.name))
    return True


async def _setgroup(server, player, fields, action):
    player.group = fields[1] if len(fields) > 1 else ""
    return True


async def _setlevelgroup(server, player, fields, action):
    if player.level is not None:
        group = fields[1] if len(fields) > 1 else ""
        for target in server.get_players_on_level(player.level.name):
            target.group = group
    return True


async def _setplayergroup(server, player, fields, action):
    if len(fields) < 2:
        return False
    target = server.get_player_by_name(fields[1])
    if target is not None:
        target.group = fields[2] if len(fields) > 2 else ""
    return True


async def _rcchat(server, player, fields, action):
    if server.rc_manager is not None:
        await server.rc_manager.broadcast_chat(
            "".join(field + "," for field in fields[1:]), exclude={player.id})
    return True


def _float32(value):
    """Use the reference's float precision, including 50ms move ticks."""
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def _tile_position(value):
    """Store positions at the reference's signed 16-bit pixel precision."""
    return ((int(value * 16) + 32768) % 65536 - 32768) / 16.0


async def _npc_move(server, player, fields, action):
    if len(fields) != 6:
        return False
    try:
        npc_id = int(fields[1])
        dx, dy = int(fields[2]), int(fields[3])
        duration = _float32(fields[4])
        ticks = int(_float32(duration / _float32(0.05)))
        options = int(fields[5])
    except (ValueError, OverflowError):
        return False
    npc = server.npc_manager.get_npc(npc_id)
    if npc is not None:
        packet = (PacketBuilder().write_gchar(PLO.MOVE).write_gint3(npc_id)
                  .write_gchar(int(npc.x * 2)).write_gchar(int(npc.y * 2))
                  .write_gchar(dx * 2 + 100).write_gchar(dy * 2 + 100)
                  .write_gshort(ticks).write_gchar(options)
                  .write_newline().build())
        if npc.level is not None:
            await server.broadcast_to_level(npc.level.name, packet)
        npc.x = _tile_position(npc.x + dx)
        npc.y = _tile_position(npc.y + dy)
    return True


async def _npc_setpos(server, player, fields, action):
    if len(fields) != 4:
        return False
    try:
        npc_id = int(fields[1])
        x, y = _float32(fields[2]), _float32(fields[3])
        stored_x, stored_y = _tile_position(x), _tile_position(y)
    except (ValueError, OverflowError):
        return False
    npc = server.npc_manager.get_npc(npc_id)
    if npc is not None:
        npc.x, npc.y = stored_x, stored_y
        # These handlers write raw half-tile chars rather than using the
        # general prop serializer (which adjusts half-tile value 223).
        packet = (PacketBuilder().write_gchar(PLO.NPCPROPS).write_gint3(npc_id)
                  .write_gchar(NPCPROP.X).write_gchar(int(x * 2))
                  .write_gchar(NPCPROP.Y).write_gchar(int(y * 2))
                  .write_newline().build())
        if npc.level is not None:
            await server.broadcast_to_level(npc.level.name, packet)
    return True


async def _setguild(server, player, fields, action):
    if len(fields) < 2 or not fields[1]:
        return True
    target = player
    if len(fields) > 2 and fields[2]:
        target = server.get_player_by_name(fields[2])
    if target is not None:
        guild = fields[1]
        nickname = target.nickname.split("(", 1)[0].strip()
        target.nickname = f"{nickname} ({guild})"
        target.guild_name = guild
        props = {PLPROP.NICKNAME: target.nickname}
        await target.send_raw(build_player_props(props))
        await server.broadcast_to_all(
            build_other_player_props(target.id, props), exclude={target.id})
    return True


async def _send_player_props(server, player, props):
    await player.send_props(props)
    if player.level is not None:
        await server.broadcast_to_level(
            player.level.name, build_other_player_props(player.id, props),
            exclude={player.id})


async def _attr(server, player, fields, action):
    # The reference's inline parser uses an inconsistent substring offset
    # (PlayerClientPackets.cpp:1248). Accept the explicit CSV n,value form;
    # retain the raw remainder, including embedded commas and quotes.
    if len(fields) < 3:
        return True
    try:
        number = int(fields[1])
    except ValueError:
        return True
    if 1 <= number <= 30:
        prop_id = getattr(PLPROP, f"GATTRIB{number}")
        value = action.split(",", 2)[2]
        if player.gattribs.get(prop_id, "") != value:
            player.gattribs[prop_id] = value
            await _send_player_props(server, player, {prop_id: value})
    return True


async def _fullhearts(server, player, fields, action):
    _, separator, remainder = action.partition(",")
    if not separator:
        return True
    try:
        hearts = int(remainder.strip()) & 255
    except ValueError:
        hearts = 0  # The reference's string::toNumber returns zero on failure.
    if hearts != player.max_hearts:
        player.max_hearts = server.config.apply_max_hearts(hearts)
        # PlayerProps.cpp:207-222 clamps the maximum, but refills from the
        # requested uint8 value. HALFHEARTS is itself stored as a uint8.
        player.hearts = ((hearts * 2) & 255) / 2.0
        await _send_player_props(server, player, {
            PLPROP.MAXPOWER: hearts,
            PLPROP.CURPOWER: int(player.hearts * 2),
        })
    return True


async def _updatelevel(server, player, fields, action):
    level = player.level
    if level is None:
        return True
    name = action.partition(",")[2].strip()
    if name:
        # World.get_level can load a file. Only cached levels are eligible.
        level = next((candidate for candidate in server.world.get_all_levels()
                      if candidate.name == name), None)
        if level is None:
            return True
    retired_npc_ids = tuple(
        npc.id for npc in level.get_npcs() if getattr(npc, "file_defined", False))
    try:
        reloaded = await server.world.reload_level(level.name)
        await server.resend_level(reloaded.name, retired_npc_ids=retired_npc_ids)
    except (OSError, ValueError) as error:
        logger.warning("Trigger level reload failed for %s: %s", level.name, error)
    return True


async def _ignore(server, player, fields, action):
    # No local guild membership store or compatible server-list cache exists;
    # consume these builtins so they cannot accidentally fire level NPCs.
    # Execscript execution and server file access are deliberately out of scope.
    logger.info("Ignoring unsupported trigger command %s", fields[0])
    return True


# Normalize the first field like PlayerClientPackets.cpp:1068; args stay intact.
_COMMANDS = {
    "gr.addweapon": ("triggerhack_weapons", _addweapon),
    "gr.deleteweapon": ("triggerhack_weapons", _deleteweapon),
    "gr.setgroup": ("triggerhack_groups", _setgroup),
    "gr.setlevelgroup": ("triggerhack_groups", _setlevelgroup),
    "gr.setplayergroup": ("triggerhack_groups", _setplayergroup),
    "gr.rcchat": ("triggerhack_rc", _rcchat),
    "gr.npc.move": ("triggerhack_levels", _npc_move),
    "gr.npc.setpos": ("triggerhack_levels", _npc_setpos),
    "gr.setguild": ("triggerhack_guilds", _setguild),
    "gr.addguildmember": ("triggerhack_guilds", _ignore),
    "gr.removeguildmember": ("triggerhack_guilds", _ignore),
    "gr.removeguild": ("triggerhack_guilds", _ignore),
    "gr.serverlist": (None, _ignore),
    "gr.attr": ("triggerhack_props", _attr),
    "gr.fullhearts": ("triggerhack_props", _fullhearts),
    "gr.updatelevel": ("triggerhack_levels", _updatelevel),
    "gr.es": ("triggerhack_execscript", _ignore),
    "gr.es_clear": ("triggerhack_execscript", _ignore),
    "gr.es_set": ("triggerhack_execscript", _ignore),
    "gr.es_append": ("triggerhack_execscript", _ignore),
    "gr.appendfile": ("triggerhack_files", _ignore),
    "gr.writefile": ("triggerhack_files", _ignore),
    "gr.readfile": ("triggerhack_files", _ignore),
}


async def dispatch_trigger_command(server, player, action: str) -> bool:
    """Return True only when a builtin consumes the action.

    Disabled options return False, preserving the ordinary relay/NPC path.
    Defaults match GServer-v2/server/include/Server.h:143-150.
    """
    fields = parse_csv_tolerant(action)
    command = _COMMANDS.get(fields[0].strip().lower())
    if command is None:
        return False
    option, handler = command
    if option is not None and not getattr(
            server.config, option, option == "triggerhack_groups"):
        return False
    return await handler(server, player, fields, action)
