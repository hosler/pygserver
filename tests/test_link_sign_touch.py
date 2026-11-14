"""Client-side versus server-side level links and signs.

Reference (GServer-v2/server/src): `serverside` loads the NPC server
(Server.cpp:973-986); with it present, links and signs stay out of the level
data unless `clientsidelinks` / `clientsidesigns` are on (PlayerClient.cpp:
1495-1500; both read false when absent, include/Server.h:154-155). Withheld
links and signs are tested by the server on each X, X2, Y, Y2 and SPRITE
change (PlayerProps.cpp:466-546, :1080-1115 -> PlayerClient.cpp:1775-1906).
The shipped default config sets all three true
(bin/servers/default/config/serveroptions.txt:308,325,328).
"""

import asyncio
from pathlib import Path

import pytest

from pygserver.config import ServerConfig
from pygserver.level import Level
from pygserver.protocol.constants import PLO, PLPROP
from pygserver.protocol.packets import PacketBuilder, PacketReader

from .test_level_entry_cache import MOD_TIME, make_player, make_server, run, sent, write_level
from .test_player_props_inbound import props_payload

FUNTIMES_PYGSERVER = Path(__file__).resolve().parents[2] / "funtimes-pygserver"


def level_with(tmp_path, name, links=(), signs=None):
    level = Level(name)
    level._links.extend(links)
    level._signs.update(signs or {})
    path = tmp_path / name
    path.write_text(level.serialize(), encoding="latin-1")
    import os
    os.utime(path, (MOD_TIME, MOD_TIME))
    return Level.load(str(path))


DOOR = {"dest_level": "b.nw", "x": 20, "y": 10, "width": 2, "height": 2,
        "dest_x": "15", "dest_y": "16"}


def make_world(tmp_path, links=(DOOR,), signs=None, **options):
    options.setdefault("serverside", True)
    options.setdefault("clientsidelinks", False)
    options.setdefault("clientsidesigns", False)
    server = make_server(tmp_path, **options)
    server.world.add_level(level_with(tmp_path, "a.nw", links, signs))
    server.world.add_level(level_with(tmp_path, "b.nw"))
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    player.send_raw.reset_mock()
    return server, player


def stand(player, x, y, facing=2):
    player.x, player.y = x, y
    player.direction = player.sprite = facing


def move(player, *pairs):
    run(player._handle_player_props(props_payload(list(pairs))))


def ids(player):
    return [packet_id for packet_id, _body in sent(player)]


def entry_ids(tmp_path, **options):
    """Packet ids sent when a fresh player enters a.nw (link and sign set)."""
    options.setdefault("serverside", True)
    options.setdefault("clientsidelinks", False)
    options.setdefault("clientsidesigns", False)
    server = make_server(tmp_path, **options)
    server.world.add_level(level_with(tmp_path, "a.nw", (DOOR,), {(5, 5): "hi"}))
    player = make_player(server)
    run(player.warp("a.nw", 30, 30))
    return ids(player)


# -- the three-option rule on level data ----------------------------------------------

@pytest.mark.parametrize("serverside,links,signs,expect_links,expect_signs", [
    (True, False, False, False, False),
    (True, True, False, True, False),
    (True, False, True, False, True),
    (True, True, True, True, True),
    (False, False, False, True, True),
])
def test_entry_withholds_links_and_signs_only_with_the_npc_server_and_the_option_off(
        tmp_path, serverside, links, signs, expect_links, expect_signs):
    """PlayerClient.cpp:1495-1500. Mutations that fail this: sending both
    always (the old behaviour), ignoring `clientsidelinks`, ignoring
    `clientsidesigns`, or reading `serverside` inverted."""
    got = entry_ids(tmp_path, serverside=serverside,
                     clientsidelinks=links, clientsidesigns=signs)
    assert (PLO.LEVELLINK in got) is expect_links
    assert (PLO.LEVELSIGN in got) is expect_signs


@pytest.mark.parametrize("links,signs", [(False, False), (True, False), (False, True)])
def test_adjacent_level_follows_the_same_gate_as_entry(tmp_path, links, signs):
    """PLI_ADJACENTLEVEL runs the same sendStaticLevelData. Mutation: keeping
    the old `links_and_signs=not serverside` rule (which withheld both under
    serverside regardless of the options) fails the (True, False) row."""
    server, player = make_world(tmp_path, signs={(5, 5): "hi"},
                                clientsidelinks=links, clientsidesigns=signs)
    player.visited_levels.clear()
    player.send_raw.reset_mock()
    run(player._handle_adjacent_level(
        PacketBuilder().write_gint5(0).write_bytes(b"a.nw").build()))
    got = ids(player)
    assert (PLO.LEVELLINK in got) is links
    assert (PLO.LEVELSIGN in got) is signs


def test_the_config_reads_a_missing_key_as_false_and_a_bare_config_as_the_shipped_default(tmp_path):
    """Server.h:154-155 (absent = false) against serveroptions.txt:325,328
    (shipped = true). Mutation: a file loader that keeps the dataclass True
    for an absent key fails the first assert."""
    path = tmp_path / "serveroptions.txt"
    path.write_text("name = x\n")
    assert (ServerConfig.from_file(str(path)).clientsidelinks,
            ServerConfig.from_file(str(path)).clientsidesigns) == (False, False)
    path.write_text("clientsidelinks = true\nclientsidesigns = false\n")
    loaded = ServerConfig.from_file(str(path))
    assert (loaded.clientsidelinks, loaded.clientsidesigns) == (True, False)
    assert (ServerConfig().clientsidelinks, ServerConfig().clientsidesigns) == (True, True)
    loaded.to_file(str(path))
    again = ServerConfig.from_file(str(path))
    assert (again.clientsidelinks, again.clientsidesigns) == (True, False)


def test_the_funtimes_pygserver_world_states_both_options():
    """The shipped world keeps client-side links and signs because its config
    says so. The world directory is not in git, so the test skips without it
    (a missing file would load as a bare config, whose defaults read true
    and pass without testing anything). Mutation: removing either line from
    its serveroptions.txt fails the file-content assert."""
    options = FUNTIMES_PYGSERVER / "config" / "serveroptions.txt"
    if not options.is_file():
        pytest.skip("funtimes-pygserver world is not present: %s" % options)
    lines = {}
    for raw in options.read_text(encoding="latin-1").splitlines():
        key, sep, value = raw.partition("=")
        if sep:
            lines[key.strip()] = value.strip().lower()
    assert (lines.get("clientsidelinks"), lines.get("clientsidesigns")) == ("true", "true")
    config = ServerConfig.from_server_dir(str(FUNTIMES_PYGSERVER))
    assert config.serverside is True
    assert (config.clientsidelinks, config.clientsidesigns) == (True, True)


# -- server-side sign detection ----------------------------------------------------------

SIGN = {(10, 10): "line one\nline two"}


def sign_walk(tmp_path, x=9.0, y=10.0625, facing=0, new_y=10.0, **options):
    server, player = make_world(tmp_path, links=(), signs=SIGN, **options)
    stand(player, x, y, facing)
    move(player, (PLPROP.Y2, new_y))
    return player


def test_walking_up_into_a_sign_sends_its_text_as_say2_with_newlines_as_hash_b(tmp_path):
    """PlayerClient.cpp:1833-1853 and sendSignMessage :1767-1771. Mutations:
    not sending, or sending the raw newline."""
    player = sign_walk(tmp_path)
    packets = [body for packet_id, body in sent(player) if packet_id == PLO.SAY2]
    assert packets == [b"line one#bline two"]


def test_a_client_side_sign_option_leaves_the_sign_to_the_client(tmp_path):
    """Gate: clientsidesigns on means no server test. Mutation: testing
    anyway fails."""
    player = sign_walk(tmp_path, clientsidesigns=True)
    assert PLO.SAY2 not in ids(player)


def test_no_npc_server_means_no_server_side_sign_test(tmp_path):
    """PlayerClient.cpp:1777: testForTouch returns without an NPC server."""
    player = sign_walk(tmp_path, serverside=False)
    assert PLO.SAY2 not in ids(player)


@pytest.mark.parametrize("facing", [1, 2, 3])
def test_a_sign_needs_the_player_facing_up(tmp_path, facing):
    """:1836 `direction == 0`. Mutation: dropping the facing test fails."""
    assert PLO.SAY2 not in ids(sign_walk(tmp_path, facing=facing))


@pytest.mark.parametrize("y,new_y", [(10.125, 10.0625), (9.9375, 9.875)])
def test_a_sign_needs_movement_up_and_the_exact_pixel_row(tmp_path, y, new_y):
    """:1836 movement direction 0 and :1846 `localPixelY == signPos.y`.
    Row off by one pixel, or no row change at all for the first case's
    second step, must stay silent. Mutation: a tolerance on y fails."""
    assert PLO.SAY2 not in ids(sign_walk(tmp_path, y=y, new_y=new_y))


def test_moving_down_onto_the_sign_row_is_not_a_read(tmp_path):
    """Movement direction 2 (down) at the right row, facing up: no message."""
    assert PLO.SAY2 not in ids(sign_walk(tmp_path, y=9.9375, new_y=10.0))


@pytest.mark.parametrize("x,hit", [(8.5, True), (10.5, True), (8.4375, False),
                                   (10.5625, False)])
def test_the_sign_x_window_is_minus_24_to_plus_8_pixels_inclusive(tmp_path, x, hit):
    """:1846 `signPos.x-24 <= x <= signPos.x+8`, sign pixel x = 160.
    Mutation: shifting either bound by a pixel fails the edges."""
    assert (PLO.SAY2 in ids(sign_walk(tmp_path, x=x))) is hit


def test_a_prop_packet_that_changes_nothing_triggers_no_test(tmp_path):
    """PlayerProps.cpp:469,488: an unchanged coordinate is ignored before the
    test, so standing still never reads the sign. Mutation: testing on every
    packet fails."""
    server, player = make_world(tmp_path, links=(), signs=SIGN)
    stand(player, 9.0, 10.0, 0)
    move(player, (PLPROP.Y2, 10.0))
    assert PLO.SAY2 not in ids(player)


def test_turning_to_face_up_beside_the_sign_reads_it(tmp_path):
    """SPRITE with a changed facing tests with that facing as the direction
    (PlayerProps.cpp:543-546). Mutation: ignoring SPRITE fails."""
    server, player = make_world(tmp_path, links=(), signs=SIGN)
    stand(player, 9.0, 10.0, 2)
    move(player, (PLPROP.SPRITE, 0))
    assert PLO.SAY2 in ids(player)


def test_a_sign_and_a_link_on_the_same_spot_give_the_sign_only(tmp_path):
    """testForTouch returns after a sign hit, before the link test
    (PlayerClient.cpp:1782-1790), so the player reads the sign and is not
    warped. Mutation: no early exit after the sign (falling through to the
    link test) warps to b.nw and fails."""
    link = {**DOOR, "x": 10, "y": 11, "width": 0, "height": 0}
    server, player = make_world(tmp_path, links=(link,), signs=SIGN)
    stand(player, 9.0, 10.0625, 0)
    move(player, (PLPROP.Y2, 10.0))
    assert [b for i, b in sent(player) if i == PLO.SAY2] == [b"line one#bline two"]
    assert player.level.name == "a.nw"
    assert PLO.PLAYERWARP not in ids(player)


@pytest.mark.parametrize("props,touch_runs", [
    ([(PLPROP.Y2, 10.0)], False),
    ([(PLPROP.X2, 9.0625), (PLPROP.Y2, 10.0)], True),
])
def test_a_sign_hit_skips_the_npc_touch_for_its_own_call_only(tmp_path, props, touch_runs):
    """The NPC touch is skipped per testForTouch call (PlayerClient.cpp:1782,
    1787-1790), not per packet. One packet that moves sideways and then up
    makes two calls: the sideways one takes no sign, so its NPC touch still
    runs. A packet whose only call reads the sign has none. Mutation: a
    per-packet skip fails the second row."""
    from unittest.mock import AsyncMock, MagicMock
    server, player = make_world(tmp_path, links=(), signs=SIGN)
    server.npc_manager = MagicMock()
    server.npc_manager.check_touches = AsyncMock()
    stand(player, 9.0, 10.0625, 0)
    move(player, *props)
    assert PLO.SAY2 in ids(player)
    assert server.npc_manager.check_touches.await_count == (1 if touch_runs else 0)


# -- server-side link detection ------------------------------------------------------------

def link_walk(tmp_path, x=17.0, y=10.0, new_x=17.0625, links=(DOOR,), facing=3, **options):
    server, player = make_world(tmp_path, links=links, **options)
    stand(player, x, y, facing)
    move(player, (PLPROP.X2, new_x))
    return server, player


def test_walking_right_into_a_link_warps_to_its_destination(tmp_path):
    """PlayerClient.cpp:1858,1875-1903: test point = player + {48,32} for a
    move right, then warp(dest, x*16, y*16). Mutation: no warp, or the wrong
    offset table, fails."""
    server, player = link_walk(tmp_path)
    assert player.level.name == "b.nw"
    assert (player.x, player.y) == (15.0, 16.0)
    assert PLO.PLAYERWARP in ids(player)


def test_a_client_side_link_option_leaves_the_link_to_the_client(tmp_path):
    """Mutation: warping anyway fails."""
    server, player = link_walk(tmp_path, clientsidelinks=True)
    assert player.level.name == "a.nw"


def test_no_npc_server_means_no_server_side_link_test(tmp_path):
    server, player = link_walk(tmp_path, serverside=False)
    assert player.level.name == "a.nw"


@pytest.mark.parametrize("tile,hit", [(19, False), (20, True), (22, True), (23, False)])
def test_the_link_box_is_inclusive_at_both_ends(tmp_path, tile, hit):
    """Level.cpp:2550-2571: `x <= pos <= x + w`, so a 2-wide link at tile 20
    covers tiles 20, 21 and 22. The move right puts the test point (player +
    48 pixels) on `tile`. Mutation: an exclusive upper bound fails the 22
    row, an exclusive lower bound the 20 row."""
    pixel_x = tile * 16 - 48
    server, player = link_walk(tmp_path, x=(pixel_x - 1) / 16.0, new_x=pixel_x / 16.0)
    assert (player.level.name == "b.nw") is hit


@pytest.mark.parametrize("prop,from_px,to_px,fixed_px", [
    (PLPROP.Y2, 145, 144, 296),     # up:    test point (+24, +16)
    (PLPROP.X2, 321, 320, 128),     # left:  (+0, +32)
    (PLPROP.Y2, 103, 104, 296),     # down:  (+24, +56)
    (PLPROP.X2, 271, 272, 128),     # right: (+48, +32)
])
def test_each_direction_probes_its_own_offset(tmp_path, prop, from_px, to_px, fixed_px):
    """The test point is the player plus {24,16} up, {0,32} left, {24,56}
    down, {48,32} right (PlayerClient.cpp:1858); each row lands on tile
    (20,10), a 1x1 link. Mutation: one shared offset, or swapped
    left/right, fails at least one row."""
    server, player = make_world(tmp_path, links=(
        {**DOOR, "x": 20, "y": 10, "width": 0, "height": 0},))
    if prop == PLPROP.X2:
        stand(player, from_px / 16.0, fixed_px / 16.0)
    else:
        stand(player, fixed_px / 16.0, from_px / 16.0)
    move(player, (prop, to_px / 16.0))
    assert player.level.name == "b.nw"


def test_a_link_to_a_missing_level_does_nothing(tmp_path):
    """PlayerClient.cpp:1892: an unloadable destination is not a hit and
    sends no PLO_WARPFAILED. Mutation: warping or answering fails."""
    server, player = link_walk(tmp_path, links=({**DOOR, "dest_level": "nowhere.nw"},))
    assert player.level.name == "a.nw"
    assert PLO.WARPFAILED not in ids(player)


def test_playerx_and_playery_keep_the_players_own_coordinate(tmp_path):
    """LevelLink.cpp:30-37,117-138: `playerx`/`playery` are the player's
    local pixels at the test, `-1` means the same. Mutation: treating them as
    0 fails."""
    server, player = link_walk(tmp_path, links=(
        {**DOOR, "dest_x": "playerx", "dest_y": "-1"},))
    assert player.level.name == "b.nw"
    assert (player.x, player.y) == (17.0625, 10.0)


def test_a_destination_names_the_players_axis_whichever_field_it_is_in(tmp_path):
    """LevelLink.cpp:31-37: `playery` is the local pixel Y even in the
    dest_x field, and `playerx` the pixel X in dest_y. Mutation: resolving
    either name to the field's own axis fails (x would be 17.0625)."""
    server, player = link_walk(tmp_path, y=10.0, links=(
        {**DOOR, "dest_x": "playery", "dest_y": "playerx"},))
    assert player.level.name == "b.nw"
    assert (player.x, player.y) == (10.0, 17.0625)


def test_a_complex_destination_is_evaluated_in_tiles(tmp_path):
    """LevelLink.cpp:119-130: an expression is evaluated and times 16.
    Mutation: reading it as 0 fails."""
    server, player = link_walk(tmp_path, links=(
        {**DOOR, "dest_x": "playerx-1", "dest_y": "4+2"},))
    assert (player.x, player.y) == (16.0625, 6.0)


EDGE = {"dest_level": "b.nw", "x": 0, "y": 10, "width": 1, "height": 1,
        "dest_x": "20", "dest_y": "playery"}


def test_a_probable_map_link_fires_off_a_gmap(tmp_path):
    """LevelLink.cpp:142-149 only applies on a map (Level.cpp:2558
    `excludeOverworld = map != nullptr`): in a plain level the same edge
    link is a normal one. Mutation: skipping it everywhere fails."""
    server, player = make_world(tmp_path, links=(EDGE,))
    stand(player, 0.0625, 8.0, 1)
    move(player, (PLPROP.X2, 0.0))
    assert player.level.name == "b.nw"


def gmap_player(tmp_path, c1_links, segment="c1.nw", **options):
    from .test_level_entry_cache import gmap_world
    server = gmap_world(tmp_path, serverside=True, clientsidelinks=False,
                        clientsidesigns=False, **options)
    server.world.add_level(level_with(tmp_path, segment, c1_links))
    player = make_player(server)
    run(player.warp("c0.nw", 30, 30))
    player.send_raw.reset_mock()
    return server, player


def test_on_a_gmap_the_link_under_the_test_point_is_in_the_neighbour_segment(tmp_path):
    """Level::getLink looks in the segment under the point (Level.cpp:
    2550-2556): a player at the east edge of c0 touches c1's door.
    Mutation: reading only the player's own segment fails."""
    door = {**DOOR, "x": 2, "y": 10, "width": 0, "height": 0}
    server, player = gmap_player(tmp_path, (door,))
    stand(player, 62.9375, 8.0, 3)
    move(player, (PLPROP.X2, 63.0))
    assert player.level.name == "b.nw"


@pytest.mark.parametrize("dest_y", ["playery", "-1"])
def test_on_a_gmap_a_probable_map_link_is_skipped(tmp_path, dest_y):
    """LevelLink.cpp:142-149: a 1-wide link at x 0 with dest y `playery` is
    the seam, which the gmap hop handles. The parser rewrites `-1` to
    `playery` first (:57-69), so `-1` is the same link. Mutations: not
    skipping, or skipping only the spelled-out name, fails a row."""
    server, player = gmap_player(tmp_path, ({**EDGE, "dest_y": dest_y},))
    stand(player, 60.9375, 8.0, 3)
    move(player, (PLPROP.X2, 61.0))
    assert player.level.name == "c0.nw"


@pytest.mark.parametrize("dest_x", ["playerx", "-1"])
def test_on_a_gmap_a_probable_map_link_on_a_row_seam_is_skipped(tmp_path, dest_x):
    """The same rule for a 1-high link on row 0 with dest x `playerx` or
    `-1` (LevelLink.cpp:142-149, :57-69). The link sits in c2, under c0; its
    column is not 0 or 63, so only the second clause can skip it. Mutation:
    dropping the `-1` form for dest_x fails the second row."""
    seam = {"dest_level": "b.nw", "x": 10, "y": 0, "width": 1, "height": 1,
            "dest_x": dest_x, "dest_y": "5"}
    server, player = gmap_player(tmp_path, (seam,), segment="c2.nw")
    stand(player, 8.5, 60.4375, 2)
    move(player, (PLPROP.Y2, 60.5))
    assert player.level.name == "c0.nw"


@pytest.mark.parametrize("left_at,board_resent", [(5000.0, True), (MOD_TIME + 10.0, False)])
def test_the_warp_passes_the_destinations_last_left_time(tmp_path, monkeypatch, left_at, board_resent):
    """getLevelLastEnteredTimeForWarping (PlayerClient.cpp:1887,1903) is the
    client time of the warp: a destination left BEFORE its file time gets its
    board again (:1471), one left after does not. Mutation: passing no time
    (the plain server warp, which counts as the level's own time) skips the
    board in the first row and fails it."""
    from pygserver import level_cache
    monkeypatch.setattr(level_cache, "clock", lambda: left_at)
    server, player = make_world(tmp_path)
    run(player.warp("b.nw", 30, 30))
    run(player.warp("a.nw", 30, 30))
    player.send_raw.reset_mock()
    stand(player, 17.0, 10.0, 3)
    move(player, (PLPROP.X2, 17.0625))
    assert player.level.name == "b.nw"
    assert (PLO.BOARDPACKET in ids(player)) is board_resent
