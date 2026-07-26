from __future__ import annotations

def players_on_level_for(server, level):
    """Everyone attached to `level`, via the server's Audience.

    Audience.players_on_level resolves a level NAME through world.get_level, so
    a level the world does not hold - a detached level, and every level a unit
    test hands this host directly - would come back empty where the old inline
    loop worked. Those fall back to the level's own player set, which is the
    same source the audience reads (audience.py:110).
    """
    if server is None or level is None or not hasattr(level, "get_player_ids"):
        return []
    name = getattr(level, "name", None)
    audience = getattr(server, "audience", None)
    world = getattr(server, "world", None)
    if (audience is not None and name and world is not None
            and getattr(world, "get_level", None) is not None
            and world.get_level(name) is level):
        return audience.players_on_level(name)
    out = []
    for pid in level.get_player_ids():
        p = server.get_player(pid)
        if p is not None:
            out.append(p)
    return out


def leader_player_for_level(server, level):
    """First player on `level` (GS1Flags.cpp isleader / Level::isPlayerLeader),
    used as the triggering-player context for NPC events that have no
    natural player of their own - notably `timeout`. GServer-v2 documents
    exactly this: the level leader "can trigger timeout events on NPCs that
    didn't issue the timereverywhere command" (scripting-gs1-flags.md), i.e.
    upstream runs a non-timereverywhere `timeout` in the leader's script
    context, not player-less. Without a player context here, bare (unprefixed)
    `set`/`unset` flags - which run_npc_event stores on player.flags - have
    nowhere to persist and can never be read back by a later event, breaking
    any quest that sets a flag on the player (e.g. a beer-guard NPC) and
    later reads it from an unrelated NPC's `timeout` (e.g. a mountain guard
    that should unblock once `drunkguard` is set).

    Same "first player" lookup as GS1Host._leader_player, and it goes through
    players_on_level_for so level membership has one definition: Level._players
    is insertion-ordered and the audience preserves that order, so element 0 is
    genuinely "first to join and still present". Returns None (matching prior
    behaviour) if the level has no players.
    """
    players = players_on_level_for(server, level)
    return players[0] if players else None


# -- script binding / event firing -----------------------------------------
