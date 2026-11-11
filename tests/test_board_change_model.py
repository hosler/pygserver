"""Seeded randomised check of the board-change list against a plain model.

Random client changes (overlapping, nested, 2x2 and not, `respawntime` 0 and
15), server `set_tile` writes, time passing and small list bounds, inside a
12x12 corner of one level so everything overlaps. After every step:

* the list (rectangles, tiles, old tiles, timers, order) equals a model kept
  as a Python list: delete the older entries the new one contains
  (Level.cpp:1703-1707), evict the oldest at the bound, swap on expiry
  (LevelBoardChange.cpp:39-50), patch the entries under a server write;
* the corrections returned for evicted entries equal base overlaid with the
  model list;
* the live board equals the model's live board, and equals what a joiner
  rebuilds (base, then the list in order) except at cells where an entry
  respawned under a newer one (the divergence the reference shares, see
  board_changes.py);
* the list's per-cell index, timer heap and counters agree with its entries.
"""

import random

import pytest

from pygserver.board_changes import BoardChangeList
from pygserver.level import Level

SIDE = 64
REGION = 12
TILES = [0, 1, 2, 3, 7, 9, 0x1a0, 0x2ac]


def cells(x, y, w, h):
    return [row * SIDE + col for row in range(y, y + h) for col in range(x, x + w)]


def tile_of(board, cell):
    return board[cell * 2] | (board[cell * 2 + 1] << 8)


def put(board, cell, tile):
    board[cell * 2] = tile & 0xFF
    board[cell * 2 + 1] = (tile >> 8) & 0xFF


class Model:
    """The list and the boards, written as directly as possible."""

    def __init__(self, max_changes, max_tiles):
        self.max_changes, self.max_tiles = max_changes, max_tiles
        self.entries = []            # dicts in list order
        self.base = bytearray(SIDE * SIDE * 2)
        self.live = bytearray(SIDE * SIDE * 2)
        self.diverged = set()
        self.order = 0

    @staticmethod
    def covers(entry, cell):
        x, y = cell % SIDE, cell // SIDE
        return (entry["x"] <= x < entry["x"] + entry["w"]
                and entry["y"] <= y < entry["y"] + entry["h"])

    @staticmethod
    def index(entry, cell):
        return (cell // SIDE - entry["y"]) * entry["w"] + cell % SIDE - entry["x"]

    def rebuilt(self, cell):
        tile = tile_of(self.base, cell)
        for entry in self.entries:
            if self.covers(entry, cell):
                tile = entry["tiles"][self.index(entry, cell)]
        return tile

    def add(self, x, y, w, h, tiles, now, seconds):
        respawns = w == 2 and h == 2
        entry = {"x": x, "y": y, "w": w, "h": h, "tiles": list(tiles),
                 "old": ([tile_of(self.base, c) for c in cells(x, y, w, h)]
                         if respawns else None),
                 "due": now + seconds if respawns and seconds > 0 else None,
                 "order": self.order}
        self.order += 1
        self.entries = [e for e in self.entries
                        if not (e["x"] >= x and e["y"] >= y and e["x"] + e["w"] <= x + w
                                and e["y"] + e["h"] <= y + h)]
        evicted = []
        while self.entries and (
                len(self.entries) >= self.max_changes
                or sum(e["w"] * e["h"] for e in self.entries) + w * h > self.max_tiles):
            evicted.append(self.entries.pop(0))
        self.entries.append(entry)
        for cell, tile in zip(cells(x, y, w, h), tiles):
            put(self.live, cell, tile)
            self.diverged.discard(cell)
        fixes = []
        for old in evicted:
            rect = cells(old["x"], old["y"], old["w"], old["h"])
            fix = [self.rebuilt(cell) for cell in rect]
            for cell, tile in zip(rect, fix):
                put(self.live, cell, tile)
                self.diverged.discard(cell)
            fixes.append((old["x"], old["y"], old["w"], old["h"], fix))
        return fixes

    def set_tile(self, x, y, tile):
        cell = y * SIDE + x
        put(self.base, cell, tile)
        put(self.live, cell, tile)
        self.diverged.discard(cell)
        for entry in self.entries:
            if self.covers(entry, cell):
                entry["tiles"][self.index(entry, cell)] = tile
                if entry["old"] is not None:
                    entry["old"][self.index(entry, cell)] = tile

    def expire(self, now):
        due = sorted((e for e in self.entries if e["due"] is not None and e["due"] <= now),
                     key=lambda e: (e["due"], e["order"]))
        for entry in due:
            entry["tiles"], entry["old"] = entry["old"], entry["tiles"]
            entry["due"] = None
            position = self.entries.index(entry)
            newer = self.entries[position + 1:]
            for cell, tile in zip(cells(entry["x"], entry["y"], entry["w"], entry["h"]),
                                  entry["tiles"]):
                put(self.live, cell, tile)
                if any(self.covers(n, cell) for n in newer):
                    self.diverged.add(cell)
                else:
                    self.diverged.discard(cell)
        return len(due)


def joiner_board(level):
    board = bytearray(level.get_base_board_packet())
    for change in level.board_changes:
        Level._write_rect(board, change.x, change.y, change.width, change.height,
                          change.tiles)
    return board


def check(level, model):
    changes = level._board_changes
    entries = level.board_changes
    assert [(c.x, c.y, c.width, c.height, c.tiles, c.old_tiles, c.expires_at)
            for c in entries] == [
        (e["x"], e["y"], e["w"], e["h"], e["tiles"], e["old"], e["due"])
        for e in model.entries]
    assert [c.seq for c in entries] == sorted(c.seq for c in entries)
    assert level.get_board_packet() == bytes(model.live)
    assert level.get_base_board_packet() == bytes(model.base)
    joiner = joiner_board(level)
    for cell in cells(0, 0, REGION, REGION):
        if cell not in model.diverged:
            assert tile_of(joiner, cell) == tile_of(model.live, cell), cell
    # The index, the heap and the counters agree with the entries.
    for cell in cells(0, 0, REGION, REGION):
        assert changes.covering(cell) == [c for c in entries
                                          if Model.covers({"x": c.x, "y": c.y,
                                                           "w": c.width, "h": c.height},
                                                          cell)]
    timed = [c for c in entries if c.expires_at is not None]
    assert changes._timed == len(timed)
    assert {(c.expires_at, c.seq) for c in timed} <= set(changes._timers)
    assert changes.tile_total == sum(c.area for c in entries)
    assert set(changes._entries) == {c.seq for c in entries}


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("max_changes, max_tiles", [(5, 40), (12, 200), (3, 1000)])
def test_the_list_matches_the_model(seed, max_changes, max_tiles):
    rng = random.Random(seed * 1000 + max_changes)
    level = Level("a.nw")
    level._board_changes = BoardChangeList(max_changes=max_changes, max_tiles=max_tiles)
    model = Model(max_changes, max_tiles)
    now = 0.0
    for x in range(REGION):                                    # some base to restore
        for y in range(REGION):
            if rng.random() < 0.3:
                tile = rng.choice(TILES)
                level.set_tile(x, y, tile)
                model.set_tile(x, y, tile)
    for _step in range(250):
        roll = rng.random()
        if roll < 0.55:
            if rng.random() < 0.5:
                w = h = 2
            elif rng.random() < 0.05:
                w = h = REGION                                 # over the small tile bound
            else:
                w, h = rng.randint(1, 5), rng.randint(1, 5)
            x, y = rng.randint(0, REGION - w), rng.randint(0, REGION - h)
            tiles = [rng.choice(TILES) for _ in range(w * h)]
            seconds = rng.choice((0, 15, 15))
            fixes = []
            assert level.alter_board(x, y, w, h, tiles, now, seconds,
                                     corrections=fixes) is not None
            expected = model.add(x, y, w, h, tiles, now, seconds)
            assert [(f.x, f.y, f.width, f.height, f.tiles) for f in fixes] == expected
        elif roll < 0.7:
            x, y, tile = rng.randrange(REGION), rng.randrange(REGION), rng.choice(TILES)
            level.set_tile(x, y, tile)
            model.set_tile(x, y, tile)
        else:
            now += rng.choice((0.5, 3.0, 7.5, 15.0))
            done = level.expire_board_changes(now)
            assert len(done) == model.expire(now)
        check(level, model)
    # Nothing outside the corner was touched; a final full-board index check.
    changes = level._board_changes
    for cell in range(SIDE * SIDE):
        if cell % SIDE >= REGION or cell // SIDE >= REGION:
            assert not changes.covering(cell)
