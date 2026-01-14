"""
pygserver.nc - NPC Control (NC) system

Handles NPC control connections and NC packet processing.
Based on GServer-v2 TPlayerNC.cpp implementation.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Set, Callable
from dataclasses import dataclass, field

from .protocol.constants import PLI, PLO, NPCPROP
from .protocol.packets import (
    PacketBuilder,
    PacketReader,
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
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player
    from .npc import NPC

logger = logging.getLogger(__name__)


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
        """Handle NC_NPCGET - Get NPC details."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            session.editing_npc = npc_id

            # Send NPC attributes
            attrs = self._build_npc_attributes(npc)
            packet = build_nc_npc_attributes(npc_id, attrs)
            await session.player.send_raw(packet)

            logger.debug(f"NC {session.player.account_name} editing NPC {npc_id}")

    async def _handle_npc_delete(self, session: NCSession, data: bytes):
        """Handle NC_NPCDELETE - Delete an NPC."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            level_name = npc.level.name if npc.level else ""
            self.server.npc_manager.remove_npc(npc_id)

            # Notify all NC sessions
            packet = build_nc_npc_delete(npc_id)
            await self._broadcast_to_ncs(packet)

            logger.info(f"NC {session.player.account_name} deleted NPC {npc_id}")

    async def _handle_npc_reset(self, session: NCSession, data: bytes):
        """Handle NC_NPCRESET - Reset an NPC."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            # Reset NPC to initial state
            npc.reset()
            logger.info(f"NC {session.player.account_name} reset NPC {npc_id}")

    async def _handle_npc_script_get(self, session: NCSession, data: bytes):
        """Handle NC_NPCSCRIPTGET - Get NPC script."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            packet = build_nc_npc_script(npc_id, npc.script or "")
            await session.player.send_raw(packet)

    async def _handle_npc_script_set(self, session: NCSession, data: bytes):
        """Handle NC_NPCSCRIPTSET - Set NPC script."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        script = reader.remaining().decode('latin-1', errors='replace')

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            npc.script = script
            npc.compile_script()

            logger.info(f"NC {session.player.account_name} updated script for NPC {npc_id}")

    async def _handle_npc_warp(self, session: NCSession, data: bytes):
        """Handle NC_NPCWARP - Warp an NPC."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace')

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            await self.server.npc_manager.warp_npc(npc_id, level_name, x, y)
            logger.info(f"NC {session.player.account_name} warped NPC {npc_id} to {level_name}")

    async def _handle_npc_flags_get(self, session: NCSession, data: bytes):
        """Handle NC_NPCFLAGSGET - Get NPC flags."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            packet = build_nc_npc_flags(npc_id, npc.flags)
            await session.player.send_raw(packet)

    async def _handle_npc_flags_set(self, session: NCSession, data: bytes):
        """Handle NC_NPCFLAGSSET - Set NPC flags."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        flags_str = reader.remaining().decode('latin-1', errors='replace')

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc:
            # Parse flags
            npc.flags.clear()
            for line in flags_str.split('\n'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    npc.flags[key.strip()] = value.strip()

            logger.info(f"NC {session.player.account_name} updated flags for NPC {npc_id}")

    async def _handle_npc_add(self, session: NCSession, data: bytes):
        """Handle NC_NPCADD - Add a new NPC."""
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.read_string()
        image = reader.read_string()
        script = reader.remaining().decode('latin-1', errors='replace')

        # Get level
        level = self.server.world.get_level(level_name)
        if not level:
            logger.warning(f"NC add NPC failed: level {level_name} not found")
            return

        # Create NPC
        npc = await self.server.npc_manager.create_npc(
            level=level,
            x=x,
            y=y,
            image=image,
            script=script
        )

        # Notify NC sessions
        packet = build_nc_npc_add(npc.id, level_name, x, y, image)
        await self._broadcast_to_ncs(packet)

        logger.info(f"NC {session.player.account_name} created NPC {npc.id} on {level_name}")

    async def _handle_local_npcs_get(self, session: NCSession, data: bytes):
        """Handle NC_LOCALNPCSGET - Get NPCs on a level."""
        reader = PacketReader(data)
        level_name = reader.remaining().decode('latin-1', errors='replace')

        level = self.server.world.get_level(level_name)
        if not level:
            return

        npcs = level.get_npcs()
        for npc in npcs:
            attrs = self._build_npc_attributes(npc)
            packet = build_nc_npc_attributes(npc.id, attrs)
            await session.player.send_raw(packet)

    def _build_npc_attributes(self, npc: 'NPC') -> Dict[int, Any]:
        """Build NPC attributes dict for NC."""
        return {
            NPCPROP.ID: npc.id,
            NPCPROP.X: npc.x,
            NPCPROP.Y: npc.y,
            NPCPROP.IMAGE: npc.image,
            NPCPROP.SCRIPT: npc.script or "",
            NPCPROP.VISFLAGS: npc.vis_flags,
            NPCPROP.BLOCKFLAGS: npc.block_flags,
        }

    # =========================================================================
    # Class Management Handlers
    # =========================================================================

    async def _handle_class_edit(self, session: NCSession, data: bytes):
        """Handle NC_CLASSEDIT - Edit a class."""
        reader = PacketReader(data)
        class_name = reader.remaining().decode('latin-1', errors='replace')

        session.editing_class = class_name

        # Get class script
        script = ""
        if hasattr(self.server, 'class_manager'):
            cls = self.server.class_manager.get_class(class_name)
            if cls:
                script = cls.script

        packet = build_nc_class_get(class_name, script)
        await session.player.send_raw(packet)

        logger.debug(f"NC {session.player.account_name} editing class {class_name}")

    async def _handle_class_add(self, session: NCSession, data: bytes):
        """Handle NC_CLASSADD - Add/update a class."""
        reader = PacketReader(data)
        class_name = reader.read_string()
        script = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'class_manager'):
            self.server.class_manager.add_class(class_name, script)

        # Notify NC sessions
        packet = build_nc_class_add(class_name)
        await self._broadcast_to_ncs(packet)

        logger.info(f"NC {session.player.account_name} updated class {class_name}")

    async def _handle_class_delete(self, session: NCSession, data: bytes):
        """Handle NC_CLASSDELETE - Delete a class."""
        reader = PacketReader(data)
        class_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'class_manager'):
            self.server.class_manager.remove_class(class_name)

        # Notify NC sessions
        packet = build_nc_class_delete(class_name)
        await self._broadcast_to_ncs(packet)

        logger.info(f"NC {session.player.account_name} deleted class {class_name}")

    # =========================================================================
    # Weapon Management Handlers
    # =========================================================================

    async def _handle_weapon_list_get(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONLISTGET - Get weapon list."""
        weapons = []
        if hasattr(self.server, 'weapon_manager'):
            weapons = self.server.weapon_manager.list_weapons()

        packet = build_nc_weapon_list(weapons)
        await session.player.send_raw(packet)

    async def _handle_weapon_get(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONGET - Get weapon details."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        session.editing_weapon = weapon_name

        image = ""
        script = ""
        if hasattr(self.server, 'weapon_manager'):
            weapon = self.server.weapon_manager.get_weapon(weapon_name)
            if weapon:
                image = weapon.image
                script = weapon.script

        packet = build_nc_weapon_get(weapon_name, image, script)
        await session.player.send_raw(packet)

        logger.debug(f"NC {session.player.account_name} editing weapon {weapon_name}")

    async def _handle_weapon_add(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONADD - Add/update a weapon."""
        reader = PacketReader(data)
        weapon_name = reader.read_string()
        image = reader.read_string()
        script = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'weapon_manager'):
            self.server.weapon_manager.add_weapon(weapon_name, image, script)

        logger.info(f"NC {session.player.account_name} updated weapon {weapon_name}")

    async def _handle_weapon_delete(self, session: NCSession, data: bytes):
        """Handle NC_WEAPONDELETE - Delete a weapon."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'weapon_manager'):
            self.server.weapon_manager.remove_weapon(weapon_name)

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
        attrs = self._build_npc_attributes(npc)
        packet = build_nc_npc_attributes(npc.id, attrs)
        await self._broadcast_to_ncs(packet)

    async def notify_npc_deleted(self, npc_id: int):
        """
        Notify NC sessions that an NPC was deleted.

        Args:
            npc_id: The deleted NPC ID
        """
        packet = build_nc_npc_delete(npc_id)
        await self._broadcast_to_ncs(packet)
