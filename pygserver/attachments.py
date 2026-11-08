"""Absolute gameplay positions over the unchanged attachment wire coordinates."""

from reborn_protocol import coords


def _attachment_npc_position(player, attachment):
    """Resolve an NPC on this level or the same gmap into the player's frame."""
    kind, npc_id = attachment
    level = player.level
    if kind != 0 or npc_id <= 0 or level is None:
        return None
    npc = level.get_npc(npc_id)
    if npc is not None:
        return npc.x, npc.y
    server = getattr(player, 'server', None)
    manager = getattr(server, 'npc_manager', None)
    npc = manager.get_npc(npc_id) if manager is not None else None
    if npc is None or npc.level is None:
        return None
    if npc.level is level:
        return npc.x, npc.y
    world = server.world
    player_info = world.get_gmap_for_level(level.name)
    npc_info = world.get_gmap_for_level(npc.level.name)
    if player_info is None or npc_info is None or player_info[0] is not npc_info[0]:
        return None
    return coords.local_to_world(npc.x, npc.y,
                                 npc_info[1] - player_info[1],
                                 npc_info[2] - player_info[2])


def absolute_position(player):
    """Return level-local tiles, adding a resolved NPC to the stored offsets.

    Resolve the NPC's current level on every call to follow segment crossings.
    An unresolved attachment has no usable absolute coordinates. Keep the last
    known absolute position until detachment or a new resolvable attach request;
    incoming relative axes still remain available verbatim for client relays.
    """
    kind, npc_id = getattr(player, 'attach_npc', (0, 0))
    if kind == 0 and npc_id > 0:
        npc_position = _attachment_npc_position(player, (kind, npc_id))
        if npc_position is None:
            player.attach_npc = (0, 0)
            player._attachment_position_unknown = True
        else:
            position = npc_position[0] + player.x, npc_position[1] + player.y
            player._last_absolute_position = position
            return position
    if vars(player).get('_attachment_position_unknown', False):
        return player._last_absolute_position
    position = getattr(player, 'x', 0), getattr(player, 'y', 0)
    player._last_absolute_position = position
    return position


def set_attachment(player, attachment):
    """Retain the wire value and link NPCs on this level or the same gmap.

    Missing NPCs and NPCs outside this level's gmap remain unresolved.
    """
    absolute_position(player)
    _, npc_id = attachment
    # This is replay data only; it is never used for absolute-position math.
    player._attachment_wire = attachment
    resolved = _attachment_npc_position(player, attachment) is not None
    player.attach_npc = attachment if resolved else (0, 0)
    player._attachment_position_unknown = npc_id > 0 and not resolved


def clear_attachment(player):
    player.attach_npc = (0, 0)
    player._attachment_wire = (0, 0)
    player._attachment_position_unknown = False
