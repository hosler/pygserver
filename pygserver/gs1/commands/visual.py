from __future__ import annotations

import logging
import math

from reborn_protocol.gs1.values import to_num, to_str

from ..execution import _schedule
from ...audience import GS1_EXPLOSION_PLAYERS, contains  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from ..host import NPC_CHARPROP, PLAYER_CHARPROP, PLAYER_PROP_WIRE, PLPROP, _NPC_GATTRIB_PROPS, _PLAYER_GATTRIB_PROPS, _charprop_target  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import asyncio  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import time  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.coords import LEVEL_SIZE, level_index  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

def _showimg_index(args):
    return math.floor(to_num(args[0])) if args else -1

def _broadcast_showimgs(host, npc, images, *, reset=False):
    if host.server is None or npc is None or getattr(npc, "level", None) is None:
        return
    from ...protocol.packets import build_npc_showimgs
    packet = build_npc_showimgs(npc.id, images, reset=reset)
    _schedule(host.server.broadcast_to_level(npc.level.name, packet))

def _c_showimg(self, a, npc, player, ctx):
    if npc is None or len(a) < 4:
        return
    index = _showimg_index(a)
    if not 0 <= index <= 199:
        return
    props = {0: to_str(a[1]), 1: int(to_num(a[2]) * 2),
             2: int(to_num(a[3]) * 2)}
    if len(a) >= 5:
        z = int(to_num(a[4]))
        if z != 0:
            props[7] = z
    npc.showimgs[index] = props
    npc._had_showimgs = True
    _broadcast_showimgs(self, npc, {index: props})

def _c_hideimg(self, a, npc, player, ctx):
    if npc is None or not a:
        return
    index = _showimg_index(a)
    if not 0 <= index <= 199:
        return
    end = math.floor(to_num(a[1])) if len(a) > 1 else index
    for layer in range(index, min(end, 199) + 1):
        npc.showimgs.pop(layer, None)
    npc._had_showimgs = True
    _broadcast_showimgs(self, npc, npc.showimgs, reset=True)

def _change_showimg(self, a, npc, prop_id, value):
    if npc is None or not a:
        return
    index = _showimg_index(a)
    if not 0 <= index <= 199 or index not in npc.showimgs:
        return
    npc.showimgs[index][prop_id] = value
    _broadcast_showimgs(self, npc, {index: {prop_id: value}})

def _c_changeimgvis(self, a, npc, player, ctx):
    if len(a) >= 2:
        _change_showimg(self, a, npc, 3, int(to_num(a[1])) & 0xff)

def _c_changeimgpart(self, a, npc, player, ctx):
    if len(a) >= 5:
        value = (int(to_num(a[1])) & 0xffff, int(to_num(a[2])) & 0xffff,
                 int(to_num(a[3])) & 0xff, int(to_num(a[4])) & 0xff)
        _change_showimg(self, a, npc, 4, value)

def _c_changeimgcolors(self, a, npc, player, ctx):
    if len(a) >= 5:
        value = tuple(int(max(0.0, min(1.0, to_num(v))) * 200) for v in a[1:5])
        _change_showimg(self, a, npc, 5, value)

def _c_changeimgzoom(self, a, npc, player, ctx):
    if len(a) >= 2:
        value = int(max(0.0, min(22.0, to_num(a[1]))) * 10)
        _change_showimg(self, a, npc, 6, value)

def _c_changeimgmode(self, a, npc, player, ctx):
    if len(a) >= 2:
        _change_showimg(self, a, npc, 8, int(to_num(a[1])) & 0xff)

_COMMANDS = {
    'showimg': _c_showimg,
    'showimg2': _c_showimg,
    'hideimg': _c_hideimg,
    'hideimgs': _c_hideimg,
    'changeimgvis': _c_changeimgvis,
    'changeimgpart': _c_changeimgpart,
    'changeimgcolors': _c_changeimgcolors,
    'changeimgzoom': _c_changeimgzoom,
    'changeimgmode': _c_changeimgmode,
}
