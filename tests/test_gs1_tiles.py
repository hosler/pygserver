"""GS1 level tile and NPC sub-level board built-ins."""

from pygserver.gs1_host import compile_gs1, run_npc_event
from pygserver.level import Level
from pygserver.npc import NPC


def run(code, level):
    npc = NPC(1, "tile test")
    npc.level = level
    npc.gs1_program = compile_gs1("if (created) { " + code + " }")
    run_npc_event(npc, "created")
    return npc.gs1_scopes["this"]


def test_tiles_basic_read():
    level = Level("tiles.nw")
    level.set_tile(3, 5, 742)
    state = run("this.result = tiles[3,5];", level)
    assert state["result"] == 742


def test_tiles_write():
    """`tiles[x,y] = n` only fills a script copy of the board
    (GS1Variables.cpp:416-442, Level.cpp:3135-3153): the live board, the
    base board and the change list stay as they were, and `tiles[]` reads do
    not see the script copy. Mutation: writing through Level.set_tile (the
    old immediate model) fails the first three asserts."""
    level = Level("tiles.nw")
    level.set_tile(7, 9, 55)
    state = run("tiles[7,9] = 1337; this.result = tiles[7,9];", level)
    assert level.get_tile(7, 9) == 55 and level.get_base_tile(7, 9) == 55
    assert level.board_changes == []
    assert state["result"] == 55
    assert level.script_tiles_rect(7, 9, 1, 1) == [1337]


def test_the_script_copy_starts_as_the_file_tiles():
    """getMapTileForEditing copies the static tiles on first use
    (Level.cpp:3150, LevelTiles.h:80-92), so an updateboard over a wider area
    publishes the file tile beside the written one. Mutation: starting the
    copy from zeros fails."""
    level = Level("tiles.nw")
    level.set_tile(8, 9, 66)
    run("tiles[7,9] = 1337;", level)
    assert level.script_tiles_rect(7, 9, 2, 1) == [1337, 66]


def test_a_level_nobody_wrote_to_has_no_script_layer():
    """Level.cpp:1765-1770: a part without script tiles contributes tile 0."""
    level = Level("tiles.nw")
    level.set_tile(8, 9, 66)
    assert level.script_tiles_rect(8, 9, 1, 1) == [0]


def test_a_reload_drops_the_script_layer():
    """Level.cpp:657-658. Mutation: keeping the layer across clear_script_tiles
    fails."""
    level = Level("tiles.nw")
    run("tiles[7,9] = 1337;", level)
    level.clear_script_tiles()
    assert level.script_tiles_rect(7, 9, 1, 1) == [0]


def test_tiles_negative_coordinates_clamp_to_zero():
    level = Level("tiles.nw")
    level.set_tile(0, 0, 91)
    state = run("this.result = tiles[-4,-8]; tiles[-2,-3] = 92;", level)
    assert state["result"] == 91
    assert level.get_tile(0, 0) == 91
    assert level.script_tiles_rect(0, 0, 1, 1) == [92]


def test_board_index_read():
    level = Level("board.nw")
    level.set_tile(2, 1, 515)
    state = run("this.result = board[66];", level)
    assert state["result"] == 515


def test_board_bare_read_returns_all_tiles():
    level = Level("board.nw")
    level.set_tile(0, 0, 12)
    level.set_tile(63, 63, 4095)
    state = run("this.result = board;", level)
    assert len(state["result"]) == 4096
    assert state["result"][0] == 12
    assert state["result"][4095] == 4095
