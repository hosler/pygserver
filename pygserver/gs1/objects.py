"""GS1 object arrays and per-element fields.

players[] allplayers[] npcs[] compus[] bombs[] arrows[] items[] explos[]
horses[] signs[], their `<name>count` variables, and the `array[i].field`
readers/writers.

Reference: GServer-v2 GS1Variables.cpp (level arrays and counts, ~l.129-364),
Server.Init.cpp:42-61 (allplayers / allplayerscount), and each element's field
table = its constructScriptParameters (Player.cpp:1329, NPC.cpp:1794,
level/LevelBomb.h, LevelArrow.h, LevelItem.h, LevelExplosion.h, LevelHorse.h,
LevelSign.h, LevelBaddy.h).

Index rules shared by every array (GS1Variables.cpp / GameVariable::getCopy):
  * players[-1] is the initiating player, npcs[-1] the running NPC; any other
    negative index is no object.
  * players/npcs/compus out of range is no object: `with` skips its block and
    a field read is 0 ("Identifier did not contain a script object" is a
    recoverable error that yields 0, GS1Visitor.cpp:1558).
  * bombs/arrows/items/explos/horses/signs hand back the raw index and the
    field lookup then finds nothing, which also reads as 0 for us.
  * an array reference without an index is element 0 (the whole-array value
    is flattened to its first element by getScriptObject).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from reborn_protocol.gs1.runtime import UNSET
from reborn_protocol.gs1.values import to_num

from .. import npc_motion
from ..attachments import absolute_position

#: array name -> element kind
ARRAYS = {
    "players": "player", "allplayers": "player", "npcs": "npc",
    "compus": "baddy", "bombs": "bomb", "arrows": "arrow", "items": "item",
    "explos": "explosion", "horses": "horse", "signs": "sign",
}

#: `<name>count` variable -> array name (playerscount predates this module)
COUNTS = {name + "count": name for name in ARRAYS}

# Reference field tables. Where an object stores the value in a different
# pygserver attribute the table maps the reference name to it. Only names that
# exist on the real Player/NPC objects belong here; reference properties
# pygserver keeps no state for are in _PLAYER_STATELESS / handled below.
_PLAYER_ATTR = {
    "hearts": "hearts", "hp": "hearts",
    "fullhearts": "max_hearts", "maxhp": "max_hearts",
    "mp": "mp", "ap": "ap",
    "rupees": "rupees", "gralats": "rupees",
    "bombs": "bombs", "darts": "arrows",
    "glovepower": "glove_power", "swordpower": "sword_power",
    "shieldpower": "shield_power",
    "dir": "direction", "sprite": "sprite",
    "kills": "kills", "deaths": "deaths",
    "carrysprite": "carrysprite",
    "rating": "elo_rating", "ratingd": "elo_deviation",
    "logintime": "login_time",
    "id": "id",
    "z": "z",
}
_PLAYER_READONLY = frozenset({"id", "logintime"})
# Properties in Player::constructScriptParameters with no pygserver state:
# they read 0 and a write is swallowed (never invents an attribute).
_PLAYER_STATELESS = frozenset({
    "bombpower", "shootpower", "lastdead", "hurtpower", "hurtdx", "hurtdy",
    "trial"})

_NPC_ATTR = {
    "hearts": "hearts", "hp": "hearts",
    "rupees": "rupees", "gralats": "rupees",
    "bombs": "bombs", "darts": "arrows",
    "glovepower": "glove_power", "swordpower": "sword_power",
    "shieldpower": "shield_power",
    "dir": "direction", "ap": "ap", "id": "id", "z": "z",
}
_NPC_READONLY = frozenset({"id", "width", "height"})

# -- write sanitising -------------------------------------------------------
# A script can compute any double. Every numeric write is clamped to what its
# wire encoding can carry (reborn_protocol.props: GBYTE n bytes, TILEZ, ...)
# and a non-finite value is dropped, so a bad write can never reach a packet
# builder (int(inf) raises OverflowError there).
_G3 = 223 * (128 * 128 + 128 + 1)   # largest GBYTE-3 value
DROP = object()

# state attribute -> (low, high, integral)
_LIMITS = {
    "hearts": (0.0, 111.5, False),      # wire CURPOWER = halves, 1 byte
    "max_hearts": (0, 223, True),
    "mp": (0, 223, True), "ap": (0, 223, True),
    "rupees": (0, _G3, True),
    "bombs": (0, 99, True), "arrows": (0, 99, True),
    "glove_power": (0, 3, True), "sword_power": (0, 20, True),
    "shield_power": (0, 3, True),
    "direction": (0, 3, True), "sprite": (0, 55, True),
    # kills/deaths never go negative (Player.cpp kills/deaths setters)
    "kills": (0, _G3, True), "deaths": (0, _G3, True),
    "carrysprite": (-1, 223, True),
    "z": (-50.0, 170.0, False),         # TILEZ range
}
_COORD_ATTRS = frozenset({"x", "y"})


def coordinate_limit(host, target):
    """Upper bound of a level-local coordinate: one level, or the whole
    gmap when the object's level is on one."""
    server = getattr(host, "server", None)
    world = getattr(server, "world", None)
    level = getattr(target, "level", None)
    if world is not None and level is not None and hasattr(
            world, "get_gmap_for_level"):
        info = world.get_gmap_for_level(getattr(level, "name", ""))
        if info is not None:
            gmap = info[0]
            return 64.0 * max(int(getattr(gmap, "width", 1) or 1),
                              int(getattr(gmap, "height", 1) or 1))
    return 64.0


def sanitize(host, target, attr, value):
    """Clamp `value` for state attribute `attr`; DROP if non-finite.
    Attributes with no numeric limit pass through unchanged."""
    if attr in _COORD_ATTRS and hasattr(target, "move_queue"):
        # An NPC's x/y are not clamped: the pixel wrap of NPC.cpp:1870-1935.
        number = to_num(value)
        wrapped = npc_motion.script_coordinate(
            getattr(host, "server", None), target, attr, number)
        return DROP if wrapped is None else wrapped
    if attr in _COORD_ATTRS:
        low, high, integral = 0.0, coordinate_limit(host, target), False
    else:
        limits = _LIMITS.get(attr)
        if limits is None:
            return value
        low, high, integral = limits
    number = to_num(value)
    if not math.isfinite(number):
        return DROP
    number = max(low, min(high, number))
    return math.floor(number) if integral else number


# NPC HURTDXDY is kept as signed half pixels in [-32, 32] (the reference's
# hurtPushDeltaInHalfPixels, NPC.h:622); the hurtdx/hurtdy builtins scale by 32
# (NPC.cpp:1976-2002) and npc.hurt_dxdy_wire() encodes the wire bytes.
_HURT_MID = 32


def npc_hurt(npc):
    """(dx, dy) in [-1, 1] from the NPC's stored half-pixel push."""
    raw = getattr(npc, "hurt_dxdy", None)
    if isinstance(raw, tuple) and len(raw) == 2:
        return raw[0] / _HURT_MID, raw[1] / _HURT_MID
    return 0.0, 0.0


def set_npc_hurt(npc, dx=None, dy=None):
    """Store a hurt push (each axis clamped to [-1, 1]; None keeps it)."""
    cur = getattr(npc, "hurt_dxdy", None)
    if not (isinstance(cur, tuple) and len(cur) == 2):
        cur = (0, 0)
    out = []
    for new, old in ((dx, cur[0]), (dy, cur[1])):
        value = None if new is None else to_num(new)
        if value is None or not math.isfinite(value):
            out.append(old)
            continue
        # static_cast<int8_t>(clamp(v, -1, 1) * 32) truncates toward zero.
        out.append(int(max(-1.0, min(1.0, value)) * _HURT_MID))
    npc.hurt_dxdy = (out[0], out[1])


def player_wire_props(player, attr, table):
    """{prop id: value} to send after state attribute `attr` changed.
    `table` is host.PLAYER_PROP_WIRE (attr -> (prop, encoder))."""
    from ..protocol.constants import PLPROP
    if attr == "x":
        return {PLPROP.X2: float(player.x)}
    if attr == "y":
        return {PLPROP.Y2: float(player.y)}
    if attr == "z":
        z = float(getattr(player, "z", 0.0))
        return {PLPROP.Z: int(z), PLPROP.Z2: z}
    if attr in ("direction", "sprite"):
        # PropertySprite: sprite << 2 | direction
        return {PLPROP.SPRITE: (int(getattr(player, "sprite", 0)) << 2)
                | (int(getattr(player, "direction", 0)) & 3)}
    simple = {"mp": PLPROP.MAGICPOINTS, "ap": PLPROP.ALIGNMENT,
              "kills": PLPROP.KILLSCOUNT, "deaths": PLPROP.DEATHSCOUNT,
              "carrysprite": PLPROP.CARRYSPRITE}
    if attr in simple:
        return {simple[attr]: int(getattr(player, attr, 0))}
    wire = table.get(attr)
    if wire is not None:
        prop_id, enc = wire
        return {prop_id: enc(getattr(player, attr))}
    return {}


@dataclass
class LevelSign:
    """A sign as seen by `signs[i]` (Level.get_signs() keys are tile x,y)."""
    x: float
    y: float
    text: str


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# -- lists ------------------------------------------------------------------

def _level_players(host, ctx):
    """The players[] list: same order hitplayer/testplayer index
    (GS1Host._test_at), i.e. the script level's audience order."""
    objects = host._players_on_level(ctx)
    level = host._level_of(ctx)
    if not objects and level is not None:
        direct = getattr(level, "players", None)
        if direct is not None:
            objects = list(direct.values()) if isinstance(direct, dict) else list(direct)
    return [p for p in objects if getattr(p, "logged_in", True)]


def _all_players(host):
    """allplayers[]: every logged-in client/RC connection, id order
    (Server.Init.cpp:33-60 playerFilter)."""
    server = host.server
    players = getattr(server, "players", None)
    if not isinstance(players, dict):
        return []
    out = []
    for pid in sorted(players):
        player = players[pid]
        if pid == 0 or not getattr(player, "logged_in", True):
            continue
        ctype = getattr(player, "connection_type", None)
        if ctype is not None:
            from ..protocol.constants import PLTYPE
            if not ctype & PLTYPE.ANYPLAYER:
                continue
        out.append(player)
    return out


def _level_npcs(level):
    if level is None:
        return []
    if hasattr(level, "get_npcs"):
        return list(level.get_npcs())
    direct = getattr(level, "npcs", getattr(level, "_npcs", []))
    return list(direct.values()) if isinstance(direct, dict) else list(direct)


def objects_of(host, array, ctx):
    """The live list behind `array` for the script's level."""
    server = host.server
    if array == "players":
        return _level_players(host, ctx)
    if array == "allplayers":
        return _all_players(host)
    level = host._level_of(ctx)
    if level is None:
        return []
    name = getattr(level, "name", "")
    if array == "npcs":
        return _level_npcs(level)
    if array == "compus":
        bm = getattr(server, "baddy_manager", None)
        if bm is None or not hasattr(bm, "get_baddies_on_level"):
            return []
        return [b for b in bm.get_baddies_on_level(name)
                if not getattr(b, "dead", False)
                and int(_num(getattr(b, "mode", 0))) != 9]  # BaddyMode::DEAD
    if array == "bombs":
        cm = getattr(server, "combat_manager", None)
        return cm.get_bombs_on_level(name) if cm is not None and hasattr(
            cm, "get_bombs_on_level") else []
    if array == "arrows":
        cm = getattr(server, "combat_manager", None)
        return cm.get_arrows_on_level(name) if cm is not None and hasattr(
            cm, "get_arrows_on_level") else []
    if array == "items":
        im = getattr(server, "item_manager", None)
        return im.get_items_on_level(name) if im is not None and hasattr(
            im, "get_items_on_level") else []
    if array == "horses":
        hm = getattr(server, "horse_manager", None)
        return hm.get_horses_on_level(name) if hm is not None and hasattr(
            hm, "get_horses_on_level") else []
    if array == "signs":
        getter = getattr(level, "get_signs", None)
        if getter is None:
            return []
        return [LevelSign(float(x), float(y), text)
                for (x, y), text in getter().items()]
    # explos[]: pygserver keeps no explosion state (they are fire-and-forget
    # broadcasts), so the list is always empty.
    return []


def count_of(host, count_name, ctx):
    array = COUNTS[count_name]
    if array == "compus":
        # compuscount counts every baddy, dead or not (Level::getBaddyCount),
        # while compus[] only lists the living (GS1Variables.cpp:~l.194).
        bm = getattr(host.server, "baddy_manager", None)
        level = host._level_of(ctx)
        if bm is None or level is None or not hasattr(bm, "get_baddies_on_level"):
            return 0.0
        return float(len(bm.get_baddies_on_level(getattr(level, "name", ""))))
    return float(len(objects_of(host, array, ctx)))


def element(host, array, index, ctx):
    """Resolve array[index] to its object, or None for "no object"."""
    if index is None:
        index = 0
    if index < 0:
        if index != -1:
            return None
        if array == "players":
            return ctx.player
        if array == "npcs":
            this = ctx.this_obj
            return getattr(this, "owner", this)
        return None
    objects = objects_of(host, array, ctx)
    if index < len(objects):
        return objects[index]
    return None


# -- fields -----------------------------------------------------------------

def _headset(image):
    image = str(image or "")
    if image.startswith("head"):
        digits = ""
        for char in image[4:]:
            if not char.isdigit():
                break
            digits += char
        if digits:
            return float(digits)
    return -1.0


def _says_number(chat):
    word = str(chat or "").split(" ", 1)[0]
    try:
        return float(word)
    except ValueError:
        return 0.0


def player_weaponscount(player):
    """weaponscount: how many weapons the player holds (Player.cpp:1613-1618,
    b46a9c03)."""
    return float(len(getattr(player, "weapons", None) or ()))


def player_levelorg(player, axis):
    """levelorgx / levelorgy: minus the attached NPC's global tile position
    (axis 0 = x, 1 = y), 0 when the player is attached to nothing
    (Player.cpp:1623-1645, b46a9c03). Global means on its gmap: the NPC's
    level-local tile plus 64 per grid step."""
    kind, npc_id = getattr(player, "attach_npc", (0, 0))
    if kind != 0 or not npc_id:
        return 0.0
    server = getattr(player, "server", None)
    level = getattr(player, "level", None)
    npc = level.get_npc(npc_id) if hasattr(level, "get_npc") else None
    manager = getattr(server, "npc_manager", None)
    if npc is None and manager is not None:
        npc = manager.get_npc(npc_id)
    if npc is None:
        return 0.0
    tile = _num(getattr(npc, "y" if axis else "x", 0))
    world = getattr(server, "world", None)
    npc_level = getattr(npc, "level", None)
    info = (world.get_gmap_for_level(npc_level.name)
            if world is not None and npc_level is not None else None)
    if info is not None:
        tile += 64.0 * info[2 if axis else 1]
    from .. import collision
    return (-collision.tiles_to_pixels(tile)) / 16.0


def _player_get(player, field):
    if field in ("x", "y"):
        return float(absolute_position(player)[field == "y"])
    if field == "weaponscount":
        return player_weaponscount(player)
    if field in ("levelorgx", "levelorgy"):
        return player_levelorg(player, field == "levelorgy")
    if field == "headset":
        return _headset(getattr(player, "head_image", ""))
    if field == "saysnumber":
        return _says_number(getattr(player, "chat", ""))
    if field == "attachid":
        return float(getattr(player, "attach_npc", (0, 0))[1])
    if field == "attachtype":
        return 1.0 if getattr(player, "attach_npc", (0, 0))[1] else 0.0
    if field in _PLAYER_STATELESS:
        return 0.0
    attr = _PLAYER_ATTR.get(field)
    if attr is None:
        return UNSET
    return _num(getattr(player, attr, 0))


def _npc_get(npc, field, sub):
    if field in ("x", "y"):
        return _num(getattr(npc, field, 0))
    if field in ("width", "height"):
        # NPC::getComputedShape (NPC.h:551): the explicit setshape box (pixels
        # stored, reported /16 as NPC.cpp:1832-1835 does); 0 when none was
        # set, except a showcharacter NPC is 3x3.
        shape = getattr(npc, "shape", None)
        if getattr(npc, "image", "") in ("#c#", "#c"):
            return 3.0
        return _num(shape[field == "height"]) / 16.0 if shape else 0.0
    if field == "headset":
        return _headset(getattr(npc, "head_image", ""))
    if field == "sprite":
        flags = getattr(npc, "flags", None)
        return _num(flags.get("sprite", 0)) if isinstance(flags, dict) else 0.0
    if field == "timeout":
        end = getattr(npc, "_timer_end", 0.0)
        return max(0.0, end - time.time()) if end else 0.0
    if field in ("hurtdx", "hurtdy"):
        return npc_hurt(npc)[field == "hurtdy"]
    if field == "save":
        slots = getattr(npc, "gs1_scopes", {}).get("this", {}).get("save")
        if not isinstance(slots, list):
            slots = [0.0] * 10
        index = int(sub[0]) if sub else 0
        return float(slots[index]) if 0 <= index < len(slots) else 0.0
    attr = _NPC_ATTR.get(field)
    if attr is None:
        return UNSET
    return _num(getattr(npc, attr, 0))


def _arrow_vector(arrow):
    d = int(_num(getattr(arrow, "direction", 0)))
    dx = 0.0 if d in (0, 2) else (-1.0 if d == 1 else 1.0)
    dy = 0.0 if d in (1, 3) else (-1.0 if d == 0 else 1.0)
    return dx, dy


def _remaining(created_at, duration):
    return max(0.0, _num(duration) - (time.time() - _num(created_at)))


def _level_object_get(kind, target, field):
    if kind == "bomb":
        if field in ("x", "y", "power"):
            return _num(getattr(target, field, 0))
        if field == "time":
            return _remaining(target.created_at, target.time_left)
    elif kind == "arrow":
        if field in ("x", "y"):
            return _num(getattr(target, field, 0))
        if field in ("dx", "dy"):
            return _arrow_vector(target)[field == "dy"]
        if field == "dir":
            return _num(getattr(target, "direction", 0))
        if field == "type":
            # arrowTypeNormal; pygserver arrows carry no type
            return _num(getattr(target, "type", 1))
    elif kind == "item":
        if field in ("x", "y"):
            return _num(getattr(target, field, 0))
        if field == "type":
            return _num(int(getattr(target, "item_type", 0)))
        if field == "time":
            if _num(getattr(target, "despawn_time", 0)) <= 0:
                return 0.0
            return _remaining(target.created_at, target.despawn_time)
    elif kind == "horse":
        if field in ("x", "y", "bushes"):
            return _num(getattr(target, field, 0))
        if field == "dir":
            return _num(getattr(target, "direction", 0))
        if field in ("bombs", "bombpower"):
            return 0.0
        if field == "type":
            return 1.0 if "boat" in str(getattr(target, "image", "")).lower() else 0.0
    elif kind == "baddy":
        if field in ("x", "y"):
            return _num(getattr(target, field, 0))
        if field in ("dir", "headdir"):
            return _num(getattr(target, "direction", 0))
        if field == "type":
            return _num(int(getattr(target, "baddy_type", 0)))
        if field == "power":
            return _num(getattr(target, "health", 0))
        if field == "mode":
            return _num(int(getattr(target, "mode", 0)))
    elif kind == "sign":
        # GServer-v2 LevelSign.h:53 divides the whole-tile position by 16,
        # which reads as a bug (it is already in tiles); report tiles.
        if field in ("x", "y"):
            return _num(getattr(target, field, 0))
    return UNSET


def read_field(ref, field, sub=()):
    """Reference-table read of `field` on an object ref; UNSET if the
    reference defines no such property."""
    kind, target = ref.kind, ref.target
    if target is None:
        return UNSET
    field = field.lower()
    if kind == "player":
        if field.startswith(("client.", "clientr.")):
            flags = getattr(target, "flags", None)
            if isinstance(flags, dict):
                value = flags.get(field)
                return UNSET if value is None else value
            return UNSET
        return _player_get(target, field)
    if kind == "npc":
        return _npc_get(target, field, sub)
    return _level_object_get(kind, target, field)


def write_field(ref, field, value, sub=()):
    """Reference-table write; True if the property exists (read-only ones
    swallow the write like a setter-less GameVariable). Numeric values are
    clamped/dropped by sanitize() before they reach any state."""
    kind, target, host, ctx = ref.kind, ref.target, ref.host, ref.ctx
    if target is None:
        return False
    field = field.lower()
    if kind == "player":
        if field.startswith(("client.", "clientr.")):
            flags = getattr(target, "flags", None)
            if isinstance(flags, dict):
                flags[field] = value
                return True
            return False
        if field == "headset":
            number = to_num(value)
            if math.isfinite(number) and int(number) != -1:
                host._set_player_attr(
                    target, "head_image",
                    f"head{max(0, min(99, int(number)))}.png", ctx)
            return True
        if field in _PLAYER_READONLY or field in _PLAYER_STATELESS or field in (
                "attachid", "attachtype", "saysnumber"):
            return True
        attr = field if field in ("x", "y") else _PLAYER_ATTR.get(field)
        if attr is None:
            return False
        host._set_player_attr(target, attr, value, ctx)  # sanitises
        return True
    if kind == "npc":
        if field in _NPC_READONLY:
            return True
        if field == "save":
            number = to_num(value)
            slots = target.gs1_scopes["this"].setdefault("save", [0.0] * 10)
            index = int(sub[0]) if sub else 0
            if math.isfinite(number) and 0 <= index < len(slots):
                slots[index] = float(min(220, max(0, int(number))))
            return True
        if field == "timeout":
            number = to_num(value)
            if math.isfinite(number):
                host._set_timer(target, number)
            return True
        if field == "sprite":
            number = to_num(value)
            if hasattr(target, "flags") and math.isfinite(number):
                target.flags["sprite"] = max(0.0, min(55.0, math.floor(number)))
                host._dirty(target)
            return True
        if field in ("hurtdx", "hurtdy"):
            set_npc_hurt(target, **{field[-2:]: value})
            host._dirty(target)
            return True
        attr = field if field in ("x", "y") else _NPC_ATTR.get(field)
        if attr is None:
            return False
        clean = sanitize(host, target, attr, value)
        if clean is not DROP:
            setattr(target, attr, clean)
            if attr in ("x", "y"):
                npc_motion.cancel_moves(target)  # NPC.cpp:1879,1910
            host._dirty(target)
        return True
    # bombs/arrows/items/explos/horses/signs/compus are all read-only
    return _level_object_get(kind, target, field) is not UNSET


# -- Host entry points --------------------------------------------------------

def split_name(name):
    """`players.x` -> ("players", "x"); None when `name` is not an array
    reference. A bare `bombs`/`arrows` (no field, no index) is the NPC's own
    inventory attribute, so callers must also require an index or a field."""
    head, dot, field = name.partition(".")
    if head not in ARRAYS:
        head = head.lower()
        if head not in ARRAYS:
            return None
    return head, (field if dot else None)


def get(host, name, indices, ctx):
    """get_builtin hook: UNSET when `name` is not an array reference."""
    parts = split_name(name)
    if parts is None:
        return UNSET
    array, field = parts
    if field is None and not indices:
        return UNSET  # bare name: legacy NPC attribute / flag
    index = indices[0] if indices else None
    target = element(host, array, index, ctx)
    if target is None:
        return 0.0
    ref = host._object_ref_for(ARRAYS[array], target, ctx)
    if field is None:
        return ref
    value = read_field(ref, field, tuple(indices[1:]))
    return 0.0 if value is UNSET else value


def put(host, name, value, indices, ctx):
    """set_builtin hook: True when `name` was an array reference (handled or
    deliberately swallowed)."""
    parts = split_name(name)
    if parts is None:
        return False
    array, field = parts
    if field is None and not indices:
        return False
    if field is None:
        return True  # assigning to an object handle is meaningless
    target = element(host, array, indices[0] if indices else None, ctx)
    if target is None:
        return True
    ref = host._object_ref_for(ARRAYS[array], target, ctx)
    write_field(ref, field, value, tuple(indices[1:]))
    return True
