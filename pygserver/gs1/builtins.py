import time

from reborn_protocol.coords import LEVEL_SIZE, LEVEL_TILE_COUNT
from reborn_protocol.gs1.runtime import UNSET
from reborn_protocol.gs1.values import to_num
from reborn_protocol.gs1.host_shared import tokens_count

from ..combat import CarryObjectSprite
from . import objects
from .host import (
    NPC_ATTR, PLAYER_ATTR, _NW_CLOCK_FIELDS, _PELTWITH_TYPE, _SHOTBY_SOURCE,
    _nw_clock_value, _source_player,
)
from reborn_protocol.gs1.values import to_str  # noqa: F401  - kept: original import block (star-import consumers rely on it)
# ---------------------------------------------------------------------------
# get_builtin readers. Same shape as the _COMMANDS table below: module-level
# functions in an explicit name -> reader dict, so grep finds every built-in
# variable this host answers. Each takes (self, name, indices, ctx, npc,
# player) and returns UNSET when its gate (a player / an NPC) is missing --
# GS1Host.get_builtin then falls back to PLAYER_ATTR / NPC_ATTR and finally to
# the plain flag lookup.
# ---------------------------------------------------------------------------

#: CarryObjectType JOLTBOMB / HOTJOLTBOMB (CommonTypes.h:485-501).
_JOLT_TYPES = (7, 8)

#: carry*-flag name -> the carrysprite value it tests.
_CARRY_SPRITE = {
    "carriesbush": CarryObjectSprite.BUSH,
    "carriesstone": CarryObjectSprite.STONE,
    "carriesvase": CarryObjectSprite.VASE,
    "carriessign": CarryObjectSprite.SIGN,
    "carriesblackstone": CarryObjectSprite.BLACKSTONE,
}


def _b_tiles(self, name, indices, ctx, npc, player):
    resolved = self._resolve_tile(indices, ctx)
    if resolved is None:
        return 0.0
    level, x, y = resolved
    return float(level.get_tile(x, y))


def _b_board(self, name, indices, ctx, npc, player):
    level = getattr(npc, "level", None) if npc is not None else None
    if level is None or not hasattr(level, "get_tile"):
        return 0.0
    # board[i] is the flat level_index() offset, so read it back the other way.
    if not indices:
        return [float(level.get_tile(i % LEVEL_SIZE, i // LEVEL_SIZE))
                for i in range(LEVEL_TILE_COUNT)]
    index = int(to_num(indices[0]))
    if index < 0 or index >= LEVEL_TILE_COUNT:
        return 0.0
    return float(level.get_tile(index % LEVEL_SIZE, index // LEVEL_SIZE))


def _b_tokenscount(self, name, indices, ctx, npc, player):
    # number of tokens from the last `tokenize` (GS1Commands.cpp:3138 sets this
    # on tokenize; mirrors the client host's implementation in
    # pyreborn.gs1_client)
    return tokens_count(ctx)


def _b_save(self, name, indices, ctx, npc, player):
    if npc is None:
        return UNSET
    slots = ctx.vars.scopes["this"].setdefault("save", [0.0] * 10)
    index = int(to_num(indices[0])) if indices else 0
    return float(slots[index]) if 0 <= index < len(slots) else 0.0


def _b_playerscount(self, name, indices, ctx, npc, player):
    return float(len(self._players_on_level(ctx)))


def _b_count(self, name, indices, ctx, npc, player):
    # allplayerscount / npcscount / compuscount / bombscount / arrowscount /
    # itemscount / exploscount / horsescount / signscount (GS1Variables.cpp
    # counts; objects.py has the per-array source lists).
    return objects.count_of(self, name, ctx)


def _b_timevar2(self, name, indices, ctx, npc, player):
    # Serverside timevar2 is the Unix timestamp (seconds).
    return float(int(time.time()))


def _b_playerfreezetime(self, name, indices, ctx, npc, player):
    if player is None or not getattr(player, "is_frozen", False):
        return -1.0
    deadline = getattr(player, "_gs1_freeze_until", None)
    if deadline is None:  # freezeplayer2 has no timed expiry.
        return 0.0
    remaining = max(0.0, deadline - time.monotonic())
    if remaining == 0.0:
        player.is_frozen = False
        player._gs1_freeze_until = None
        return -1.0
    return remaining


def _b_playerlevel(self, name, indices, ctx, npc, player):
    if player is None:
        return UNSET
    lvl = getattr(player, "level", None)
    return getattr(lvl, "name", "") if lvl else ""


def _b_playeronline(self, name, indices, ctx, npc, player):
    return player is not None


def _b_isweapon(self, name, indices, ctx, npc, player):
    return False


def _b_playerswimming(self, name, indices, ctx, npc, player):
    return player is not None and self._player_is_swimming(player)


def _b_carrying(self, name, indices, ctx, npc, player):
    return player is not None and int(getattr(player, "carrysprite", -1)) != -1


def _b_carries_object(self, name, indices, ctx, npc, player):
    sprite = int(getattr(player, "carrysprite", -1)) if player is not None else -1
    return sprite == int(_CARRY_SPRITE[name])


def _b_carriesnpc(self, name, indices, ctx, npc, player):
    carried_npc = (getattr(player, "carryNPC", 0) or
                   getattr(player, "carry_npc", 0) or
                   getattr(player, "npc_id", 0)) if player is not None else 0
    return bool(carried_npc)


def _b_attached(self, name, indices, ctx, npc, player):
    # TServerPlayerProperties.cpp:74-78: attached means attached to actionnpc.
    return (npc is not None and player is not None and
            getattr(player, 'attach_npc', (0, 0)) == (0, npc.id))


def _b_sprite(self, name, indices, ctx, npc, player):
    if npc is None:
        return UNSET
    return self._coerce(npc.flags.get("sprite", 0)) if hasattr(npc, "flags") else 0.0


def _b_timeout(self, name, indices, ctx, npc, player):
    if npc is None:
        return UNSET
    end = getattr(npc, "_timer_end", 0.0)
    return max(0.0, end - time.time()) if end else 0.0


def _b_nw_clock(self, name, indices, ctx, npc, player):
    # nw* clock variables (Server.cpp:178-185, upstream ac3adf01)
    return _nw_clock_value(name)


def _b_shotby(self, name, indices, ctx, npc, player):
    # hit-source flags: WASSHOT only (GS1Flags.cpp:136-138); washit has no
    # equivalent source flags upstream.
    if ctx.active_event != "wasshot":
        return False
    return getattr(ctx, "hit_source", None) == _SHOTBY_SOURCE[name]


def _b_peltwith(self, name, indices, ctx, npc, player):
    if ctx.active_event != "waspelt":
        return False
    return getattr(ctx, "carryobject_type", None) == _PELTWITH_TYPE[name]


def _b_player_weaponscount(self, name, indices, ctx, npc, player):
    # weaponscount / playerweaponscount (b46a9c03). The reference hides the
    # bare spelling; hiding is not implemented here, so both read the player.
    player = _source_player(ctx)
    return UNSET if player is None else objects.player_weaponscount(player)


def _b_player_levelorg(self, name, indices, ctx, npc, player):
    player = _source_player(ctx)
    if player is None:
        return UNSET
    return objects.player_levelorg(player, name.endswith("y"))


def _b_explodedwithjolt(self, name, indices, ctx, npc, player):
    """True while an `exploded` event caused by a joltbomb blast runs (25396a18,
    GS1Flags.cpp:166-178), gated by the scriptengine-gs1.txt option of the same
    name. The reference only ADDS the flag on exploded events, so after a jolt
    blast it stays true until the next exploded event; this is derived from the
    running event instead, so it is false again for every other event (a
    deliberate divergence: the stale value is a bug, not a feature)."""
    config = getattr(self.server, "config", None)
    if getattr(config, "explodedwithjolt", True) is False:
        return False
    return (ctx.active_event == "exploded"
            and getattr(ctx, "carryobject_type", None) in _JOLT_TYPES)


# -- player flags with real pygserver-side backing state

def _b_weaponsenabled(self, name, indices, ctx, npc, player):
    if player is None:
        return UNSET
    return not getattr(player, "weapons_disabled", False)


def _b_playeronhorse(self, name, indices, ctx, npc, player):
    if player is None:
        return UNSET
    hm = getattr(self.server, "horse_manager", None) if self.server is not None else None
    pid = getattr(player, "id", None)
    return hm is not None and pid is not None and hm.is_mounted(pid)


def _b_player_gender(self, name, indices, ctx, npc, player):
    # player.gender only ever exists if a GS1 script set it
    # (_c_setgender/_c_setchargender) - pygserver has no other gender source.
    # 0 = male by the same raw-int convention those commands already use
    # (classic script "sex" 0/1); unset defaults to male, matching upstream's
    # PLSTATUS_MALE-set default.
    if player is None:
        return UNSET
    is_male = int(to_num(getattr(player, "gender", 0))) == 0
    return is_male == (name == "playerismale")


def _b_isleader(self, name, indices, ctx, npc, player):
    if player is None:
        return UNSET
    leader = self._leader_player(ctx)
    return leader is not None and leader is player


# -- NPC/level flags (GS1Flags.cpp setNPCFlags/setLevelFlags)

def _b_visible(self, name, indices, ctx, npc, player):
    if npc is None:
        return UNSET
    return bool(getattr(npc, "visible", True))


def _b_isonmap(self, name, indices, ctx, npc, player):
    return self._gmap_info(ctx) is not None


def _b_compsdead(self, name, indices, ctx, npc, player):
    return bool(self._all_baddies_dead(ctx))


_BUILTINS = {
    "tiles": _b_tiles, "board": _b_board,
    "tokenscount": _b_tokenscount, "save": _b_save,
    **{n: _b_count for n in objects.COUNTS},
    "playerscount": _b_playerscount, "timevar2": _b_timevar2,
    "playerfreezetime": _b_playerfreezetime, "playerlevel": _b_playerlevel,
    "playeronline": _b_playeronline, "isweapon": _b_isweapon,
    "playerswimming": _b_playerswimming,
    "carrying": _b_carrying, "carriesnpc": _b_carriesnpc,
    "attached": _b_attached, "player.attached": _b_attached,
    "sprite": _b_sprite, "timeout": _b_timeout,
    "weaponsenabled": _b_weaponsenabled, "playeronhorse": _b_playeronhorse,
    "playerismale": _b_player_gender, "playerisfemale": _b_player_gender,
    "isleader": _b_isleader, "visible": _b_visible,
    "isonmap": _b_isonmap, "compsdead": _b_compsdead,
    "explodedwithjolt": _b_explodedwithjolt,
    "weaponscount": _b_player_weaponscount,
    "playerweaponscount": _b_player_weaponscount,
    "levelorgx": _b_player_levelorg, "levelorgy": _b_player_levelorg,
    "playerlevelorgx": _b_player_levelorg,
    "playerlevelorgy": _b_player_levelorg,
    **{n: _b_carries_object for n in _CARRY_SPRITE},
    **{n: _b_nw_clock for n in _NW_CLOCK_FIELDS},
    **{n: _b_shotby for n in _SHOTBY_SOURCE},
    **{n: _b_peltwith for n in _PELTWITH_TYPE},
}

# A name in both a reader table and a plain attribute table would be
# unreachable in the attribute table (readers are consulted first).
assert not (set(_BUILTINS) & (set(PLAYER_ATTR) | set(NPC_ATTR)))
# -- command handlers -------------------------------------------------------
