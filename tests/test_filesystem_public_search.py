"""Which files a game client can download by name.

Reference: the world file system binds `world/` recursively and answers
PLI_WANTFILE from FileCategory::ALL by file name, case-insensitively
(GServer-v2 Server.cpp:491-530, FileSystem.cpp:76-116 and :442-454,
Player.cpp:577). Worlds here also keep content in folders beside `world/`.
"""

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pygserver.filesystem import FileSystem

FUNTIMES = Path(__file__).resolve().parents[2] / "funtimes-pygserver"


def make_fs(root):
    return FileSystem(SimpleNamespace(), str(root))


def touch(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def old_find_file(base, filename):
    """The search as it stood before the hardening (git HEAD), for comparison."""
    filename = filename.replace('..', '').strip('/')
    name = Path(filename).name
    for dirname in ('levels', 'gani', 'images', 'sounds', 'scripts', 'world'):
        candidate = base / dirname / name
        if candidate.is_file():
            return candidate
    if (base / name).is_file():
        return base / name
    if (base / filename).is_file():
        return base / filename
    return None


def test_nested_folders_are_searched_recursively(tmp_path):
    expected = {
        "walk.gani": touch(tmp_path / "world" / "ganis" / "walk.gani"),
        "body.png": touch(tmp_path / "bodies" / "body.png"),
        "head1.png": touch(tmp_path / "world" / "global" / "heads" / "head1.png"),
        "sword1.png": touch(tmp_path / "swords" / "deep" / "er" / "sword1.png"),
        "shield1.png": touch(tmp_path / "shields" / "shield1.png"),
        "hat.png": touch(tmp_path / "hats" / "hat.png"),
        "boom.wav": touch(tmp_path / "sounds" / "fx" / "boom.wav"),
        "town.nw": touch(tmp_path / "levels" / "town.nw"),
    }
    fs = make_fs(tmp_path)
    for name, path in expected.items():
        assert fs._find_file(name) == path
        assert fs._find_file(f"some/dir/{name}") == path


def test_lookup_is_case_insensitive_and_prefers_the_exact_case(tmp_path):
    lower = touch(tmp_path / "world" / "pics" / "body.png")
    upper = touch(tmp_path / "images" / "Body.png")
    fs = make_fs(tmp_path)
    assert fs._find_file("BODY.PNG") in (lower, upper)
    assert fs._find_file("Body.png") == upper
    assert fs._find_file("body.png") == lower


def test_private_folders_are_never_reachable(tmp_path):
    for rel in ("accounts/hosler.txt", "config/serveroptions.txt",
                "config/foldersconfig.txt", "weapons/weaponWand.txt",
                "npcs/npcguard.txt", "scripts/helper.txt", "logs/rclog.txt",
                "guilds/g.txt", "translations/en.txt", "serverflags.txt",
                "top.png"):
        touch(tmp_path / rel, "PRIVATE")
    fs = make_fs(tmp_path)
    for name in ("hosler.txt", "accounts/hosler.txt", "config/serveroptions.txt",
                 "serveroptions.txt", "foldersconfig.txt", "weaponWand.txt",
                 "npcguard.txt", "helper.txt", "rclog.txt", "g.txt", "en.txt",
                 "serverflags.txt", "top.png", "../top.png", "/etc/passwd"):
        assert fs._find_file(name) is None, name


def test_foldersconfig_adds_public_folders_but_never_private_ones(tmp_path):
    touch(tmp_path / "config" / "foldersconfig.txt",
          "# comment\n"
          "file    extras/*.png   # trailing comment\n"
          "head    heads/*\n"
          "file    accounts/*\n"
          "file    ../outside/*\n"
          "file    *.gani\n")
    touch(tmp_path / "extras" / "extra.png")
    # A non-standard first folder must also exist under world/ (filesystem.py
    # _folders_config_root); see tests/test_rcnc_1110_rc.py for the refusals.
    (tmp_path / "world" / "extras").mkdir(parents=True)
    touch(tmp_path / "accounts" / "acct.txt")
    touch(tmp_path.parent / "outside" / "out.png")
    fs = make_fs(tmp_path)
    assert fs._find_file("extra.png") == tmp_path / "extras" / "extra.png"
    assert fs._find_file("acct.txt") is None
    assert fs._find_file("out.png") is None


def test_symlink_leaving_a_public_folder_is_refused(tmp_path):
    secret = touch(tmp_path / "config" / "serveroptions.txt", "PRIVATE")
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "leak.png").symlink_to(secret)
    touch(tmp_path / "images" / "ok.png")
    fs = make_fs(tmp_path)
    assert fs._find_file("leak.png") is None
    assert fs._find_file("ok.png") == tmp_path / "images" / "ok.png"


def test_a_world_folder_that_is_itself_a_link_is_served(tmp_path):
    # pyReborn's LocalWorld symlinks `world/` and the asset folders in.
    real = tmp_path / "elsewhere" / "content"
    touch(real / "ganis" / "walk.gani")
    server = tmp_path / "server"
    server.mkdir()
    (server / "world").symlink_to(real, target_is_directory=True)
    assert make_fs(server)._find_file("walk.gani") == server / "world" / "ganis" / "walk.gani"


def test_a_file_added_while_running_is_found_after_the_rescan_window(tmp_path):
    fs = make_fs(tmp_path)
    assert fs._find_file("new.png") is None
    path = touch(tmp_path / "world" / "new.png")
    assert fs._find_file("new.png") is None        # inside the throttle window
    fs._public_index_time -= fs.INDEX_RESCAN_SECONDS + 1
    assert fs._find_file("new.png") == path
    os.remove(path)
    assert fs._find_file("new.png") is None


def age_tree(root, seconds=60):
    """Back-date every directory so its mtime is settled, not racy."""
    past = time.time() - seconds
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        os.utime(dirpath, (past, past))


def count_rescans(fs):
    calls = []
    real = fs._rebuild_public_index

    def counted():
        calls.append(1)
        return real()

    fs._rebuild_public_index = counted
    return calls


def open_rescan_window(fs):
    fs._public_index_time -= fs.INDEX_RESCAN_SECONDS + 1


def test_misses_on_an_unchanged_tree_do_not_rescan(tmp_path):
    touch(tmp_path / "world" / "sub" / "a.png")
    touch(tmp_path / "images" / "b.png")
    fs = make_fs(tmp_path)          # also creates the standard folders
    age_tree(tmp_path)
    assert fs._find_file("a.png") is not None
    calls = count_rescans(fs)
    for _ in range(3):
        open_rescan_window(fs)
        assert fs._find_file("absent.png") is None
    assert calls == []


def test_a_file_added_or_removed_in_a_nested_public_folder_is_tracked(tmp_path):
    touch(tmp_path / "world" / "sub" / "deep" / "old.png")
    fs = make_fs(tmp_path)
    age_tree(tmp_path)
    assert fs._find_file("old.png") is not None
    calls = count_rescans(fs)

    added = touch(tmp_path / "world" / "sub" / "deep" / "later.png")
    open_rescan_window(fs)
    assert fs._find_file("later.png") == added
    assert len(calls) == 1

    fresh = touch(tmp_path / "images" / "newdir" / "x.png")     # new public subfolder
    open_rescan_window(fs)
    assert fs._find_file("x.png") == fresh

    os.remove(added)
    open_rescan_window(fs)
    assert fs._find_file("later.png") is None       # deleted: no longer served
    assert fs._find_file("old.png") is not None


def test_a_change_in_the_same_timestamp_tick_as_the_scan_is_not_lost(tmp_path):
    (tmp_path / "world").mkdir()
    fs = make_fs(tmp_path)
    assert fs._find_file("first.png") is None       # scan: world/ mtime is fresh
    path = touch(tmp_path / "world" / "late.png")
    # Pin world/ to the mtime the scan recorded, as a coarse-grained
    # filesystem clock would give a change inside the same tick.
    _roots, mtimes = fs._public_signature
    stamp = mtimes[str(tmp_path / "world")]
    os.utime(tmp_path / "world", ns=(stamp, stamp))
    open_rescan_window(fs)
    assert fs._find_file("late.png") == path


@pytest.fixture
def funtimes(tmp_path):
    if not FUNTIMES.is_dir():
        pytest.skip("funtimes-pygserver checkout not present")
    for entry in FUNTIMES.iterdir():
        (tmp_path / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    return tmp_path, make_fs(tmp_path)


def under(path, folders):
    return any(folder in path.parts[:1] for folder in folders)


def test_funtimes_every_public_file_is_still_found(funtimes):
    root, fs = funtimes
    public = set(FileSystem.CLIENT_FILE_DIRS) | set(fs._folders_config_dirs())
    checked = 0
    for path in sorted(FUNTIMES.rglob("*")):
        rel = path.relative_to(FUNTIMES)
        if not path.is_file() or not under(rel, public) or not fs.is_bare_filename(path.name):
            continue
        found = fs._find_file(path.name)
        assert found is not None and found.name.lower() == path.name.lower(), rel
        checked += 1
        # Whatever the old search reached through a public folder is still reached.
        old = old_find_file(FUNTIMES, path.name)
        if old is not None and under(old.relative_to(FUNTIMES), public):
            assert found.name.lower() == old.name.lower()
    assert checked > 40            # the world folder alone holds over fifty files


def test_funtimes_nothing_sensitive_is_served(funtimes):
    root, fs = funtimes
    public = set(FileSystem.CLIENT_FILE_DIRS) | set(fs._folders_config_dirs())
    public_names = {p.name.lower() for p in FUNTIMES.rglob("*")
                    if p.is_file() and under(p.relative_to(FUNTIMES), public)}
    refused = 0
    for path in sorted(FUNTIMES.rglob("*")):
        rel = path.relative_to(FUNTIMES)
        if not path.is_file() or under(rel, public):
            continue
        # A private file may only be reachable through a same-named public one.
        for request in (rel.as_posix(), path.name, "../" + rel.as_posix()):
            found = fs._find_file(request)
            if path.name.lower() in public_names:
                assert found is None or under(found.relative_to(root), public), rel
            else:
                assert found is None, (request, found)
                refused += 1
    assert refused > 100           # accounts, config, weapons, npcs, logs, ...
    for request in ("accounts/testbot1.json", "config/serveroptions.txt",
                    "serverflags.txt", "weapons/weaponWand.txt", "bootstrap.js"):
        assert fs._find_file(request) is None
