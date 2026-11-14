"""
pygserver.nc - NPC Control (NC) system

Handles NPC control connections and NC packet processing.
Based on GServer-v2 TPlayerNC.cpp implementation.
"""

import logging
from typing import TYPE_CHECKING, Optional, Dict, List, Set, Callable
from dataclasses import dataclass

from .irc import guntokenize
from .items import DEFAULT_WEAPON_IDS
from .protocol.builders.admin import from_csv
from .protocol.constants import PLI, PLTYPE, NPCBLOCKFLAG, NPCVISFLAG
from .text_safety import has_control_chars
from .protocol.packets import (
    PacketReader,
    build_nc_level_dump,
    build_nc_level_list,
    build_nc_npc_attributes,
    build_nc_npc_add,
    build_nc_npc_delete,
    build_nc_npc_script,
    build_nc_npc_flags,
    build_nc_class_get,
    build_nc_class_add,
    build_nc_class_delete,
    build_nc_weapon_list,
    build_nc_weapon_get,
    build_npc_weapon_del,
    build_rc_chat,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .npc import NPC

logger = logging.getLogger(__name__)

# GServer-v2 CommonTypes.h:145: smallest NPC id NC_NPCADD accepts.
NPCID_GEN_MANUAL = 3
# Largest id the 3-byte GINT used by every NPC packet can carry (codec
# write_gint3 clamps above it, which would alias distinct NPCs on the wire).
# The reference has no error text for this bound; the message mirrors the
# "must be greater than" one it does have (PlayerNCPackets.cpp:410-414).
NPCID_MAX = 3682399


@dataclass
class NCSession:
    """Represents an NC (NPC Control) session."""
    player: 'Player'
    editing_npc: Optional[int] = None  # NPC ID currently being edited
    editing_class: Optional[str] = None  # Class name being edited
    editing_weapon: Optional[str] = None  # Weapon name being edited


class NCManager:
    """
    Manages NPC Control (NC) connections.

    Handles:
    - NPC editing (get, add, delete, script, flags)
    - Class management (get, add, delete)
    - Weapon management (list, get, add, delete)
    - Level NPC listing
    """

    def __init__(self, server: 'GameServer'):
        self.server = server

        # Active NC sessions
        self._sessions: Dict[int, NCSession] = {}  # player_id -> NCSession

        # NC packet handlers
        self._handlers: Dict[int, Callable] = {
            PLI.NC_NPCGET: self._handle_npc_get,
            PLI.NC_NPCDELETE: self._handle_npc_delete,
            PLI.NC_NPCRESET: self._handle_npc_reset,
            PLI.NC_NPCSCRIPTGET: self._handle_npc_script_get,
            PLI.NC_NPCWARP: self._handle_npc_warp,
            PLI.NC_NPCFLAGSGET: self._handle_npc_flags_get,
            PLI.NC_NPCSCRIPTSET: self._handle_npc_script_set,
            PLI.NC_NPCFLAGSSET: self._handle_npc_flags_set,
            PLI.NC_NPCADD: self._handle_npc_add,
            PLI.NC_CLASSEDIT: self._handle_class_edit,
            PLI.NC_CLASSADD: self._handle_class_add,
            PLI.NC_LOCALNPCSGET: self._handle_local_npcs_get,
            PLI.NC_WEAPONLISTGET: self._handle_weapon_list_get,
            PLI.NC_WEAPONGET: self._handle_weapon_get,
            PLI.NC_WEAPONADD: self._handle_weapon_add,
            PLI.NC_WEAPONDELETE: self._handle_weapon_delete,
            PLI.NC_CLASSDELETE: self._handle_class_delete,
            PLI.NC_LEVELLISTGET: self._handle_level_list_get,
            PLI.NC_LEVELLISTSET: self._handle_level_list_set,
        }

    def register_session(self, player: 'Player') -> NCSession:
        """
        Register an NC session for a player.

        Args:
            player: The NC player

        Returns:
            The created NCSession
        """
        session = NCSession(player=player)
        self._sessions[player.id] = session
        logger.info(f"NC session registered for {player.account_name}")
        return session

    def unregister_session(self, player_id: int):
        """Unregister an NC session."""
        session = self._sessions.pop(player_id, None)
        if session:
            logger.info(f"NC session unregistered for player {player_id}")

    def get_session(self, player_id: int) -> Optional[NCSession]:
        """Get an NC session by player ID."""
        return self._sessions.get(player_id)

    def is_nc(self, player_id: int) -> bool:
        """Check if a player has an active NC session."""
        return player_id in self._sessions

    def get_all_sessions(self) -> List[NCSession]:
        """Get all active NC sessions."""
        return list(self._sessions.values())

    async def _require_gs2_manager(self, session: NCSession, kind: str):
        manager = getattr(self.server, 'gs2_manager', None)
        if manager is None:
            await self._send_error(session, kind, "script store is unavailable")
        return manager

    async def handle_packet(self, player: 'Player', packet_id: int, data: bytes):
        """
        Handle an NC packet.

        Args:
            player: Player sending packet
            packet_id: Packet ID
            data: Packet data
        """
        session = self.get_session(player.id)
        if not session:
            logger.warning(f"NC packet from non-NC player {player.id}")
            return

        handler = self._handlers.get(packet_id)
        if handler:
            try:
                await handler(session, data)
            except Exception as e:
                logger.error(f"NC handler error (packet {packet_id}): {e}")
        else:
            logger.warning(f"Unhandled NC packet: {packet_id}")

    # =========================================================================
    # NPC Management Handlers
    # =========================================================================

    async def _handle_npc_get(self, session: NCSession, data: bytes):
        """Handle NC_NPCGET - Get NPC details (PlayerNCPackets.cpp:42)."""
        # An empty packet is the npc-server's keep-alive poll (:50-53).
        if not data:
            return
        npc_id = PacketReader(data).read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            session.editing_npc = npc_id
            await session.player.send_raw(
                build_nc_npc_attributes(self._variable_dump(npc)))

            logger.debug(f"NC {session.player.account_name} editing NPC {npc_id}")

    async def _handle_npc_delete(self, session: NCSession, data: bytes):
        """Handle NC_NPCDELETE - Delete an NPC (PlayerNCPackets.cpp:64).

        Only database NPCs, i.e. the ones NC_NPCADD created (:75); level and
        server-spawned NPCs are left alone.
        """
        npc_id = PacketReader(data).read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc is not None and getattr(npc, 'database_npc', False):
            # destroy_npc tells every NC connection (Server.cpp:1636-1639).
            await self.server.npc_manager.destroy_npc(npc)
            await self._send_to_ncs(
                f"NPC {npc.name} deleted by {session.player.account_name}")

            logger.info(f"NC {session.player.account_name} deleted NPC {npc_id}")

    async def _handle_npc_reset(self, session: NCSession, data: bytes):
        """Handle NC_NPCRESET - Reset an NPC (PlayerNCPackets.cpp:88-111).

        Database NPCs only (:99). The delete goes to every connection on every
        level (Server.cpp:2329-2341), the NPC returns to the state recorded
        when it was added and warps back there (NPC::resetToInitialState,
        NPC.cpp:117-162), then its `created` event runs again (:103). The
        script source is not reloaded.
        """
        npc_id = PacketReader(data).read_gint3()

        manager = self.server.npc_manager
        npc = manager.get_npc(npc_id)
        if npc is None or not getattr(npc, 'database_npc', False):
            return
        from .protocol.packets import build_npc_del
        await self.server.broadcast_to_all(build_npc_del(npc.id))
        await manager.reset_to_initial_state(npc)
        if npc.gs1_source:
            manager.attach_gs1(npc, npc.gs1_source)
        else:
            await manager._trigger_created(npc)
        await self._send_to_ncs(
            f"NPC script of {npc.name} reset by {session.player.account_name}")
        logger.info(f"NC {session.player.account_name} reset NPC {npc_id}")

    async def _handle_npc_script_get(self, session: NCSession, data: bytes):
        """Handle NC_NPCSCRIPTGET - Get NPC script."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            # The script text lives in `gs1_source` (npc.py:91, written next
            # to the compiled `gs1_program`). Reading a `script` attribute no
            # NPC has raised AttributeError here, so this answered NOTHING and
            # a script editor showed an empty buffer for every NPC.
            packet = build_nc_npc_script(npc_id, npc.gs1_source or "")
            await session.player.send_raw(packet)

    async def _handle_npc_script_set(self, session: NCSession, data: bytes):
        """Handle NC_NPCSCRIPTSET - Set NPC script."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        # PlayerNCPackets.cpp:226: the script arrives gtokenized.
        script = "\n".join(guntokenize(
            reader.remaining().decode('latin-1', errors='replace')))

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            try:
                # setScript, then a CREATED event is queued
                # (PlayerNCPackets.cpp:226-228, origin/beta4); the join
                # statements in `created` refill the CLASS list.
                self.server.npc_manager.attach_gs1(
                    npc, script, raise_errors=True)
            except Exception as error:
                await self._send_compile_error(session, "NPC", str(error))
                return

            logger.info(f"NC {session.player.account_name} updated script for NPC {npc_id}")

    async def _handle_npc_warp(self, session: NCSession, data: bytes):
        """Handle NC_NPCWARP - Warp an NPC (PlayerNCPackets.cpp:113)."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()

        manager = self.server.npc_manager
        npc = manager.get_npc(npc_id)
        if npc is None:
            return
        if npc.level is not None and level_name == npc.level.name:
            # Same level: only the position changes (:152-160).
            npc.x, npc.y = x, y
            npc.mark_dirty()
        elif self.server.world.get_level(level_name) is not None:
            # warp_npc detaches the NPC first, so check the target exists.
            await manager.warp_npc(npc, level_name, x, y)
        else:
            return
        logger.info(f"NC {session.player.account_name} warped NPC {npc_id} to {level_name}")

    async def _handle_npc_flags_get(self, session: NCSession, data: bytes):
        """Handle NC_NPCFLAGSGET - Get NPC flags (PlayerNCPackets.cpp:183)."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            flags = [f"{name}={value}" if value != "" else name
                     for name, value in npc.flags.items()]
            await session.player.send_raw(build_nc_npc_flags(npc_id, flags))

    async def _handle_npc_flags_set(self, session: NCSession, data: bytes):
        """Handle NC_NPCFLAGSSET - Replace NPC flags (PlayerNCPackets.cpp:241).

        The flag list is a toCSV row of `flag` / `flag=value` entries.
        """
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        flags = guntokenize(reader.remaining().decode('latin-1', errors='replace'))

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            npc.flags.clear()
            for entry in flags:
                name, _, value = entry.partition('=')
                if name.strip():
                    npc.flags[name.strip()] = value.strip()

            await self._send_to_ncs(
                f"NPC flags of {npc.name} updated by {session.player.account_name}")
            logger.info(f"NC {session.player.account_name} updated flags for NPC {npc_id}")

    async def _handle_npc_add(self, session: NCSession, data: bytes):
        """Handle NC_NPCADD - Add a new NPC (PlayerNCPackets.cpp:359).

        The body is one CSV row: name, id, type, scripter, level, x, y.
        """
        fields = guntokenize(data.decode('latin-1', errors='replace'))
        fields += [""] * (7 - len(fields))
        name, raw_id, npc_type, scripter, level_name, raw_x, raw_y = (
            value.strip() for value in fields[:7])

        manager = self.server.npc_manager
        if not name:
            await self._send_to_ncs("Error adding database npc: NPC name cannot be empty")
            return
        if manager.get_npc_by_name(name) is not None:
            await self._send_to_ncs("Error adding database npc: NPC name already exists")
            return
        try:
            npc_id = int(raw_id)
            x, y = float(raw_x or 0), float(raw_y or 0)
        except ValueError:
            await self._send_to_ncs("Error adding database npc: invalid NPC id or position")
            return
        if npc_id < NPCID_GEN_MANUAL:
            await self._send_to_ncs(
                f"Error adding database npc: NPC ID must be greater than {NPCID_GEN_MANUAL}")
            return
        if npc_id > NPCID_MAX:
            await self._send_to_ncs(
                f"Error adding database npc: NPC ID must be less than {NPCID_MAX + 1}")
            return
        if manager.get_npc(npc_id) is not None:
            await self._send_to_ncs("Error adding database npc: NPC ID already exists")
            return

        # An unknown or empty level leaves the NPC unattached (:371-387).
        level, x, y = self._npc_add_location(level_name, x, y)
        npc = manager.create_npc(name=name, level=level, x=x, y=y, npc_id=npc_id)
        npc.database_npc = True
        npc.script_type = npc_type
        npc.scripter = scripter
        npc.mark_dirty()

        await self._broadcast_to_ncs(build_nc_npc_add(
            npc.id, name, npc_type, level.name if level else ""))
        await self._send_to_ncs(f"NPC {name} added by {session.player.account_name}")
        logger.info(f"NC {session.player.account_name} created NPC {npc.id} on {level_name}")

    def _npc_add_location(self, level_name: str, x: float, y: float):
        """Level and level-local position for NC_NPCADD.

        PlayerNCPackets.cpp:392-408 puts an NPC named on a gmap segment onto
        the gmap, its position shifted by the segment's offset. NPCs here live
        on the segment itself in its own coordinates, which is the same place.
        Named on the gmap itself (Server.cpp:1350-1355), the position is
        gmap-wide and picks the segment it falls in.
        """
        world = self.server.world
        if not level_name:
            return None, x, y
        if level_name.endswith('.gmap') and hasattr(world, 'get_gmap'):
            gmap = world.get_gmap(level_name) or world.get_gmap(level_name[:-5])
            if gmap is not None:
                local_x, local_y, grid_x, grid_y = gmap.world_to_local(x, y)
                segment = gmap.get_level_at(grid_x, grid_y)
                level = world.get_level(segment) if segment else None
                if level is not None:
                    return level, local_x, local_y
                return None, x, y
        return world.get_level(level_name), x, y

    async def _handle_local_npcs_get(self, session: NCSession, data: bytes):
        """Handle NC_LOCALNPCSGET - Dump the NPCs on a level
        (PlayerNCPackets.cpp:526)."""
        level_name = data.decode('latin-1', errors='replace').strip()
        if not level_name:
            return

        level = self.server.world.get_level(level_name)
        if not level:
            return

        lines = [f"Variables dump from level {level.name}"]
        for npc in level.get_npcs():
            lines.append("")
            lines.extend(self._variable_dump(npc))
            lines.append("")
        await session.player.send_raw(build_nc_level_dump(lines))

    @staticmethod
    def _variable_dump(npc: 'NPC') -> List[str]:
        """NPC variable dump lines, after NPC::getVariableDump (NPC.cpp:2144).

        Only the properties pygserver tracks are listed.
        """
        label = npc.name or f"npcs[{npc.id}]"
        visibility = "visible" if npc.vis_flags & NPCVISFLAG.VISIBLE else "hidden"
        if npc.vis_flags & NPCVISFLAG.DRAWOVERPLAYER:
            visibility += ", drawoverplayer"
        if npc.vis_flags & NPCVISFLAG.DRAWUNDERPLAYER:
            visibility += ", drawunderplayer"
        blocking = "noblock" if npc.block_flags & NPCBLOCKFLAG.NOBLOCK else "block"

        lines = [f"Variables dump from npc {label}", ""]
        script_type = getattr(npc, 'script_type', '')
        if script_type:
            lines.append(f"{label}.type: {script_type}")
        scripter = getattr(npc, 'scripter', '')
        if scripter:
            lines.append(f"{label}.scripter: {scripter}")
        lines += [
            f"{label}.level: {npc.level.name if npc.level else ''}",
            f"{label}.pos: {npc.x:.2f}, {npc.y:.2f}, 0",
            "",
            "Attributes:",
            f"{label}.id: {npc.id}",
            f"{label}.image: {npc.image}",
            f"{label}.script: size: {len(npc.gs1_source or '')}",
            f"{label}.visibility flags: {visibility}",
            f"{label}.blocking flags: {blocking}",
        ]
        return lines

    # =========================================================================
    # Class Management Handlers
    # =========================================================================

    async def _handle_class_edit(self, session: NCSession, data: bytes):
        """Handle NC_CLASSEDIT - Edit a class."""
        reader = PacketReader(data)
        class_name = reader.remaining().decode('latin-1', errors='replace')

        session.editing_class = class_name

        manager = await self._require_gs2_manager(session, "class")
        if manager is None:
            return
        cls = manager.get_class(class_name)
        if cls is None:
            await self._send_error(session, "class", f"not found: {class_name}")
            return

        packet = build_nc_class_get(cls.name, cls.source)
        await session.player.send_raw(packet)

        logger.debug(f"NC {session.player.account_name} editing class {class_name}")

    async def _handle_class_add(self, session: NCSession, data: bytes):
        """Handle NC_CLASSADD: `{CHAR len}{name}` then the source as a toCSV
        row of lines (PlayerNCPackets.cpp:468-470)."""
        reader = PacketReader(data)
        class_name = reader.read_gstring()
        script = "\n".join(from_csv(
            reader.remaining().decode('latin-1', errors='replace')))
        if not class_name or has_control_chars(class_name):
            await self._send_error(session, "class", "invalid class name")
            return

        from .gs1_host import compile_gs1
        try:
            compile_gs1(script, raise_errors=True)
        except Exception as error:
            await self._send_compile_error(session, "class", str(error))
            return

        manager = await self._require_gs2_manager(session, "class")
        if manager is None:
            return
        existing = manager.get_class(class_name) is not None
        try:
            manager.upsert_class(class_name, script)
        except OSError as error:
            await self._send_error(session, "class", f"save failed: {error}")
            return

        # PlayerNCPackets.cpp:481-490: only a new class is announced; either
        # way every NC is told who added or updated it.
        if not existing:
            await self._broadcast_to_ncs(build_nc_class_add(class_name))
        await self._send_to_ncs(
            f"Script {class_name} {'updated' if existing else 'added'} "
            f"by {session.player.account_name}")

        logger.info(f"NC {session.player.account_name} updated class {class_name}")

    async def _handle_class_delete(self, session: NCSession, data: bytes):
        """Handle NC_CLASSDELETE - Delete a class."""
        reader = PacketReader(data)
        class_name = reader.remaining().decode('latin-1', errors='replace')

        manager = await self._require_gs2_manager(session, "class")
        if manager is None:
            return
        try:
            deleted = manager.delete_class(class_name)
        except OSError as error:
            await self._send_error(session, "class", f"delete failed: {error}")
            return
        if not deleted:
            await self._send_error(session, "class", f"not found: {class_name}")
            return

        # Notify NC sessions
        packet = build_nc_class_delete(class_name)
        await self._broadcast_to_ncs(packet)

        logger.info(f"NC {session.player.account_name} deleted class {class_name}")

    # =========================================================================
    # Weapon Management Handlers
    # =========================================================================

    async def _handle_weapon_list_get(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONLISTGET - Get weapon list."""
        manager = await self._require_gs2_manager(session, "weapon")
        if manager is None:
            return
        weapons = sorted(weapon.name for weapon in
                         manager.weapons.values())

        packet = build_nc_weapon_list(weapons)
        await session.player.send_raw(packet)

    async def _handle_weapon_get(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONGET - Get weapon details."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        session.editing_weapon = weapon_name

        manager = await self._require_gs2_manager(session, "weapon")
        if manager is None:
            return
        weapon = manager.get_weapon(weapon_name)
        if weapon is None:
            # PlayerNCPackets.cpp:595-598: told to every NC.
            await self._broadcast_to_ncs(build_rc_chat(
                f"{session.player.account_name} prob: weapon {weapon_name} doesn't exist"))
            return

        packet = build_nc_weapon_get(weapon.name, weapon.image, weapon.source)
        await session.player.send_raw(packet)

        logger.debug(f"NC {session.player.account_name} editing weapon {weapon_name}")

    async def _handle_weapon_add(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONADD: `{CHAR len}{name}{CHAR len}{image}` then the
        source with newlines sent as 0xA7 (PlayerNCPackets.cpp:633-638)."""
        reader = PacketReader(data)
        weapon_name = reader.read_gstring()
        image = reader.read_gstring()
        script = reader.remaining().decode('latin-1', errors='replace').replace('\xa7', '\n')
        if (not weapon_name or has_control_chars(weapon_name)
                or has_control_chars(image)):
            await self._send_error(session, "weapon", "invalid weapon name or image")
            return
        # :646-648: a built-in weapon (Server.cpp:1050-1056) is never replaced.
        # Stored weapons are keyed by lowercase name (gs2.py get_weapon), so
        # `Bow` would shadow the built-in `bow`: compare the same way.
        manager = getattr(self.server, 'gs2_manager', None)
        if (weapon_name.strip().lower() in DEFAULT_WEAPON_IDS and manager is not None
                and manager.get_weapon(weapon_name) is None):
            return

        from .gs1_host import compile_gs1
        try:
            compile_gs1(script, raise_errors=True)
        except Exception as error:
            await self._send_compile_error(session, "weapon", str(error))
            return

        manager = await self._require_gs2_manager(session, "weapon")
        if manager is None:
            return
        existing = manager.get_weapon(weapon_name) is not None
        try:
            manager.upsert_weapon(weapon_name, image, script)
        except OSError as error:
            await self._send_error(session, "weapon", f"save failed: {error}")
            return

        # A player who already holds this weapon keeps the inventory entry and
        # immediately receives its replacement image and clientside script.
        for player in self.server.get_all_players():
            if weapon_name.lower() in {
                    held.lower() for held in (getattr(player, 'weapons', []) or [])}:
                await manager.announce_weapon(player, weapon_name)

        await self._send_to_ncs(
            f"Weapon/GUI-script {weapon_name} {'updated' if existing else 'added'} "
            f"by {session.player.account_name}")
        # PlayerNCPackets.cpp:677-679: only a new weapon refreshes the
        # sender's list.
        if not existing:
            await self._handle_weapon_list_get(session, b"")

        logger.info(f"NC {session.player.account_name} updated weapon {weapon_name}")

    @staticmethod
    async def _send_compile_error(session: NCSession, kind: str, error: str):
        """Return a compiler failure using the existing control-chat packet.

        NC connections already accept the shared RC_CHAT notification packet,
        which is visible immediately and avoids defining a new wire message.
        """
        await session.player.send_raw(build_rc_chat(
            f"[NC {kind} compile error] {error}"))

    @staticmethod
    async def _send_error(session: NCSession, kind: str, error: str):
        """Return an explicit asset operation failure to its NC editor."""
        await session.player.send_raw(build_rc_chat(f"[NC {kind} error] {error}"))

    async def _handle_weapon_delete(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONDELETE - Delete a weapon."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        manager = await self._require_gs2_manager(session, "weapon")
        if manager is None:
            return
        try:
            deleted = manager.delete_weapon(weapon_name)
        except OSError as error:
            await self._send_error(session, "weapon", f"delete failed: {error}")
            return
        # PlayerNCPackets.cpp:692-710: every NC hears the outcome, the sender
        # gets the fresh list after a delete, and Server.cpp:2464 drops the
        # weapon from every game client.
        if not deleted:
            await self._send_to_ncs(
                f"{session.player.account_name} prob: weapon {weapon_name} doesn't exist")
            return
        await self._send_to_ncs(
            f"Weapon {weapon_name} deleted by {session.player.account_name}")
        packet = build_npc_weapon_del(weapon_name)
        for player in self.server.get_all_players():
            if getattr(player, 'connection_type', PLTYPE.CLIENT) & PLTYPE.ANYCLIENT:
                await player.send_raw(packet)
        await self._handle_weapon_list_get(session, b"")

        logger.info(f"NC {session.player.account_name} deleted weapon {weapon_name}")

    # =========================================================================
    # Level Management Handlers
    # =========================================================================

    async def _handle_level_list_get(self, session: NCSession, data: bytes):
        """Handle NC_LEVELLISTGET - Get level list."""
        levels = []
        if hasattr(self.server.world, 'get_all_levels'):
            for level in self.server.world.get_all_levels():
                levels.append(level.name)

        packet = build_nc_level_list(levels)
        await session.player.send_raw(packet)

    async def _handle_level_list_set(self, session: NCSession, data: bytes):
        """Handle NC_LEVELLISTSET - Set level list (usually ignored)."""
        # This is typically not used - levels are determined by filesystem
        pass

    # =========================================================================
    # Utility Methods
    # =========================================================================

    async def _send_to_ncs(self, message: str):
        """Server::sendToNC (Server.h:611): first line only, to every NC."""
        await self._broadcast_to_ncs(build_rc_chat(message.split('\n', 1)[0]))

    async def _broadcast_to_ncs(self, packet: bytes, exclude: Optional[Set[int]] = None):
        """
        Broadcast a packet to all NC sessions.

        Args:
            packet: Packet to send
            exclude: Player IDs to exclude
        """
        exclude = exclude or set()
        for session in self._sessions.values():
            if session.player.id not in exclude:
                await session.player.send_raw(packet)

    async def notify_npc_changed(self, npc: 'NPC'):
        """
        Notify NC sessions that an NPC changed.

        Args:
            npc: The NPC that changed
        """
        await self._broadcast_to_ncs(
            build_nc_npc_attributes(self._variable_dump(npc)))

    async def notify_npc_deleted(self, npc_id: int):
        """
        Notify NC sessions that an NPC was deleted.

        Args:
            npc_id: The deleted NPC ID
        """
        packet = build_nc_npc_delete(npc_id)
        await self._broadcast_to_ncs(packet)
