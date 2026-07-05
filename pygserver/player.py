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
    build_trigger_action,
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

        # MP/AP (PLPROP_MAGICPOINTS=26 / PLPROP_ALIGNMENT=32). Defaults match
        # GServer-v2 (server/include/object/Character.h): mp starts at 0,
        # ap starts at 50 (neutral on the 0-100 good/evil scale).
        self.mp = 0
        self.ap = 50

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

        # Leave current level. The leave broadcast itself is left to
        # GameServer._remove_player (server.py), which runs right after this
        # in the connection's finally block - broadcasting it here too sent
        # every disconnect as two duplicate PLO_OTHERPLPROPS leave packets.
        if self.level:
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.on_player_leaves(self, self.level)
            self.level.remove_player(self)

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
            PLPROP.COLORS: self.colors,
            PLPROP.MAGICPOINTS: self.mp,
            PLPROP.ALIGNMENT: self.ap,
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

            # Dispatch to a registered game-client handler first. The RC/NC
            # packet id ranges below overlap ordinary PLI ids (e.g.
            # PLI_PROFILEGET=80/PLI_PROFILESET=81 both fall inside 51-98), so
            # checking the ranges first hijacked those packets and made the
            # registered _handle_profile_get/_handle_profile_set handlers
            # unreachable for game clients.
            handler = self._handlers.get(packet_id)
            if handler:
                try:
                    await handler(packet_body)
                except Exception as e:
                    logger.error(f"Packet handler error (id={packet_id}): {e}")
                continue

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
        """Handle PLI_LEVELWARPMOD packet (modified warp).

        Wire format (GServer-v2 msgPLI_LEVELWARP, PlayerClientPackets.cpp:
        52-58): LEVELWARPMOD carries a leading GUINT5 modtime before the
        x/y/level body that plain LEVELWARP has. Without consuming it first,
        the 5 modtime bytes get read as x/y and the start of the level name,
        corrupting every modified warp.
        """
        reader = PacketReader(data)
        reader.read_gint5()  # modtime, unused
        await self._handle_level_warp(reader.remaining())

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

        # Local level chat (PLPROP_CURCHAT, sent by Client.send_level_chat via
        # PLI_PLAYERPROPS) fires the GS1 "playerchats" NPC event, e.g. the
        # qa_tier3.nw fixture's unfreezeplayer-on-chat handler.
        if PLPROP.CURCHAT in props:
            self.chat = props[PLPROP.CURCHAT]
            if self.level and getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.on_player_chats(self, self.chat)

        # Appearance updates
        if PLPROP.HEADIMAGE in props:
            self.head_image = props[PLPROP.HEADIMAGE]
        if PLPROP.BODYIMAGE in props:
            self.body_image = props[PLPROP.BODYIMAGE]

        # Health: the client is authoritative for its own damage (e.g. baddies it
        # drives as leader), reporting new hearts via CURPOWER (= hearts * 2). A
        # transition to <= 0 means the player died, so kick off the death/respawn
        # flow once (it would otherwise never fire for client-side damage).
        if PLPROP.CURPOWER in props:
            new_hearts = props[PLPROP.CURPOWER] / 2.0
            was_alive = self.hearts > 0
            self.hearts = new_hearts
            if new_hearts <= 0 and was_alive and hasattr(self.server, 'combat_manager'):
                await self.server.combat_manager.handle_player_death(self)

        # Broadcast to other players on level. Previously this whitelist
        # omitted CURCHAT/HEADIMAGE/BODYIMAGE/COLORS/SWORDPOWER/SHIELDPOWER,
        # so other players never saw a player's chat bubble or an
        # appearance/gear change made mid-session - only movement updated.
        if self.level:
            broadcast_props = {}
            # Position: clients may send classic X/Y (15/16, half-tiles) OR
            # X2/Y2 (78/79) - keying the relay on X2/Y2 alone silently
            # dropped every movement update from classic-prop senders, so
            # other players saw them frozen at their spawn position. Relay
            # as X2/Y2 (self.x/y were normalized above) whichever arrived.
            if PLPROP.X in props or PLPROP.X2 in props:
                broadcast_props[PLPROP.X2] = self.x
            if PLPROP.Y in props or PLPROP.Y2 in props:
                broadcast_props[PLPROP.Y2] = self.y
            for key in [PLPROP.DIRECTION, PLPROP.SPRITE,
                        PLPROP.GANI, PLPROP.CURCHAT, PLPROP.HEADIMAGE,
                        PLPROP.BODYIMAGE, PLPROP.COLORS, PLPROP.SWORDPOWER,
                        PLPROP.SHIELDPOWER]:
                if key in props:
                    broadcast_props[key] = getattr(self, {
                        PLPROP.DIRECTION: 'direction',
                        PLPROP.SPRITE: 'sprite',
                        PLPROP.GANI: 'gani',
                        PLPROP.CURCHAT: 'chat',
                        PLPROP.HEADIMAGE: 'head_image',
                        PLPROP.BODYIMAGE: 'body_image',
                        PLPROP.COLORS: 'colors',
                        PLPROP.SWORDPOWER: 'sword_power',
                        PLPROP.SHIELDPOWER: 'shield_power',
                    }[key])

            if broadcast_props:
                packet = build_other_player_props(self.id, broadcast_props)
                await self.server.broadcast_to_level(
                    self.level.name, packet, exclude={self.id}
                )

            # Fire GS1 playertouchsme for NPCs the player has walked onto
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.check_touches(self)

    async def _handle_adjacent_level(self, data: bytes):
        """Handle PLI_ADJACENTLEVEL - client preloading a neighbouring GMAP
        segment. Send that level's name + board so the client can stitch the
        world together; no warp and no player-add (the player stays put)."""
        reader = PacketReader(data)
        level_name = reader.remaining().decode('latin-1', errors='replace').strip()
        if not level_name:
            return
        level = self.server.world.get_level(level_name)
        if not level:
            logger.debug(f"Adjacent level not found: {level_name}")
            return

        # Only the board — adjacent segments are for rendering. Their signs/links
        # belong to that segment and are sent when the player actually warps in;
        # sending them here leaks e.g. neighbouring signs into the current level.
        level_name_pkt = build_level_name(level.name)
        tile_data = level.get_board_packet()
        board_packet = bytes([PLO.BOARDPACKET + 32]) + tile_data + b'\n'
        announcement = build_raw_data_announcement(len(board_packet))
        await self.send_raw(level_name_pkt + announcement + board_packet)

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
        # {GCHAR player_power}{GCHAR timer}: power is bits 0-1, timer is
        # 50ms increments (+50ms) - see GServer-v2 msgPLI_BOMBADD
        power = (reader.read_gchar() & 0x03) if reader.remaining() else 1
        time_left = (reader.read_gchar() * 0.05 + 0.05) if reader.remaining() else 3.0

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
        """Handle PLI_ARROWADD packet.

        Wire format (GServer-v2 msgPLI_ARROWADD, PlayerClientPackets.cpp):
            {GCHAR x*2}{GCHAR y*2}{GCHAR flags}{GCHAR sprite}{GCHAR power}
        flags: bit0-1 direction, bit2 reflect, bit3 fromPlayer.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        flags = reader.read_gchar() if reader.remaining() else (int(self.direction) & 0x03)
        sprite = reader.read_gchar() if reader.remaining() else 0
        power = reader.read_gchar() if reader.remaining() else 1

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_arrow_add(self, x, y, flags, sprite, power)

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
        """Handle PLI_HURTPLAYER packet.

        Wire format (GServer-v2 msgPLI_HURTPLAYER, PlayerClientPackets.cpp:
        811-820):
        - victim_id (gshort)
        - hurt_dx (SIGNED gchar) - knockback X direction
        - hurt_dy (SIGNED gchar) - knockback Y direction
        - power (gchar) - damage amount
        - npc_id (gint3) - optional
        hurt_dx/hurt_dy must use the signed reader: the unsigned read_gchar()
        clamps negative values to 0, silently dropping all left/up knockback.
        """
        reader = PacketReader(data)
        target_id = reader.read_gshort()
        hurt_dx = reader.read_gchar_signed()
        hurt_dy = reader.read_gchar_signed()
        power = reader.read_gchar() if reader.remaining() else 1
        # npc_id = reader.read_gint3() if reader.remaining() else 0  # Not used yet

        if hasattr(self.server, 'combat_manager'):
            await self.server.combat_manager.handle_hurt_player(self, target_id, power, hurt_dx, hurt_dy)

    async def _handle_explosion(self, data: bytes):
        """Handle PLI_EXPLOSION packet.

        Wire format (GServer-v2 msgPLI_EXPLOSION, PlayerClientPackets.cpp:
        829-844): {GCHAR radius}{GCHAR x*2}{GCHAR y*2}{GCHAR power} - radius
        comes first and is a raw byte (not a half-tile value). Previously
        this read x/y before radius/power (wrong field order) and had no
        `self.level` guard, so it raised AttributeError for any player not
        currently on a level, and it broadcast back to the sender too.

        The PLO_EXPLOSION relay GServer actually sends also prepends a
        (short) owner id that build_explosion() here does not write; leave
        that as-is since pyReborn's parser (pyReborn/pyreborn/packets.py
        parse_explosion) expects [x][y][radius][power] with no owner id, and
        the client side is owned by another team.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        radius = reader.read_gchar() if reader.remaining() else 4
        x = reader.read_gchar() / 2.0 if reader.remaining() else self.x
        y = reader.read_gchar() / 2.0 if reader.remaining() else self.y
        power = reader.read_gchar() if reader.remaining() else 2

        # Broadcast explosion effect
        from .protocol.packets import build_explosion
        packet = build_explosion(x, y, radius, power)
        await self.server.broadcast_to_level(
            self.level.name, packet, exclude={self.id}
        )

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
        """Handle PLI_HORSEADD packet.

        Wire format (GServer-v2 msgPLI_HORSEADD, PlayerClientPackets.cpp:
        256-269): {GCHAR x*2}{GCHAR y*2}{GCHAR dir_bushes}{RAW image}.
        dir_bushes packs direction in bits 0-1 and bush count in the rest of
        the byte; image is a raw trailing string with no length prefix.
        Previously this read direction/bushes as two separate gchars and a
        length-prefixed image, which doesn't match what real clients send.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        dir_bushes = reader.read_gchar() if reader.remaining() else 0x0E  # dir=2, bushes=3
        direction = dir_bushes & 0x03
        bushes = dir_bushes >> 2
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
        """Handle PLI_BADDYHURT packet.

        Client format (build_baddy_hurt): [gchar baddy_id][gchar damage].
        """
        if not self.level:
            return
        reader = PacketReader(data)
        baddy_id = reader.read_gchar()
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
        """Handle PLI_PRIVATEMESSAGE packet.

        Format: [gshort count][gshort player_id]*count[raw message].
        """
        if self.is_muted:
            return

        reader = PacketReader(data)
        count = reader.read_gshort()
        target_ids = [reader.read_gshort() for _ in range(count)]
        message = reader.remaining().decode('latin-1', errors='replace')

        for target_id in target_ids:
            target = self.server.get_player(target_id)
            if target:
                packet = build_private_message(self.id, self.nickname, message)
                await target.send_raw(packet)

    async def _handle_show_img(self, data: bytes):
        """Handle PLI_SHOWIMG packet (used here for level chat).

        The client parses PLO_SHOWIMG with the same layout as chat
        (gshort id + message), so relay it via build_chat.
        """
        reader = PacketReader(data)
        message = reader.remaining().decode('latin-1', errors='replace')

        if self.level and not self.is_muted:
            packet = build_chat(self.id, message)
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
        """Handle PLI_TRIGGERACTION packet.

        Wire format (GServer-v2 msgPLI_TRIGGERACTION, PlayerClientPackets.cpp):
            {GUINT3 npc_id}{GCHAR x*2}{GCHAR y*2}{action CSV}
        npc_id is a 3-byte GInt (readGUInt() == readGInt(), 3 bytes on the
        wire, not 4) - it must be consumed before x/y or every triggeraction
        parses garbage.
        """
        reader = PacketReader(data)
        npc_id = reader.read_gint3()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        action = reader.remaining().decode('latin-1', errors='replace').strip()

        logger.debug(f"Trigger action at ({x}, {y}): {action}")

        # Handle serverside triggers
        if action.startswith("serverside"):
            await self.server.handle_trigger_action(self, x, y, action)

        # Relay to other players on the level (GServer-v2 msgPLI_TRIGGERACTION:
        # sendPacketToOneLevelPart(..., { m_id }) when sendplayertriggers=true,
        # the default; excludes the sender).
        if self.level:
            packet = build_trigger_action(self.id, npc_id, x, y, action)
            await self.server.broadcast_to_level(self.level.name, packet, exclude={self.id})

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

        # Parse modification data
        reader = PacketReader(data)
        x = reader.read_gchar()
        y = reader.read_gchar()
        w = reader.read_gchar()
        h = reader.read_gchar()
        tile_data = reader.remaining()

        # Validate bounds
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            return
        if x + w > 64 or y + h > 64:
            return

        # Check permissions (require admin rights for permanent changes)
        # For now, allow all players to modify tiles (temporary changes)
        # Permanent changes would require: if not (self.admin_rights & PLPERM.UPDATELEVEL):

        # Apply tile changes
        expected_size = w * h * 2  # 2 bytes per tile
        if len(tile_data) < expected_size:
            return

        idx = 0
        for ty in range(y, y + h):
            for tx in range(x, x + w):
                if idx + 1 < len(tile_data):
                    tile_id = tile_data[idx] | (tile_data[idx + 1] << 8)
                    self.level.set_tile(tx, ty, tile_id)
                    idx += 2

        # Broadcast modification to other players on level
        from .protocol.packets import build_board_modify
        packet = build_board_modify(x, y, w, h, tile_data[:expected_size])
        await self.server.broadcast_to_level(
            self.level.name, packet, exclude={self.id}
        )

    async def _handle_request_update_board(self, data: bytes):
        """Handle PLI_REQUESTUPDATEBOARD packet."""
        if self.level:
            # Resend level board
            await self._send_level(self.level)

    # =========================================================================
    # Profile Handlers
    # =========================================================================

    async def _handle_profile_get(self, data: bytes):
        """Handle PLI_PROFILEGET packet (request another player's profile).

        Payload is the raw target account name, no length prefix (see
        build_profile_get in pyReborn). GServer-v2 just forwards this to the
        list server as SVO_GETPROF and relays whatever SVI_PROFILE comes
        back; pygserver has no such external profile service, so it answers
        from the locally-persisted account profile fields instead (see
        ProfileManager).
        """
        reader = PacketReader(data)
        account_name = reader.remaining().decode('latin-1', errors='replace')

        if hasattr(self.server, 'profile_manager'):
            profile = self.server.profile_manager.get_profile(account_name)
            if not profile:
                return
            from .protocol.packets import build_profile
            packet = build_profile(profile['account'], profile, profile.get('online_time', ''))
            await self.send_raw(packet)

    async def _handle_profile_set(self, data: bytes):
        """Handle PLI_PROFILESET packet (update our own profile).

        Payload: {GCHAR len}{account} then 9 free-text fields. GServer-v2
        (Player.cpp msgPLI_PROFILESET) rejects the packet outright if the
        embedded account name isn't the sender's own - mirror that here
        before persisting anything.
        """
        from .protocol.packets import parse_profile
        profile_data = parse_profile(data)

        if profile_data.get('account') != self.account_name:
            return

        if hasattr(self.server, 'profile_manager'):
            self.server.profile_manager.set_profile(self, profile_data)

    # =========================================================================
    # Server/Misc Handlers
    # =========================================================================

    async def _handle_map_info(self, data: bytes):
        """Handle PLI_MAPINFO packet.

        PLI_MAPINFO (39) is defined in GServer-v2's own packet enum
        (dependencies/gs2lib/include/IEnums.h) but is never wired to a
        handler there either (absent from IPacketHandler.h's
        FOR_INPUT_PACKETS list) - the reference server silently drops it
        too. True no-op, not a missing feature.
        """
        pass

    async def _handle_server_warp(self, data: bytes):
        """Handle PLI_SERVERWARP packet (warp to another server).

        GServer-v2 (PlayerClientPackets.cpp msgPLI_SERVERWARP) just forwards
        this to the connected list server as SVO_SERVERINFO ({GUSHORT player
        id}{raw server name}); the list server looks up the named server and
        replies with SVI_SERVERINFO, which is relayed back to the client
        verbatim as PLO_SERVERWARP (see ServerListClient.request_server_info
        / _handle_server_info). A single pygserver instance has no server
        directory of its own to consult, so without a live list server
        connection there's nowhere to look this up - log and drop.
        """
        reader = PacketReader(data)
        server_name = reader.remaining().decode('latin-1', errors='replace')

        listserver = getattr(self.server, 'listserver', None)
        if listserver is not None and listserver.connected:
            await listserver.request_server_info(self.id, server_name)
        else:
            logger.info(
                f"{self.account_name} requested serverwarp to '{server_name}' "
                f"but no list server connection is available"
            )

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
        """Handle PLI_MUTEPLAYER packet.

        Format (IEnums.h comment): {GSHORT playerId}{GBYTE 1/0}. GServer-v2
        lists this in FOR_INPUT_PACKETS for packet-name tracing but never
        assigns it a handler function - muting is purely a client-side
        playerlist feature (it filters chat locally), so the server has
        nothing to do besides not choke on the bytes. Parse for
        observability only; true no-op otherwise.
        """
        reader = PacketReader(data)
        target_id = reader.read_gshort()
        muted = bool(reader.read_gchar())
        logger.debug(f"{self.account_name} {'muted' if muted else 'unmuted'} player id {target_id} (client-local only)")

    async def _handle_process_list(self, data: bytes):
        """Handle PLI_PROCESSLIST packet.

        GServer-v2 (PlayerClientPackets.cpp msgPLI_PROCESSLIST) detokenizes
        the client's process list and discards it without acting on it -
        this is the same no-op, just with the parse for observability.
        """
        reader = PacketReader(data)
        processes = reader.remaining().decode('latin-1', errors='replace')
        logger.debug(f"{self.account_name} process list: {processes!r}")

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

        # Find or load the destination FIRST: a bad/nonexistent level name
        # must not detach the player from their current level (this used to
        # remove_player + broadcast leave before validating, stranding the
        # player in limbo server-side while the client kept playing).
        level = self.server.world.get_level(level_name)
        if not level:
            logger.warning(f"Level not found: {level_name}")
            if self.level:
                # Snap the (possibly optimistic) client back to where it is:
                # warp packet for position, full level re-send so the client
                # (which may have already reset its local level state for the
                # bogus name) recovers name/board/entities.
                await self.send_raw(build_warp(self.x, self.y, self.level.name))
                await self._send_level(self.level)
            return

        # Handle horse across levels
        old_level = self.level

        # Leave current level. GS1's "playerleaves" event previously only
        # fired on disconnect (_cleanup), never on a normal warp to another
        # level, so NPCs never saw a player leave via warp.
        if old_level:
            if getattr(self.server, 'npc_manager', None):
                await self.server.npc_manager.on_player_leaves(self, old_level)
            old_level.remove_player(self)
            await self.server.broadcast_to_level(
                old_level.name, build_player_left(self.id), exclude={self.id}
            )

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

        # In a GMAP, warp via PLO_PLAYERWARP2, which carries LOCAL coords plus the
        # segment's grid (gmap_x/gmap_y) separately. PLO_PLAYERWARP packs the
        # position into a single gchar (max ~111 tiles), so world coords for grid
        # cell 2+ (x >= 128) overflow and the player lands at the wrong spot; the
        # client recombines local + grid*64 itself.
        gmap_info = self.server.world.get_gmap_for_level(level.name)
        if gmap_info:
            _, gx, gy = gmap_info
            warp_packet = build_warp2(self.x, self.y, level.name, gx, gy)
        else:
            warp_packet = build_warp(self.x, self.y, level.name)

        combined = level_name_pkt + announcement + board_packet + warp_packet
        await self.send_raw(combined)

        # If this level is a GMAP segment, announce the .gmap name. That makes the
        # client request the gmap file (PLI_WANTFILE), build the grid, enter gmap
        # mode and request adjacent segments — without it the client treats the
        # segment as a standalone level (no stitching, broken edge warps).
        if gmap_info:
            gmap = gmap_info[0]
            # The client keys gmap handling off the ".gmap" suffix; gmap.name is
            # the bare stem ("chicken"), so announce the full filename.
            gmap_file = gmap.name if gmap.name.endswith('.gmap') else gmap.name + '.gmap'
            await self.send_raw(build_level_name(gmap_file))

        # Send signs and links on level
        from .protocol.packets import build_level_sign, build_level_link
        for (sx, sy), text in level.get_signs().items():
            await self.send_raw(build_level_sign(sx, sy, text))
        for link in level.get_links():
            await self.send_raw(build_level_link(
                link['dest_level'], link['x'], link['y'],
                link['width'], link['height'], link['dest_x'], link['dest_y'],
            ))

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
            PLPROP.COLORS: self.colors,
            PLPROP.MAGICPOINTS: self.mp,
            PLPROP.ALIGNMENT: self.ap,
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
