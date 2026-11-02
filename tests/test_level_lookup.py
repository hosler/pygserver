"""LevelManager resolves levels kept in nested world folders."""
from pathlib import Path

from pygserver.level import LevelManager

MINIMAL_NW = "GLEVNW01\n"


def _write(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MINIMAL_NW)


def test_nested_levels_are_indexed_and_loadable(tmp_path):
    _write(tmp_path / "lobby.nw")
    _write(tmp_path / "levels" / "playerbase" / "room0.nw")
    _write(tmp_path / "levels" / "arenas" / "arena1.graal")
    manager = LevelManager(str(tmp_path))

    assert set(manager.level_paths()) == {"lobby.nw", "room0.nw", "arena1.graal"}
    assert manager.find_level_path("room0.nw") == tmp_path / "levels" / "playerbase" / "room0.nw"
    assert manager.find_level_path("room0") == tmp_path / "levels" / "playerbase" / "room0.nw"
    assert manager.get_level("room0.nw") is not None
    assert manager.get_level("missing.nw") is None


def test_duplicate_names_resolve_deterministically(tmp_path):
    _write(tmp_path / "b" / "dup.nw")
    _write(tmp_path / "a" / "dup.nw")
    manager = LevelManager(str(tmp_path))
    assert manager.find_level_path("dup.nw") == tmp_path / "a" / "dup.nw"


def test_path_traversal_names_are_rejected(tmp_path):
    _write(tmp_path / "lobby.nw")
    manager = LevelManager(str(tmp_path))
    assert manager.find_level_path("../lobby.nw") is None
    assert manager.find_level_path("") is None


def test_level_added_after_startup_is_found_on_rescan(tmp_path):
    _write(tmp_path / "lobby.nw")
    manager = LevelManager(str(tmp_path))
    assert manager.find_level_path("late.nw") is None
    _write(tmp_path / "late.nw")
    # Still inside the rescan interval: the miss stays cached...
    assert manager.find_level_path("late.nw") is None
    # ...and the next lookup after the interval picks the new file up.
    manager._last_scan -= manager.RESCAN_INTERVAL
    assert manager.find_level_path("late.nw") == tmp_path / "late.nw"


def test_misses_do_not_rescan_within_interval(tmp_path, monkeypatch):
    _write(tmp_path / "lobby.nw")
    manager = LevelManager(str(tmp_path))
    manager.level_paths()
    calls = []
    original = manager.level_paths

    def counting(refresh=False):
        calls.append(refresh)
        return original(refresh=refresh)

    monkeypatch.setattr(manager, "level_paths", counting)
    for _ in range(20):
        assert manager.find_level_path("bogus.nw") is None
    assert not any(calls), "a miss inside RESCAN_INTERVAL must not rescan the tree"


def test_duplicate_names_warn_once(tmp_path, caplog):
    _write(tmp_path / "a" / "dup.nw")
    _write(tmp_path / "b" / "dup.nw")
    manager = LevelManager(str(tmp_path))
    with caplog.at_level("WARNING"):
        manager.level_paths()
    assert sum("Duplicate level name dup.nw" in r.message for r in caplog.records) == 1
