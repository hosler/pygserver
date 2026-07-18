"""
pygserver.npc - NPC system with Python scripting

Handles NPC state, events, and Python-based scripting.
"""

import asyncio
import importlib.util
import logging
import math
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Callable
from pathlib import Path

from .protocol.packets import PacketBuilder, build_npc_props
from .protocol.constants import PLO, NPCPROP

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .level import Level

logger = logging.getLogger(__name__)


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
        self.horse_image = ""
        self.colors = [0, 0, 0, 0, 0]

        # Properties
        self.message = ""
        self.nickname = ""
        self.hearts = 3.0
        self.rupees = 0
        self.arrows = 0
        self.bombs = 0

        # Visibility
        self.visible = True
        self.block_flags = 0
        self.vis_flags = 0

        # Flags (custom state)
        self.flags: Dict[str, str] = {}

        # Gani animation attributes (NPCPROP.GATTRIB1..30), set via GS1
        # `setcharprop #P1..#P30`. Keyed by wire prop id -> string value.
        self.gattribs: Dict[int, str] = {}

        # Script (Python class-based)
        self.script_class: Optional[type] = None
        self.script_instance: Optional[Any] = None

        # GS1 script (legacy Reborn scripting): parsed program + persistent
        # NPC-scoped variable dicts. Only this./thiso. survive across events;
        # local. is TEMPORARY (like temp.) and gets a fresh dict per script
        # execution in gs1_host.run_npc_event, so it is deliberately NOT
        # given a slot here (see memory: gs1-python-port, upstream d6c78ef3).
        self.gs1_program: Optional[Any] = None
        self.gs1_scopes: Dict[str, dict] = {"this": {}, "thiso": {}}

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

        # Timer
        self._timer_end: float = 0.0

        # Dirty flag: set when a visible property changes so the manager
        # re-broadcasts this NPC's props to players on the next tick.
        self._dirty = False

        # API wrapper for scripts
        self._api: Optional['NPCApi'] = None

    def mark_dirty(self):
        """Flag this NPC for a props re-broadcast on the next server tick."""
        self._dirty = True

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

    def build_props_packet(self) -> bytes:
        """Build NPC properties packet."""
        props = {
            NPCPROP.IMAGE: self.image,
            NPCPROP.X: self.x,
            NPCPROP.Y: self.y,
            # High-precision position (pixel-accurate), sent alongside X/Y for
            # compat. Dict order matters here: the client parses props
            # sequentially and applies each as it's read, so X2/Y2 must come
            # after X/Y for the high-precision value to win.
            NPCPROP.X2: self.x,
            NPCPROP.Y2: self.y,
            # SPRITE carries the facing direction in its low 2 bits. direction
            # can arrive as a float (script/default), so coerce before masking.
            NPCPROP.SPRITE: int(self.direction) & 0x03,
        }
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
            props[NPCPROP.SWORDIMAGE] = self.sword_image
        if self.shield_image:
            props[NPCPROP.SHIELDIMAGE] = self.shield_image
        if self.horse_image:
            props[NPCPROP.HORSEIMAGE] = self.horse_image
        if getattr(self, 'imagepart', None):
            props[NPCPROP.IMAGEPART] = self.imagepart
        if any(self.colors):
            props[NPCPROP.COLORS] = self.colors
        for prop_id, val in self.gattribs.items():
            props[prop_id] = val
        return build_npc_props(self.id, props)

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
        self._npc.x = value
        self._npc.mark_dirty()

    @property
    def y(self) -> float:
        return self._npc.y

    @y.setter
    def y(self, value: float):
        self._npc.y = value
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

    def __init__(self, server: 'GameServer'):
        self.server = server

        # All NPCs by ID
        self._npcs: Dict[int, NPC] = {}
        self._next_id = 10001  # NPC IDs start at 10001

        # Script classes by name
        self._script_classes: Dict[str, type] = {}

        # Last tick() timestamp, used to compute real elapsed time for
        # smooth (per-tick) NPC movement. None until the first tick.
        self._last_move_tick: Optional[float] = None

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
                   x: float = 0, y: float = 0) -> NPC:
        """
        Create a new NPC.

        Args:
            name: NPC name
            script_name: Name of script class to use
            level: Initial level
            x, y: Initial position

        Returns:
            Created NPC instance
        """
        npc_id = self._next_id
        self._next_id += 1

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

    def attach_gs1(self, npc: NPC, code: str):
        """Compile a GS1 script onto an NPC and fire its 'created' handler."""
        from .gs1_host import compile_gs1, run_npc_event
        prog = compile_gs1(code)
        if prog is None:
            return
        npc.gs1_program = prog
        run_npc_event(npc, 'created', self.server, None)

    def _fire_gs1(self, npc: NPC, event: str, player: Optional['Player'] = None,
                  source: Optional[str] = None):
        """Run a GS1 event handler on an NPC if it has a GS1 program.

        `source` is who/what initiated the hit for events that expose it as
        a flag (wasshot's shotbyplayer/shotbybaddy/shotbynpc, GS1Flags.cpp) -
        one of "player"/"baddy"/"npc", or None for events with no such flags.
        """
        if npc.gs1_program is None:
            return
        from .gs1_host import run_npc_event
        run_npc_event(npc, event, self.server, player, source=source)

    async def on_npc_washit(self, npc: NPC, player: Optional['Player'] = None):
        """Fire `washit` on an NPC hit by hitnpc/hitobjects or a player's
        sword swing (see gs1_host._c_hitnpc, combat.handle_hit_objects)."""
        self._fire_gs1(npc, 'washit', player)

    async def on_npc_wasshot(self, npc: NPC, source: str,
                              player: Optional['Player'] = None):
        """Fire `wasshot` on an NPC hit by an arrow. `source` is
        "player"/"baddy"/"npc" (see _fire_gs1)."""
        self._fire_gs1(npc, 'wasshot', player, source=source)

    async def on_npc_exploded(self, npc: NPC, player: Optional['Player'] = None):
        """Fire `exploded` on an NPC caught in a bomb explosion radius."""
        self._fire_gs1(npc, 'exploded', player)

    async def on_baddies_cleared(self, level: 'Level'):
        """Fire `compusdied` on every NPC on a level once its last living
        baddy has died (scripting-gs1-events.md compusdied)."""
        for npc in self.get_npcs_on_level(level):
            self._fire_gs1(npc, 'compusdied')

    def get_npc(self, npc_id: int) -> Optional[NPC]:
        """Get NPC by ID."""
        return self._npcs.get(npc_id)

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

    async def destroy_npc(self, npc: NPC):
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
            from .protocol.packets import build_npc_del
            packet = build_npc_del(npc.id)
            await self.server.broadcast_to_level(level_name, packet)

    async def warp_npc(self, npc: NPC, level_name: str, x: float, y: float):
        """Warp an NPC to a new location."""
        old_level = npc.level

        # Remove from old level
        if old_level:
            old_level.remove_npc(npc)

        # Find new level
        new_level = self.server.world.get_level(level_name)
        if new_level:
            npc.x = x
            npc.y = y
            new_level.add_npc(npc)

            # Notify players
            packet = npc.build_props_packet()
            await self.server.broadcast_to_level(new_level.name, packet)

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

        for npc in list(self._npcs.values()):
            if npc.is_moving and npc.advance_movement(dt):
                api = npc.get_api(self)
                await npc.trigger_event('on_move_done', api)

        for npc in list(self._npcs.values()):
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
                    packet = npc.build_props_packet()
                    await self.server.broadcast_to_level(npc.level.name, packet)

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
        for npc in self.get_npcs_on_level(player.level):
            if not getattr(npc, 'visible', True):
                continue
            if abs(npc.x - player.x) < 2.0 and abs(npc.y - player.y) < 2.0:
                current.add(npc.id)
                if npc.id not in touching:
                    await self.on_player_touches(player, npc)
        player._touching_npcs = current
