"""
pygserver.server - Main game server implementation

Asyncio-based TCP server that manages player connections,
levels, NPCs, and game state. Full GServer-v2 compatible implementation.
"""

import asyncio
import logging
import math
import time
from typing import Dict, Optional, Set, List
from pathlib import Path

from .attachments import absolute_position, clear_attachment

from .audience import Audience
from .ap_system import regen_step
from .config import ServerConfig
from .player import Player
from .level import Level
from .world import World
from .npc import NPCManager
from .server_flags import ServerFlags
from .protocol.constants import PLTYPE, PLPROP, PLSTATUS
from .admin_access import is_admin_ip, peer_ip
from .protocol.packets import (
    build_flag_del,
    build_flag_set,
    build_npc_del,
    build_world_time,
)

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

        # "Who is affected by this event" - the one spatial/level query service
        # (see audience.py for the shape policies).
        self.audience = Audience(self)

        # NPC management
        self.npc_manager = NPCManager(self)

        # Server flags (custom game state)
        self.server_flags = ServerFlags(self)
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

        # Server-local serverlist-chat / IRC channels
        self.irc_manager = None

        # GS2 clientside bytecode (compiled weapons/classes)
        self.gs2_manager = None

        # List server client
        self.listserver = None

        # Remote IP (set by list server)
        self.remote_ip = ""

        # Asyncio server
        self._server: Optional[asyncio.Server] = None
        self._last_heartbeat = 0.0
        self._last_ap_tick = 0.0
        self._last_flag_save = time.monotonic()

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
        try:
            self.server_flags.save()
        except Exception:
            logger.exception("Failed to save server flags during shutdown")

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
        # from_server_dir sets base_dir (e.g. ../funtimes); fall back to cwd.
        base_path = getattr(self.config, 'base_dir', None) or "."
        self.filesystem = FileSystem(self, base_path)

        # GS2 clientside bytecode
        from .gs2 import GS2ScriptManager
        self.gs2_manager = GS2ScriptManager(self, base_path)
        self.gs2_manager.load()

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

        # Server-local serverlist-chat / IRC channels
        from .irc import IrcManager
        self.irc_manager = IrcManager(self)

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

            # Load individual levels, including nested folders (the
            # reference indexes level dirs recursively - FileSystem.cpp:91).
            # Only .nw files load eagerly; other suffixes resolve on demand.
            for level_file in sorted(self.world._level_manager.level_paths().values()):
                if level_file.suffix.lower() != '.nw':
                    continue
                try:
                    level = Level.load(str(level_file))
                    self.world.add_level(level)
                    await self._register_level_features(level)
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

        # Populate the world with ambient villager NPCs.
        await self._populate_world()

    async def _populate_world(self):
        """Spawn ambient villager NPCs on the start level to fill the world."""
        count = getattr(self.config, 'villager_count', 0)
        if count <= 0:
            return
        if 'VillagerNPC' not in self.npc_manager._script_classes:
            logger.warning("villager_count set but no VillagerNPC script in npcs/")
            return
        level = self.world.get_level(self.config.start_level)
        if not level:
            logger.warning(
                f"Cannot spawn villagers: start level "
                f"{self.config.start_level} not loaded"
            )
            return

        cx, cy = self.config.start_x, self.config.start_y
        for i in range(count):
            # Scatter on a ring around the spawn point, clamped to the level.
            # Radius 9 (was 5): villager sprites are ~3 tiles tall, so a
            # tighter ring packed them into a visual clump around the player.
            angle = (2 * math.pi * i) / count
            x = max(2.0, min(61.0, cx + math.cos(angle) * 9.0))
            y = max(2.0, min(61.0, cy + math.sin(angle) * 9.0))
            self.npc_manager.create_npc(
                name=f"villager_{i}", script_name="VillagerNPC",
                level=level, x=x, y=y,
            )
        logger.info(f"Spawned {count} villager NPC(s) on {level.name}")

    async def _register_level_features(self, level):
        """Register chests/baddies parsed from a level file into the managers."""
        from .protocol.constants import LevelItemType
        from .baddy import BaddyType

        if hasattr(self, 'item_manager') and self.item_manager:
            for chest in level.get_chest_defs():
                try:
                    self.item_manager.add_chest(
                        level, chest['x'], chest['y'],
                        LevelItemType(chest['item']), chest.get('sign', 0)
                    )
                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping chest in {level.name}: {e}")

        if hasattr(self, 'baddy_manager') and self.baddy_manager:
            for baddy in level.get_baddy_defs():
                try:
                    await self.baddy_manager.add_baddy(
                        level, baddy['x'], baddy['y'], BaddyType(baddy['type']),
                        verses=baddy.get('verses'),
                    )
                except (ValueError, KeyError) as e:
                    logger.debug(f"Skipping baddy in {level.name}: {e}")

        # Spawn level NPCs that have an image (visible) and/or a GS1 script.
        # Script-only NPCs (no image) are common, so spawn on either, and run
        # their GS1 via the interpreter.
        if self.npc_manager:
            for ndef in level.get_npc_defs():
                image = ndef.get('image', '') or ''
                code = ndef.get('code', '') or ''
                if not image and not code.strip():
                    continue
                npc = self.npc_manager.create_npc(
                    name="levelnpc", level=level,
                    x=ndef['x'], y=ndef['y']
                )
                npc.file_defined = True
                if image and image != '-':
                    npc.image = image
                if code.strip():
                    self.npc_manager.attach_gs1(npc, code)

    async def resend_level(self, level_name: str,
                           retired_npc_ids=()):
        """Re-warp occupants in place so client level stores are rebuilt.

        A full same-level entry is intentional: client sign, chest, and link
        stores append incremental arrival packets and otherwise retain stale
        or duplicate definitions. Retired NPC ids must be deleted before the
        replacement level state sends its new NPC property packets.
        """
        level = self.world.get_level(level_name)
        if level is None:
            raise FileNotFoundError(f"level not found: {level_name}")
        for player in list(self.get_players_on_level(level.name)):
            for npc_id in retired_npc_ids:
                await player.send_raw(build_npc_del(npc_id))
            await player.warp(level.name, *absolute_position(player))

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
        player.npc_id = 0
        clear_attachment(player)
        manager = getattr(self, 'combat_manager', None)
        if manager is not None:
            manager.discard_held_bomb(player.id)
        if (self.rc_manager and player.logged_in
                and player.connection_type & PLTYPE.ANYCLIENT):
            await self.rc_manager.notify_player_leave(player.id)

        # Remove from listserver
        if self.listserver and player.logged_in:
            await self.listserver.remove_player(player)

        if player.id in self.players:
            del self.players[player.id]

        # Part chat channels + roster pushes (after the players-dict removal
        # so the parted player gets no roster traffic)
        if self.irc_manager:
            await self.irc_manager.remove_player(player)

        # Notify other players in the same contiguous world.
        if player.level:
            await self.broadcast_to_world(
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

                # GServer-v2/server/include/Server.h:537 and
                # GServer-v2/server/src/Server.cpp:427-435: one player step/s.
                if now - self._last_ap_tick >= 1.0:
                    self._last_ap_tick = now
                    await self._regen_ap()

                # Run NPC timers
                await self.npc_manager.tick()

                if time.monotonic() - self._last_flag_save >= 60:
                    self._last_flag_save = time.monotonic()
                    try:
                        self.server_flags.save()
                    except Exception:
                        logger.exception("Failed to save server flags")

                # Sleep briefly
                await asyncio.sleep(0.05)  # 50ms tick rate

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")

    async def _regen_ap(self):
        """Advance AP timers for eligible logged-in players."""
        if not self.config.apsystem:
            return
        # GServer-v2/server/src/player/PlayerClient.cpp:239-263.
        for player in list(self.players.values()):
            if not player.logged_in or not player.level:
                continue
            if (getattr(player, 'client_status', 0) & PLSTATUS.PAUSED
                    or player.level.is_sparring_zone):
                continue
            player.ap, player.ap_counter, changed = regen_step(
                player.ap, player.ap_counter, self.config.aptime)
            if changed:
                await player.send_props({PLPROP.ALIGNMENT: player.ap})

    async def _send_heartbeat(self):
        """Send world time heartbeat to all connected players."""
        packet = build_world_time()
        for player in list(self.players.values()):
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
        await self.audience.broadcast_to_level(level_name, packet, exclude)

    async def broadcast_to_world(self, level_name: str, packet: bytes,
                                 exclude: Optional[Set[int]] = None):
        """Broadcast a packet across a level's containing map."""
        await self.audience.broadcast_to_world(level_name, packet, exclude)

    async def broadcast_to_all(self, packet: bytes, exclude: Optional[Set[int]] = None):
        """Broadcast packet to all logged-in players."""
        exclude = exclude or set()
        for player in list(self.players.values()):
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
        return self.audience.players_on_level(level_name)

    def get_all_players(self) -> List[Player]:
        """Get all logged-in players."""
        return [p for p in self.players.values() if p.logged_in]

    def get_player_count(self) -> int:
        """Get number of logged-in players."""
        return len([p for p in self.players.values() if p.logged_in])

    def is_staff(self, account_name: str) -> bool:
        """Server.cpp:1773: case-insensitive `staff` membership. Grants no rights."""
        if hasattr(self.config, 'staff'):
            return account_name.lower() in [s.lower() for s in self.config.staff]
        return False

    def get_flag(self, name: str) -> str:
        """Get server flag value."""
        return self.server_flags.get(name, "")

    async def set_flag(self, name: str, value: str):
        """Set server flag value."""
        if isinstance(self.server_flags, ServerFlags):
            self.server_flags.put(name, value, notify=False)
        else:
            self.server_flags[name] = value
        await self._broadcast_flag(name, self.server_flags[name])

    async def del_flag(self, name: str):
        """Delete server flag."""
        if isinstance(self.server_flags, ServerFlags):
            if name in self.server_flags:
                self.server_flags.remove(name, notify=False)
        else:
            self.server_flags.pop(name, None)
        await self._broadcast_flag(name, None, deleted=True)

    def flag_is_public(self, name: str) -> bool:
        # GServer-v2 Server.cpp:1939 shares only serverr.* in NPC-server mode.
        return (not getattr(getattr(self, "config", None), "serverside", False)
                or name.startswith("serverr."))

    async def _broadcast_flag(self, name, value, deleted=False):
        if self.flag_is_public(name):
            packet = build_flag_del(name) if deleted else build_flag_set(name, value)
            await self.broadcast_to_all(packet)

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
        from .gs2 import parse_csv_tolerant
        parts = parse_csv_tolerant(action)
        if len(parts) < 2:
            return False

        carrier = parts[0].strip().lower()
        action_type = parts[1].strip()
        params = parts[2:] if len(parts) > 2 else []

        logger.debug(f"Serverside trigger: {action_type} with params {params}")

        if carrier == "serverside" and player is not None and self.gs2_manager is not None:
            weapon = self.gs2_manager.get_weapon(action_type)
            runtime = getattr(weapon, "server_runtime", None)
            if runtime is not None:
                from .gs1_host import run_npc_event
                runtime.level = player.level
                run_npc_event(runtime, "actionserverside", self, player,
                              params=params)
                return True

        if carrier == "servernpc":
            target = next((
                npc for npc in self.npc_manager._npcs.values()
                if npc.name.lower() == action_type.lower()
            ), None)
            if target is None:
                return False
            from .gs1_host import run_npc_event
            run_npc_event(target, "actionserverside", self, player,
                          params=params)
            return True

        if carrier != "serverside" or player is None:
            return False

        # Handle common triggers
        if action_type == "warp":
            # Format: serverside,warp,level,x,y
            if len(params) >= 3:
                level_name = params[0]
                try:
                    dest_x = float(params[1])
                    dest_y = float(params[2])
                except ValueError:
                    return False
                await player.warp(level_name, dest_x, dest_y)
                return True

        elif action_type == "setflag":
            # Format: serverside,setflag,name,value
            if len(params) >= 2:
                flag_name = params[0]
                flag_value = params[1]
                player.set_flag(flag_name, flag_value)
                return True

        elif action_type == "addweapon":
            # Format: serverside,addweapon,name
            if len(params) >= 1:
                weapon_name = params[0]
                player.add_weapon(weapon_name)
                if self.gs2_manager is not None:
                    await self.gs2_manager.announce_weapon(player, weapon_name)
                return True

        elif action_type == "removeweapon":
            # Format: serverside,removeweapon,name
            if len(params) >= 1:
                weapon_name = params[0]
                player.remove_weapon(weapon_name)
                return True

        elif action_type == "giverupees":
            # Format: serverside,giverupees,amount
            if len(params) >= 1:
                try:
                    amount = int(params[0])
                except ValueError:
                    return False
                player.rupees = min(9999, player.rupees + amount)
                return True

        elif action_type == "heal":
            # Format: serverside,heal,amount
            if len(params) >= 1:
                try:
                    amount = float(params[0])
                except ValueError:
                    return False
                player.hearts = min(player.max_hearts, player.hearts + amount)
                return True

        elif action_type == "setlevel":
            # Format: serverside,setlevel,flag,value
            if len(params) >= 2 and player.level:
                flag_name = params[0]
                flag_value = params[1]
                # Set level-specific flag
                pass
                return True
        return False

    def control_login_allowed(self, player: Player) -> bool:
        """Player.cpp:685-694: RC, RC2 and NC need staff AND an admin-IP match.

        Rights are not consulted here; the IP is the real socket peer.
        """
        if not self.is_staff(player.account_name):
            return False
        account = self._account_for(player)
        ip_range = getattr(account, 'admin_ip', '') if account is not None else ''
        return is_admin_ip(peer_ip(player.remote_ip), ip_range)

    def _account_for(self, player: Player):
        if not self.account_manager:
            return None
        return self.account_manager.get_account(player.account_name)

    def _stored_rights(self, player: Player) -> int:
        # Staff membership grants no rights; only the account's stored bits count.
        rights = getattr(self._account_for(player), 'admin_rights', 0)
        return rights if isinstance(rights, int) else 0

    def register_rc_session(self, player: Player) -> bool:
        """Register an RC session if control_login_allowed admits the player."""
        if not self.rc_manager or not self.control_login_allowed(player):
            return False
        rights = self._stored_rights(player)
        self.rc_manager.register_session(player, rights)
        logger.info(f"RC session registered for {player.account_name} with rights {rights}")
        return True

    def register_nc_session(self, player: Player) -> bool:
        """Register an NC session if control_login_allowed admits the player.

        The NPC-control right is not checked: the reference gates only the
        NC address push on it (NPCServer.cpp:120-131), never the NC login or
        the NC packets (PlayerNCPackets.cpp checks isNC() only).
        """
        if not self.nc_manager or not self.control_login_allowed(player):
            return False
        self.nc_manager.register_session(player)
        player.connection_type = PLTYPE.NC
        logger.info(f"NC session registered for {player.account_name}")
        return True


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
