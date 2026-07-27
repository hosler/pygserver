"""`lay <itemname>` resolves the NAME, not to_num of it (which is 0 =
greenrupee for every word) — the numeric index form still works."""
import pygserver.gs1.commands.world as world
from pygserver.protocol.constants import LevelItemType


class _Recorder:
    def __init__(self):
        self.calls = []

    def spawn_item(self, lvl, x, y, item_type):
        self.calls.append((x, y, item_type))


class _Host:
    def __init__(self, recorder):
        self.server = type("S", (), {"item_manager": recorder})()

    def _level_of(self, ctx):
        return "level"


def _spawned(code):
    rec = _Recorder()
    host = _Host(rec)
    scheduled = []
    orig = world._schedule
    world._schedule = scheduled.append
    try:
        world._spawn_item(host, None, code, 3, 4)
    finally:
        world._schedule = orig
    assert rec.calls, f"nothing spawned for {code!r}"
    return rec.calls[0][2]


def test_lay_by_name():
    assert _spawned("bombs") is LevelItemType.BOMBS
    assert _spawned(" Heart ") is LevelItemType.HEART


def test_lay_by_numeric_index_still_works():
    assert _spawned("3") is LevelItemType.BOMBS


def test_lay_unknown_name_is_dropped():
    rec = _Recorder()
    host = _Host(rec)
    world._spawn_item(host, None, "nosuchitem", 1, 1)
    assert not rec.calls
