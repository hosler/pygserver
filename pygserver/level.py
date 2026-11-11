"""
pygserver.level - Level/map management

Handles loading, storing, and managing level data including
tiles, NPCs, items, signs, and links.
"""

import logging
import os
import struct
import tempfile
import time
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple
from pathlib import Path

from reborn_protocol.coords import LEVEL_SIZE, in_level_bounds, level_index

from . import tiletypes
from .board_changes import BoardChange, BoardChangeList

if TYPE_CHECKING:
    from .player import Player
    from .npc import NPC

logger = logging.getLogger(__name__)


class LevelLoadError(ValueError):
    """A level file the reference loader refuses (too short to carry a version)."""


# Item name -> id, mirrors GServer-v2 LevelItem::ItemNames (index == LevelItemType).
from .protocol.constants import LevelItemType as _LevelItemType
ITEM_NAME_TO_ID: Dict[str, int] = {
    t.name.lower(): int(t) for t in _LevelItemType if int(t) >= 0
}

# Baddy name -> type id, mirrors GServer-v2 LevelBaddy::BaddyNames. "spider"
# is a name-only alias for octopus (upstream 580c4888: BaddyType has no
# separate SPIDER value, "if using, spider needs to be manually mapped to
# octopus" - LevelBaddy.h), not a new baddy type.
BADDY_NAME_TO_TYPE: Dict[str, int] = {
    "graysoldier": 0, "bluesoldier": 1, "redsoldier": 2, "shootingsoldier": 3,
    "swampsoldier": 4, "frog": 5, "octopus": 6, "goldenwarrior": 7,
    "lizardon": 8, "dragon": 9, "spider": 6,
}
ITEM_ID_TO_NAME = {value: name for name, value in ITEM_NAME_TO_ID.items()}
BADDY_TYPE_TO_NAME = {
    value: name for name, value in BADDY_NAME_TO_TYPE.items() if name != "spider"
}


class Level:
    """
    Represents a game level (64x64 tiles).

    Handles tile data, player/NPC tracking, and level objects.
    """

    # Level dimensions
    WIDTH = LEVEL_SIZE
    HEIGHT = LEVEL_SIZE
    TILE_COUNT = WIDTH * HEIGHT  # 4096 tiles
    BOARD_SIZE = TILE_COUNT * 2  # 8192 bytes (2 bytes per tile)

    def __init__(self, name: str):
        self.name = name
        self.file_path: Optional[str] = None

        # Tile data (64x64 = 4096 tiles, 2 bytes each)
        self._tiles = bytearray(self.BOARD_SIZE)
        # The tiles as loaded (plus server-side set_tile writes). Board changes
        # never rewrite them (Level.cpp:1743): joiners are sent this board and
        # then the change list, a respawn restores from it, the bush/vase drop
        # roll reads it and a save writes it. See board_changes.py.
        self._base_tiles = bytearray(self.BOARD_SIZE)
        self._board_changes = BoardChangeList()
        self.board_layers: Dict[int, bytearray] = {}

        # Players on this level, insertion-ordered (dict[id, None] as an
        # ordered set) so the first key is always the current level leader -
        # GServer-v2's Level::getPlayers() is a std::vector, and
        # isPlayerLeader()/PLO_ISLEADER rely on "first to join, still
        # present" (see add_player/remove_player/is_player_leader below). A
        # plain Set had no join order, so leader assignment could only ever
        # guess at "lowest id currently on the level" (see the now-stale
        # caveat this replaces in gs1_host.leader_player_for_level).
        self._players: Dict[int, None] = {}

        # NPCs on this level
        self._npcs: Dict[int, 'NPC'] = {}
        self.is_sparring_zone = False

        # Level links (warps)
        self._links: List[Dict] = []

        # Signs
        self._signs: Dict[Tuple[int, int], str] = {}

        # Items on ground
        self._items: List[Dict] = []

        # Chests (parsed from file: {x, y, item, sign})
        self._chests: List[Dict] = []

        # Baddies (parsed from file: {x, y, type, verses})
        self._baddies: List[Dict] = []

        # NPC definitions (parsed from file: {x, y, image, code})
        self._npc_defs: List[Dict] = []

        # Non-empty directives outside the supported file sections. Keeping
        # these makes serialization reject data it would otherwise discard.
        self._unsupported_features: List[str] = []

    @classmethod
    def load(cls, file_path: str) -> 'Level':
        """
        Load a level from a .nw file.

        Args:
            file_path: Path to the .nw level file

        Returns:
            Loaded Level instance
        """
        path = Path(file_path)
        name = path.stem + path.suffix  # Keep extension in name

        level = cls(name)
        level.file_path = file_path

        try:
            with open(file_path, 'rb') as f:
                data = f.read()

            level._parse_nw_file(data)
            logger.debug(f"Loaded level {name} from {file_path}")

        except FileNotFoundError:
            logger.warning(f"Level file not found: {file_path}")
        except LevelLoadError:
            raise
        except Exception as e:
            logger.error(f"Error loading level {file_path}: {e}")

        return level

    # Base64-like encoding used in NW files for tile data
    BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

    def _decode_board_string(self, board_str: str) -> List[int]:
        """Decode a row of tile data from base64-like encoding."""
        tiles = []
        i = 0
        while i + 1 < len(board_str):
            char1 = board_str[i]
            char2 = board_str[i + 1]
            idx1 = self.BASE64_CHARS.find(char1)
            idx2 = self.BASE64_CHARS.find(char2)
            if idx1 >= 0 and idx2 >= 0:
                tile_id = idx1 * 64 + idx2
            else:
                tile_id = 0
            tiles.append(tile_id)
            i += 2
        return tiles

    def _encode_board_row(self, y: int, tiles=None) -> str:
        """Encode one complete tile row for a GLEVNW01 BOARD record."""
        if tiles is None:
            tiles = self._tiles
        encoded = []
        for x in range(self.WIDTH):
            index = level_index(x, y) * 2
            tile_id = tiles[index] | (tiles[index + 1] << 8)
            if not 0 <= tile_id < 4096:
                raise ValueError(f"tile ({x}, {y}) id {tile_id} is not representable")
            encoded.append(self.BASE64_CHARS[tile_id // 64])
            encoded.append(self.BASE64_CHARS[tile_id % 64])
        return ''.join(encoded)

    @staticmethod
    def _single_line(value, field: str) -> str:
        text = str(value)
        if '\n' in text or '\r' in text:
            raise ValueError(f"{field} cannot contain a newline")
        return text

    def serialize(self) -> str:
        """Return this level in the GLEVNW01 text format."""
        if self._unsupported_features:
            raise ValueError(
                "unsupported level feature: " + self._unsupported_features[0]
            )
        lines = ["GLEVNW01"]
        # The base tiles only: client board changes never reach a level file
        # (Level::saveLevel writes subLevel->getTiles(), Level.cpp:999-1030).
        for y in range(self.HEIGHT):
            lines.append(f"BOARD 0 {y} 64 0 {self._encode_board_row(y, self._base_tiles)}")

        for layer, board in sorted(self.board_layers.items()):
            for y in range(self.HEIGHT):
                x = 0
                while x < self.WIDTH:
                    start = x
                    encoded = []
                    while x < self.WIDTH:
                        index = level_index(x, y) * 2
                        tile = int.from_bytes(board[index:index + 2], 'little')
                        if tile == 65535:
                            break
                        if not 0 <= tile < 4096:
                            raise ValueError(f"layer {layer} tile {tile} is not representable")
                        encoded.append(self.BASE64_CHARS[tile // 64] + self.BASE64_CHARS[tile % 64])
                        x += 1
                    if encoded:
                        lines.append(f"BOARD {start} {y} {x - start} {layer} {''.join(encoded)}")
                    x += 1

        for link in self._links:
            required = ('dest_level', 'x', 'y', 'width', 'height',
                        'dest_x', 'dest_y')
            if any(key not in link for key in required):
                raise ValueError(f"link has missing fields: {link!r}")
            values = [self._single_line(link[key], f"link {key}")
                      for key in required]
            if not values[0]:
                raise ValueError("link destination level cannot be empty")
            lines.append("LINK " + ' '.join(values))

        for (x, y), text in self._signs.items():
            if 'SIGNEND' in str(text).splitlines():
                raise ValueError(f"sign ({x}, {y}) contains SIGNEND")
            lines.extend((f"SIGN {int(x)} {int(y)}", str(text), "SIGNEND"))

        for chest in self._chests:
            required = ('x', 'y', 'item')
            if any(key not in chest for key in required):
                raise ValueError(f"chest has missing fields: {chest!r}")
            item_id = int(chest['item'])
            item = ITEM_ID_TO_NAME.get(item_id, str(item_id))
            lines.append(
                f"CHEST {int(chest['x'])} {int(chest['y'])} {item} "
                f"{int(chest.get('sign', 0))}"
            )

        # RC eval scratch NPCs are marked `_rc_eval_scratch`; serialization
        # writes only file definitions, never live `_npcs`, so they cannot leak.
        for definition in self._npc_defs:
            required = ('x', 'y', 'image', 'code')
            if any(key not in definition for key in required):
                raise ValueError(f"NPC definition has missing fields: {definition!r}")
            image = self._single_line(definition['image'] or '-', "NPC image")
            code = str(definition['code'])
            if 'NPCEND' in code.splitlines():
                raise ValueError("NPC code contains NPCEND")
            lines.extend((
                f"NPC {image} {definition['x']} {definition['y']}",
                code, "NPCEND",
            ))

        for baddy in self._baddies:
            required = ('x', 'y', 'type', 'verses')
            if any(key not in baddy for key in required):
                raise ValueError(f"baddy definition has missing fields: {baddy!r}")
            baddy_type = int(baddy['type'])
            type_name = BADDY_TYPE_TO_NAME.get(baddy_type, str(baddy_type))
            verses = [str(verse) for verse in baddy['verses']]
            if any('\n' in verse or '\r' in verse or verse == 'BADDYEND'
                   for verse in verses):
                raise ValueError("baddy verse is not representable")
            lines.append(f"BADDY {baddy['x']} {baddy['y']} {type_name}")
            lines.extend(verses)
            lines.append("BADDYEND")

        return '\n'.join(lines) + '\n'

    def save_to_file(self, path=None) -> Path:
        """Atomically write the level, defaulting to its loaded path."""
        target = Path(path) if path is not None else (
            Path(self.file_path) if self.file_path else None
        )
        if target is None:
            raise ValueError("level has no file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode='w', encoding='latin-1', newline='\n',
                    dir=target.parent, prefix=f".{target.name}.",
                    suffix='.tmp', delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(self.serialize())
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        self.file_path = str(target)
        return target

    def _parse_nw_file(self, data: bytes):
        """
        Parse NW level file format.

        Raises LevelLoadError for data shorter than the 8-byte version header:
        the reference loader returns false there (LevelLoader.cpp:263-265,
        0baf970a) rather than registering a blank level.

        NW format sections:
        - BOARD: Tile data as base64-encoded text rows
        - LINKS: Level warps
        - SIGNS: Sign text
        - NPCS: Level NPCs
        """
        if len(data) < 8:
            raise LevelLoadError(
                f"level data is {len(data)} bytes; needs the 8-byte version header")
        # Decode to text for parsing BOARD lines
        try:
            text = data.decode('latin-1', errors='replace')
        except:
            text = data.decode('utf-8', errors='replace')

        lines = text.split('\n')

        # Parse BOARD lines - each is: "BOARD x y width height tile_data"
        # y is row number (0-63), tile_data is base64-encoded
        # LevelLoader.cpp:779-808: BOARD records include x, width and layer.
        self._tiles = bytearray(self.BOARD_SIZE)
        self.board_layers.clear()
        for line in lines:
            parts = line.split(None, 5)
            if len(parts) != 6 or parts[0] != 'BOARD':
                continue
            try:
                x, y, width, layer = map(int, parts[1:5])
            except ValueError:
                continue
            if not (0 <= x < 64 and 0 <= y < 64 and 0 < width <= 64 - x
                    and 0 <= layer <= 255 and len(parts[5].strip()) >= width * 2):
                continue
            if layer == 0:
                board = self._tiles
            else:
                board = self.board_layers.setdefault(
                    layer, bytearray(b'\xff\xff' * self.TILE_COUNT))
            for offset, tile_id in enumerate(self._decode_board_string(parts[5])[:width]):
                index = level_index(x + offset, y) * 2
                board[index:index + 2] = tile_id.to_bytes(2, 'little')
        self._base_tiles = bytearray(self._tiles)
        self._board_changes.clear()

        # Parse line-based features (GLEVNW01 text format): LINK, SIGN/SIGNEND,
        # CHEST, BADDY/BADDYEND. Mirrors GServer-v2 LevelLoader.
        self._parse_features(lines)

    def _parse_features(self, lines: List[str]):
        """Parse LINK/SIGN/CHEST/BADDY entries from GLEVNW01 lines."""
        i = 0
        n = len(lines)
        while i < n:
            raw = lines[i].rstrip('\r\n')
            i += 1
            stripped = raw.strip()
            if not stripped:
                continue
            parts = stripped.split()
            section = parts[0]

            if section == 'LINK' and len(parts) >= 8:
                # LINK destLevel... x y w h destX destY (level name may contain spaces)
                try:
                    dest_level = ' '.join(parts[1:-6])
                    x, y, w, h = (int(parts[-6]), int(parts[-5]),
                                  int(parts[-4]), int(parts[-3]))
                    self._links.append({
                        'dest_level': dest_level,
                        'x': x, 'y': y, 'width': w, 'height': h,
                        'dest_x': parts[-2], 'dest_y': parts[-1],
                    })
                except ValueError:
                    pass

            elif section == 'SIGN' and len(parts) == 3:
                try:
                    sx, sy = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                text_lines = []
                while i < n and lines[i].strip() != 'SIGNEND':
                    text_lines.append(lines[i].rstrip('\r\n'))
                    i += 1
                i += 1  # skip SIGNEND
                self._signs[(sx, sy)] = '\n'.join(text_lines)

            elif section == 'CHEST' and len(parts) >= 4:
                try:
                    cx, cy = int(parts[1]), int(parts[2])
                    item_id = ITEM_NAME_TO_ID.get(parts[3].lower())
                    if item_id is None:
                        item_id = int(parts[3])
                    sign = int(parts[4]) if len(parts) >= 5 else 0
                    self._chests.append({
                        'x': cx, 'y': cy, 'item': item_id, 'sign': sign,
                    })
                except (ValueError, KeyError):
                    pass

            elif section == 'NPC' and len(parts) >= 3:
                # NPC <image...> <x> <y>, then GS1 code until NPCEND.
                try:
                    nx, ny = float(parts[-2]), float(parts[-1])
                except ValueError:
                    continue
                image = ' '.join(parts[1:-2]).strip()
                if image == '-':
                    image = ''
                code_lines = []
                while i < n and lines[i].strip() != 'NPCEND':
                    code_lines.append(lines[i].rstrip('\r\n'))
                    i += 1
                i += 1  # skip NPCEND
                self._npc_defs.append({
                    'x': nx, 'y': ny, 'image': image,
                    'code': '\n'.join(code_lines),
                })

            elif section == 'BADDY' and len(parts) == 4:
                try:
                    bx, by = float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                btype = BADDY_NAME_TO_TYPE.get(parts[3].lower())
                if btype is None:
                    try:
                        btype = int(parts[3])
                    except ValueError:
                        btype = 0
                verses = []
                while i < n and lines[i].strip() != 'BADDYEND':
                    verses.append(lines[i].rstrip('\r\n'))
                    i += 1
                i += 1  # skip BADDYEND
                self._baddies.append({
                    'x': bx, 'y': by, 'type': btype, 'verses': verses,
                })

            elif section not in ('GLEVNW01', 'BOARD'):
                self._unsupported_features.append(stripped)

    def get_tile(self, x: int, y: int) -> int:
        """Get tile ID at position."""
        if in_level_bounds(x, y):
            idx = level_index(x, y) * 2
            return self._tiles[idx] | (self._tiles[idx + 1] << 8)
        return 0

    def get_base_tile(self, x: int, y: int) -> int:
        """Tile ID in the base array (the level as loaded; board changes never
        touch it). The bush/vase drop roll reads this
        (PlayerClientPackets.cpp:144-148)."""
        if in_level_bounds(x, y):
            idx = level_index(x, y) * 2
            return self._base_tiles[idx] | (self._base_tiles[idx + 1] << 8)
        return 0

    def set_tile(self, x: int, y: int, tile_id: int):
        """Set tile ID at position, permanently: the base array follows, and so
        does every board change over the cell, pending or respawned, so neither
        a respawn nor a joiner's replay brings the client's tile back. Client
        board edits go through alter_board instead.

        The reference routes script tile writes through its own board-change
        path (Level::applyBoardChangeFromScriptTiles -> alterBoard,
        Level.cpp:1751-1799, on `updateboard`); pygserver's script writes are
        immediate, so the entries over the cell are patched instead."""
        if in_level_bounds(x, y):
            idx = level_index(x, y) * 2
            for board in (self._tiles, self._base_tiles):
                board[idx] = tile_id & 0xFF
                board[idx + 1] = (tile_id >> 8) & 0xFF
            self._board_changes.patch_cell(level_index(x, y), tile_id & 0xFFFF)

    # -- board changes (board_changes.py) --------------------------------------

    @staticmethod
    def _write_rect(board: bytearray, x: int, y: int, width: int, height: int,
                    tiles: List[int]) -> None:
        """Write a rectangle of tile ids (row by row) into one board array."""
        if (width > 0 and height > 0 and len(tiles) >= width * height
                and in_level_bounds(x, y)
                and in_level_bounds(x + width - 1, y + height - 1)):
            # Whole rows at once: a 64x64 change is on the board-change path.
            row_format = '<%dH' % width
            for dy in range(height):
                start = level_index(x, y + dy) * 2
                row = tiles[dy * width:(dy + 1) * width]
                board[start:start + 2 * width] = struct.pack(
                    row_format, *[tile & 0xFFFF for tile in row])
            return
        index = 0
        for dy in range(height):
            for dx in range(width):
                if index >= len(tiles):
                    return
                tx, ty = x + dx, y + dy
                if in_level_bounds(tx, ty):
                    idx = level_index(tx, ty) * 2
                    board[idx] = tiles[index] & 0xFF
                    board[idx + 1] = (tiles[index] >> 8) & 0xFF
                index += 1

    def alter_board(self, x: int, y: int, width: int, height: int,
                    tiles: List[int], now: float, respawn_seconds: int,
                    allow_respawn: bool = True,
                    force_respawn: bool = False,
                    corrections: Optional[List[BoardChange]] = None
                    ) -> Optional[BoardChange]:
        """Level::alterBoard (Level.cpp:1668-1749) for one level part: record
        the change, apply it to the live board, replace older changes it
        covers. None, and nothing changed, only for a rectangle outside the
        level or too few tiles (the handler checks both first).

        When the change list is full its oldest entries are evicted
        (board_changes.py): their rectangles, rebuilt from the entries left,
        are written to the live board and appended to `corrections` for the
        caller to send to the level like a board change."""
        if not (width > 0 and height > 0 and in_level_bounds(x, y)
                and in_level_bounds(x + width - 1, y + height - 1)
                and len(tiles) >= width * height):
            return None
        do_respawn = allow_respawn and (force_respawn or (width == 2 and height == 2))
        old_tiles = None
        if do_respawn:
            old_tiles = [self.get_base_tile(x + dx, y + dy)
                         for dy in range(height) for dx in range(width)]
        change = BoardChange(
            x=x, y=y, width=width, height=height, tiles=list(tiles[:width * height]),
            old_tiles=old_tiles,
            expires_at=now + respawn_seconds if do_respawn and respawn_seconds > 0 else None,
            mod_time=now)
        evicted = self._board_changes.add(change, self._base_tiles)
        self._apply_change_tiles(change)
        for fix in evicted:
            self._apply_change_tiles(fix)
        if corrections is not None:
            corrections.extend(evicted)
        return change

    @property
    def board_changes(self) -> List[BoardChange]:
        """The change list in order (what a late joiner is replayed)."""
        return list(self._board_changes)

    def next_board_expiry(self) -> Optional[float]:
        """When the earliest running board-change timer is due, if any."""
        return self._board_changes.next_expiry()

    def _apply_change_tiles(self, change: BoardChange) -> None:
        """Show `change.tiles` on the live board (the base array is untouched)."""
        self._write_rect(self._tiles, change.x, change.y, change.width,
                         change.height, change.tiles)

    def expire_board_changes(self, now: float) -> List[BoardChange]:
        """LevelBoardChange::update (LevelBoardChange.cpp:39-50) for every
        change that is due: swap it, show its old tiles on the live board and
        return it so the caller can tell the level."""
        done = self._board_changes.expire(now)
        for change in done:
            self._apply_change_tiles(change)
        return done

    def clear_board_changes(self) -> None:
        """Level reload drops the list (Level.cpp:656)."""
        self._board_changes.clear()

    def tile_type_at(self, x: int, y: int,
                     tileset: int = tiletypes.TILESET_CLASSIC) -> int:
        """Tile type at whole-tile (x, y) of THIS level (Level::getTileTypeAt,
        Level.cpp:3083-3101): outside 0..63 is BLOCKING and an id >= 4096 is
        BLOCKING. Neighbouring gmap segments are resolved by
        collision.tile_type_at, which calls this for the in-range case."""
        if not in_level_bounds(x, y):
            return tiletypes.BLOCKING
        return tiletypes.strict_tile_type(self.get_tile(x, y), tileset)

    def is_on_wall(self, x: int, y: int,
                   tileset: int = tiletypes.TILESET_CLASSIC) -> bool:
        """Level::isOnWall(tile) (Level.cpp:2872-2884): throw-through, jump
        stone and blocking tiles are walls."""
        return self.tile_type_at(x, y, tileset) in tiletypes.WALL_TYPES

    #: Older spelling kept for callers that already use it.
    is_blocking = is_on_wall

    def get_board_packet(self) -> bytes:
        """Get the live tile data as board packet (8192 bytes)."""
        return bytes(self._tiles)

    def get_base_board_packet(self) -> bytes:
        """The base tiles, what a player entering the level is sent before the
        board change list (BoardChangeManager.join_packets)."""
        return bytes(self._base_tiles)

    def add_player(self, player: 'Player'):
        """Add a player to this level."""
        self._players[player.id] = None

    def remove_player(self, player: 'Player'):
        """Remove a player from this level."""
        self._players.pop(player.id, None)

    def get_player_ids(self) -> List[int]:
        """Get IDs of players on this level, in join order (first = leader)."""
        return list(self._players.keys())

    def get_leader_id(self) -> Optional[int]:
        """ID of this level's leader (the first player who joined and is
        still present), or None if the level is empty. GServer-v2
        Level::getPlayers().front() / Level::isPlayerLeader."""
        return next(iter(self._players), None)

    def is_player_leader(self, player: 'Player') -> bool:
        """True if `player` is this level's leader. Backs PLO_ISLEADER
        assignment/handoff (Player._send_level, Player.warp, Player._cleanup)
        and gates leader-only packets like PLI_BADDYPROPS."""
        return self.get_leader_id() == player.id

    def add_npc(self, npc: 'NPC'):
        """Add an NPC to this level."""
        self._npcs[npc.id] = npc
        npc.level = self
        self.check_sparring_zone(npc.client_script)

    def check_sparring_zone(self, client_script: str):
        """Cache the zone flag when an NPC is added or its script attached."""
        # GServer-v2/server/src/level/Level.cpp:1563-1566: trim left, then
        # test the client script prefix. The flag stays set after NPC removal.
        if client_script.lstrip(' \t\r\n\v\f\xa7').startswith('sparringzone'):
            self.is_sparring_zone = True

    def remove_npc(self, npc: 'NPC'):
        """Remove an NPC from this level."""
        self._npcs.pop(npc.id, None)
        if npc.level == self:
            npc.level = None

    def get_npcs(self) -> List['NPC']:
        """Get all NPCs on this level."""
        return list(self._npcs.values())

    def get_npc(self, npc_id: int) -> Optional['NPC']:
        """Get NPC by ID."""
        return self._npcs.get(npc_id)

    def get_links(self) -> List[Dict]:
        """Get all level links."""
        return self._links.copy()

    def get_sign(self, x: int, y: int) -> Optional[str]:
        """Get sign text at position."""
        return self._signs.get((x, y))

    def get_signs(self) -> Dict[Tuple[int, int], str]:
        """Get all signs as {(x, y): text}."""
        return self._signs.copy()

    def get_chest_defs(self) -> List[Dict]:
        """Get chest definitions parsed from the level file."""
        return self._chests.copy()

    def get_baddy_defs(self) -> List[Dict]:
        """Get baddy definitions parsed from the level file."""
        return self._baddies.copy()

    def get_npc_defs(self) -> List[Dict]:
        """Get NPC definitions parsed from the level file."""
        return self._npc_defs.copy()

    def check_warp(self, x: float, y: float) -> Optional[Dict]:
        """
        Check if position triggers a warp link.

        Returns link info if position is inside a link, None otherwise.
        """
        for link in self._links:
            if (link['x'] <= x < link['x'] + link['width'] and
                link['y'] <= y < link['y'] + link['height']):
                return link
        return None


class LevelManager:
    """
    Manages level loading and caching.
    """

    LEVEL_SUFFIXES = ('.nw', '.graal', '.zelda')

    # Minimum spacing between directory rescans triggered by lookup misses.
    # PLI_ADJACENTLEVEL hands client-chosen names to get_level, so a miss
    # must stay cheap instead of walking the whole tree every time.
    RESCAN_INTERVAL = 5.0

    def __init__(self, levels_dir: str = "levels"):
        self.levels_dir = levels_dir
        self._levels: Dict[str, Level] = {}
        self._paths: Optional[Dict[str, Path]] = None
        self._last_scan = 0.0

    def level_paths(self, refresh: bool = False) -> Dict[str, Path]:
        """Map bare level file names to paths, searching subdirectories.

        Real worlds keep levels in nested folders (e.g. levels/playerbase/
        room0.nw) and scripts/links name them by file name alone; the
        reference indexes its level folders recursively (GServer-v2
        server/src/filesystem/FileSystem.cpp:91). First occurrence wins on
        duplicate names, in sorted path order, so lookups are deterministic.
        """
        if self._paths is None or refresh:
            paths: Dict[str, Path] = {}
            root = Path(self.levels_dir)
            if root.is_dir():
                for path in sorted(root.rglob('*')):
                    if path.is_file() and path.suffix.lower() in self.LEVEL_SUFFIXES:
                        if path.name in paths:
                            logger.warning(
                                f"Duplicate level name {path.name}: using "
                                f"{paths[path.name]}, ignoring {path}")
                            continue
                        paths[path.name] = path
            self._paths = paths
            self._last_scan = time.monotonic()
        return self._paths

    def find_level_path(self, name: str) -> Optional[Path]:
        """Resolve a level name (with or without extension) to its file."""
        if not name or '/' in name or '\\' in name or name.startswith('.'):
            return None
        paths = self.level_paths()
        candidates = [name] + [f"{name}{ext}" for ext in self.LEVEL_SUFFIXES]
        for candidate in candidates:
            path = paths.get(candidate)
            if path is not None:
                return path
        # A level added after startup: rescan before giving up, but never
        # more often than RESCAN_INTERVAL so repeated misses stay cheap.
        if time.monotonic() - self._last_scan >= self.RESCAN_INTERVAL:
            paths = self.level_paths(refresh=True)
            for candidate in candidates:
                path = paths.get(candidate)
                if path is not None:
                    return path
        return None

    def get_level(self, name: str) -> Optional[Level]:
        """
        Get a level by name, loading if necessary.

        Args:
            name: Level filename (e.g., "onlinestartlocal.nw")

        Returns:
            Level instance, or None if not found
        """
        # Check cache
        if name in self._levels:
            return self._levels[name]

        level_path = self.find_level_path(name)
        if level_path is None:
            return None
        try:
            level = Level.load(str(level_path))
        except LevelLoadError as e:
            logger.warning(f"Refusing level {level_path}: {e}")
            return None
        self._levels[name] = level
        return level

    def get_loaded_level(self, name: str) -> Optional[Level]:
        """A level already in the cache; never loads one."""
        return self._levels.get(name)

    def add_level(self, level: Level):
        """Add a level to the cache."""
        self._levels[level.name] = level

    def get_all_levels(self) -> List[Level]:
        """Get all loaded levels."""
        return list(self._levels.values())
