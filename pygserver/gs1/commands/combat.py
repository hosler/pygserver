from __future__ import annotations

import logging
import math

from reborn_protocol.gs1.values import to_num

from ...attachments import absolute_position
from ..execution import _schedule
from .. import objects as gs1_objects
from ..host import NPC_CHARPROP, PLAYER_CHARPROP, PLAYER_PROP_WIRE, PLPROP, _NPC_GATTRIB_PROPS, _PLAYER_GATTRIB_PROPS, _charprop_target  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import asyncio  # noqa: F401  - kept: original import block (star-import consumers rely on it)
import time  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.coords import LEVEL_SIZE, level_index  # noqa: F401  - kept: original import block (star-import consumers rely on it)
from reborn_protocol.gs1.values import to_str  # noqa: F401  - kept: original import block (star-import consumers rely on it)

logger = logging.getLogger(__name__)

def _c_putbomb(self, a, npc, player, ctx):
    # putbomb power,x,y
    if len(a) < 3 or self.server is None:
        return
    lvl = self._level_of(ctx)
    if lvl is None:
        return
    try:
        from ...combat import wire_tile_ok
        from ...protocol.packets import build_bomb_add
        x, y, power = to_num(a[1]), to_num(a[2]), to_num(a[0])
        # One GCHAR carries each field; a bomb the wire cannot place is not
        # announced (the server keeps no state for script bombs).
        if not wire_tile_ok(x, y) or not math.isfinite(power):
            return
        # GServer-v2 Level.cpp:2085: script bombs have owner 0, no player flag.
        # Its default three-second fuse is sent as 60 ticks.
        _schedule(self.server.broadcast_to_level(lvl.name, build_bomb_add(
            0, x, y, int(power) & 7, 60)))
    except Exception:
        logger.debug("putbomb failed", exc_info=True)

def _explode(self, ctx, radius, power, x, y):
    lvl = self._level_of(ctx)
    if lvl is None or self.server is None:
        return
    from ...combat import MAX_WIRE_RADIUS, wire_tile_ok
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (radius, power)):
        return
    radius = max(0, min(int(radius), MAX_WIRE_RADIUS))
    power = int(power)
    cm = getattr(self.server, "combat_manager", None)
    # Decide once whether this blast happens at all: a flood or a runaway
    # chain of script blasts loses its wire packet and its server effect
    # together (CombatManager.reserve_script_blast).
    if hasattr(cm, "reserve_script_blast") and not cm.reserve_script_blast(lvl.name):
        return
    # One GCHAR carries each wire field: a position it cannot hold (off-range
    # or NaN/infinite) is not announced, but the server-side effect still runs.
    if wire_tile_ok(x, y):
        try:
            from ...protocol.packets import build_explosion
            _schedule(self.server.broadcast_to_level(
                lvl.name, build_explosion(0, x, y, radius, power & 7)))
        except Exception:
            logger.debug("explosion broadcast failed", exc_info=True)
    if cm is not None:
        # GServer-v2 Level.cpp:2211: script explosions inform clients and
        # affect NPCs; TExplosion.cpp:48-96 leaves player damage to clients.
        _schedule(cm.blast_level_objects(
            lvl, x, y, radius, power & 7, script=True, admitted=True))

def _c_putexplosion(self, a, npc, player, ctx):
    # putexplosion radius,x,y  (power=1)
    if len(a) < 3:
        return
    _explode(self, ctx, to_num(a[0]), 1, to_num(a[1]), to_num(a[2]))

def _c_putexplosion2(self, a, npc, player, ctx):
    # putexplosion2 power,radius,x,y
    if len(a) < 4:
        return
    _explode(self, ctx, to_num(a[1]), to_num(a[0]), to_num(a[2]), to_num(a[3]))

def _c_shootarrow(self, a, npc, player, ctx):
    # shootarrow dir: same flight and wire as the shootfire* commands.
    from .objects import _c_shootarrow as shoot
    shoot(self, a, npc, player, ctx)

def _c_hitplayer(self, a, npc, player, ctx):
    # hitplayer index,halfhearts,fromx,fromy — damage the level player at index
    if len(a) < 2 or self.server is None:
        return
    cm = getattr(self.server, "combat_manager", None)
    if cm is None or not hasattr(cm, "apply_damage"):
        return
    players = self._players_on_level(ctx)
    idx = int(to_num(a[0]))
    if 0 <= idx < len(players):
        try:
            from ...combat import DamageType
            dtype = DamageType.OTHER
        except Exception:
            dtype = None
        target = players[idx]
        hurt_dx, hurt_dy = _player_hurt_push(target, to_num(a[2]), to_num(a[3]))
        _schedule(cm.apply_damage(target, math.floor(to_num(a[1])),
                                  hurt_dx, hurt_dy, dtype))

def _normalized_push(target, from_x, from_y, distance=1.0):
    """C++ GS1 hit direction: normalize target tile minus source tile."""
    dx = to_num(getattr(target, "x", 0)) - to_num(from_x)
    dy = to_num(getattr(target, "y", 0)) - to_num(from_y)
    length = math.hypot(dx, dy)
    if length:
        dx /= length
        dy /= length
    return dx * distance, dy * distance

def _player_hurt_push(target, from_x, from_y):
    # Server::hitPlayer pushes four tiles, converts to pixels (*16), then
    # recentres both wire components at 64. The direction is measured from the
    # player's event-box centre, top-left + (1.5, 2.0) tiles (12ad27cf,
    # Server.cpp:1960-1963), not from the sprite origin.
    x, y = absolute_position(target)
    dx, dy = x + 1.5 - to_num(from_x), y + 2.0 - to_num(from_y)
    length = math.hypot(dx, dy)
    if length:
        dx, dy = dx / length * 4.0, dy / length * 4.0
    return int(dx * 16) + 64, int(dy * 16) + 64

def _c_hitobjects(self, a, npc, player, ctx):
    # hitobjects power,x,y — GS1Commands.cpp fn_hitobjects calls
    # Server::hitObjectsAtPoint(pos, power, level, npc) which, for an
    # NPC-sourced call, ONLY broadcasts a PLO_HITOBJECTS notification to
    # nearby clients (Server.cpp:2253-2257 in the GServer-v2 checkout) — it
    # does NOT itself look up or damage any NPC/baddy/player server-side.
    # The real server-side hit detection + washit firing happens in the
    # CLIENT-REPORTED PLI_HITOBJECTS packet handler (msgPLI_HITOBJECTS,
    # PlayerClientPackets.cpp:1017), i.e. combat.handle_hit_objects, which is
    # what actually applies a player's own sword swing to nearby NPCs (see
    # that function's docstring). A serverside NPC script calling
    # `hitobjects` itself (e.g. from a timeout/AI loop) therefore only ever
    # produces a client-side visual/audio hit effect here, matching upstream.
    if npc is None or self.server is None or len(a) < 3:
        return
    lvl = self._level_of(ctx)
    if lvl is None:
        return
    try:
        from ...protocol.packets import build_hit_objects
        power = int(to_num(a[0]) * 2)
        pkt = build_hit_objects(0, power, to_num(a[1]), to_num(a[2]), npc_id=npc.id)
        # Server::hitObjectsAtPoint: sendPacketToNearby, nobody excluded
        # (Server.cpp:1952-1956).
        cast = (getattr(self.server, "broadcast_nearby", None)
                or self.server.broadcast_to_level)
        _schedule(cast(lvl.name, pkt))
    except Exception:
        logger.debug("hitobjects failed", exc_info=True)

def _c_hitnpc(self, a, npc, player, ctx):
    # hitnpc index,halfhearts,fromx,fromy — GS1Commands.cpp fn_hitnpc: hits
    # the NPC at position <index> in the level's NPC list, decrementing its
    # health and firing washit. HURTDXDY stores the normalized target-from
    # source direction at midpoint 32.
    if npc is None or self.server is None or len(a) < 4:
        return
    lvl = self._level_of(ctx)
    if lvl is None or not hasattr(lvl, "get_npcs"):
        return
    npcs = lvl.get_npcs()
    idx = int(to_num(a[0]))
    if not (0 <= idx < len(npcs)):
        return
    target = npcs[idx]
    halfhearts = math.floor(to_num(a[1]))
    dx, dy = _normalized_push(target, a[2], a[3])
    # stored as the NPC's signed half-pixel HURTDXDY push
    gs1_objects.set_npc_hurt(target, dx, dy)
    target.hearts = max(0.0, to_num(getattr(target, "hearts", 0)) - halfhearts / 2.0)
    self._dirty(target)
    nm = getattr(self.server, "npc_manager", None)
    if nm is not None and hasattr(nm, "on_npc_washit"):
        _schedule(nm.on_npc_washit(target, player))

def _c_hitcompu(self, a, npc, player, ctx):
    # hitcompu index,power,fromx,fromy — GS1Commands.cpp fn_hitcompu.
    # Upstream is a client-trust artifact: it sends a bare PLO_BADDYHURT
    # packet to the level's leader player ONLY and never touches server-side
    # baddy health at all (relying on that one client to self-report the
    # damage back, same as a real sword swing would). pygserver treats baddy
    # health as server-authoritative everywhere else (explosion/arrow/sword
    # all go through BaddyManager.handle_baddy_hurt), so this deliberately
    # applies REAL damage via that same path instead of replicating the
    # leader-only notify quirk — a real hit is strictly more useful than a
    # packet only one player's client happens to see.
    if self.server is None or len(a) < 4:
        return
    lvl = self._level_of(ctx)
    bm = getattr(self.server, "baddy_manager", None)
    if lvl is None or bm is None or not hasattr(bm, "get_baddies_on_level"):
        return
    baddies = bm.get_baddies_on_level(lvl.name)
    idx = int(to_num(a[0]))
    if not (0 <= idx < len(baddies)):
        return
    leader = self._leader_player(ctx)
    if leader is None:
        return
    _schedule(bm.handle_baddy_hurt(
        leader, baddies[idx].id, math.floor(to_num(a[1])),
        to_num(a[2]), to_num(a[3])))

def _c_hurt(self, a, npc, player, ctx):
    # hurt <halfhearts> — C++ fn_hurt (GS1Commands.cpp:1423) floors the
    # argument to an int and hits the acting player for that many
    # HALF-hearts (hitPlayer power), so `hurt 1` removes 0.5 hearts.
    #
    # Clamp at 0 and hand off to the death path, matching combat.py's
    # apply_damage (combat.py:522/548-549) — a GS1 hurt must not be able to
    # drive hearts negative or push a garbage negative CURPOWER prop.
    if player is None or not a:
        return
    halfhearts = math.floor(to_num(a[0]))
    new_hearts = max(0.0, to_num(getattr(player, "hearts", 0)) - halfhearts / 2.0)
    self._set_player_attr(player, "hearts", new_hearts)
    if new_hearts <= 0:
        cm = getattr(self.server, "combat_manager", None) if self.server is not None else None
        if cm is not None and hasattr(cm, "handle_player_death"):
            try:
                from ...combat import DamageType
                dtype = DamageType.OTHER
            except Exception:
                dtype = None
            _schedule(cm.handle_player_death(player, None, dtype))

_COMMANDS = {
    'hurt': _c_hurt,
    'putbomb': _c_putbomb,
    'putexplosion': _c_putexplosion,
    'putexplosion2': _c_putexplosion2,
    'shootarrow': _c_shootarrow,
    'hitplayer': _c_hitplayer,
    'hitobjects': _c_hitobjects,
    'hitnpc': _c_hitnpc,
    'hitcompu': _c_hitcompu,
}
