"""
pygserver.level_entry - entering, leaving and re-entering levels

The reference server's modern-generation level path (GServer-v2
server/src/player/PlayerClient.cpp), mixed into Player:

- `warp`: a server warp (login, scripts, RC, death). PLO_PLAYERWARP or
  PLO_PLAYERWARP2 goes out first, then the level (PlayerClient::warp,
  :1229-1275).
- `client_warp`: PLI_LEVELWARP / PLI_LEVELWARPMOD
  (PlayerClientPackets.cpp:52-102). A plain level is entered without any
  PLAYERWARP (Player::enterLevel, Player.cpp:1250-1285); a gmap segment gets
  its static data first, then the gmap warp (:73-82).
- `send_static_level_data` / `send_dynamic_level_data`: the two halves of an
  entry (:1446-1522, :1524-1605). The static half (board, links, signs) is
  skipped when the player has left the level before this session and the
  client's cached time is not older than the level's.
- `gmap_hop`: crossing gmap segments by the client's GMAPLEVELX/Y props
  (PlayerProps.cpp:905-973): the old segment is recorded as visited and only
  the new segment's dynamic data goes out.

pygserver keeps one Level per gmap segment where the reference has one gmap
Level with sub-levels, so "the same level" below means the same level or the
same gmap, and a gmap hop moves the player between segment Levels without
any leave or enter packets.
"""

import logging
from typing import TYPE_CHECKING, List, Optional

from . import level_cache
from .attachments import absolute_position, clear_attachment
from .level import gmap_file_name
from .link_sign_touch import level_data_has_links, level_data_has_signs
from .protocol.constants import PLPROP
from .protocol.packets import (
    build_board_layer_raw,
    build_board_packet_raw,
    build_ghost_icon,
    build_is_leader,
    build_level_board,
    build_level_link,
    build_level_modtime,
    build_level_name,
    build_level_sign,
    build_npc_props,
    build_other_player_props,
    build_player_left,
    build_player_props,
    build_raw_data_announcement,
    build_set_active_level,
    build_warp,
    build_warp2,
)

if TYPE_CHECKING:
    from .level import Level

logger = logging.getLogger(__name__)


def raw_packet(packet: bytes) -> bytes:
    """`packet` behind the PLO_RAWDATA announcement of its size."""
    return build_raw_data_announcement(len(packet)) + packet


class LevelTransitions:
    """Mixin for Player: level membership and the level packets."""

    # -- lookups ------------------------------------------------------------

    def _gmap_info(self, level):
        if level is None:
            return None
        return self.server.world.get_gmap_for_level(level.name)

    def wire_level_name(self, level=None) -> str:
        """PlayerClient::getLevelName: the gmap on a gmap, else the level."""
        level = self.level if level is None else level
        if level is None:
            return ""
        info = self._gmap_info(level)
        return gmap_file_name(info[0]) if info else level.name

    def _same_world(self, level) -> bool:
        """`account.level == level->levelName` (PlayerClient.cpp:1240): the
        same level, or a segment of the gmap the player is on."""
        if self.level is None:
            return False
        if self.level is level:
            return True
        here, there = self._gmap_info(self.level), self._gmap_info(level)
        return (here is not None and there is not None
                and here[0].name == there[0].name)

    async def _inform_if_level_leader(self) -> None:
        """checkAndInformIfLevelLeader (PlayerClient.cpp:1607-1618): the
        level's first player, or anyone on a gmap."""
        level = self.level
        if level is None:
            return
        if level.is_player_leader(self) or self._gmap_info(level):
            await self.send_raw(build_is_leader())

    # -- entry points -------------------------------------------------------

    async def warp(self, level_name: str, x: float, y: float):
        """Server-initiated warp (PlayerClient::warp, PlayerClient.cpp:1203)."""
        logger.info(f"Player {self.id} warping to {level_name} at ({x}, {y})")
        level = self.server.world.get_level(level_name)
        if not level:
            await self._warp_not_found(level_name)
            return
        await self._warp_to(level, x, y, None)

    async def client_warp(self, level_name: str, x: float, y: float,
                          cached_time: Optional[int]):
        """msgPLI_LEVELWARP / PLI_LEVELWARPMOD dispatch
        (PlayerClientPackets.cpp:64-87). `cached_time` is the client's mod
        time from PLI_LEVELWARPMOD, None for PLI_LEVELWARP (:54-57)."""
        logger.info(f"Player {self.id} client warp to {level_name} at ({x}, {y})")
        if level_name.endswith('.gmap'):
            # :64-67: a warp naming the gmap itself is a gmap warp to the
            # map position the coordinates fall in.
            gmap = self.server.world.get_gmap(level_name[:-len('.gmap')])
            segment = None
            if gmap is not None:
                gx, gy = int(x // 64), int(y // 64)
                name = gmap.get_level_at(gx, gy)
                segment = self.server.world.get_level(name) if name else None
            if segment is None:
                await self._warp_not_found(level_name)
                return
            await self._warp_to(segment, x - gx * 64, y - gy * 64, cached_time)
            return
        level = self.server.world.get_level(level_name)
        if not level:
            await self._warp_not_found(level_name)
            return
        if self._gmap_info(level):
            # :73-82: a gmap segment's static data goes out first, then the
            # gmap warp, whose entry sends it a second time (the record is
            # only written by a leave).
            await self.send_static_level_data(level, cached_time)
            await self._warp_to(level, x, y, cached_time)
            return
        await self._enter_level(level, x, y, cached_time)

    async def _warp_not_found(self, level_name: str):
        """A destination that cannot be loaded leaves the player where it is.
        pygserver re-anchors the client with a warp and a re-send of the
        current level (the player is never detached first)."""
        logger.warning(f"Level not found: {level_name}")
        if self.level:
            await self.send_raw(build_warp(*absolute_position(self), self.level.name))
            await self._send_level(self.level)

    async def _warp_to(self, level, x: float, y: float,
                       cached_time: Optional[int]):
        """PlayerClient::warp(level, ...) (PlayerClient.cpp:1229-1275)."""
        if self._same_world(level):
            await self._set_position(level, x, y)
            return
        info = self._gmap_info(level)
        if info:
            _, gx, gy = info
            warp_packet = build_warp2(x, y, gmap_file_name(info[0]), gx, gy)
        else:
            warp_packet = build_warp(x, y, level.name)
        await self._change_level(level, x, y, cached_time, warp_packet)

    async def _enter_level(self, level, x: float, y: float,
                           cached_time: Optional[int]):
        """Player::enterLevel (Player.cpp:1250-1285): no PLAYERWARP; the same
        level only moves the player."""
        if self.level is level:
            await self._set_position(level, x, y)
            return
        await self._change_level(level, x, y, cached_time, b"")

    # -- level change -------------------------------------------------------

    async def _release_carried_state(self):
        """A level change breaks NPC carry and attachment links and drops a
        held bomb (TServerPlayer.cpp:140-160)."""
        # The reference moves the carried NPC with the player
        # (PlayerClient.cpp:1556-1583); pygserver releases the carry and leaves
        # the NPC where it is until pickup, throw and disconnect are modelled
        # together.
        if (getattr(self, 'npc_id', 0) or getattr(self, 'attach_npc', (0, 0))[1]
                or getattr(self, '_attachment_position_unknown', False)):
            await self.send_props({PLPROP.CARRYNPC: 0, PLPROP.ATTACHNPC: (0, 0)})
        self.npc_id = 0
        clear_attachment(self)
        manager = getattr(self.server, 'combat_manager', None)
        if manager is not None and manager.discard_held_bomb(self.id) is not None:
            self.carrysprite = -1
            await self.send_props({PLPROP.CARRYSPRITE: -1})

    def _record_leave(self, level) -> None:
        """leaveSubLevel (PlayerClient.cpp:1406-1444): the record takes the
        leave time. NPC props changed before now are stamped first, so only
        later changes count as news on the way back."""
        now = level_cache.clock()
        observe = getattr(getattr(self.server, 'npc_manager', None),
                          'observe_level', None)
        if observe is not None:
            observe(level, now)
        self.visited_levels.record_leave(level.name, now)

    def _detach(self):
        """The synchronous half of leaveLevel: record, then leave the
        level's player list (PlayerClient.cpp:1362-1365).

        The record goes on the cell actually being left. The reference reads
        the cell from the position warp() has already overwritten with the
        destination (:1357), so a warp marks the destination's cell (0,0 off a gmap)
        as visited on the old gmap, a cell never entered; not reproduced."""
        old = self.level
        if old is not None:
            self._record_leave(old)
            old.remove_player(self)
            self.level = None
        return old

    async def _announce_leave(self, old, destination=None):
        """The packet half of leaveLevel (PlayerClient.cpp:1367-1396): the new
        leader of a plain level is told, the players around the leaver get
        JOINLEAVELVL=0 for it (sendPacketToNearby, the 3x3 on a gmap, :1390)
        and the leaver gets it for each player in its sync range (:1392-1396).

        `destination` is ((cell x, cell y), x, y): the position warp() has
        already stored when it calls leaveLevel (:1246-1272), the (0, 0) cell
        for a destination off every gmap. Both ranges are taken around it on
        the old gmap, as the reference does, so a player that leaves the gmap
        from a far cell announces itself around the cell it arrives in; the
        packet that re-levels it for everybody is the location update sent
        at the end of the level change (:1327-1339). Without a destination
        (a reload) the leaver's own cell and position are used."""
        if (not self._gmap_info(old)
                and old.get_leader_id() is not None):
            new_leader = self.server.get_player(old.get_leader_id())
            if new_leader:
                await new_leader.send_raw(build_is_leader())
        npc_manager = getattr(self.server, 'npc_manager', None)
        if npc_manager is not None:
            await npc_manager.on_player_leaves(self, old)
        audience = getattr(self.server, 'audience', None)
        if audience is None:
            return
        if destination is not None:
            cell, x, y = destination
        else:
            cell, (x, y) = None, absolute_position(self)
        for other in audience.players_in_range_for_data(
                old.name, exclude={self.id}, cell=cell):
            await other.send_raw(build_player_left(self.id))
        for other in audience.players_in_range_for_sync(
                old.name, x, y, exclude={self.id}, cell=cell):
            await self.send_raw(build_player_left(other.id))

    async def leave_level(self):
        """PlayerClient::leaveLevel on its own (a level reload)."""
        old = self._detach()
        if old is not None:
            await self._announce_leave(old)

    async def _change_level(self, level, x: float, y: float,
                            cached_time: Optional[int], warp_packet: bytes):
        """PlayerClient::enterLevel on a level change
        (PlayerClient.cpp:1277-1350), preceded by the warp packet when there
        is one."""
        await self._release_carried_state()
        # Membership flips synchronously, with no await between leaving the
        # old level and joining the new one: level broadcasts read
        # player.level live, so a gap would let a concurrent NPC or chest
        # update for the old level reach a client that has already switched.
        old = self._detach()
        self.x = x
        self.y = y
        self._last_absolute_position = (x, y)
        self.level = level
        level.add_player(self)

        if warp_packet:
            await self.send_raw(warp_packet)
        if old is not None:
            target = self._gmap_info(level)
            await self._announce_leave(
                old, ((target[1], target[2]) if target else (0, 0), x, y))
        horse_manager = getattr(self.server, 'horse_manager', None)
        if horse_manager is not None:
            await horse_manager.handle_player_warp(self, old, level)

        await self.send_static_level_data(level, cached_time)
        await self.send_dynamic_level_data(level)

        # :1314-1317: the client ignores Z until its first level is in.
        if not getattr(self, 'level_loaded', True):
            await self.send_props({PLPROP.Z: int(self.z)})

        # :1328-1339: everybody else learns the new location (the minimap).
        minimap = build_other_player_props(self.id, {
            PLPROP.CURLEVEL: self.wire_level_name(level),
            PLPROP.X: self.x,
            PLPROP.Y: self.y,
        })
        broadcast_to_all = getattr(self.server, 'broadcast_to_all', None)
        if broadcast_to_all is not None:
            await broadcast_to_all(minimap, exclude={self.id})

        npc_manager = getattr(self.server, 'npc_manager', None)
        if npc_manager is not None:
            await npc_manager.on_player_enters(self, level)

    # -- same level ---------------------------------------------------------

    async def _set_position(self, level, x: float, y: float):
        """PlayerClient::setPosition (PlayerClient.cpp:1182-1201): the
        position props are set as the server, which runs the gmap hop for a
        new map position and sends what changed back to the source; the
        shared ones go to the other players too (PlayerProps.cpp:1225-1248)."""
        changed = {}
        if x != self.x:
            changed[PLPROP.X2] = x
        if y != self.y:
            changed[PLPROP.Y2] = y
        self.x, self.y = x, y
        self._last_absolute_position = (x, y)
        info = self._gmap_info(level)
        here = self._gmap_info(self.level)
        if info and here and level is not self.level:
            gmap, gx, gy = info
            _, cx, cy = here
            axes = []
            if gx != cx:
                axes.append((PLPROP.GMAPLEVELX, gx))
            if gy != cy:
                axes.append((PLPROP.GMAPLEVELY, gy))
            changed.update(axes)
            await self.gmap_hop(gmap, axes)
        if changed:
            await self.send_props(changed)
            await self.server.broadcast_nearby(
                self.level.name, build_other_player_props(self.id, changed),
                exclude={self.id})

    # -- gmap hops ----------------------------------------------------------

    async def gmap_hop(self, gmap, axes) -> None:
        """Run the GMAPLEVELX / GMAPLEVELY setters (PlayerProps.cpp:905-973)
        for `axes`, (prop, new value) pairs in the order they are set. Each
        records the cell being left as visited and sends the dynamic data of
        the cell entered, so a diagonal hop passes through the cell at the
        new value of the first axis and the old value of the second, which
        is recorded too. No leave or enter packets, no static data and no
        PLO_LEVELMODTIME."""
        here = self._gmap_info(self.level)
        if here is None:
            return
        _, cx, cy = here
        for prop, value in axes:
            if prop == PLPROP.GMAPLEVELX:
                cx = value
            else:
                cy = value
            name = gmap.get_level_at(cx, cy)
            target = self.server.world.get_level(name) if name else None
            if target is None:
                # A cell with no level: the reference sends nothing for it
                # and records nothing when leaving it (leaveSubLevel(nullptr)).
                continue
            await self._hop_to(target)

    async def apply_client_map_position(self, axes) -> None:
        """GMAPLEVELX / GMAPLEVELY from PLI_PLAYERPROPS, in wire order
        (PlayerProps.cpp:905-973, setPropsFromPacket :1191-1210). An unchanged
        value is ignored; off a gmap, or outside the map, the prop is sent
        back with the current value (SETPROP_RETURN_REVERT). pygserver also
        reverts a hop whose final cell has no level, since it cannot keep a
        player on a cell without one."""
        info = self._gmap_info(self.level)
        cx, cy = (info[1], info[2]) if info else (0, 0)
        current = {PLPROP.GMAPLEVELX: cx, PLPROP.GMAPLEVELY: cy}
        reverts = {}
        moves = []
        for prop, value in axes:
            if value == current[prop]:
                continue
            if info is None:
                reverts[prop] = current[prop]
                continue
            limit = info[0].width if prop == PLPROP.GMAPLEVELX else info[0].height
            if not 0 <= value < limit:
                reverts[prop] = current[prop]
                continue
            moves.append((prop, value))
        if moves:
            fx, fy = cx, cy
            for prop, value in moves:
                if prop == PLPROP.GMAPLEVELX:
                    fx = value
                else:
                    fy = value
            name = info[0].get_level_at(fx, fy)
            if name and self.server.world.get_level(name) is not None:
                await self.gmap_hop(info[0], moves)
            else:
                reverts.update((prop, current[prop]) for prop, _ in moves)
        if reverts:
            await self.send_props(reverts)

    async def _hop_to(self, target) -> None:
        old = self.level
        if old is target:
            return
        self._record_leave(old)
        old.remove_player(self)
        self.level = target
        target.add_player(self)
        npc_manager = getattr(self.server, 'npc_manager', None)
        if npc_manager is not None:
            await npc_manager.on_player_leaves(self, old)
        horse_manager = getattr(self.server, 'horse_manager', None)
        if horse_manager is not None:
            await horse_manager.handle_player_warp(self, old, target)
        await self.send_dynamic_level_data(target)
        if npc_manager is not None:
            await npc_manager.on_player_enters(self, target)

    # -- level data ---------------------------------------------------------

    @staticmethod
    def board_bytes(level) -> bytes:
        """BOARDPACKET then each extra layer, raw (PlayerClient.cpp:1474-1485)."""
        out = bytearray(raw_packet(build_board_packet_raw(level.get_base_board_packet())))
        for layer, tiles in sorted(level.board_layers.items()):
            out += raw_packet(build_board_layer_raw(layer, bytes(tiles)))
        return bytes(out)

    async def send_static_level_data(self, level, cached_time: Optional[int]):
        """sendStaticLevelData (PlayerClient.cpp:1446-1522).

        The board goes out unless the player has left this level before AND
        the client's cached time is not older than the level's (:1471). No
        cached time (a plain warp or any server warp) counts as the level's
        own time (:1454). Chests always go; an empty PLO_LEVELBOARD tells the
        client no board is coming (:1507-1510). The mod time and the
        player's own level name close the block (:1513, :1518)."""
        level_mod_time = level.mod_time
        visited = self.visited_levels.visited(level.name)
        if cached_time is None:
            cached_time = level_mod_time

        out = bytearray(build_level_name(level.name))
        if not getattr(self, 'level_loaded', True):
            # :1465-1466: a blank board before the first one of the session.
            out += build_level_board(b"")
        sent_board = False
        if not visited or cached_time < level_mod_time:
            out += self.board_bytes(level)
            sent_board = True
            # Withheld only with the NPC server present and the matching
            # clientside option off (:1495-1500; link_sign_touch).
            config = getattr(self.server, 'config', None)
            if level_data_has_links(config):
                for link in level.get_links():
                    out += build_level_link(
                        link['dest_level'], link['x'], link['y'],
                        link['width'], link['height'],
                        link['dest_x'], link['dest_y'])
            if level_data_has_signs(config):
                for (sx, sy), text in level.get_signs().items():
                    out += build_level_sign(sx, sy, text)
        item_manager = getattr(self.server, 'item_manager', None)
        if item_manager is not None:
            out += item_manager.chest_packets(self, level)
        if not sent_board:
            out += build_level_board(b"")
        out += build_level_modtime(level_mod_time)
        out += build_level_name(self.wire_level_name() or level.name)
        await self.send_raw(bytes(out))
        await self._inform_if_level_leader()

    async def send_dynamic_level_data(self, level):
        """sendDynamicLevelData (PlayerClient.cpp:1524-1605): board changes,
        then horses and baddies (not on a gmap), the ghost icon, the leader
        flag, the NPCs and the props exchange.

        On a gmap the NPCs are those of the 3x3 around the player's cell
        (Level.cpp:1450), every one filtered by the time the player last left
        the cell entered (:1534, passed at :1552); a neighbour's own record
        is never read. Cells shared with the previous 3x3 go out again, as
        deltas or whole, and cells leaving range get nothing."""
        since = self.visited_levels.left_at(level.name)
        board_changes = getattr(self.server, 'board_changes', None)
        if board_changes is not None:
            # Every change on every entry for v6 (Level.cpp:483-530).
            from .handlers.versions import client_version
            packets = board_changes.join_packets(level, client_version(self))
            if packets:
                await self.send_raw(b"".join(packets))
        if not self._gmap_info(level):
            horse_manager = getattr(self.server, 'horse_manager', None)
            if horse_manager is not None:
                await horse_manager.send_level_horses(self, level)
            baddy_manager = getattr(self.server, 'baddy_manager', None)
            if baddy_manager is not None:
                await baddy_manager.send_level_baddies(self, level)
        await self.send_raw(build_ghost_icon(False))
        await self._inform_if_level_leader()

        # PLO_SETACTIVELEVEL names the NPC's owner before the first NPC and
        # again whenever the owner changes (Level.cpp:1455-1459), then the
        # player's level closes the block (PlayerClient.cpp:1555). On a gmap
        # every name is the gmap's: its NPCs belong to the gmap level.
        active = None
        for npc in self.server.audience.npcs_in_range_for_data(level.name):
            owner = npc.wire_level_name()
            if owner != active:
                await self.send_raw(build_set_active_level(owner))
                active = owner
            await self._send_npc(npc, since)
        await self.send_raw(build_set_active_level(self.wire_level_name(level)))

        # Exchange props with the players in sync range (:1587-1602): on a
        # gmap the syncdistancex/y box around the player, else the level.
        x, y = absolute_position(self)
        for other in self.server.audience.players_in_range_for_sync(
                level.name, x, y, exclude={self.id}):
            await self.send_raw(other.build_props_packet())
            await other.send_raw(self.build_props_packet())

    async def _send_npc(self, npc, since: Optional[float]):
        """Level::sendNPCToPlayer (Level.cpp:1466-1488): the props stamped at
        or after `since` (all of them on a first visit), the bytecode only
        when props went, then the show-images and the move queue since."""
        props = npc.props_since(since)
        if props:
            await self.send_raw(build_npc_props(npc.id, props))
            bytecode = npc.build_bytecode_packet()
            if bytecode:
                await self.send_raw(bytecode)
        showimgs = npc.showimgs_since(since)
        if showimgs is not None:
            await self.send_raw(showimgs)
        if npc.move_queue:
            from .npc_motion import send_queue_to_player
            await send_queue_to_player(npc, self.server, self, since=since)

    async def _send_level(self, level: 'Level'):
        """The whole level as a fresh entry, with no warp packet: static
        data with no cached time, then the dynamic data."""
        await self.send_static_level_data(level, None)
        await self.send_dynamic_level_data(level)
