"""Client-side versus server-side level links and signs.

Three options decide who handles a level's links and signs, as in the
reference server (GServer-v2/server/src):

- `serverside` decides whether the NPC server exists
  (Server.cpp:973-986; `hasNPCServer()`, include/Server.h:435). pygserver has
  no separate NPC-server object: `config.serverside` IS that presence, the
  same reading flag_is_public and the HASNPCSERVER login packet already use.
- `clientsidelinks` / `clientsidesigns` (include/Server.h:154-155) send them
  to the client anyway.

With the NPC server present and the option off, the links and signs are
withheld from the level data (PlayerClient.cpp:1495-1500) and the server
tests every player position or facing change itself (PlayerClient.cpp:
1775-1906, called from PlayerProps.cpp:466-546 and :1080-1115).

Not modelled: the reference also always sends links on a classic bigmap
(`isOnBigMap`, PlayerClient.cpp:1495; no testing there, :1870-1872).
pygserver parses `bigmaps` text only and has no bigmap level, so that clause
has nothing to apply to here.
"""

import ast
import math
import operator
from typing import List, NamedTuple, Optional

from .attachments import absolute_position
from .protocol.constants import PLPROP
from .protocol.packets import build_say2

# PlayerClient.cpp:1858 (links) and :1775-1790 (NPC touch uses its own box).
LINK_TEST_OFFSETS = ((24, 16), (0, 32), (24, 56), (48, 32))
PIXELS_PER_TILE = 16
SEGMENT_TILES = 64


def server_tests_links(config) -> bool:
    """True when the server, not the client, detects link touches."""
    if config is None:
        return False
    return bool(getattr(config, 'serverside', True)) and not getattr(
        config, 'clientsidelinks', False)


def server_tests_signs(config) -> bool:
    """True when the server, not the client, detects sign reads."""
    if config is None:
        return False
    return bool(getattr(config, 'serverside', True)) and not getattr(
        config, 'clientsidesigns', False)


def level_data_has_links(config) -> bool:
    """PlayerClient.cpp:1495: links go with the board unless withheld."""
    return not server_tests_links(config)


def level_data_has_signs(config) -> bool:
    """PlayerClient.cpp:1499: signs go with the board unless withheld."""
    return not server_tests_signs(config)


class Touch(NamedTuple):
    """One testForTouch call: the movement direction and the character
    state (local pixels, facing) at that moment."""
    direction: int
    pixel_x: int
    pixel_y: int
    facing: int
    # Where the call came from: the index of the prop in the packet and the
    # sprite byte in force, so the props after it can be tested again once
    # a link warp has moved the player (PlayerProps.cpp:1191-1210).
    prop_index: int = -1
    sprite: int = 0


_TOUCH_PROPS = frozenset((PLPROP.X, PLPROP.X2, PLPROP.Y, PLPROP.Y2, PLPROP.SPRITE))


def _pixel(tiles: float) -> int:
    return int(round(tiles * PIXELS_PER_TILE))


def touches_for_props(player, props, state=None,
                      first_index=0) -> List[Touch]:
    """The testForTouch calls a PLI_PLAYERPROPS packet makes, in wire order
    (PlayerProps.cpp:466-501 X/Y, :543-546 SPRITE, :1080-1115 X2/Y2). An
    unchanged value is ignored before the test (:469, :488), so standing
    still never triggers one. Reads the player's state BEFORE the packet is
    applied. A player attached to an NPC reports relative offsets, which
    are not level positions, so it makes no tests. `state` is (pixel x,
    pixel y, facing, sprite byte) to start from instead of the player's, and
    `first_index` the packet position of the first of `props`."""
    config = getattr(player.server, 'config', None)
    if not (server_tests_links(config) or server_tests_signs(config)):
        return []
    if not _TOUCH_PROPS.intersection(props):
        return []
    npc_id = getattr(player, 'attach_npc', (0, 0))[1]
    if npc_id or vars(player).get('_attachment_position_unknown', False):
        return []
    if state is None:
        px, py = _pixel(player.x), _pixel(player.y)
        facing = player.direction & 3
        sprite_byte = player.sprite
    else:
        px, py, facing, sprite_byte = state
    touches: List[Touch] = []
    for index, (prop, value) in enumerate(props.items(), first_index):
        if prop in (PLPROP.X, PLPROP.X2):
            new = _pixel(value / 2.0) if prop == PLPROP.X else _pixel(value)
            if new == px:
                continue
            direction = 2 + max(-1, min(1, new - px))
            px = new
            touches.append(Touch(direction, px, py, facing, index, sprite_byte))
        elif prop in (PLPROP.Y, PLPROP.Y2):
            new = _pixel(value / 2.0) if prop == PLPROP.Y else _pixel(value)
            if new == py:
                continue
            direction = 1 + max(-1, min(1, new - py))
            py = new
            touches.append(Touch(direction, px, py, facing, index, sprite_byte))
        elif prop == PLPROP.SPRITE:
            if value == sprite_byte:
                continue
            sprite_byte = value
            if (value & 3) != facing:
                facing = value & 3
                touches.append(Touch(facing, px, py, facing, index, sprite_byte))
    return touches


_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: math.fmod,
}


def _expression_value(text: str, player_x: float, player_y: float) -> float:
    """Arithmetic over numbers, `playerx` and `playery`, in tiles. The
    reference hands a complex destination to the GS1 math evaluator and
    falls back to 0 when it fails (LevelLink.cpp:119-130,
    ScriptEngineGS1.cpp:508-553); only arithmetic is modelled here."""
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == 'playerx':
            return player_x
        if isinstance(node, ast.Name) and node.id == 'playery':
            return player_y
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            return _OPERATORS[type(node.op)](walk(node.left), walk(node.right))
        raise ValueError(text)

    if len(text) > 64:
        return 0.0
    try:
        return walk(ast.parse(text.strip(), mode='eval'))
    except (ValueError, SyntaxError, ZeroDivisionError, RecursionError):
        return 0.0


def link_destination_pixel(text: str, own_pixel: int, player_pixel_x: int,
                           player_pixel_y: int) -> int:
    """LevelLink::getDestinationForCharacter (LevelLink.cpp:31-37, :117-138)
    for one axis, in pixels. `playerx` is the player's local pixel X and
    `playery` the local pixel Y whichever field they appear in, so a
    `dest_x` of `playery` takes the Y. A number is tiles * 16, anything else
    is evaluated. `-1` is rewritten to the field's own axis name when the
    link is parsed (:57-69, :98-104), so it keeps `own_pixel`."""
    word = text.strip()
    if word == '-1':
        return own_pixel
    if word == 'playerx':
        return player_pixel_x
    if word == 'playery':
        return player_pixel_y
    try:
        return int(float(word) * PIXELS_PER_TILE)
    except ValueError:
        value = _expression_value(
            word.lower(), player_pixel_x / PIXELS_PER_TILE,
            player_pixel_y / PIXELS_PER_TILE)
        return int(value * PIXELS_PER_TILE)


def _signs_in_reference_order(player):
    """Level::getSigns (Level.cpp:1204-1226): off a gmap the level's signs;
    on one every segment's signs, the segments in cell-index order (row by
    row, Level.h:504-514), each in file order. The reference holds every
    part of a gmap from the moment it loads the gmap (LevelLoader.cpp:
    198-226), so every segment counts. `get_level` reads a segment from disk
    once and the level manager caches it, so later packets cost a dict
    lookup. A segment with no file is skipped, as a null part is
    (Level.cpp:1218)."""
    info = player._gmap_info(player.level)
    if info is None:
        yield from player.level.get_signs().items()
        return
    gmap = info[0]
    for cell_y in range(gmap.height):
        for cell_x in range(gmap.width):
            name = gmap.get_level_at(cell_x, cell_y)
            segment = player.server.world.get_level(name) if name else None
            if segment is not None:
                yield from segment.get_signs().items()


def _sign_hit(player, touch: Touch) -> Optional[str]:
    """testForSigns (PlayerClient.cpp:1833-1853): facing up AND moving up,
    the same local pixel row as the sign tile, within [-24, +8] pixels of
    its column. On a gmap the reference walks the signs of every segment
    (Level.cpp:1204-1226), so a sign of another segment at the same local
    pixel position answers too; the first hit wins."""
    if touch.facing != 0 or touch.direction != 0:
        return None
    for (tile_x, tile_y), text in _signs_in_reference_order(player):
        sign_x, sign_y = tile_x * PIXELS_PER_TILE, tile_y * PIXELS_PER_TILE
        if touch.pixel_y == sign_y and sign_x - 24 <= touch.pixel_x <= sign_x + 8:
            return text
    return None


def _link_hit(player, touch: Touch):
    """testForLinks lookup (PlayerClient.cpp:1856-1876, Level::getLink
    Level.cpp:2550-2571): the segment under the test point, an inclusive
    tile box, and on a gmap the probable map links skipped
    (LevelLink.cpp:142-149). Returns (segment, link) or None."""
    offset_x, offset_y = LINK_TEST_OFFSETS[touch.direction]
    info = player._gmap_info(player.level)
    world_x, world_y = touch.pixel_x + offset_x, touch.pixel_y + offset_y
    if info is not None:
        gmap, cell_x, cell_y = info
        world_x += cell_x * SEGMENT_TILES * PIXELS_PER_TILE
        world_y += cell_y * SEGMENT_TILES * PIXELS_PER_TILE
    if world_x < 0 or world_y < 0:
        return None
    tile_x, tile_y = world_x // PIXELS_PER_TILE, world_y // PIXELS_PER_TILE
    if info is None:
        if tile_x >= SEGMENT_TILES or tile_y >= SEGMENT_TILES:
            return None
        segment = player.level
    else:
        name = info[0].get_level_at(tile_x // SEGMENT_TILES,
                                    tile_y // SEGMENT_TILES)
        segment = player.server.world.get_level(name) if name else None
        if segment is None:
            return None
    local_x, local_y = tile_x % SEGMENT_TILES, tile_y % SEGMENT_TILES
    for link in segment.get_links():
        if info is not None and _probable_map_link(link):
            continue
        if (link['x'] <= local_x <= link['x'] + link['width']
                and link['y'] <= local_y <= link['y'] + link['height']):
            return segment, link
    return None


def _probable_map_link(link) -> bool:
    if (link['x'] in (0, 63) and link['width'] == 1
            and link['dest_y'] in ('playery', '-1')):
        return True
    return (link['y'] in (0, 63) and link['height'] == 1
            and link['dest_x'] in ('playerx', '-1'))


async def _warp_through(player, touch: Touch, link) -> bool:
    """The warp half of testForLinks (:1877-1905). A destination that cannot
    be loaded does nothing: no PLO_WARPFAILED from this path is reached in
    pygserver, the lookup fails first. The client time handed to the warp is
    the destination's last-left time (getLevelLastEnteredTimeForWarping,
    :1627-1630)."""
    dest = player.server.world.get_level(link['dest_level'])
    if dest is None:
        return False
    x = link_destination_pixel(link['dest_x'], touch.pixel_x,
                               touch.pixel_x, touch.pixel_y)
    y = link_destination_pixel(link['dest_y'], touch.pixel_y,
                               touch.pixel_x, touch.pixel_y)
    left = player.visited_levels.left_at(dest.name)
    await player._warp_to(dest, x / PIXELS_PER_TILE, y / PIXELS_PER_TILE,
                          None if left is None else int(left))
    return True


def _after_warp(player, props, touch: Touch) -> List[Touch]:
    """The props after the one that warped, applied to the destination: the
    tests they make start from the destination position and the facing in
    force, then the packet's own position values replace the warp's
    (PlayerProps.cpp:466-501, 1080-1115 set the pixel straight from the
    prop)."""
    rest = dict(list(props.items())[touch.prop_index + 1:])
    if not rest:
        return []
    state = (_pixel(player.x), _pixel(player.y), touch.facing, touch.sprite)
    touches = touches_for_props(player, rest, state, touch.prop_index + 1)
    for prop, value in rest.items():
        if prop == PLPROP.X:
            player.x = value / 2.0
        elif prop == PLPROP.X2:
            player.x = value
        elif prop == PLPROP.Y:
            player.y = value / 2.0
        elif prop == PLPROP.Y2:
            player.y = value
    absolute_position(player)
    return touches


async def run_touches(player, touches: List[Touch], props=None) -> bool:
    """testForTouch (PlayerClient.cpp:1775-1790) for each recorded test:
    signs, then links, stopping at the first hit of a call (a sign hit
    returns before the link test, so a sign and a link on one spot give the
    sign only). The NPC touch is skipped per call, not per packet, so this
    returns True only when EVERY call was taken by a sign or link; a call
    nothing took still gets its NPC touch from the caller.
    `absolute_position` keeps the resolved position cache current.

    After a link warp the packet goes on (Player::setPropsFromPacket applies
    its props one at a time and the warp is synchronous, PlayerProps.cpp:
    1191-1210): with `props`, the props after the one that warped set the
    destination's position and are tested there. The warp ends the call
    either way, so the result is True once one has happened."""
    config = getattr(player.server, 'config', None)
    taken = 0
    warped = False
    queue = list(touches)
    while queue:
        touch = queue.pop(0)
        level = player.level
        if level is None:
            break
        if server_tests_signs(config):
            text = _sign_hit(player, touch)
            if text is not None:
                # sendSignMessage (PlayerClient.cpp:1767-1771).
                await player.send_raw(build_say2(text.replace('\n', '#b')))
                taken += 1
                continue
        if server_tests_links(config):
            found = _link_hit(player, touch)
            if found is not None and await _warp_through(player, touch, found[1]):
                absolute_position(player)
                warped = True
                if props is None:
                    return True
                queue = _after_warp(player, props, touch)
    return warped or (bool(touches) and taken == len(touches))
