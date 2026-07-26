from __future__ import annotations

import logging
import time

from reborn_protocol.gs1.values import to_num, to_str

from ..host import (
    NPC_CHARPROP, PLAYER_CHARPROP, PLAYER_PROP_WIRE, PLPROP, _CLASSIC_COLOR_INDEX, _NPC_GATTRIB_PROPS, _PLAYER_GATTRIB_PROPS, _charprop_target,
)
from ..execution import _schedule
from ...audience import GS1_EXPLOSION_PLAYERS, contains  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..host import _CLASSIC_COLORS  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import asyncio  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import math  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.coords import LEVEL_SIZE, level_index  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)


def _c_setimg(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.image = to_str(a[0])
        self._dirty(npc)

def _c_setimgpart(self, a, npc, player, ctx):
    # setimgpart filename,x,y,width,height — show only a sub-rect of the
    # sheet (GS1Commands.cpp:2228 fn_setimgpart sets NPCProp::IMAGE +
    # NPCProp::IMAGEPART). The rect flows to clients via
    # NPC.build_props_packet() -> build_npc_props() NPCPROP.IMAGEPART.
    if npc is None or len(a) < 5:
        return
    npc.image = to_str(a[0])
    npc.imagepart = (int(to_num(a[1])), int(to_num(a[2])),
                      int(to_num(a[3])), int(to_num(a[4])))
    self._dirty(npc)

def _c_setani(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.gani = to_str(a[0])
        self._dirty(npc)

def _c_message(self, a, npc, player, ctx):
    if npc is not None:
        npc.message = to_str(a[0]) if a else ""
        self._dirty(npc)

def _c_hide(self, a, npc, player, ctx):
    if npc is not None:
        npc.visible = False
        self._dirty(npc)

def _c_show(self, a, npc, player, ctx):
    if npc is not None:
        npc.visible = True
        self._dirty(npc)

def _c_move(self, a, npc, player, ctx):
    if npc is not None and len(a) >= 2:
        npc.x = to_num(getattr(npc, "x", 0)) + to_num(a[0])
        npc.y = to_num(getattr(npc, "y", 0)) + to_num(a[1])
        self._dirty(npc)

def _c_setnick(self, a, npc, player, ctx):
    if npc is not None and a:
        npc.nickname = to_str(a[0])
        self._dirty(npc)

def _c_setshape(self, a, npc, player, ctx):
    # setshape type,width,height — type 1 is a fully-solid box; other type
    # values are unimplemented in the GServer-v2 C++ oracle too
    # (GS1Commands.cpp:2384 fn_setshape returns early unless type == 1).
    # width/height arguments are pixels and the stored collision shape uses
    # tiles, matching the client host's conversion. Not a wire prop (no NPCPROP for a
    # shape rect) — this is server-side collision geometry; note for the
    # touch-handling owner: nothing in gs1_host.py currently *reads*
    # npc.shape for collision, so a touch-handler change elsewhere would be
    # needed to make setshape blocking actually take effect.
    if npc is None or len(a) < 3:
        return
    if int(to_num(a[0])) != 1:
        return
    width = max(1, (int(to_num(a[1])) + 15) // 16)
    height = max(1, (int(to_num(a[2])) + 15) // 16)
    npc.shape = (width, height)

def _gattribs_of(obj):
    ga = getattr(obj, "gattribs", None)
    if ga is None:
        ga = {}
        obj.gattribs = ga
    return ga

def _resolve_color(val):
    """Colour value for a #C0-#C7 slot, matching the C++ engine
    (GS1MessageCodes.cpp COLORS write + GS1Visitor::getColorValueFromString):
    a STRING is looked up as a classic-colour name (unknown name -> 0, white),
    a genuine NUMBER is used as a raw palette index. GS1 passes bare tokens as
    strings, so `setplayerprop #C0,red` -> 4 but `setplayerprop #C0,9` -> 0
    ("9" is not a colour name), where treating it as a raw index would give 9."""
    if isinstance(val, str):
        return _CLASSIC_COLOR_INDEX.get(val, 0)
    return int(to_num(val)) & 0xFF

def _apply_charprop(obj, code, val, table):
    """Set the attr / color / gani-attribute an NPC setcharprop code maps to.
    Returns True if the code was recognized and applied."""
    target = _charprop_target(code, table)
    if target is None:
        return False
    if isinstance(target, tuple):
        kind, n = target
        if kind == "color":
            colors = getattr(obj, "colors", None)
            if isinstance(colors, list) and 0 <= n < len(colors):
                colors[n] = _resolve_color(val)
        elif kind == "gattrib" and 1 <= n <= len(_NPC_GATTRIB_PROPS):
            _gattribs_of(obj)[_NPC_GATTRIB_PROPS[n - 1]] = to_str(val)
        return True
    setattr(obj, target, to_str(val))
    return True

def _c_setcharprop(self, a, npc, player, ctx):
    # setcharprop <messagecode>, <value> — set the NPC's appearance/identity
    if npc is None or len(a) < 2:
        return
    if _apply_charprop(npc, to_str(a[0]), a[1], NPC_CHARPROP):
        self._dirty(npc)

def _c_setplayerprop(self, a, npc, player, ctx):
    if player is None or len(a) < 2:
        return
    target = _charprop_target(to_str(a[0]), PLAYER_CHARPROP)
    if target is None:
        return
    if isinstance(target, tuple):
        kind, n = target
        if kind == "color":  # set slot + queue the full COLORS prop
            colors = getattr(player, "colors", None)
            if isinstance(colors, list) and 0 <= n < len(colors):
                colors[n] = _resolve_color(a[1])
                if PLPROP is not None:
                    _queue_player_prop(player, PLPROP.COLORS, list(colors))
        elif kind == "gattrib" and 1 <= n <= len(_PLAYER_GATTRIB_PROPS):
            prop_id = _PLAYER_GATTRIB_PROPS[n - 1]
            _gattribs_of(player)[prop_id] = to_str(a[1])
            _queue_player_prop(player, prop_id, to_str(a[1]))
    elif target in PLAYER_PROP_WIRE:
        self._set_player_attr(player, target, a[1])  # sets + queues wire prop
    else:
        setattr(player, target, to_str(a[1]))  # e.g. sword/shield/horse image
    if hasattr(player, "mark_dirty"):
        player.mark_dirty()

def _c_addweapon(self, a, npc, player, ctx):
    # addweapon <name> — give the acting player a weapon and push it to client
    if player is None or not a:
        return
    name = to_str(a[0])
    if hasattr(player, "add_weapon"):
        player.add_weapon(name)
    # The GS2 manager owns weapons/*.txt whether or not the clientside half
    # compiled: a compiled one is announced by it (image + joined classes +
    # script header, then the client pulls the bytecode), an uncompiled one
    # goes out as a classic GS1-text weapon.
    gs2 = getattr(self.server, "gs2_manager", None)
    weapon = gs2.get_weapon(name) if gs2 is not None else None
    if weapon is None:
        return
    if weapon.bytecode:
        _schedule(gs2.announce_weapon(player, name))
        return
    if not hasattr(player, "send_raw"):
        return
    try:
        from ...protocol.packets import build_npc_weapon_add
        pkt = build_npc_weapon_add(weapon.name, weapon.image, weapon.clientside)
        _schedule(player.send_raw(pkt))
    except Exception:
        logger.debug("addweapon send failed for %s", name, exc_info=True)

def _c_triggeraction(self, a, npc, player, ctx):
    # triggeraction x,y,action,params... — dispatch a serverside trigger.
    # handle_trigger_action reads token[1] as the action, so prefix with "gs1".
    if player is None or self.server is None or len(a) < 3:
        return
    if not hasattr(self.server, "handle_trigger_action"):
        return
    x, y = to_num(a[0]), to_num(a[1])
    parts = ["gs1"] + [to_str(v) for v in a[2:]]
    _schedule(self.server.handle_trigger_action(player, x, y, ",".join(parts)))

def _spawn_npc(self, image, script, x, y, ctx):
    lvl = self._level_of(ctx)
    nm = getattr(self.server, "npc_manager", None) if self.server is not None else None
    if lvl is None or nm is None or not hasattr(nm, "create_npc"):
        return None
    npc = nm.create_npc(level=lvl, x=to_num(x), y=to_num(y))
    if image:
        npc.image = to_str(image)
    if script:
        nm.attach_gs1(npc, to_str(script))
    self._dirty(npc)
    if hasattr(self.server, "broadcast_to_level"):
        _schedule(self.server.broadcast_to_level(lvl.name, npc.build_props_packet()))
    return npc

def _c_putnpc(self, a, npc, player, ctx):
    # putnpc image,script,x,y — create a level NPC
    if len(a) < 4:
        return
    _spawn_npc(self, a[0], a[1], a[2], a[3], ctx)

def _c_putnpc2(self, a, npc, player, ctx):
    # putnpc2 x,y,script — create a level NPC running the inline script
    if len(a) < 3:
        return
    _spawn_npc(self, "", a[2], a[0], a[1], ctx)

def _c_sethead(self, a, npc, player, ctx):
    if player is not None and a:
        self._set_player_attr(player, "head_image", a[0])  # wired HEADIMAGE

def _c_setbody(self, a, npc, player, ctx):
    if player is not None and a:
        self._set_player_attr(player, "body_image", a[0])  # wired BODYIMAGE

def _c_setsword(self, a, npc, player, ctx):
    # setsword image,power — acting player's sword image + power
    if player is None or not a:
        return
    img = to_str(a[0])
    power = int(to_num(a[1])) if len(a) > 1 else int(to_num(getattr(player, "sword_power", 0)))
    player.sword_image = img
    player.sword_power = power
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.SWORDPOWER, (power, img))

def _c_setshield(self, a, npc, player, ctx):
    # setshield image,power — acting player's shield image + power
    if player is None or not a:
        return
    img = to_str(a[0])
    power = int(to_num(a[1])) if len(a) > 1 else int(to_num(getattr(player, "shield_power", 0)))
    player.shield_image = img
    player.shield_power = power
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.SHIELDPOWER, (power, img))

def _c_setgender(self, a, npc, player, ctx):
    if player is not None and a:
        try:
            player.gender = int(to_num(a[0]))
        except Exception:
            pass

def _c_showcharacter(self, a, npc, player, ctx):
    if npc is None:
        return
    npc.image = "#c#"
    npc.shape = (0, 0)
    self._dirty(npc)

def _c_destroy(self, a, npc, player, ctx):
    # destroy — the NPC deletes itself from the level
    if npc is None or self.server is None:
        return
    nm = getattr(self.server, "npc_manager", None)
    if nm is not None and hasattr(nm, "destroy_npc"):
        _schedule(nm.destroy_npc(npc))

def _set_player_color(player, slot, value):
    """Set one of the acting player's 5 color slots and queue the COLORS prop."""
    colors = getattr(player, "colors", None)
    if not isinstance(colors, list) or not (0 <= slot < len(colors)):
        return
    colors[slot] = int(to_num(value)) & 0xFF
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.COLORS, list(colors))
    if hasattr(player, "mark_dirty"):
        player.mark_dirty()

def _make_color_cmd(slot):
    def handler(self, a, npc, player, ctx):
        if player is not None and a:
            _set_player_color(player, slot, a[0])
    return handler

_c_setskincolor = _make_color_cmd(0)
_c_setcoatcolor = _make_color_cmd(1)
_c_setsleevecolor = _make_color_cmd(2)
_c_setshoecolor = _make_color_cmd(3)
_c_setbeltcolor = _make_color_cmd(4)


def _c_puthorse(self, a, npc, player, ctx):
    # puthorse imagefile,x,y — drop a horse on the level (bushes=2, dir=0)
    if self.server is None or len(a) < 3:
        return
    hm = getattr(self.server, "horse_manager", None)
    lvl = self._level_of(ctx)
    if hm is None or lvl is None or not hasattr(hm, "add_horse"):
        return
    _schedule(hm.add_horse(lvl, to_num(a[1]), to_num(a[2]),
                           direction=0, bushes=2, image=to_str(a[0])))

def _c_takehorse(self, a, npc, player, ctx):
    # takehorse index — mount the level horse at <index> onto this NPC
    if npc is None or self.server is None or not a:
        return
    hm = getattr(self.server, "horse_manager", None)
    lvl = self._level_of(ctx)
    if hm is None or lvl is None or not hasattr(hm, "get_horses_on_level"):
        return
    horses = hm.get_horses_on_level(lvl.name)
    idx = int(to_num(a[0]))
    if 0 <= idx < len(horses):
        horse = horses[idx]
        npc.horse_image = getattr(horse, "image", "")
        self._dirty(npc)
        _schedule(hm.remove_horse(lvl.name, horse.id))

def _c_setplayerdir(self, a, npc, player, ctx):
    if player is None or not a:
        return
    d = int(to_num(a[0])) & 3
    player.direction = d
    if PLPROP is not None:
        _queue_player_prop(player, PLPROP.DIRECTION, d)

def _c_enableweapons(self, a, npc, player, ctx):
    if player is not None:
        try:
            player.weapons_disabled = False
        except Exception:
            pass

def _c_disableweapons(self, a, npc, player, ctx):
    if player is not None:
        try:
            player.weapons_disabled = True
        except Exception:
            pass

def _c_setchargender(self, a, npc, player, ctx):
    if npc is not None and a:
        try:
            npc.gender = int(to_num(a[0]))
        except Exception:
            pass

def _c_carryobject(self, a, npc, player, ctx):
    if npc is not None:
        npc.gani = "carrystill"
        self._dirty(npc)

def _c_throwcarry(self, a, npc, player, ctx):
    if npc is not None and to_str(getattr(npc, "gani", "")).startswith("carry"):
        npc.gani = "idle"
        self._dirty(npc)

def _make_blockflag_cmd(bit, on):
    def handler(self, a, npc, player, ctx):
        if npc is not None:
            bf = int(getattr(npc, "block_flags", 0) or 0)
            npc.block_flags = (bf | bit) if on else (bf & ~bit)
    return handler

# NPCBlockFlags: CANBECARRIED=2, CANBEPULLED=4, CANBEPUSHED=8
_c_canbecarried = _make_blockflag_cmd(0x02, True)
_c_cannotbecarried = _make_blockflag_cmd(0x02, False)
_c_canbepulled = _make_blockflag_cmd(0x04, True)
_c_cannotbepulled = _make_blockflag_cmd(0x04, False)
_c_canbepushed = _make_blockflag_cmd(0x08, True)
_c_cannotbepushed = _make_blockflag_cmd(0x08, False)


def _c_takeplayercarry(self, a, npc, player, ctx):
    # force the player to drop a carried object (PLO_THROWCARRIED)
    if player is None or self.server is None or not hasattr(self.server, "broadcast_to_level"):
        return
    lvl = getattr(player, "level", None)
    if lvl is None:
        return
    try:
        from ...protocol.packets import PacketBuilder
        from ...protocol.constants import PLO
        pkt = (PacketBuilder().write_gchar(PLO.THROWCARRIED)
               .write_gshort(getattr(player, "id", 0)).write_newline().build())
        _schedule(self.server.broadcast_to_level(lvl.name, pkt))
    except Exception:
        logger.debug("takeplayercarry failed", exc_info=True)

def _c_sendtorc(self, a, npc, player, ctx):
    message = to_str(a[0]) if a else ""
    rc_manager = getattr(self.server, "rc_manager", None) if self.server else None
    if rc_manager is not None and hasattr(rc_manager, "process_chat"):
        _schedule(rc_manager.process_chat(message))

def _queue_player_prop(player, prop_id, value):
    dirty = getattr(player, "_gs1_dirty_props", None)
    if dirty is None:
        dirty = {}
        player._gs1_dirty_props = dirty
    dirty[prop_id] = value

def _c_freezeplayer(self, a, npc, player, ctx):
    # freezeplayer/freezeplayer2 - GServer-v2 PlayerClient::freezePlayer()
    # sends a bare PLO_FREEZEPLAYER2 packet (PlayerClient.cpp:1700-1703).
    if player is None:
        return
    try:
        player.is_frozen = True
        player._gs1_freeze_until = (
            time.monotonic() + max(0.0, to_num(a[0])) if a else None
        )
    except Exception:
        pass
    if hasattr(player, "send_raw"):
        from ...protocol.packets import build_freeze_player
        _schedule(player.send_raw(build_freeze_player()))

def _c_unfreezeplayer(self, a, npc, player, ctx):
    # unfreezeplayer/unfreezeplayer2 - GServer-v2 PlayerClient::unfreezePlayer()
    # sends a bare PLO_UNFREEZEPLAYER packet (PlayerClient.cpp:1705-1708).
    if player is None:
        return
    try:
        player.is_frozen = False
        player._gs1_freeze_until = None
    except Exception:
        pass
    if hasattr(player, "send_raw"):
        from ...protocol.packets import build_unfreeze_player
        _schedule(player.send_raw(build_unfreeze_player()))

def _c_say2(self, a, npc, player, ctx):
    # say2 <raw text> - GServer-v2 PlayerClient::sendSignMessage() sends
    # PLO_SAY2 with the translated text (PlayerClient.cpp:1717-1721). This is
    # the RPG-style textbox/sign message sent directly to the triggering
    # player, distinct from `message`/`say` which just set the NPC's chat
    # bubble (NPCPROP #c) for everyone on the level to see.
    if player is None or not hasattr(player, "send_raw"):
        return
    text = to_str(a[0]) if a else ""
    from ...protocol.packets import build_say2
    _schedule(player.send_raw(build_say2(text)))

def _c_setlevel2(self, a, npc, player, ctx):
    # warp the acting player to level,x,y (doors/teleports)
    if player is None or not a or not hasattr(player, "warp"):
        return
    lvl = to_str(a[0])
    x = to_num(a[1]) if len(a) > 1 else getattr(player, "x", 30)
    y = to_num(a[2]) if len(a) > 2 else getattr(player, "y", 30)
    _schedule(player.warp(lvl, x, y))

def _c_setlevel(self, a, npc, player, ctx):
    if player is None or not a or not hasattr(player, "warp"):
        return
    _schedule(player.warp(to_str(a[0]), getattr(player, "x", 30),
                          getattr(player, "y", 30)))

def _c_noop(self, a, npc, player, ctx):
    pass

_COMMANDS = {
    'setimg': _c_setimg,
    'setgif': _c_setimg,
    'seticon': _c_noop,
    'setimgpart': _c_setimgpart,
    'setani': _c_setani,
    'setcharani': _c_setani,
    'message': _c_message,
    'say2': _c_say2,
    'say': _c_message,
    'hide': _c_hide,
    'show': _c_show,
    'hidelocal': _c_hide,
    'showlocal': _c_show,
    'move': _c_move,
    'setlevel2': _c_setlevel2,
    'setlevel': _c_setlevel,
    'setcharprop': _c_setcharprop,
    'setplayerprop': _c_setplayerprop,
    'addweapon': _c_addweapon,
    'triggeraction': _c_triggeraction,
    'putnpc': _c_putnpc,
    'putnpc2': _c_putnpc2,
    'puthorse': _c_puthorse,
    'takehorse': _c_takehorse,
    'destroy': _c_destroy,
    'sethead': _c_sethead,
    'setbody': _c_setbody,
    'setsword': _c_setsword,
    'setshield': _c_setshield,
    'setgender': _c_setgender,
    'showcharacter': _c_showcharacter,
    'setskincolor': _c_setskincolor,
    'setcoatcolor': _c_setcoatcolor,
    'setsleevecolor': _c_setsleevecolor,
    'setshoecolor': _c_setshoecolor,
    'setbeltcolor': _c_setbeltcolor,
    'freezeplayer': _c_freezeplayer,
    'freezeplayer2': _c_freezeplayer,
    'unfreezeplayer': _c_unfreezeplayer,
    'setplayerdir': _c_setplayerdir,
    'setchargender': _c_setchargender,
    'enableweapons': _c_enableweapons,
    'disableweapons': _c_disableweapons,
    'carryobject': _c_carryobject,
    'throwcarry': _c_throwcarry,
    'takeplayercarry': _c_takeplayercarry,
    'canbecarried': _c_canbecarried,
    'cannotbecarried': _c_cannotbecarried,
    'canbepulled': _c_canbepulled,
    'cannotbepulled': _c_cannotbepulled,
    'canbepushed': _c_canbepushed,
    'cannotbepushed': _c_cannotbepushed,
    'sendtorc': _c_sendtorc,
    'setshape': _c_setshape,
}
