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
    level = Level("tiles.nw")
    run("tiles[7,9] = 1337;", level)
    assert level.get_tile(7, 9) == 1337


def test_tiles_negative_coordinates_clamp_to_zero():
    level = Level("tiles.nw")
    level.set_tile(0, 0, 91)
    state = run("this.result = tiles[-4,-8]; tiles[-2,-3] = 92;", level)
    assert state["result"] == 91
    assert level.get_tile(0, 0) == 92


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
