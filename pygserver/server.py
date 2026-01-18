"""
pygserver.server - Main game server implementation

Asyncio-based TCP server that manages player connections,
levels, NPCs, and game state. Full GServer-v2 compatible implementation.
"""

import asyncio
import logging
import time
from typing import Dict, Optional, Set, List
from pathlib import Path

from .config import ServerConfig
from .player import Player
from .level import Level
from .world import World
from .npc import NPCManager
from .protocol.constants import PLTYPE, PLPERM
from .protocol.packets import build_world_time

logger = logging.getLogger(__name__)


class GameServer:
    """
    Main game server class.

    Manages player connections, game state, levels, NPCs, and all
    subsystems (combat, items, baddies, horses, RC, NC, filesystem, accounts).
    Uses asyncio for concurrent connection handling.
    """

    def __init__(self, config: Optional[ServerConfig] = None):
        self.config = config or ServerConfig()
        self.running = False

        # Player management
        self.players: Dict[int, Player] = {}
        self.next_player_id = 2  # IDs 0-1 reserved

        # World/level management
        self.world = World(self)

        # NPC management
        self.npc_manager = NPCManager(self)

        # Server flags (custom game state)
        self.server_flags: Dict[str, str] = {}
        self.flags = self.server_flags  # Alias for compatibility

        # Combat system (bombs, arrows, damage)
        self.combat_manager = None

        # Item system (ground items, chests)
        self.item_manager = None

        # Baddy system (enemies, AI)
        self.baddy_manager = None

        # Horse system (mounting)
        self.horse_manager = None

        # RC (Remote Control) admin system
        self.rc_manager = None

        # NC (NPC Control) system
        self.nc_manager = None

        # File system (file serving)
        self.filesystem = None

        # Account system (persistence)
        self.account_manager = None

        # Profile manager
        self.profile_manager = None

        # Class manager for NPC classes
        self.class_manager = None

        # Weapon manager
        self.weapon_manager = None

        # List server client
        self.listserver = None

        # Remote IP (set by list server)
        self.remote_ip = ""

        # Asyncio server
        self._server: Optional[asyncio.Server] = None
        self._last_heartbeat = 0.0

    async def start(self):
        """Start the game server."""
        logger.info(f"Starting {self.config.name} on {self.config.host}:{self.config.port}")

        # Initialize all subsystems
        await self._init_subsystems()

        # Load world data
        await self._load_world()

        # Start TCP server
        self._server = await asyncio.start_server(
            self._handle_connection,
            self.config.host,
            self.config.port
        )

        self.running = True
        logger.info(f"Server listening on {self.config.host}:{self.config.port}")

        # Run main loop
        async with self._server:
            await self._main_loop()

    async def stop(self):
        """Stop the game server."""
        logger.info("Stopping server...")
        self.running = False

        # Disconnect all players
        for player in list(self.players.values()):
            await player.disconnect()

        # Stop subsystems
        await self._stop_subsystems()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("Server stopped")

    async def _init_subsystems(self):
        """Initialize all server subsystems."""
        logger.info("Initializing subsystems...")

        # Combat system
        from .combat import CombatManager
        self.combat_manager = CombatManager(self)
        await self.combat_manager.start()

        # Item system
        from .items import ItemManager
        self.item_manager = ItemManager(self)
        await self.item_manager.start()

        # Baddy system
        from .baddy import BaddyManager
        self.baddy_manager = BaddyManager(self)
        await self.baddy_manager.start()

        # Horse system
        from .horse import HorseManager
        self.horse_manager = HorseManager(self)
        await self.horse_manager.start()

        # RC (Remote Control) system
        from .rc import RCManager
        self.rc_manager = RCManager(self)

        # NC (NPC Control) system
        from .nc import NCManager
        self.nc_manager = NCManager(self)

        # File system
        from .filesystem import FileSystem
        base_path = self.config.server_dir if hasattr(self.config, 'server_dir') else "."
        self.filesystem = FileSystem(self, base_path)

        # Account system
        from .account import AccountManager, ProfileManager
        accounts_dir = self.config.accounts_dir if hasattr(self.config, 'accounts_dir') else "accounts"
        self.account_manager = AccountManager(self, accounts_dir)
        await self.account_manager.start()

        # Set staff list
        if hasattr(self.config, 'staff'):
            self.account_manager.set_staff_list(self.config.staff)

        # Profile manager
        self.profile_manager = ProfileManager(self)

        # List server client
        from .listserver import ServerListClient
        self.listserver = ServerListClient(self)
        await self.listserver.start()

        logger.info("All subsystems initialized")

    async def _stop_subsystems(self):
        """Stop all server subsystems."""
        logger.info("Stopping subsystems...")

        if self.combat_manager:
            await self.combat_manager.stop()

        if self.item_manager:
            await self.item_manager.stop()

        if self.baddy_manager:
            await self.baddy_manager.stop()

        if self.horse_manager:
            await self.horse_manager.stop()

        if self.account_manager:
            await self.account_manager.stop()

        if self.listserver:
            await self.listserver.stop()

        logger.info("All subsystems stopped")

    async def _load_world(self):
        """Load world data (levels, GMAPs, NPCs, etc.)."""
        from .world import GMap

        levels_path = Path(self.config.levels_dir)
        if levels_path.exists():
            logger.info(f"Loading levels from {levels_path}")

            # Load individual levels
            for level_file in levels_path.glob("*.nw"):
                try:
                    level = Level.load(str(level_file))
                    self.world.add_level(level)
                    logger.debug(f"Loaded level: {level.name}")
                except Exception as e:
                    logger.warning(f"Failed to load level {level_file}: {e}")

            # Load GMAPs
            for gmap_name in self.config.gmaps:
                gmap_path = levels_path / gmap_name
                if gmap_path.exists():
                    try:
                        gmap = GMap.load(str(gmap_path))
                        self.world.add_gmap(gmap)
                        logger.info(f"Loaded GMAP: {gmap.name} ({gmap.width}x{gmap.height})")
                    except Exception as e:
                        logger.warning(f"Failed to load GMAP {gmap_name}: {e}")

        # Load NPCs from scripts
        npcs_path = Path(self.config.npcs_dir)
        if npcs_path.exists():
            logger.info(f"Loading NPC scripts from {npcs_path}")
            await self.npc_manager.load_scripts(npcs_path)

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter):
        """Handle a new client connection."""
        addr = writer.get_extra_info('peername')
        logger.info(f"New connection from {addr}")

        # Create player
        player_id = self._allocate_player_id()
        if player_id is None:
            logger.warning(f"Server full, rejecting connection from {addr}")
            writer.close()
            await writer.wait_closed()
            return

        player = Player(self, player_id, reader, writer)
        self.players[player_id] = player

        try:
            await player.run()
        except Exception as e:
            logger.error(f"Player {player_id} error: {e}")
        finally:
            await self._remove_player(player)

    def _allocate_player_id(self) -> Optional[int]:
        """Allocate a new player ID."""
        if len(self.players) >= self.config.max_players:
            return None

        # Find available ID (2-15999)
        for i in range(2, 16000):
            if i not in self.players:
                return i

        return None

    async def _remove_player(self, player: Player):
        """Remove a player from the server."""
        # Remove from listserver
        if self.listserver and player.logged_in:
            await self.listserver.remove_player(player)

        if player.id in self.players:
            del self.players[player.id]

        # Notify other players on same level
        if player.level:
            await self.broadcast_to_level(
                player.level.name,
                player.build_leave_packet(),
                exclude={player.id}
            )

        logger.info(f"Player {player.id} ({player.account_name}) disconnected")

    async def _main_loop(self):
        """Main server loop for periodic tasks."""
        while self.running:
            try:
                now = time.time()

                # Send heartbeat to all players
                if now - self._last_heartbeat >= self.config.heartbeat_interval:
                    self._last_heartbeat = now
                    await self._send_heartbeat()

                # Run NPC timers
                await self.npc_manager.tick()

                # Sleep briefly
                await asyncio.sleep(0.05)  # 50ms tick rate

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")

    async def _send_heartbeat(self):
        """Send world time heartbeat to all connected players."""
        packet = build_world_time()
        for player in self.players.values():
            if player.logged_in:
                await player.send_raw(packet)

    async def broadcast_to_level(self, level_name: str, packet: bytes,
                                  exclude: Optional[Set[int]] = None):
        """
        Broadcast packet to all players on a level.

        Args:
            level_name: Name of the level
            packet: Packet data to send
            exclude: Set of player IDs to exclude
        """
        exclude = exclude or set()
        for player in self.players.values():
            if player.logged_in and player.level and player.level.name == level_name:
                if player.id not in exclude:
                    await player.send_raw(packet)

    async def broadcast_to_all(self, packet: bytes, exclude: Optional[Set[int]] = None):
        """Broadcast packet to all logged-in players."""
        exclude = exclude or set()
        for player in self.players.values():
            if player.logged_in and player.id not in exclude:
                await player.send_raw(packet)

    async def broadcast_to_rcs(self, packet: bytes, exclude: Optional[Set[int]] = None):
        """Broadcast packet to all RC sessions."""
        if self.rc_manager:
            await self.rc_manager.broadcast_to_rcs(packet, exclude)

    async def broadcast_to_ncs(self, packet: bytes, exclude: Optional[Set[int]] = None):
        """Broadcast packet to all NC sessions."""
        if self.nc_manager:
            await self.nc_manager._broadcast_to_ncs(packet, exclude)

    def get_player(self, player_id: int) -> Optional[Player]:
        """Get player by ID."""
        return self.players.get(player_id)

    def get_player_by_name(self, name: str) -> Optional[Player]:
        """Get player by account name."""
        name_lower = name.lower()
        for player in self.players.values():
            if player.account_name.lower() == name_lower:
                return player
        return None

    def get_players_on_level(self, level_name: str) -> List[Player]:
        """Get all players on a level."""
        return [
            p for p in self.players.values()
            if p.logged_in and p.level and p.level.name == level_name
        ]

    def get_all_players(self) -> List[Player]:
        """Get all logged-in players."""
        return [p for p in self.players.values() if p.logged_in]

    def get_player_count(self) -> int:
        """Get number of logged-in players."""
        return len([p for p in self.players.values() if p.logged_in])

    def is_staff(self, account_name: str) -> bool:
        """Check if account is staff."""
        if hasattr(self.config, 'staff'):
            return account_name.lower() in [s.lower() for s in self.config.staff]
        return False

    def get_flag(self, name: str) -> str:
        """Get server flag value."""
        return self.server_flags.get(name, "")

    def set_flag(self, name: str, value: str):
        """Set server flag value."""
        self.server_flags[name] = value

    def del_flag(self, name: str):
        """Delete server flag."""
        self.server_flags.pop(name, None)

    async def handle_trigger_action(self, player: Player, x: float, y: float, action: str):
        """
        Handle a serverside trigger action.

        Args:
            player: Player who triggered
            x: X position
            y: Y position
            action: Action string (e.g., "serverside,action,param1,param2")
        """
        # Parse action
        parts = action.split(',')
        if len(parts) < 2:
            return

        action_type = parts[1] if len(parts) > 1 else ""
        params = parts[2:] if len(parts) > 2 else []

        logger.debug(f"Serverside trigger: {action_type} with params {params}")

        # Handle common triggers
        if action_type == "warp":
            # Format: serverside,warp,level,x,y
            if len(params) >= 3:
                level_name = params[0]
                dest_x = float(params[1])
                dest_y = float(params[2])
                await player.warp(level_name, dest_x, dest_y)

        elif action_type == "setflag":
            # Format: serverside,setflag,name,value
            if len(params) >= 2:
                flag_name = params[0]
                flag_value = params[1]
                player.set_flag(flag_name, flag_value)

        elif action_type == "addweapon":
            # Format: serverside,addweapon,name
            if len(params) >= 1:
                weapon_name = params[0]
                player.add_weapon(weapon_name)

        elif action_type == "removeweapon":
            # Format: serverside,removeweapon,name
            if len(params) >= 1:
                weapon_name = params[0]
                player.remove_weapon(weapon_name)

        elif action_type == "giverupees":
            # Format: serverside,giverupees,amount
            if len(params) >= 1:
                amount = int(params[0])
                player.rupees = min(9999, player.rupees + amount)

        elif action_type == "heal":
            # Format: serverside,heal,amount
            if len(params) >= 1:
                amount = float(params[0])
                player.hearts = min(player.max_hearts, player.hearts + amount)

        elif action_type == "setlevel":
            # Format: serverside,setlevel,flag,value
            if len(params) >= 2 and player.level:
                flag_name = params[0]
                flag_value = params[1]
                # Set level-specific flag
                pass

        # Let NPCs handle their own triggers
        await self.npc_manager.on_trigger_action(player, x, y, action)

    def register_rc_session(self, player: Player) -> bool:
        """
        Register a player as an RC (Remote Control) admin.

        Args:
            player: Player to register

        Returns:
            True if registered successfully
        """
        if not self.rc_manager:
            return False

        # Get rights from account
        rights = 0
        if self.account_manager:
            account = self.account_manager.get_account(player.account_name)
            if account:
                rights = account.admin_rights

        # Also check staff config
        if self.is_staff(player.account_name):
            rights |= PLPERM.ALL if hasattr(PLPERM, 'ALL') else 0xFFFFF

        if rights > 0:
            self.rc_manager.register_session(player, rights)
            player.connection_type = PLTYPE.RC
            logger.info(f"RC session registered for {player.account_name} with rights {rights}")
            return True

        return False

    def register_nc_session(self, player: Player) -> bool:
        """
        Register a player as an NC (NPC Control) user.

        Args:
            player: Player to register

        Returns:
            True if registered successfully
        """
        if not self.nc_manager:
            return False

        # Check for NC rights
        rights = 0
        if self.account_manager:
            account = self.account_manager.get_account(player.account_name)
            if account:
                rights = account.admin_rights

        if self.is_staff(player.account_name) or (rights & PLPERM.NPCCONTROL):
            self.nc_manager.register_session(player)
            player.connection_type = PLTYPE.NC
            logger.info(f"NC session registered for {player.account_name}")
            return True

        return False


async def run_server(config_path: Optional[str] = None):
    """
    Run the game server.

    Args:
        config_path: Path to serveroptions.txt file
    """
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )

    # Load config
    if config_path:
        config = ServerConfig.from_file(config_path)
    else:
        config = ServerConfig()

    # Create and start server
    server = GameServer(config)
    try:
        await server.start()
    except KeyboardInterrupt:
        await server.stop()


def main():
    """Entry point for running server from command line."""
    import sys
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run_server(config_path))


if __name__ == "__main__":
    main()
