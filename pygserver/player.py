"""
pygserver.player - Player connection and state management

Handles individual player connections, login, packet handling,
and player state. Implements all PLI packet handlers.
"""

import asyncio
import logging
import struct
import time
from typing import TYPE_CHECKING, Optional, Dict, Any, List, Set

from .protocol.codec import ServerCodec, PacketBuffer
from .protocol.constants import PLI, PLO, PLPROP, PLTYPE, PLPERM
from .protocol.packets import (
    PacketReader,
    PacketBuilder,
    parse_login_packet,
    parse_player_props,
    parse_level_warp,
    parse_trigger_action,
    parse_hurt_player,
    parse_npc_props,
    build_player_props,
    build_other_player_props,
    build_warp,
    build_warp2,
    build_chat,
    build_player_left,
    build_level_name,
    build_raw_data_announcement,
    build_private_message,
    build_flag_set,
    build_flag_del,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .level import Level

logger = logging.getLogger(__name__)


class Player:
    """
    Represents a connected player.

    Handles connection I/O, login, packet dispatch, and player state.
    Implements all PLI (Player Input) packet handlers.
    """

    def __init__(self, server: 'GameServer', player_id: int,
                 reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.server = server
        self.id = player_id
        self._reader = reader
        self._writer = writer

        # Connection state
        self.connected = True
        self.logged_in = False
        self._codec: Optional[ServerCodec] = None
        self._buffer = PacketBuffer()

        # Connection type (CLIENT, RC, NC, NPCSERVER)
        self.connection_type = PLTYPE.CLIENT

        # Player identity
        self.account_name = ""
        self.nickname = ""
        self.guild_name = ""
        self.guild_nickname = ""

        # Current level
        self.level: Optional['Level'] = None

        # Position (in tiles)
        self.x = 0.0
        self.y = 0.0
        self.direction = 2  # Down

        # Stats
        self.hearts = 3.0
        self.max_hearts = 3.0
        self.rupees = 0
        self.arrows = 10
        self.bombs = 5
        self.glove_power = 0
        self.sword_power = 1
        self.shield_power = 1

        # Combat stats
        self.kills = 0
        self.deaths = 0

        # Appearance
        self.head_image = "head19.png"
        self.body_image = "body.png"
        self.sword_image = "sword1.png"
        self.shield_image = "shield1.png"
        self.colors = [0, 0, 0, 0, 0]  # Skin, coat, sleeve, shoe, belt

        # Animation
        self.gani = "idle"
        self.sprite = 0

        # Chat
        self.chat = ""

        # Flags
        self.flags: Dict[str, str] = {}

        # GATTRIBS (custom attributes)
        self.gattribs: Dict[int, str] = {}

        # Weapons
        self.weapons: List[str] = []

        # Status
        self.is_frozen = False
        self.is_ghost = False
        self.is_muted = False

        # Admin
        self.admin_rights = 0

        # Session timing
        self.login_time = 0.0
        self.last_packet_time = 0.0

        # PLI Packet handlers
        self._handlers = {
            # Movement and position
            PLI.LEVELWARP: self._handle_level_warp,
            PLI.LEVELWARPMOD: self._handle_level_warp_mod,
            PLI.PLAYERPROPS: self._handle_player_props,
            PLI.ADJACENTLEVEL: self._handle_adjacent_level,

            # Combat
            PLI.BOMBADD: self._handle_bomb_add,
            PLI.BOMBDEL: self._handle_bomb_del,
            PLI.ARROWADD: self._handle_arrow_add,
            PLI.FIRESPY: self._handle_fire_spy,
            PLI.THROWCARRIED: self._handle_throw_carried,
            PLI.HURTPLAYER: self._handle_hurt_player,
            PLI.EXPLOSION: self._handle_explosion,
            PLI.HITOBJECTS: self._handle_hit_objects,
            PLI.SHOOT: self._handle_shoot,
            PLI.SHOOT2: self._handle_shoot2,

            # Items
            PLI.ITEMADD: self._handle_item_add,
            PLI.ITEMDEL: self._handle_item_del,
            PLI.ITEMTAKE: self._handle_item_take,
            PLI.OPENCHEST: self._handle_open_chest,

            # Horse
            PLI.HORSEADD: self._handle_horse_add,
            PLI.HORSEDEL: self._handle_horse_del,

            # Baddies
            PLI.BADDYPROPS: self._handle_baddy_props,
            PLI.BADDYHURT: self._handle_baddy_hurt,
            PLI.BADDYADD: self._handle_baddy_add,

            # NPCs
            PLI.NPCPROPS: self._handle_npc_props,
            PLI.PUTNPC: self._handle_put_npc,
            PLI.NPCDEL: self._handle_npc_del,
            PLI.NPCWEAPONDEL: self._handle_npc_weapon_del,

            # Communication
            PLI.TOALL: self._handle_chat,
            PLI.PRIVATEMESSAGE: self._handle_private_message,
            PLI.SHOWIMG: self._handle_show_img,

            # Flags
            PLI.FLAGSET: self._handle_flag_set,
            PLI.FLAGDEL: self._handle_flag_del,

            # Triggers
            PLI.TRIGGERACTION: self._handle_trigger_action,

            # Files
            PLI.WANTFILE: self._handle_want_file,
            PLI.UPDATEFILE: self._handle_update_file,
            PLI.VERIFYWANTSEND: self._handle_verify_want_send,
            PLI.UPDATEGANI: self._handle_update_gani,
            PLI.UPDATESCRIPT: self._handle_update_script,
            PLI.UPDATECLASS: self._handle_update_class,

            # Weapons
            PLI.WEAPONADD: self._handle_weapon_add,

            # Board
            PLI.BOARDMODIFY: self._handle_board_modify,
            PLI.REQUESTUPDATEBOARD: self._handle_request_update_board,

            # Profile
            PLI.PROFILEGET: self._handle_profile_get,
            PLI.PROFILESET: self._handle_profile_set,

            # Server/misc
            PLI.MAPINFO: self._handle_map_info,
            PLI.SERVERWARP: self._handle_server_warp,
            PLI.PACKETCOUNT: self._handle_packet_count,
            PLI.LANGUAGE: self._handle_language,
            PLI.MUTEPLAYER: self._handle_mute_player,
            PLI.PROCESSLIST: self._handle_process_list,
            PLI.CLAIMPKER: self._handle_claim_pker,
            PLI.RAWDATA: self._handle_raw_data,

            # Text/variables
            PLI.REQUESTTEXT: self._handle_request_text,
            PLI.SENDTEXT: self._handle_send_text,

            # NPC Server query
            PLI.NPCSERVERQUERY: self._handle_npc_server_query,
        }

    async def run(self):
        """Main player loop - handle packets until disconnect."""
        try:
            # Wait for login packet
            if not await self._handle_login():
                return

            # Main packet loop
            while self.connected:
                try:
                    data = await asyncio.wait_for(
                        self._reader.read(65536),
                        timeout=300.0  # 5 minute timeout
                    )
                except asyncio.TimeoutError:
                    logger.info(f"Player {self.id} timed out")
                    break

                if not data:
                    break

                self.last_packet_time = time.time()
                await self._process_data(data)

        except ConnectionResetError:
            pass
        except Exception as e:
            logger.error(f"Player {self.id} error: {e}")
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Clean up player resources on disconnect."""
        self.connected = False

        # Leave current level
        if self.level:
            self.level.remove_player(self)
            await self.server.broadcast_to_level(
                self.level.name, build_player_left(self.id), exclude={self.id}
            )

        # Dismount horse if mounted
        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_dismount(self)

        # Save account
        if hasattr(self.server, 'account_manager'):
            account = self.server.account_manager.get_account(self.account_name)
            if account:
                self.server.account_manager.save_player_to_account(self, account)

        # Unregister RC/NC sessions
        if hasattr(self.server, 'rc_manager'):
            self.server.rc_manager.unregister_session(self.id)
        if hasattr(self.server, 'nc_manager'):
            self.server.nc_manager.unregister_session(self.id)

    async def disconnect(self, message: str = ""):
        """Disconnect the player."""
        if message:
            from .protocol.packets import build_disc_message
            try:
                packet = build_disc_message(message)
                await self.send_raw(packet)
            except:
                pass

        self.connected = False
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except:
            pass

    async def _handle_login(self) -> bool:
        """Handle the initial login packet."""
        try:
            # Read login packet (plain zlib compressed)
            data = await asyncio.wait_for(self._reader.read(65536), timeout=30.0)
            if not data or len(data) < 2:
                return False

            # Extract length and packet
            length = struct.unpack('>H', data[:2])[0]
            packet_data = data[2:2 + length]

            # Create codec with no encryption key yet
            self._codec = ServerCodec(0)
            decrypted = self._codec.decode_packet(packet_data)
            if not decrypted:
                logger.warning(f"Failed to decode login from {self.id}")
                return False

            # Parse login
            login = parse_login_packet(decrypted)
            logger.info(f"Login from {login.get('username', '?')}, protocol={login.get('protocol', '?')}")

            # Verify protocol
            protocol = login.get('protocol', '')
            if protocol not in ['G3D0311C', 'G3D0511C', 'GNW03014']:
                logger.warning(f"Unsupported protocol: {protocol}")

            # Set encryption key
            encryption_key = login.get('encryption_key', 0)
            self._codec.set_key(encryption_key)

            # Store account info
            self.account_name = login.get('username', f'player_{self.id}')
            self.nickname = self.account_name

            # Load account data if available
            logger.debug(f"Loading account data for {self.account_name}")
            if hasattr(self.server, 'account_manager'):
                account = self.server.account_manager.get_account(self.account_name)
                if not account:
                    account = self.server.account_manager.create_account(self.account_name)
                self.server.account_manager.load_player_from_account(self, account)
                self.admin_rights = account.admin_rights
            logger.debug(f"Account data loaded for {self.account_name}")

            # Check if banned
            logger.debug(f"Checking ban status for {self.account_name}")
            if hasattr(self.server, 'account_manager'):
                account = self.server.account_manager.get_account(self.account_name)
                if account and account.is_banned:
                    await self.disconnect(f"You are banned: {account.ban_reason}")
                    return False
            logger.debug(f"Ban check passed for {self.account_name}")

            # Verify login (skip if noverifylogin)
            logger.debug(f"Verify login: {self.server.config.verify_login}")
            if self.server.config.verify_login:
                password = login.get('password', '')
                if hasattr(self.server, 'account_manager'):
                    if not self.server.account_manager.verify_password(self.account_name, password):
                        await self.disconnect("Invalid password")
                        return False
            logger.debug(f"Login verification passed for {self.account_name}")

            # Send login response (may be delayed if using listserver verification)
            if self.server.config.verify_login and hasattr(self.server, 'listserver') and self.server.listserver:
                # Use listserver for account verification
                logger.debug(f"Requesting account verification from listserver for {self.account_name}")
                await self.server.listserver.verify_account(self, password)
                # send_login will be called by listserver on success
                return True
            else:
                # Local verification or no verification
                logger.debug(f"About to send login response for {self.account_name}")
                await self._send_login_response()

            self.logged_in = True
            self.login_time = time.time()
            logger.info(f"Player {self.id} logged in as {self.account_name}")

            # Add player to listserver
            if hasattr(self.server, 'listserver') and self.server.listserver:
                await self.server.listserver.add_player(self)

            # Warp to start level
            logger.debug(f"Warping player {self.id} to {self.server.config.start_level}")
            await self.warp(
                self.server.config.start_level,
                self.server.config.start_x,
                self.server.config.start_y
            )
            logger.debug(f"Warp complete for player {self.id}")

            return True

        except asyncio.TimeoutError:
            logger.warning(f"Login timeout for {self.id}")
            return False
        except Exception as e:
            import traceback
            logger.error(f"Login error for {self.id}: {e}")
            logger.error(traceback.format_exc())
            return False

    async def send_login(self):
        """Send login data to player (called after verification)."""
        await self._send_login_response()

        # Mark as logged in
        self.logged_in = True
        self.login_time = time.time()
        logger.info(f"Player {self.id} logged in as {self.account_name}")

        # Add player to listserver
        if hasattr(self.server, 'listserver') and self.server.listserver:
            await self.server.listserver.add_player(self)

        # Warp to start level
        await self.warp(
            self.server.config.start_level,
            self.server.config.start_x,
            self.server.config.start_y
        )

    async def _send_login_response(self):
        """Send the login response packet."""
        logger.debug(f"Sending login response for player {self.id}")

        # Add PLO_PLAYERPROPS with initial state
        props = {
            PLPROP.NICKNAME: self.nickname,
            PLPROP.MAXPOWER: int(self.max_hearts * 2),
            PLPROP.CURPOWER: int(self.hearts * 2),
            PLPROP.RUPEESCOUNT: self.rupees,
            PLPROP.ARROWSCOUNT: self.arrows,
            PLPROP.BOMBSCOUNT: self.bombs,
            PLPROP.GLOVEPOWER: self.glove_power,
            PLPROP.SWORDPOWER: self.sword_power,
            PLPROP.SHIELDPOWER: self.shield_power,
            PLPROP.HEADIMAGE: self.head_image,
            PLPROP.BODYIMAGE: self.body_image,
            PLPROP.ACCOUNTNAME: self.account_name,
        }

        packet = build_player_props(props)
        logger.debug(f"Built player props packet, length={len(packet)}")
        encoded = self._codec.encode_packet(packet, is_login_response=True)
        logger.debug(f"Encoded packet, length={len(encoded)}")
        self._writer.write(encoded)
        await self._writer.drain()
        logger.debug(f"Login response sent for player {self.id}")

    async def _process_data(self, data: bytes):
        """Process received data."""
        self._buffer.add_data(data)

        for packet_data in self._buffer.get_packets():
            decrypted = self._codec.decode_packet(packet_data)
            if not decrypted:
                continue

            await self._handle_packets(decrypted)

    async def _handle_packets(self, data: bytes):
        """Handle decoded packet data (may contain multiple packets)."""
        pos = 0
        while pos < len(data):
            # Find newline
            newline = data.find(b'\n', pos)
            if newline == -1:
                break

            packet_bytes = data[pos:newline]
            pos = newline + 1

            if len(packet_bytes) < 1:
                continue

            # Extract packet ID
            packet_id = packet_bytes[0] - 32
            packet_body = packet_bytes[1:] if len(packet_bytes) > 1 else b""

            # Check for RC/NC packets
            if packet_id >= 51 and packet_id <= 98:
                # RC packet
                if hasattr(self.server, 'rc_manager'):
                    await self.server.rc_manager.handle_packet(self, packet_id, packet_body)
                continue
            elif packet_id >= 103 and packet_id <= 119:
                # NC packet
                if hasattr(self.server, 'nc_manager'):
                    await self.server.nc_manager.handle_packet(self, packet_id, packet_body)
                continue
            elif packet_id in [150, 151]:
                # NC level list packets
                if hasattr(self.server, 'nc_manager'):
                    await self.server.nc_manager.handle_packet(self, packet_id, packet_body)
                continue

            # Dispatch to handler
            handler = self._handlers.get(packet_id)
            if handler:
                try:
                    await handler(packet_body)
                except Exception as e:
                    logger.error(f"Packet handler error (id={packet_id}): {e}")

    # =========================================================================
    # Movement/Position Handlers
    # =========================================================================

    async def _handle_level_warp(self, data: bytes):
        """Handle PLI_LEVELWARP packet."""
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()

        if level_name:
            await self.warp(level_name, x, y)

    async def _handle_level_warp_mod(self, data: bytes):
        """Handle PLI_LEVELWARPMOD packet (modified warp)."""
        await self._handle_level_warp(data)

    async def _handle_player_props(self, data: bytes):
        """Handle PLI_PLAYERPROPS packet."""
        props = parse_player_props(data)

        # Update position
        if PLPROP.X2 in props:
            self.x = props[PLPROP.X2]
        elif PLPROP.X in props:
            self.x = props[PLPROP.X] / 2.0

        if PLPROP.Y2 in props:
            self.y = props[PLPROP.Y2]
        elif PLPROP.Y in props:
            self.y = props[PLPROP.Y] / 2.0

        if PLPROP.DIRECTION in props:
            self.direction = props[PLPROP.DIRECTION]

        if PLPROP.SPRITE in props:
            self.sprite = props[PLPROP.SPRITE]

        if PLPROP.GANI in props:
            self.gani = props[PLPROP.GANI]

        # Appearance updates
        if PLPROP.HEADIMAGE in props:
            self.head_image = props[PLPROP.HEADIMAGE]
        if PLPROP.BODYIMAGE in props:
            self.body_image = props[PLPROP.BODYIMAGE]

        # Broadcast to other players on level
        if self.level:
            broadcast_props = {}
            for key in [PLPROP.X2, PLPROP.Y2, PLPROP.DIRECTION, PLPROP.SPRITE, PLPROP.GANI]:
                if key in props:
                    broadcast_props[key] = getattr(self, {
                        PLPROP.X2: 'x',
                        PLPROP.Y2: 'y',
                        PLPROP.DIRECTION: 'direction',
                        PLPROP.SPRITE: 'sprite',
                        PLPROP.GANI: 'gani'
                    }[key])

            if broadcast_props:
                packet = build_other_player_props(self.id, broadcast_props)
                await self.server.broadcast_to_level(
                    self.level.name, packet, exclude={self.id}
                )

    async def _handle_adjacent_level(self, data: bytes):
        """Handle PLI_ADJACENTLEVEL packet (request for adjacent level data)."""
        reader = PacketReader(data)
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()
        # Client requesting adjacent level for preloading - typically ignored

    # =========================================================================
    # Combat Handlers
    # =========================================================================

    async def _handle_bomb_add(self, data: bytes):
        """Handle PLI_BOMBADD packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        power = reader.read_gchar() if reader.remaining() else 1
        time_left = reader.read_gchar() / 10.0 if reader.remaining() else 3.0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_bomb_add(self, x, y, power, time_left)

    async def _handle_bomb_del(self, data: bytes):
        """Handle PLI_BOMBDEL packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_bomb_del(self, x, y)

    async def _handle_arrow_add(self, data: bytes):
        """Handle PLI_ARROWADD packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        direction = reader.read_gchar() if reader.remaining() else self.direction

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_arrow_add(self, x, y, direction)

    async def _handle_fire_spy(self, data: bytes):
        """Handle PLI_FIRESPY packet (fire from wand)."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_fire_spy(self, x, y)

    async def _handle_throw_carried(self, data: bytes):
        """Handle PLI_THROWCARRIED packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        carried_type = reader.read_gchar() if reader.remaining() else 0

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_throw_carried(self, x, y, carried_type)

    async def _handle_hurt_player(self, data: bytes):
        """Handle PLI_HURTPLAYER packet."""
        reader = PacketReader(data)
        target_id = reader.read_gshort()
        power = reader.read_gchar()
        from_x = reader.read_gchar() / 2.0 if reader.remaining() else self.x
        from_y = reader.read_gchar() / 2.0 if reader.remaining() else self.y

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_hurt_player(self, target_id, power, from_x, from_y)

    async def _handle_explosion(self, data: bytes):
        """Handle PLI_EXPLOSION packet."""
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        radius = reader.read_gchar() / 2.0 if reader.remaining() else 2.0
        power = reader.read_gchar() if reader.remaining() else 2

        # Broadcast explosion effect
        from .protocol.packets import build_explosion
        packet = build_explosion(x, y, radius, power)
        await self.server.broadcast_to_level(self.level.name, packet)

    async def _handle_hit_objects(self, data: bytes):
        """Handle PLI_HITOBJECTS packet."""
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        power = reader.read_gchar()

        # Parse object IDs
        objects = []
        while reader.remaining():
            obj_id = reader.read_gint3()
            objects.append(obj_id)

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_hit_objects(self, x, y, power, objects)

    async def _handle_shoot(self, data: bytes):
        """Handle PLI_SHOOT packet."""
        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_shoot(self, data)

    async def _handle_shoot2(self, data: bytes):
        """Handle PLI_SHOOT2 packet."""
        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_shoot2(self, data)

    # =========================================================================
    # Item Handlers
    # =========================================================================

    async def _handle_item_add(self, data: bytes):
        """Handle PLI_ITEMADD packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        item_type = reader.read_gchar() if reader.remaining() else 0

        if hasattr(self.server, 'item_manager'):
            from .protocol.constants import LevelItemType
            await self.server.item_manager.spawn_item(
                self.level, x, y, LevelItemType(item_type)
            )

    async def _handle_item_del(self, data: bytes):
        """Handle PLI_ITEMDEL packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.remove_item(self.level.name, x, y)

    async def _handle_item_take(self, data: bytes):
        """Handle PLI_ITEMTAKE packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.handle_item_take(self, x, y)

    async def _handle_open_chest(self, data: bytes):
        """Handle PLI_OPENCHEST packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar()
        y = reader.read_gchar()

        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.handle_open_chest(self, x, y)

    # =========================================================================
    # Horse Handlers
    # =========================================================================

    async def _handle_horse_add(self, data: bytes):
        """Handle PLI_HORSEADD packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        direction = reader.read_gchar() if reader.remaining() else 2
        bushes = reader.read_gchar() if reader.remaining() else 3
        image = reader.remaining().decode('latin-1', errors='replace') if reader.remaining() else "horse.png"

        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_horse_add_packet(
                self, x, y, direction, bushes, image
            )

    async def _handle_horse_del(self, data: bytes):
        """Handle PLI_HORSEDEL packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_horse_del_packet(self, x, y)

    # =========================================================================
    # Baddy Handlers
    # =========================================================================

    async def _handle_baddy_props(self, data: bytes):
        """Handle PLI_BADDYPROPS packet."""
        # Client updating baddy props (usually from server-controlled baddies)
        pass

    async def _handle_baddy_hurt(self, data: bytes):
        """Handle PLI_BADDYHURT packet."""
        if not self.level:
            return
        reader = PacketReader(data)
        baddy_id = reader.read_gint3()
        damage = reader.read_gchar() if reader.remaining() else 1

        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.handle_baddy_hurt(self, baddy_id, damage)

    async def _handle_baddy_add(self, data: bytes):
        """Handle PLI_BADDYADD packet (admin adding baddy)."""
        if not self.level:
            return
        if not self.admin_rights & PLPERM.SETATTRIBUTES:
            return

        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        baddy_type = reader.read_gchar() if reader.remaining() else 0

        if hasattr(self.server, 'baddy_manager'):
            from .baddy import BaddyType
            await self.server.baddy_manager.add_baddy(
                self.level, x, y, BaddyType(baddy_type)
            )

    # =========================================================================
    # NPC Handlers
    # =========================================================================

    async def _handle_npc_props(self, data: bytes):
        """Handle PLI_NPCPROPS packet."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()

        npc = self.server.npc_manager.get_npc(npc_id)
        if npc and npc.level == self.level:
            # Process NPC prop updates from client
            pass

    async def _handle_put_npc(self, data: bytes):
        """Handle PLI_PUTNPC packet (place NPC)."""
        if not self.level:
            return
        # Usually requires admin rights
        pass

    async def _handle_npc_del(self, data: bytes):
        """Handle PLI_NPCDEL packet."""
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        # Usually requires admin rights
        pass

    async def _handle_npc_weapon_del(self, data: bytes):
        """Handle PLI_NPCWEAPONDEL packet."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        if weapon_name in self.weapons:
            self.weapons.remove(weapon_name)

    # =========================================================================
    # Communication Handlers
    # =========================================================================

    async def _handle_chat(self, data: bytes):
        """Handle PLI_TOALL chat packet."""
        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace').strip()

        if not message or self.is_muted:
            return

        self.chat = message
        logger.info(f"[Chat] {self.nickname}: {message}")

        # Broadcast to level
        if self.level:
            packet = build_chat(self.id, message)
            await self.server.broadcast_to_level(self.level.name, packet)

            # Trigger NPC events
            await self.server.npc_manager.on_player_chats(self, message)

    async def _handle_private_message(self, data: bytes):
        """Handle PLI_PRIVATEMESSAGE packet."""
        reader = PacketReader(data)
        target_name = reader.read_string()
        message = reader.remaining().decode('latin-1', errors='replace')

        if self.is_muted:
            return

        # Find target player
        target = self.server.get_player_by_name(target_name)
        if target:
            packet = build_private_message(self.nickname, message)
            await target.send_raw(packet)

    async def _handle_show_img(self, data: bytes):
        """Handle PLI_SHOWIMG packet (level chat)."""
        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace')

        # This is used for level chat messages with coordinates
        if self.level and not self.is_muted:
            from .protocol.packets import build_show_img
            packet = build_show_img(self.id, message)
            await self.server.broadcast_to_level(self.level.name, packet)

    # =========================================================================
    # Flag Handlers
    # =========================================================================

    async def _handle_flag_set(self, data: bytes):
        """Handle PLI_FLAGSET packet."""
        reader = PacketReader(data)
        flag_data = reader.remaining().decode('latin-1', errors='replace')
        if '=' in flag_data:
            name, value = flag_data.split('=', 1)
            self.flags[name.strip()] = value.strip()

    async def _handle_flag_del(self, data: bytes):
        """Handle PLI_FLAGDEL packet."""
        flag_name = data.decode('latin-1', errors='replace').strip()
        self.flags.pop(flag_name, None)

    # =========================================================================
    # Trigger Handler
    # =========================================================================

    async def _handle_trigger_action(self, data: bytes):
        """Handle PLI_TRIGGERACTION packet."""
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        action = reader.remaining().decode('latin-1', errors='replace').strip()

        logger.debug(f"Trigger action at ({x}, {y}): {action}")

        # Handle serverside triggers
        if action.startswith("serverside"):
            await self.server.handle_trigger_action(self, x, y, action)

        # Notify NPC manager
        await self.server.npc_manager.on_trigger_action(self, x, y, action)

    # =========================================================================
    # File Handlers
    # =========================================================================

    async def _handle_want_file(self, data: bytes):
        """Handle PLI_WANTFILE packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_want_file(self, filename)

    async def _handle_update_file(self, data: bytes):
        """Handle PLI_UPDATEFILE packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_want_file(self, filename)

    async def _handle_verify_want_send(self, data: bytes):
        """Handle PLI_VERIFYWANTSEND packet."""
        reader = PacketReader(data)
        checksum = reader.read_gint5()
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_verify_want_send(self, checksum, filename)

    async def _handle_update_gani(self, data: bytes):
        """Handle PLI_UPDATEGANI packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_update_gani(self, filename)

    async def _handle_update_script(self, data: bytes):
        """Handle PLI_UPDATESCRIPT packet."""
        reader = PacketReader(data)
        filename = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_update_script(self, filename)

    async def _handle_update_class(self, data: bytes):
        """Handle PLI_UPDATECLASS packet."""
        reader = PacketReader(data)
        classname = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'filesystem'):
            await self.server.filesystem.handle_update_class(self, classname)

    # =========================================================================
    # Weapon Handler
    # =========================================================================

    async def _handle_weapon_add(self, data: bytes):
        """Handle PLI_WEAPONADD packet (client requesting to add weapon)."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        if weapon_name and weapon_name not in self.weapons:
            self.weapons.append(weapon_name)

    # =========================================================================
    # Board Handlers
    # =========================================================================

    async def _handle_board_modify(self, data: bytes):
        """Handle PLI_BOARDMODIFY packet."""
        if not self.level:
            return
        # Requires admin rights for permanent changes
        reader = PacketReader(data)
        x = reader.read_gchar()
        y = reader.read_gchar()
        w = reader.read_gchar()
        h = reader.read_gchar()
        # Tile data follows...

    async def _handle_request_update_board(self, data: bytes):
        """Handle PLI_REQUESTUPDATEBOARD packet."""
        if self.level:
            # Resend level board
            await self._send_level(self.level)

    # =========================================================================
    # Profile Handlers
    # =========================================================================

    async def _handle_profile_get(self, data: bytes):
        """Handle PLI_PROFILEGET packet."""
        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'profile_manager'):
            profile = self.server.profile_manager.get_profile(account_name)
            # Send profile response
            from .protocol.packets import build_profile
            packet = build_profile(profile)
            await self.send_raw(packet)

    async def _handle_profile_set(self, data: bytes):
        """Handle PLI_PROFILESET packet."""
        reader = PacketReader(data)
        profile_data = {}  # Parse profile data from packet

        if hasattr(self.server, 'profile_manager'):
            self.server.profile_manager.set_profile(self, profile_data)

    # =========================================================================
    # Server/Misc Handlers
    # =========================================================================

    async def _handle_map_info(self, data: bytes):
        """Handle PLI_MAPINFO packet."""
        # Client requesting map/world info
        pass

    async def _handle_server_warp(self, data: bytes):
        """Handle PLI_SERVERWARP packet (warp to another server)."""
        reader = PacketReader(data)
        server_name = reader.remaining().decode('latin-1', errors='replace')

        # Server warps not implemented in this server
        logger.info(f"Server warp requested to: {server_name}")

    async def _handle_packet_count(self, data: bytes):
        """Handle PLI_PACKETCOUNT packet."""
        # Client reporting packet count - used for sync checking
        pass

    async def _handle_language(self, data: bytes):
        """Handle PLI_LANGUAGE packet."""
        reader = PacketReader(data)
        language = reader.remaining().decode('latin-1', errors='replace')
        logger.debug(f"Player {self.id} language: {language}")

    async def _handle_mute_player(self, data: bytes):
        """Handle PLI_MUTEPLAYER packet."""
        # Client requesting to mute another player (local only)
        pass

    async def _handle_process_list(self, data: bytes):
        """Handle PLI_PROCESSLIST packet."""
        # Debug packet - list server processes
        pass

    async def _handle_claim_pker(self, data: bytes):
        """Handle PLI_CLAIMPKER packet."""
        # PK claim system
        pass

    async def _handle_raw_data(self, data: bytes):
        """Handle PLI_RAWDATA packet."""
        reader = PacketReader(data)
        size = reader.read_gint3()
        # Raw data follows

    async def _handle_request_text(self, data: bytes):
        """Handle PLI_REQUESTTEXT packet (get server variable)."""
        reader = PacketReader(data)
        var_name = reader.remaining().decode('latin-1', errors='replace')

        # Get server variable
        value = ""
        if hasattr(self.server, 'server_flags'):
            value = self.server.server_flags.get(var_name, "")

        from .protocol.packets import build_server_text
        packet = build_server_text(var_name, value)
        await self.send_raw(packet)

    async def _handle_send_text(self, data: bytes):
        """Handle PLI_SENDTEXT packet (set server variable)."""
        reader = PacketReader(data)
        var_data = reader.remaining().decode('latin-1', errors='replace')

        if '=' in var_data:
            var_name, value = var_data.split('=', 1)
            if hasattr(self.server, 'server_flags'):
                self.server.server_flags[var_name.strip()] = value.strip()

    async def _handle_npc_server_query(self, data: bytes):
        """Handle PLI_NPCSERVERQUERY packet."""
        # Query about NPC server capabilities
        pass

    # =========================================================================
    # Public Methods
    # =========================================================================

    async def warp(self, level_name: str, x: float, y: float):
        """Warp player to a level."""
        logger.info(f"Player {self.id} warping to {level_name} at ({x}, {y})")

        # Handle horse across levels
        old_level = self.level

        # Leave current level
        if old_level:
            old_level.remove_player(self)
            await self.server.broadcast_to_level(
                old_level.name, build_player_left(self.id), exclude={self.id}
            )

        # Find or load level
        level = self.server.world.get_level(level_name)
        if not level:
            logger.warning(f"Level not found: {level_name}")
            return

        # Update position
        self.x = x
        self.y = y
        self.level = level
        level.add_player(self)

        # Handle horse warp
        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.handle_player_warp(self, old_level, level)

        # Send level data
        await self._send_level(level)

        # Notify NPCs
        await self.server.npc_manager.on_player_enters(self, level)

    async def _send_level(self, level: 'Level'):
        """Send level data to player."""
        logger.info(f"Sending level {level.name} to player {self.id}")

        # Build packets
        level_name_pkt = build_level_name(level.name)

        # Board data format: [packet_id + 32] + [8192 tile bytes] + [\n]
        tile_data = level.get_board_packet()  # 8192 bytes
        board_packet = bytes([PLO.BOARDPACKET + 32]) + tile_data + b'\n'

        # Announce raw data size (1 + 8192 + 1 = 8194)
        announcement = build_raw_data_announcement(len(board_packet))
        warp_packet = build_warp(self.x, self.y, level.name)

        combined = level_name_pkt + announcement + board_packet + warp_packet
        await self.send_raw(combined)

        # Send NPCs on level
        for npc in level.get_npcs():
            await self.send_raw(npc.build_props_packet())

        # Send items on level
        if hasattr(self.server, 'item_manager'):
            await self.server.item_manager.send_level_items(self, level)

        # Send baddies on level
        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.send_level_baddies(self, level)

        # Send horses on level
        if hasattr(self.server, 'horse_manager'):
            await self.server.horse_manager.send_level_horses(self, level)

        # Send other players on level
        for other_id in level.get_player_ids():
            if other_id != self.id:
                other = self.server.get_player(other_id)
                if other:
                    await self.send_raw(other.build_props_packet())
                    await other.send_raw(self.build_props_packet())

    async def send_raw(self, data: bytes):
        """Send raw packet data (will be encoded)."""
        if not self.connected or not self._codec:
            return
        try:
            encoded = self._codec.encode_packet(data)
            self._writer.write(encoded)
            await self._writer.drain()
        except Exception as e:
            logger.error(f"Send error: {e}")
            self.connected = False

    async def send_packet(self, packet_id: int, data: bytes = b""):
        """Send a packet with given ID and data."""
        packet = PacketBuilder().write_gchar(packet_id).write_bytes(data).write_byte(ord('\n')).build()
        await self.send_raw(packet)

    async def send_props(self, props: dict):
        """Send player props to this player (PLO_PLAYERPROPS)."""
        packet = build_player_props(props)
        await self.send_raw(packet)

    def build_props_packet(self) -> bytes:
        """Build PLO_OTHERPLPROPS packet for this player."""
        props = {
            PLPROP.NICKNAME: self.nickname,
            PLPROP.X2: self.x,
            PLPROP.Y2: self.y,
            PLPROP.DIRECTION: self.direction,
            PLPROP.SPRITE: self.sprite,
            PLPROP.GANI: self.gani,
            PLPROP.HEADIMAGE: self.head_image,
            PLPROP.BODYIMAGE: self.body_image,
            PLPROP.CURLEVEL: self.level.name if self.level else "",
        }
        return build_other_player_props(self.id, props)

    def build_leave_packet(self) -> bytes:
        """Build PLO_PLAYERLEFT packet."""
        return build_player_left(self.id)

    def get_flag(self, name: str) -> str:
        """Get player flag value."""
        return self.flags.get(name, "")

    def set_flag(self, name: str, value: str):
        """Set player flag value."""
        self.flags[name] = value

    def has_weapon(self, name: str) -> bool:
        """Check if player has a weapon."""
        return name in self.weapons

    def add_weapon(self, name: str):
        """Add a weapon to player."""
        if name not in self.weapons:
            self.weapons.append(name)

    def remove_weapon(self, name: str):
        """Remove a weapon from player."""
        if name in self.weapons:
            self.weapons.remove(name)
