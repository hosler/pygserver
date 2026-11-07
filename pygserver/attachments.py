"""Absolute gameplay positions over the unchanged attachment wire coordinates."""


def absolute_position(player):
    """Return level-local tiles, adding a resolved NPC to the stored offsets.

    An unresolved attachment has no usable absolute coordinates. Keep the last
    known absolute position until detachment or a new resolvable attach request;
    incoming relative axes still remain available verbatim for client relays.
    """
    kind, npc_id = getattr(player, 'attach_npc', (0, 0))
    if kind == 0 and npc_id > 0:
        level = player.level
        npc = level.get_npc(npc_id) if level is not None else None
        if npc is None:
            player.attach_npc = (0, 0)
            player._attachment_position_unknown = True
        else:
            position = npc.x + player.x, npc.y + player.y
            player._last_absolute_position = position
            return position
    if vars(player).get('_attachment_position_unknown', False):
        return player._last_absolute_position
    position = getattr(player, 'x', 0), getattr(player, 'y', 0)
    player._last_absolute_position = position
    return position


def set_attachment(player, attachment):
    """Retain the wire value for observers, but only link an NPC on this level."""
    absolute_position(player)
    kind, npc_id = attachment
    # This is replay data only; it is never used for absolute-position math.
    player._attachment_wire = attachment
    level = player.level
    npc = level.get_npc(npc_id) if level is not None and npc_id > 0 else None
    resolved = kind == 0 and npc is not None
    player.attach_npc = attachment if resolved else (0, 0)
    player._attachment_position_unknown = npc_id > 0 and not resolved


def clear_attachment(player):
    player.attach_npc = (0, 0)
    player._attachment_wire = (0, 0)
    player._attachment_position_unknown = False
