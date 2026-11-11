"""
pygserver.npc - NPC system with Python scripting

Handles NPC state, events, and Python-based scripting.
"""

import asyncio
import importlib.util
import logging
import math
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Callable, Tuple
from pathlib import Path

from . import collision, npc_motion
from .attachments import absolute_position

from .protocol.packets import (
    PacketBuilder, build_npc_del, build_npc_moved, build_npc_props)
from .protocol.constants import PLO, NPCPROP
from .text_safety import strip_control_chars

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)

# reborn_protocol.props.encode_value(NPC_PROPS[1]) uses two G-encoded
# length lanes, each at most 223: (223 << 7) + 223, not a 16-bit integer.
_MAX_CLIENT_SCRIPT_BYTES = 0x705F

# Marks a decoded client prop value that cannot be applied.
_REJECT = object()

# PropertyHurtDxDy<> (PropertySerializers.h:600-657): MidPoint = 32. The push is
# kept as signed half pixels in [-32, 32]; serialize() writes
# (char)(v + MidPoint) through operator>>(char) = writeGChar (CString.h:317,
# CString.cpp:1519-1524), which adds 32 again, so each wire byte is v + 64
# (32..96, always printable); deserialize() reads GChar then subtracts MidPoint.
HURT_MIDPOINT = 32
_HURT_WIRE_BIAS = 2 * HURT_MIDPOINT


def clamp_hurt_half_pixels(value) -> int:
    """Clamp one push axis to the reference's [-MidPoint, MidPoint]."""
    return max(-HURT_MIDPOINT, min(HURT_MIDPOINT, int(value)))


def hurt_dxdy_wire(push) -> bytes:
    """The two NPCPROP_HURTDXDY wire bytes for a signed (dx, dy) push."""
    return bytes(clamp_hurt_half_pixels(v) + _HURT_WIRE_BIAS for v in push)


def hurt_dxdy_from_wire(raw) -> Optional[Tuple[int, int]]:
    """Decode two wire bytes back to a clamped signed push (None if short)."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 2:
        return None
    return (clamp_hurt_half_pixels(raw[0] - _HURT_WIRE_BIAS),
            clamp_hurt_half_pixels(raw[1] - _HURT_WIRE_BIAS))


def split_gs1_script(code: str, npcserver: bool) -> tuple[str, str]:
    """Split GS1 source using GServer-v2 Script.cpp:415-483 script modes.

    The marker's entire line is discarded with server scripting enabled.
    Keep internal newlines here; only the packet builder mangles them.
    StringUtils.h:265-301 also trims the mangled newline character (0xa7).
    """
    whitespace = " \t\r\n\v\f\xa7"
    if not npcserver:
        return "", code.strip(whitespace)
    marker = code.find("//#CLIENTSIDE")
    if marker < 0:
        return code.strip(whitespace), ""
    line_end = code.find("\n", marker)
    client = code[line_end + 1:] if line_end >= 0 else ""
    return code[:marker].strip(whitespace), client.strip(whitespace)


class NPC:
    """
    Represents a non-player character.

    NPCs have properties, can execute Python scripts, and respond to events.
    """

    def __init__(self, npc_id: int, name: str = ""):
        self.id = npc_id
        self.name = name

        # Location
        self.level: Optional['Level'] = None
        self.x = 0.0
        self.y = 0.0
        self.direction = 2  # Down

        # Appearance
        self.image = ""
        self.gani = ""
        self.head_image = ""
        self.body_image = ""
        self.sword_image = ""
        self.shield_image = ""
        # NPCPROP_SWORDIMAGE/SHIELDIMAGE carry a power AND the image name
        # (GServer-v2 NPC.h:591-592 maps both to PropertySwordPower/
        # PropertyShieldPower). Default 1 = the weakest visible weapon, so an
        # NPC given only an image still shows one; power 0 makes the reference
        # client treat the NPC as unarmed.
        self.sword_power = 1
        self.shield_power = 1
        self.horse_image = ""
        self.colors = [0, 0, 0, 0, 0]

        # Properties
        self.message = ""
        self.nickname = ""
        self.hearts = 3.0
        self.rupees = 0
        self.arrows = 0
        self.bombs = 0
        self.glove_power = 0  # NPC scale: glove1=1, glove2=2.

        # Visibility
        self.visible = True
        self.block_flags = 0
        self.vis_flags = 0
        # `noplayeronwall;` - onwall() ignores players for this NPC.
        self.no_player_on_wall = False

        # NPCPROP_HURTDXDY: signed push (dx, dy) in half pixels, each clamped to
        # [-32, 32] (hurtPushDeltaInHalfPixels, NPC.h:622); None until something
        # sets it (hitnpc, NPC.cpp:1277). The wire form is hurt_dxdy_wire().
        self.hurt_dxdy: Optional[Tuple[int, int]] = None

        # Flags (custom state)
        self.flags: Dict[str, str] = {}

        # Gani animation attributes (NPCPROP.GATTRIB1..30), set via GS1
        # `setcharprop #P1..#P30`. Keyed by wire prop id -> string value.
        self.gattribs: Dict[int, str] = {}

        # Script (Python class-based)
        self.script_class: Optional[type] = None
        self.script_instance: Optional[Any] = None

        # GS1 script (legacy Reborn scripting): parsed program + persistent
        # NPC-scoped variable dicts. this./thiso. and local. survive across
        # events (local. is created on first use by gs1.execution, per NPC and
        # per joined class; upstream 07932208); temp. is fresh every run.
        self.gs1_program: Optional[Any] = None
        self.gs1_source = ""
        self.client_script: str = ""
        self.gs1_scopes: Dict[str, dict] = {"this": {}, "thiso": {}}

        # Server-owned GS1 showimg layers. Only indices 0..199 belong here;
        # 200+ are local to a client-side script and are never broadcast by
        # the server. Values contain the image's currently-set wire props.
        self.showimgs: Dict[int, dict] = {}
        self._had_showimgs = False

        # Resumable-sleep state (gs1_host.run_npc_event): `_gs1_ctx` is this
        # NPC's single persistent reborn_protocol Context, reused across
        # every event so a bare `timeout = x;` can cancel a sleep left
        # pending by an earlier execution (Context.sleep_cancelled).
        # `_gs1_pending` is the still-suspended _PendingGS1Sleep (if any) -
        # only resumed by this NPC's own next `timeout` event, matching
        # GServer-v2's m_sleepCallStack (see gs1_host.py for the full
        # design/citations).
        self._gs1_ctx: Optional[Any] = None
        self._gs1_pending: Optional[Any] = None

        # Movement (smooth, per-tick): target tile the NPC is walking toward,
        # advanced by NPCManager.tick() using real elapsed time.
        self._move_target: Optional[tuple] = None
        self._move_speed = 0.0

        # Move queue (npc_motion.py): timed moves announced to clients with
        # PLO_MOVE(2). `_move_events` counts finished informwhendone moves not
        # yet fired as `movementfinished`; `_position_check` asks the manager
        # to clamp / roll the position after the queue or a prop moved it.
        self.move_queue = npc_motion.new_queue()
        self._move_events = 0
        self._position_check = False
        # Players who knew this NPC on the segment it just left, player -> that
        # level's name. They keep hearing about it while it stays on a
        # neighbouring segment of the gmap (NPCManager._npc_recipients). The
        # player object is kept with the id and compared by identity: ids are
        # reused by later logins, which must not inherit a predecessor's hold
        # (NPCManager.forget_player purges on disconnect).
        self._seam_players: Dict[int, Tuple['Player', str]] = {}
        self._last_relocate = 0.0
        # Wall probes left for script-driven move flushes this tick
        # (npc_motion.WALL_BUDGET); the tick loop refills it.
        self._wall_budget = npc_motion.WALL_BUDGET

        # Timer
        self._timer_end: float = 0.0

        # Dirty flag: set when a visible property changes so the manager
        # re-broadcasts this NPC's props to players on the next tick.
        self._dirty = False
        self._script_dirty = False

        # API wrapper for scripts
        self._api: Optional['NPCApi'] = None

    def mark_dirty(self):
        """Flag this NPC for a props re-broadcast on the next server tick."""
        self._dirty = True

    async def hurtAndPush(self, damage: int, direction_vec, event,
                          source: Optional['Player'], carryobject_type=None):
        """Apply server damage and dispatch the matching GS1 hit event."""
        # Callers may supply the client-facing sprite or the already-mapped
        # script-facing type.  Preserve typed CarryObjectType values; map
        # CarryObjectSprite and plain wire integers here at the API boundary.
        from .combat import CarryObjectSprite, CarryObjectType, _SPRITE_TO_TYPE
        if isinstance(carryobject_type, CarryObjectSprite):
            carryobject_type = _SPRITE_TO_TYPE.get(carryobject_type)
        elif not isinstance(carryobject_type, CarryObjectType):
            try:
                carryobject_type = _SPRITE_TO_TYPE.get(
                    CarryObjectSprite(int(carryobject_type))
                )
            except (TypeError, ValueError):
                carryobject_type = None
        self.hearts = max(0.0, self.hearts - damage / 2.0)
        self.mark_dirty()
        manager = getattr(getattr(source, 'server', None), 'npc_manager', None)
        event_name = getattr(event, 'value', event)
        if manager is not None and event_name == 'waspelt':
            await manager.on_npc_waspelt(self, carryobject_type, source)

    def set_script(self, script_class: type):
        """Set the script class for this NPC."""
        self.script_class = script_class
        try:
            self.script_instance = script_class()
            logger.debug(f"NPC {self.id} ({self.name}) script loaded: {script_class.__name__}")
        except Exception as e:
            logger.error(f"Error instantiating NPC script: {e}")

    def get_api(self, manager: 'NPCManager') -> 'NPCApi':
        """Get API wrapper for script calls."""
        if not self._api:
            self._api = NPCApi(self, manager)
        return self._api

    @staticmethod
    def _client_prop_value(descriptor, value):
        """Normalise a decoded client value, or return _REJECT when unusable.

        The decoder yields None for an empty string and for any truncated
        payload, and shapes the handlers below never see in well-formed
        traffic. Everything is checked here, before apply_client_prop changes
        any state, so a malformed prop is dropped whole instead of raising
        halfway through an update.
        """
        from reborn_protocol.props import Wire
        wire = descriptor.wire
        if wire in (Wire.STRING, Wire.LONGSTRING):
            return strip_control_chars(value) if isinstance(value, str) else ''
        if wire is Wire.HEADGIF:
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            return strip_control_chars(value) if isinstance(value, str) else _REJECT
        if wire is Wire.GBYTE:
            return value if isinstance(value, int) else _REJECT
        if wire in (Wire.TILE, Wire.HALFTILE, Wire.TILEZ, Wire.PIXEL):
            return (value if isinstance(value, (int, float))
                    and not isinstance(value, bool) else _REJECT)
        if wire is Wire.SWORDPOWER:
            if (not isinstance(value, tuple) or len(value) != 2
                    or not isinstance(value[0], int)):
                return _REJECT
            image = value[1]
            return (value[0],
                    strip_control_chars(image) if isinstance(image, str) else None)
        if wire is Wire.COLORS:
            return (list(value) if isinstance(value, list) and value
                    and all(isinstance(item, int) for item in value) else _REJECT)
        if wire is Wire.IMAGEPART:
            return (value if isinstance(value, tuple) and len(value) == 4
                    and all(isinstance(item, int) for item in value) else _REJECT)
        if wire is Wire.RAW:
            return value if isinstance(value, (bytes, bytearray)) else _REJECT
        return _REJECT

    def apply_client_prop(self, prop_id, value):
        """Apply classic-mode updates before relaying their normalized values."""
        # NPC.cpp:982-1608: scripts are server-only; position has paired props.
        from reborn_protocol.props import NPC_PROPS
        if prop_id == NPCPROP.SCRIPT:
            return {}
        descriptor = NPC_PROPS.get(prop_id)
        if descriptor is None:
            return {}
        value = self._client_prop_value(descriptor, value)
        if value is _REJECT:
            return {}
        coordinates = {2: ('x', 75), 3: ('y', 76), 43: ('z', 77),
                       75: ('x', 2), 76: ('y', 3), 77: ('z', 43)}
        if prop_id == NPCPROP.HURTDXDY:
            push = hurt_dxdy_from_wire(value)
            if push is None or push == self.hurt_dxdy:
                return {}
            self.hurt_dxdy = push
            return {prop_id: hurt_dxdy_wire(push)}
        if prop_id in coordinates:
            if getattr(self, '_block_position_updates', False):
                return {}
            attr, paired = coordinates[prop_id]
            if attr == 'z' and not -50 <= value <= 170:
                value = -51
            if value == getattr(self, attr, 0):
                return {}
            setattr(self, attr, value)
            if attr in ('x', 'y'):
                # A client moving the NPC ends a running move, and the
                # testForLinks clamp follows (NPC.cpp:2038).
                npc_motion.cancel_moves(self)
            return {prop_id: value, paired: value}
        if 23 <= prop_id <= 32:
            saves = getattr(self, 'saves', [0] * 10)
            saves[prop_id - 23] = value
            self.saves = saves
            return {prop_id: value}
        if descriptor.name.startswith('GATTRIB'):
            if self.gattribs.get(prop_id, '') == value:
                return {}
            self.gattribs[prop_id] = value
            return {prop_id: value}
        attrs = {0: 'image', 4: 'hearts', 5: 'rupees', 6: 'arrows', 7: 'bombs',
                 8: 'glove_power', 9: 'bomb_power', 12: 'gani', 13: 'vis_flags',
                 14: 'block_flags', 15: 'message', 18: 'sprite',
                 19: 'colors', 20: 'nickname', 21: 'horse_image', 22: 'head_image',
                 33: 'ap', 34: 'imagepart', 35: 'body_image', 41: 'map_x',
                 42: 'map_y', 49: 'scripter', 50: 'name', 51: 'script_type',
                 74: 'joined_classes'}
        if prop_id in (10, 11):
            prefix = 'sword' if prop_id == 10 else 'shield'
            power, image = value
            power = min(power, 20 if prop_id == 10 else 3)
            setattr(self, prefix + '_power', power)
            if image is None:
                image = prefix + str(power) + '.png'
            setattr(self, prefix + '_image', image[:223])
            return {prop_id: value}
        attr = attrs.get(prop_id)
        if attr is None:
            return {}
        current = getattr(self, attr, None)
        stored = value / 2 if prop_id == 4 else value
        if prop_id in (6, 7, 8, 9):
            stored = min(value, {6: 99, 7: 99, 8: 2, 9: 3}[prop_id])
        if prop_id == 22:
            stored = f'head{value}.png' if isinstance(value, int) else value[:123]
        if stored == current and prop_id != 12:
            return {}
        setattr(self, attr, stored)
        updates = {prop_id: value}
        if prop_id == 18:
            self.direction = value & 3
        if prop_id == 13:
            self.visible = bool(value & 1)
        if prop_id == 0:
            old = self.vis_flags
            self.vis_flags = ((old | 1) if value and value != '-' else old & ~1)
            self.visible = bool(self.vis_flags & 1)
            if old != self.vis_flags:
                updates[13] = self.vis_flags
            if value == '#c#' and current != '#c':
                self.vis_flags |= 8
                self.gani = 'idle'
                updates[12] = self.gani
        if prop_id == 12:
            if self.image not in ('#c#', '#c'):
                self.image = '#c#'
                updates[0] = self.image
            if not value.startswith('def[') and getattr(self, 'sprite', 0):
                self.sprite = 0
                updates[18] = 0
        return updates

    def build_props_packet(self, *, include_script: bool = True,
                           include_position: bool = True) -> bytes:
        """Build NPC properties, including the client script for full sends.

        `include_position=False` leaves out X/Y/X2/Y2: a client that is
        animating a PLO_MOVE ends the move when it receives a position prop
        (NPC.cpp:493-500)."""
        props = {
            NPCPROP.IMAGE: self.image,
            NPCPROP.X: self.x,
            NPCPROP.Y: self.y,
            # High-precision position (pixel-accurate), sent alongside X/Y for
            # compat. The client applies each prop as it reads it, so X2/Y2
            # (75/76) must arrive after X/Y (2/3) for the high-precision value
            # to win - which the ascending-id order build_npc_props writes in
            # guarantees, independently of this dict's insertion order.
            NPCPROP.X2: self.x,
            NPCPROP.Y2: self.y,
            # SPRITE carries the facing direction in its low 2 bits. direction
            # can arrive as a float (script/default), so coerce before masking.
            NPCPROP.SPRITE: int(self.direction) & 0x03,
            NPCPROP.GLOVEPOWER: self.glove_power,
        }
        if not include_position:
            for prop in (NPCPROP.X, NPCPROP.Y, NPCPROP.X2, NPCPROP.Y2):
                del props[prop]
        if include_script and self.client_script:
            script = self.client_script.replace("\n", "\xa7").encode(
                "latin-1", errors="replace")
            if len(script) > _MAX_CLIENT_SCRIPT_BYTES:
                # Unlike GServer-v2 NPC.cpp:675-677 (warning only), truncate
                # so the encoded length still describes the whole payload.
                logger.warning(
                    "Clientside script of NPC %s (%s) exceeds %s bytes; truncating",
                    self.id, self.name, _MAX_CLIENT_SCRIPT_BYTES)
                script = script[:_MAX_CLIENT_SCRIPT_BYTES]
            props[NPCPROP.SCRIPT] = script.decode("latin-1")
        if self.gani:
            props[NPCPROP.GANI] = self.gani
        if self.nickname:
            props[NPCPROP.NICKNAME] = self.nickname
        if self.message:
            props[NPCPROP.MESSAGE] = self.message
        if self.head_image:
            props[NPCPROP.HEADIMAGE] = self.head_image
        if self.body_image:
            props[NPCPROP.BODYIMAGE] = self.body_image
        if self.sword_image:
            props[NPCPROP.SWORDIMAGE] = (self.sword_power, self.sword_image)
        if self.shield_image:
            props[NPCPROP.SHIELDIMAGE] = (self.shield_power, self.shield_image)
        if self.horse_image:
            props[NPCPROP.HORSEIMAGE] = self.horse_image
        if self.block_flags:
            props[NPCPROP.BLOCKFLAGS] = self.block_flags
        # The reference sends every prop whose modTime is set (NPC.cpp:1758); the
        # push direction is set by hitnpc (GS1Commands.cpp:1470) and the hurtdx
        # / hurtdy builtins (NPC.cpp:1984,1998).
        if self.hurt_dxdy is not None:
            props[NPCPROP.HURTDXDY] = hurt_dxdy_wire(self.hurt_dxdy)
        if getattr(self, 'imagepart', None):
            props[NPCPROP.IMAGEPART] = self.imagepart
        if any(self.colors):
            props[NPCPROP.COLORS] = self.colors
        for prop_id, val in self.gattribs.items():
            props[prop_id] = val
        return build_npc_props(self.id, props)

    def build_showimgs_packet(self) -> Optional[bytes]:
        """Build the authoritative reset-and-replay packet for this NPC."""
        if not self._had_showimgs and not self.showimgs:
            return None
        from .protocol.packets import build_npc_showimgs
        self._had_showimgs = True
        return build_npc_showimgs(self.id, self.showimgs, reset=True)

    async def trigger_event(self, event_name: str, *args):
        """Trigger a script event."""
        if not self.script_instance:
            return

        handler = getattr(self.script_instance, event_name, None)
        if handler and callable(handler):
            try:
                result = handler(*args)
                # Handle async handlers
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"NPC {self.id} event {event_name} error: {e}")

    def set_timer(self, seconds: float):
        """Set timer for on_timeout event."""
        self._timer_end = time.time() + seconds

    def check_timer(self) -> bool:
        """Check if timer has expired."""
        if self._timer_end > 0 and time.time() >= self._timer_end:
            self._timer_end = 0
            return True
        return False

    def start_move(self, x: float, y: float, speed: float):
        """Begin smoothly walking toward (x, y) at `speed` tiles/sec.

        Advanced every server tick by NPCManager.tick() via advance_movement().
        """
        self._move_target = (float(x), float(y))
        self._move_speed = max(float(speed), 0.1)

    @property
    def is_moving(self) -> bool:
        """True while a start_move() target is still in progress."""
        return self._move_target is not None

    def advance_movement(self, dt: float) -> bool:
        """Step toward the current move target by `_move_speed * dt`.

        Snaps to the target and clears it once within range. Returns True if
        the NPC arrived at its target on this call.
        """
        if self._move_target is None:
            return False

        tx, ty = self._move_target
        dx = tx - self.x
        dy = ty - self.y
        dist = math.hypot(dx, dy)
        step = self._move_speed * dt

        if dist <= step:
            self.x = tx
            self.y = ty
            self._move_target = None
            self.mark_dirty()
            return True

        self.x += dx / dist * step
        self.y += dy / dist * step
        self.mark_dirty()
        return False


class NPCApi:
    """
    API exposed to NPC scripts.

    Provides methods for NPC actions like movement, warping, etc.
    """

    def __init__(self, npc: NPC, manager: 'NPCManager'):
        self._npc = npc
        self._manager = manager

    @property
    def id(self) -> int:
        return self._npc.id

    @property
    def name(self) -> str:
        return self._npc.name

    @property
    def x(self) -> float:
        return self._npc.x

    @x.setter
    def x(self, value: float):
        self._write_position('x', value)

    @property
    def y(self) -> float:
        return self._npc.y

    @y.setter
    def y(self, value: float):
        self._write_position('y', value)

    def _write_position(self, attr: str, value: float):
        """Same rules as a script `x = ...`: pixel wrap, moves cancelled."""
        try:
            tiles = npc_motion.script_coordinate(
                self._manager.server, self._npc, attr, float(value))
        except (TypeError, ValueError):
            return
        if tiles is None:
            return
        setattr(self._npc, attr, tiles)
        npc_motion.cancel_moves(self._npc)
        self._npc.mark_dirty()

    @property
    def level(self) -> Optional['Level']:
        return self._npc.level

    @property
    def level_name(self) -> str:
        return self._npc.level.name if self._npc.level else ""

    @property
    def image(self) -> str:
        return self._npc.image

    @image.setter
    def image(self, value: str):
        self._npc.image = value
        self._npc.mark_dirty()

    @property
    def gani(self) -> str:
        return self._npc.gani

    @gani.setter
    def gani(self, value: str):
        self._npc.gani = value
        self._npc.mark_dirty()

    @property
    def direction(self) -> int:
        return self._npc.direction

    @direction.setter
    def direction(self, value: int):
        self._npc.direction = value
        self._npc.mark_dirty()

    @property
    def message(self) -> str:
        return self._npc.message

    @message.setter
    def message(self, value: str):
        self._npc.message = value
        self._npc.mark_dirty()

    @property
    def nickname(self) -> str:
        return self._npc.nickname

    @nickname.setter
    def nickname(self, value: str):
        self._npc.nickname = value
        self._npc.mark_dirty()

    @property
    def head_image(self) -> str:
        return self._npc.head_image

    @head_image.setter
    def head_image(self, value: str):
        self._npc.head_image = value
        self._npc.mark_dirty()

    @property
    def body_image(self) -> str:
        return self._npc.body_image

    @body_image.setter
    def body_image(self, value: str):
        self._npc.body_image = value
        self._npc.mark_dirty()

    @property
    def colors(self) -> List[int]:
        return self._npc.colors

    @colors.setter
    def colors(self, value: List[int]):
        self._npc.colors = list(value)
        self._npc.mark_dirty()

    @property
    def flags(self) -> Dict[str, str]:
        return self._npc.flags

    def move(self, dx: float, dy: float):
        """Move NPC by offset."""
        self._npc.x += dx
        self._npc.y += dy
        self._npc.mark_dirty()

    def face(self, direction: int):
        """Set facing direction (0=up, 1=left, 2=down, 3=right)."""
        self._npc.direction = direction & 0x03
        self._npc.mark_dirty()

    def move_to(self, x: float, y: float, speed: float = 3.0):
        """Smoothly walk to (x, y) at `speed` tiles/sec, advanced every tick."""
        self._npc.start_move(x, y, speed)

    @property
    def is_moving(self) -> bool:
        """True while a move_to() is still in progress."""
        return self._npc.is_moving

    def set_nickname(self, nickname: str):
        """Set the NPC's nickname (shown above its head)."""
        self._npc.nickname = nickname
        self._npc.mark_dirty()

    def set_character(self, head: str = "", body: str = "",
                      colors: Optional[List[int]] = None):
        """Make the NPC look like a player character (head/body/colors)."""
        if head:
            self._npc.head_image = head
        if body:
            self._npc.body_image = body
        if colors is not None:
            self._npc.colors = list(colors)
        self._npc.mark_dirty()

    def warp(self, level_name: str, x: float, y: float):
        """Warp NPC to a location."""
        asyncio.create_task(self._manager.warp_npc(self._npc, level_name, x, y))

    def set_image(self, image: str):
        """Set NPC image."""
        self._npc.image = image
        self._npc.mark_dirty()

    def set_ani(self, animation: str):
        """Set NPC animation."""
        self._npc.gani = animation
        self._npc.mark_dirty()

    def set_timer(self, seconds: float):
        """Set timer for on_timeout event."""
        self._npc.set_timer(seconds)

    def say(self, text: str):
        """Display message above NPC."""
        self._npc.message = text
        self._npc.mark_dirty()

    def hide(self):
        """Hide the NPC."""
        self._npc.visible = False
        self._npc.mark_dirty()

    def show(self):
        """Show the NPC."""
        self._npc.visible = True
        self._npc.mark_dirty()

    def destroy(self):
        """Destroy this NPC."""
        asyncio.create_task(self._manager.destroy_npc(self._npc))

    def get_flag(self, name: str) -> str:
        """Get NPC flag value."""
        return self._npc.flags.get(name, "")

    def set_flag(self, name: str, value: str):
        """Set NPC flag value."""
        self._npc.flags[name] = value


class NPCManager:
    """
    Manages all NPCs in the server.

    Handles NPC creation, destruction, script loading, and event dispatch.
    """

    #: How long a sword-gani washit shadows the client's hit report.
    SWORD_SWING_SECONDS = 0.6
    #: Longest a tick advances the NPC move queues, like the 0.25 s clamp on
    #: smooth movement: a stalled loop does not replay seconds of motion.
    MAX_MOVE_TICK_MS = 250
    #: Minimum gap between two gmap rolls of one NPC.
    RELOCATE_COOLDOWN = 0.5

    def __init__(self, server: 'GameServer'):
        self.server = server

        # All NPCs by ID
        self._npcs: Dict[int, NPC] = {}
        self._weapon_runtimes: Dict[int, NPC] = {}
        self._next_id = 10001  # NPC IDs start at 10001

        # Script classes by name
        self._script_classes: Dict[str, type] = {}

        # Last tick() timestamp, used to compute real elapsed time for
        # smooth (per-tick) NPC movement. None until the first tick.
        self._last_move_tick: Optional[float] = None
        self._move_clock_ns: Optional[int] = None

        # (player id, npc id) -> when a sword-gani rectangle last fired washit.
        self._sword_hits: Dict[Tuple[int, int], float] = {}

    def _allocate_id(self) -> int:
        """Next NPC id that no NPC or weapon runtime already holds."""
        while (self._next_id in self._npcs
               or self._next_id in self._weapon_runtimes):
            self._next_id += 1
        npc_id = self._next_id
        self._next_id += 1
        return npc_id

    def register_weapon_runtime(self, npc: NPC) -> NPC:
        """Give a server-side weapon runtime a collision-free ticking ID."""
        npc.id = self._allocate_id()
        self._weapon_runtimes[npc.id] = npc
        return npc

    def unregister_weapon_runtime(self, npc: NPC):
        """Stop ticking a replaced or deleted server-side weapon script."""
        self._weapon_runtimes.pop(npc.id, None)

    async def load_scripts(self, scripts_path: Path):
        """
        Load NPC script classes from Python files.

        Each Python file should define a class with event handlers:
        - on_created(npc)
        - on_timeout(npc)
        - on_player_chats(npc, player, message)
        - on_player_enters(npc, player)
        - on_player_leaves(npc, player)
        - on_player_touches(npc, player)
        """
        for py_file in scripts_path.glob("*.py"):
            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Look for NPC script classes (classes with on_created method)
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (isinstance(attr, type) and
                            hasattr(attr, 'on_created') and
                            attr_name not in ['NPC', 'NPCApi']):
                            self._script_classes[attr_name] = attr
                            logger.info(f"Loaded NPC script: {attr_name}")

            except Exception as e:
                logger.error(f"Error loading script {py_file}: {e}")

    def create_npc(self, name: str = "", script_name: str = "",
                   level: Optional['Level'] = None,
                   x: float = 0, y: float = 0,
                   npc_id: Optional[int] = None) -> NPC:
        """
        Create a new NPC.

        Args:
            name: NPC name
            script_name: Name of script class to use
            level: Initial level
            x, y: Initial position
            npc_id: Explicit ID (NC_NPCADD); None takes the next free one

        Returns:
            Created NPC instance
        """
        if npc_id is None:
            npc_id = self._allocate_id()
        else:
            # An id chosen by NC_NPCADD is reserved: the auto counter must
            # never hand the same id to a later create_npc().
            self._next_id = max(self._next_id, npc_id + 1)

        npc = NPC(npc_id, name)
        npc.x = x
        npc.y = y

        if level:
            level.add_npc(npc)

        if script_name and script_name in self._script_classes:
            npc.set_script(self._script_classes[script_name])

        self._npcs[npc_id] = npc

        # Trigger on_created
        asyncio.create_task(self._trigger_created(npc))

        return npc

    async def _trigger_created(self, npc: NPC):
        """Trigger on_created event."""
        api = npc.get_api(self)
        await npc.trigger_event('on_created', api)

    def attach_gs1(self, npc: NPC, code: str, *, raise_errors: bool = False,
                   fire_created: bool = True):
        """Attach both script halves, compiling and running only serverside.

        NC saves use strict compilation and retain the old script on errors;
        they do not fire a new 'created' event.
        """
        from .gs1_host import compile_gs1, run_npc_event
        config = getattr(self.server, "config", None)
        server_code, client_code = split_gs1_script(
            code, getattr(config, "serverside", True))
        prog = (compile_gs1(server_code, raise_errors=raise_errors)
                if server_code.strip() else None)
        npc.gs1_program = prog
        # A new script is a new wrapper in the reference: no old locals.
        npc.gs1_scopes.pop("local", None)
        npc.gs1_scopes.pop("local_classes", None)
        npc.gs1_source = code
        npc.client_script = client_code
        # Test doubles stand in for Level here, so look the hook up.
        check_zone = getattr(npc.level, 'check_sparring_zone', None)
        if check_zone is not None:
            check_zone(client_code)
        npc._script_dirty = True
        npc.mark_dirty()
        if prog is not None and fire_created:
            run_npc_event(npc, 'created', self.server, None)

    def _fire_gs1(self, npc: NPC, event: str, player: Optional['Player'] = None,
                  source: Optional[str] = None, carryobject_type=None, params=None):
        """Run a GS1 event handler on an NPC if it has a GS1 program.

        `source` is who/what initiated the hit for events that expose it as
        a flag (wasshot's shotbyplayer/shotbybaddy/shotbynpc, GS1Flags.cpp) -
        one of "player"/"baddy"/"npc", or None for events with no such flags.
        """
        if npc.gs1_program is None:
            return
        from .gs1_host import run_npc_event
        run_npc_event(npc, event, self.server, player, source=source,
                      carryobject_type=carryobject_type, params=params)

    async def on_npc_washit(self, npc: NPC, player: Optional['Player'] = None):
        """Fire `washit` on an NPC hit by hitnpc/hitobjects or a player's
        sword swing (see gs1_host._c_hitnpc, combat.handle_hit_objects)."""
        self._fire_gs1(npc, 'washit', player)

    async def on_player_gani(self, player: 'Player'):
        """Serverside sword washit (98f18f46, PlayerProps.cpp:362-391): a
        gani named in `playerswordganis` hits every visible NPC whose event
        box meets the sword rectangle in front of the player (inclusive
        edges). The `spin` gani's four-point test is left to the client's
        PLI_HITOBJECTS. Needs the NPC server, i.e. `serverside`."""
        config = self.server.config
        level = player.level
        if level is None or not getattr(config, 'serverside', True):
            return
        name = (getattr(player, 'gani', '') or 'idle').split(',', 1)[0]
        if name == 'spin' or name not in getattr(config, 'playerswordganis', ()):
            return
        x, y = absolute_position(player)
        rect = collision.sword_hit_rect(
            collision.tiles_to_pixels(x), collision.tiles_to_pixels(y),
            getattr(player, 'direction', 2))
        # The reference searches by distance in whole-map coordinates
        # (Level::findIntersectingNPCsForEvent, Level.cpp:3546-3562), so an NPC
        # on a neighbouring segment can be hit: shift its box into this level's
        # frame.
        for near, (shift_x, shift_y) in self.nearby_levels(level):
            for npc in self.get_npcs_on_level(near):
                box = collision.npc_event_rect(npc)
                if box is not None:
                    box = (box[0] + shift_x, box[1] + shift_y, box[2], box[3])
                if collision.rects_intersect(rect, box):
                    self._note_sword_hit(player, npc)
                    self._fire_gs1(npc, 'washit', player)

    def _gmap_info(self, level: 'Level'):
        lookup = getattr(getattr(self.server, 'world', None),
                         'get_gmap_for_level', None)
        return lookup(level.name) if lookup is not None else None

    def nearby_levels(self, level: 'Level'):
        """`level` and the gmap segments around it as (level, pixel offset of
        that segment from `level`); just `level` off a gmap. Missing segments
        are skipped."""
        found = [(level, (0, 0))]
        info = self._gmap_info(level)
        if info is None:
            return found
        world = self.server.world
        gmap, grid_x, grid_y = info
        for step_y in (-1, 0, 1):
            for step_x in (-1, 0, 1):
                if not step_x and not step_y:
                    continue
                name = gmap.get_level_at(grid_x + step_x, grid_y + step_y)
                near = world.get_level(name) if name else None
                if near is not None:
                    found.append((near, (step_x * 1024, step_y * 1024)))
        return found

    def _note_sword_hit(self, player: 'Player', npc: NPC):
        now = time.monotonic()
        if len(self._sword_hits) > 256:
            self._sword_hits = {key: when for key, when in self._sword_hits.items()
                                if now - when < self.SWORD_SWING_SECONDS}
        self._sword_hits[(player.id, npc.id)] = now

    def sword_hit_recent(self, player: 'Player', npc: NPC) -> bool:
        """True while a gani-rectangle washit from `player` on `npc` is
        still within the same swing; PLI_HITOBJECTS skips its own washit then
        so one swing fires one event (the reference tolerates both)."""
        when = self._sword_hits.get((player.id, npc.id))
        return when is not None and time.monotonic() - when < self.SWORD_SWING_SECONDS

    async def on_npc_wasshot(self, npc: NPC, source: str,
                              player: Optional['Player'] = None):
        """Fire `wasshot` on an NPC hit by an arrow. `source` is
        "player"/"baddy"/"npc" (see _fire_gs1)."""
        self._fire_gs1(npc, 'wasshot', player, source=source)

    async def on_npc_exploded(self, npc: NPC, player: Optional['Player'] = None,
                              carryobject_type=None):
        """Fire `exploded` on an NPC caught in a bomb explosion radius.

        `carryobject_type` is the blast's CarryObjectType (BOMB or JOLTBOMB,
        Level.cpp:2255-2262); it rides the event so `explodedwithjolt` can
        read it (25396a18)."""
        self._fire_gs1(npc, 'exploded', player,
                       carryobject_type=carryobject_type)

    async def on_npc_waspelt(self, npc: NPC, carryobject_type,
                             player: Optional['Player'] = None):
        self._fire_gs1(npc, 'waspelt', player,
                       carryobject_type=carryobject_type)

    async def on_npc_wasthrown(self, npc: NPC,
                               player: Optional['Player'] = None):
        self._fire_gs1(npc, 'wasthrown', player)

    async def on_baddies_cleared(self, level: 'Level'):
        """Fire `compusdied` on every NPC on a level once its last living
        baddy has died (scripting-gs1-events.md compusdied)."""
        for npc in self.get_npcs_on_level(level):
            self._fire_gs1(npc, 'compusdied')

    def get_npc(self, npc_id: int) -> Optional[NPC]:
        """Get NPC by ID."""
        return self._npcs.get(npc_id)

    def get_npc_by_name(self, name: str) -> Optional[NPC]:
        """Get an NPC by exact name (NPCServer::getNPCByName)."""
        for npc in self._npcs.values():
            if npc.name == name:
                return npc
        return None

    def get_npcs_on_level(self, level: 'Level') -> List[NPC]:
        """Get all NPCs on a level.

        Uses Level's own per-level NPC dict instead of scanning every NPC on
        the server - this is called on essentially every player movement
        packet (check_touches) and every chat/enter/leave event, so a
        server-wide scan doesn't scale past a handful of levels. Falls back
        to the old server-wide scan for level-like objects that don't expose
        get_npcs() (e.g. test doubles).
        """
        get_npcs = getattr(level, 'get_npcs', None)
        if get_npcs is not None:
            return get_npcs()
        return [npc for npc in self._npcs.values() if npc.level == level]

    async def destroy_npc(self, npc: NPC, *, notify: bool = True):
        """Destroy an NPC."""
        if npc.id in self._npcs:
            del self._npcs[npc.id]

        if npc.level:
            # Level.remove_npc() clears npc.level back to None as part of
            # removal, so grab the name FIRST - reading npc.level.name after
            # the call (as this used to) always raised AttributeError on a
            # None, which silently killed this as a background task
            # (asyncio only logs "Task exception was never retrieved", never
            # surfaces to a player/admin) before the PLO_NPCDEL broadcast
            # below ever ran. Every GS1 `destroy;` was hitting this - the
            # NPC was gone server-side (removed from self._npcs above) but
            # every connected client kept rendering a ghost at its last
            # position forever, since they never got told it was deleted.
            level_name = npc.level.name
            npc.level.remove_npc(npc)

            # Notify players on level
            packet = build_npc_del(npc.id)
            if notify:
                await self.server.broadcast_to_level(level_name, packet)
                # Holders on a segment the NPC left. One that has since
                # walked onto the NPC's own level just got the broadcast.
                for player, home in list(getattr(npc, '_seam_players', {}).values()):
                    if self._holds(player, home) and home != level_name:
                        await player.send_raw(packet)
        getattr(npc, '_seam_players', {}).clear()

    async def warp_npc(self, npc: NPC, level_name: str, x: float, y: float):
        """Warp an NPC to a new location (NPC::warp, NPC.cpp:166-240)."""
        new_level = self.server.world.get_level(level_name)
        if new_level is None:
            return  # NPC::warp(nullptr) is a no-op; the NPC stays where it is
        # A forced move ends any running move (NPC.cpp:185).
        npc.move_queue.clear()
        npc.x = x
        npc.y = y
        if new_level is npc.level:
            npc.mark_dirty()
            return
        await self.relocate_npc(npc, new_level)

    async def relocate_npc(self, npc: NPC, new_level: 'Level'):
        """Move `npc` into `new_level` and tell the clients.

        NPC.cpp:206-231: PLO_NPCMOVED (new level and local position) makes a
        client that knows the NPC re-home or hide it; players on the new level
        then get the full props, show-images and running move queue as for a
        new arrival (Level::sendNPCToPlayer, Level.cpp:1466-1485). The
        reference sends PLO_NPCMOVED to every player and keeps updating the
        ones nearby; only the old level's players held the NPC before, so only
        they are told, and while the NPC stays on a neighbouring segment of the
        gmap they keep receiving its updates (`_npc_recipients`)."""
        old_level = npc.level
        if old_level is not None:
            old_level.remove_npc(npc)
            await self.server.broadcast_to_level(
                old_level.name,
                build_npc_moved(npc.id, new_level.name, npc.x, npc.y))
            # Players of the segment it left (not those it never reached).
            near = {level.name for level, _ in self.nearby_levels(new_level)}
            if old_level.name in near:
                for player in self.server.audience.players_on_level(old_level.name):
                    npc._seam_players[player.id] = (player, old_level.name)
        new_level.add_npc(npc)
        npc._dirty = False
        npc._last_relocate = time.monotonic()
        # The new level has heard of none of the moves yet; the tick sends the
        # whole queue to it with the next batch (NPC.cpp:1484 equivalent).
        for move in npc.move_queue:
            move.sent = False
        props = npc.build_props_packet()
        await self.server.broadcast_to_level(new_level.name, props)
        showimgs = npc.build_showimgs_packet()
        if showimgs is not None:
            await self.server.broadcast_to_level(new_level.name, showimgs)
        # Holders that are still in reach (this also covers the ones from an
        # earlier roll) get the exact position, not just PLO_NPCMOVED's half
        # tiles; the ones the NPC left behind are told to forget it now, as
        # nothing may be dirty afterwards to trigger that.
        for holder in await self._seam_holders(npc):
            await holder.send_raw(props)

    def forget_player(self, player: 'Player') -> None:
        """A player left the server: no NPC may keep it as a holder (its id
        can be handed to the next login)."""
        for npc in self._npcs.values():
            held = npc._seam_players.get(player.id)
            if held is not None and held[0] is player:
                del npc._seam_players[player.id]

    def _holds(self, player, home: str) -> bool:
        """True while `player` is still connected and still on the segment
        `home` it held an NPC from."""
        if self.server.get_player(getattr(player, 'id', None)) is not player:
            return False
        return getattr(getattr(player, 'level', None), 'name', None) == home

    async def _seam_holders(self, npc: NPC) -> List['Player']:
        """Reconcile the players holding `npc` from a segment it left, and
        return the ones that still do. A holder is dropped once it enters the
        NPC's level (it got the NPC as an arrival), leaves its segment or
        disconnects; one the NPC has moved out of reach of is told to delete
        it so it does not keep a frozen copy."""
        if not npc._seam_players or npc.level is None:
            return []
        level_name = npc.level.name
        near = {near.name for near, _ in self.nearby_levels(npc.level)}
        holders = []
        for player_id, (player, home) in list(npc._seam_players.items()):
            if not self._holds(player, home) or home == level_name:
                del npc._seam_players[player_id]
            elif home not in near:
                del npc._seam_players[player_id]
                await player.send_raw(build_npc_del(npc.id))
            else:
                holders.append(player)
        return holders

    async def _npc_recipients(self, npc: NPC) -> List['Player']:
        """Players on the NPC's level plus those still holding it from a
        segment it left (see relocate_npc and _seam_holders)."""
        recipients = self.server.audience.players_on_level(npc.level.name)
        if not npc._seam_players:
            return recipients
        recipients.extend(await self._seam_holders(npc))
        return recipients

    async def _broadcast_to_npc(self, npc: NPC, packet: bytes):
        if not npc._seam_players:
            await self.server.broadcast_to_level(npc.level.name, packet)
            return
        for player in await self._npc_recipients(npc):
            await player.send_raw(packet)

    async def confine_position(self, npc: NPC):
        """NPC::testForLinks position handling (cc20c9d0, NPC.cpp:2038-2091).

        Off a gmap the NPC is clamped to local pixels 0..976 (61 tiles) on
        both axes. On a gmap there is no clamp: a position outside the
        segment rolls the NPC into the neighbouring segment, i.e. into that
        level. The reference computes the roll with a truncating divide and
        abs(), which leaves a small negative offset where it is and mirrors a
        larger one; we floor instead so walking left rolls left. A neighbour
        that does not exist leaves the NPC where it is (it would otherwise
        vanish into a segment nobody can load).

        An NPC that just rolled waits RELOCATE_COOLDOWN seconds before it may
        roll again, so one that oscillates across a seam cannot make every tick
        send the whole NPC (script included) to a level."""
        npc._position_check = False
        level = npc.level
        if level is None:
            return
        px = collision.tiles_to_pixels(npc.x)
        py = collision.tiles_to_pixels(npc.y)
        info = self._gmap_info(level)
        if info is None:
            clamped = (min(max(px, 0), 61 * 16), min(max(py, 0), 61 * 16))
            if clamped != (px, py):
                if clamped[0] != px:
                    npc.x = clamped[0] / 16.0
                if clamped[1] != py:
                    npc.y = clamped[1] / 16.0
                npc.mark_dirty()
            return
        world = self.server.world
        gmap, grid_x, grid_y = info
        shift_x, shift_y = px // 1024, py // 1024
        if not shift_x and not shift_y:
            return
        name = gmap.get_level_at(grid_x + shift_x, grid_y + shift_y)
        target = world.get_level(name) if name else None
        if target is None:
            return
        if time.monotonic() - npc._last_relocate < self.RELOCATE_COOLDOWN:
            npc._position_check = True      # roll once the cooldown is over
            return
        npc.x = (px - shift_x * 1024) / 16.0
        npc.y = (py - shift_y * 1024) / 16.0
        await self.relocate_npc(npc, target)

    async def _run_move_queues(self, delta_ms: int):
        """NPCServer.cpp:208: step every NPC's move queue, then let
        `movementfinished` scripts run."""
        for npc in list(self._npcs.values()):
            npc_motion.refill_wall_budget(npc)
            if npc.move_queue and delta_ms:
                npc_motion.process_queue(npc, self.server, delta_ms)
        for npc in list(self._npcs.values()):
            if npc._position_check:
                await self.confine_position(npc)
            while npc._move_events:
                npc._move_events -= 1
                if npc.id in self._npcs:
                    self._fire_gs1(npc, 'movementfinished')

    async def _send_new_moves(self):
        for npc in list(self._npcs.values()):
            if npc.level is None or not any(
                    not move.sent for move in npc.move_queue):
                continue
            await npc_motion.send_unsent_moves(
                npc, self.server, await self._npc_recipients(npc))

    async def tick(self):
        """Process NPC timers and re-broadcast changed NPCs (every server tick)."""
        # Advance smooth movement using real elapsed time (the tick loop's
        # sleep isn't exact), clamped so a stall/debugger pause can't produce
        # a huge catch-up step.
        now = time.monotonic()
        if self._last_move_tick is None:
            dt = 0.0
        else:
            dt = max(0.0, min(now - self._last_move_tick, 0.25))
        self._last_move_tick = now

        # Whole milliseconds since the last tick for the move queues; the
        # sub-millisecond remainder carries over so no time is lost.
        now_ns = time.monotonic_ns()
        if self._move_clock_ns is None:
            self._move_clock_ns = now_ns
        delta_ms = (now_ns - self._move_clock_ns) // 1_000_000
        self._move_clock_ns += delta_ms * 1_000_000
        delta_ms = min(delta_ms, self.MAX_MOVE_TICK_MS)

        for npc in list(self._npcs.values()):
            if npc.is_moving and npc.advance_movement(dt):
                api = npc.get_api(self)
                await npc.trigger_event('on_move_done', api)

        await self._run_move_queues(delta_ms)

        timed_npcs = list(self._npcs.values()) + list(self._weapon_runtimes.values())
        for npc in timed_npcs:
            if npc.check_timer():
                api = npc.get_api(self)
                await npc.trigger_event('on_timeout', api)
                # `timeout` has no triggering player of its own, but GS1
                # scripts commonly gate it on bare (unprefixed) flags set
                # earlier by an unrelated NPC (e.g. a beer-quest guard
                # setting `drunkguard` for a mountain guard's timeout to
                # read later) - those bare flags live on player.flags
                # (run_npc_event), so without a player context here they'd
                # always resolve against a fresh, throwaway dict and never
                # see what was actually set. GServer-v2 ties exactly this
                # case to the level leader (scripting-gs1-flags.md isleader:
                # "can trigger timeout events on NPCs that didn't issue the
                # timereverywhere command"), so do the same here.
                from .gs1_host import leader_player_for_level
                leader = leader_player_for_level(self.server, npc.level)
                self._fire_gs1(npc, 'timeout', leader)

        # Push props for any NPC whose visible state changed this tick so
        # players see movement, animation, chat, and appearance updates live.
        for npc in list(self._npcs.values()):
            if npc._dirty:
                npc._dirty = False
                if npc.level:
                    # Movement/appearance updates only carry the script after
                    # an attachment or NC edit; level-entry sends remain full.
                    packet = npc.build_props_packet(
                        include_script=npc._script_dirty,
                        include_position=not npc.move_queue)
                    npc._script_dirty = False
                    await self._broadcast_to_npc(npc, packet)

        # PLO_MOVE(2) for the moves queued since the last tick, after the props
        # (NPCServer.cpp:231).
        await self._send_new_moves()

    async def on_player_enters(self, player: 'Player', level: 'Level'):
        """Trigger on_player_enters for NPCs on level."""
        for npc in self.get_npcs_on_level(level):
            api = npc.get_api(self)
            await npc.trigger_event('on_player_enters', api, player)
            self._fire_gs1(npc, 'playerenters', player)

    async def on_player_leaves(self, player: 'Player', level: 'Level'):
        """Trigger on_player_leaves for NPCs on level."""
        for npc in self.get_npcs_on_level(level):
            api = npc.get_api(self)
            await npc.trigger_event('on_player_leaves', api, player)
            self._fire_gs1(npc, 'playerleaves', player)

    async def on_player_chats(self, player: 'Player', message: str):
        """Trigger on_player_chats for NPCs on player's level."""
        if not player.level:
            return

        for npc in self.get_npcs_on_level(player.level):
            api = npc.get_api(self)
            await npc.trigger_event('on_player_chats', api, player, message)
            try:
                player.chat = message  # so #c / playersays() see it
            except Exception:
                pass
            self._fire_gs1(npc, 'playerchats', player)

    async def on_player_touches(self, player: 'Player', npc: NPC):
        """Trigger on_player_touches for an NPC."""
        api = npc.get_api(self)
        await npc.trigger_event('on_player_touches', api, player)
        self._fire_gs1(npc, 'playertouchsme', player)

    async def on_trigger_action(self, player: 'Player', x: float, y: float,
                                action: str):
        await self.fire_trigger_at(player.level, x, y, action, player, None)

    async def fire_trigger_at(self, level, x: float, y: float, action_csv: str,
                              player=None, source_npc=None):
        """Dispatch a trigger at a point, retaining the caller's player context.

        GServer-v2 Server.cpp:2003-2010 only relays NPC-originated triggers
        to nearby players. Dispatch to other containing NPCs is our extension;
        the player-originated NPC dispatch follows the reference behavior.
        """
        from .gs2 import parse_csv_tolerant
        if not level or not action_csv:
            return
        fields = parse_csv_tolerant(action_csv)
        event = fields[0].strip()
        if not event:
            return
        # Strict containment, matching the reference: triggerDistance there is
        # only a candidate-search radius; the hit test is positionInRectangle
        # against the NPC's EVENT box (GServer-v2 server/include/npcserver/
        # NPCServer.h addEventToLevelNPCsAtPosition, 56ac0f20): an invisible
        # NPC gets none, a character NPC is its 32x32 feet box, and NOBLOCK
        # does not matter. A shapeless non-character NPC keeps pygserver's
        # 2x2-tile box (the reference has no such default).
        # The point stays unrounded so a sub-pixel miss still misses.
        point = (x * 16, y * 16)
        for npc in self.get_npcs_on_level(level):
            if npc is source_npc:
                continue
            rect = collision.npc_event_rect(npc)
            if rect is None:
                continue
            if not getattr(npc, 'shape', None) and not collision.npc_is_character(npc):
                rect = (rect[0], rect[1], 32, 32)
            if collision.rect_contains_point(rect, *point):
                self._fire_gs1(npc, 'action' + event, player,
                               source='npc' if source_npc is not None else 'player',
                               params=fields[1:])

    async def check_touches(self, player: 'Player'):
        """Fire playertouchsme when the player newly overlaps an NPC.

        Tracks the set of NPCs currently touched so the event fires on entry
        rather than every movement packet while standing on the NPC.
        """
        if not player.level:
            return
        touching = getattr(player, '_touching_npcs', None)
        if touching is None:
            touching = set()
        current = set()
        px, py = absolute_position(player)
        for npc in self.get_npcs_on_level(player.level):
            if not getattr(npc, 'visible', True):
                continue
            if abs(npc.x - px) < 2.0 and abs(npc.y - py) < 2.0:
                current.add(npc.id)
                if npc.id not in touching:
                    await self.on_player_touches(player, npc)
        player._touching_npcs = current
