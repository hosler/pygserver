"""
pygserver.update_packages - `.gupd` update packages (PLI 159, PLO 105/106).

A package is a text file in a public folder. The server reads only its `FILE`
lines (UpdatePackage.cpp:51-74, origin/beta4): a line that starts with `FILE`
(any case) names one file by the rest of the line, trimmed, of which only the
final component is used. Each named file is answered with its size and zlib
CRC32; a file that cannot be found is reported to RC chat and skipped (:63).

Divergences from the reference, all deliberate:
- The package name a client sends must be a bare file name ending `.gupd`;
  the reference passes it straight to its file lookup.
- Package and FILE entries resolve through FileSystem._find_file(strict=True):
  the public folders, by bare name. The reference uses its FILE category,
  which only differs when foldersconfig.txt has `file` patterns. An entry
  must still match its on-disk name exactly, as the reference's
  case-sensitive `info` lookup does (:60).
- FILE entries keep their order in the package and are de-duplicated by name
  (the reference keeps them in an unordered_map, so the k-th checksum of a
  request is compared with an unspecified entry, PlayerClientPackets.cpp:
  1568-1577). Here the k-th GInt5 is compared with the k-th `FILE ` line as
  the client numbers them (exact-case `FILE ` with a non-empty rest, missing
  files included, TUpdatePackage.cpp:407-439), and counts as current when it
  equals the file's CRC32 or its whole-second mtime, the two values the
  client sends (the decompiled reference client, FourPlay
  quattroplay/src/TClient.cpp:301-338). A zero value
  is what the client sends for a file it does not have, so it never counts.
- An entry above FileSystem.max_file_size is left out of the PLO 105 total
  and not sent (the reference counts it, then fails to send it).
- Sizes and CRCs are cached per file and refreshed when the file's
  (mtime, size, inode) changes, instead of on a file-watch event.
"""

import asyncio
import logging
import os
import stat
import time
import weakref
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from .protocol.packets import (
    build_update_package_done,
    build_update_package_size,
)

if TYPE_CHECKING:
    from .filesystem import FileSystem
    from .player import Player

logger = logging.getLogger(__name__)

PACKAGE_SUFFIX = '.gupd'
# Largest `.gupd` read; a bigger one is treated as missing.
MAX_PACKAGE_BYTES = 256 * 1024
# FILE lines honoured per package; later ones are ignored.
MAX_PACKAGE_FILES = 1024
# GInt5 values read from one PLI 159; never more than the package has entries.
MAX_REQUEST_CHECKSUMS = MAX_PACKAGE_FILES
# Per-player PLI 159 pacing: burst of 4, then one answer per 0.5 s (2/s).
REQUEST_BURST = 4
REQUEST_RATE = 2.0
REQUEST_TYPE_INSTALL = 1
REQUEST_TYPE_REINSTALL = 2

_StatKey = Tuple[int, int, int]


def _stat_key(info: os.stat_result) -> _StatKey:
    return (info.st_mtime_ns, info.st_size, info.st_ino)


@dataclass
class EntryState:
    """Size, CRC32 and whole-second mtime of an entry at request time."""
    name: str
    path: Path
    size: int
    crc32: int
    mod_time: int
    # Above FileSystem.max_file_size: never checksummed, counted or sent.
    oversize: bool = False


@dataclass
class UpdatePackage:
    name: str
    path: Path
    stat_key: _StatKey
    # Non-empty bare names of the FILE lines, in order, de-duplicated. Never
    # a path: each request resolves them afresh through the public lookup,
    # so a share removed, foldersconfig narrowed or a file moved is honoured
    # at once.
    names: List[str] = field(default_factory=list)
    # Entries over FileSystem.max_file_size already logged for this version.
    oversize_logged: set = field(default_factory=set)
    # Bare name per value slot of the client's request, '' for a slot that
    # names nothing usable.
    slots: List[str] = field(default_factory=list)


_TRIM = ''.join(chr(c) for c in range(33))


def parse_package_lines(text: str,
                        limit: int = MAX_PACKAGE_FILES) -> List[Tuple[str, bool]]:
    """(bare name, client slot) of each FILE line, in order, at most `limit`.

    UpdatePackage.cpp:51-57: split on '\\n', keep lines starting with `FILE`
    (case-insensitive), trim the rest (bytes <= ' ', CString::trim) and take
    the final path component; the name may come out empty. `client slot` is
    whether the client also counts the line, i.e. it starts with exact-case
    `FILE ` and has something after it (TUpdatePackage.cpp:407-409).
    """
    lines: List[Tuple[str, bool]] = []
    for line in text.split('\n'):
        if line[:4].upper() != 'FILE':
            continue
        rest = line[4:].strip(_TRIM)
        base = rest.replace('\\', '/').rsplit('/', 1)[-1]
        lines.append((base, line.startswith('FILE ') and len(line) > 5))
        if len(lines) >= limit:
            break
    return lines


def parse_package_file_names(text: str,
                             limit: int = MAX_PACKAGE_FILES) -> List[str]:
    """Non-empty bare names of the FILE lines, in order, de-duplicated."""
    names: List[str] = []
    for base, _slot in parse_package_lines(text, limit):
        if base and base not in names:
            names.append(base)
    return names


def valid_package_name(name) -> bool:
    """A bare file name ending `.gupd`: no separator, dot segment or NUL."""
    from .filesystem import FileSystem
    return (isinstance(name, str) and name.lower().endswith(PACKAGE_SUFFIX)
            and len(name) > len(PACKAGE_SUFFIX)
            and FileSystem.is_bare_filename(name))


class UpdatePackageManager:
    """Loads `.gupd` files on demand and answers PLI 159."""

    def __init__(self, filesystem: 'FileSystem'):
        self.filesystem = filesystem
        # lower-case package name -> parsed package
        self._packages: Dict[str, UpdatePackage] = {}
        # resolved path -> (stat key, crc32)
        self._crc_cache: Dict[str, Tuple[_StatKey, int]] = {}
        # Held only around disk work, never around a send to a peer.
        self._lock = asyncio.Lock()
        self._notice_tasks: set = set()
        # player -> [tokens, last refill]
        self._throttle: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()

    # -- loading -------------------------------------------------------------

    async def load(self, name: str) -> Tuple[Optional[UpdatePackage], bool]:
        """(parsed package, whether it was just (re)read). The package is
        re-read when its file changed; None when the name is invalid or no
        such package exists. The stat and read run in the executor."""
        if not valid_package_name(name):
            return None, False
        path = self.filesystem._find_file(name, strict=True)
        if path is None:
            self._packages.pop(name.lower(), None)
            return None, False
        cached = self._packages.get(name.lower())

        def probe() -> Tuple[_StatKey, Optional[bytes]]:
            info = path.stat()
            key = _stat_key(info)
            if (cached is not None and cached.path == path
                    and cached.stat_key == key):
                return key, None
            if info.st_size > MAX_PACKAGE_BYTES:
                return key, b''
            with open(path, 'rb') as stream:
                return key, stream.read(MAX_PACKAGE_BYTES + 1)
        try:
            key, data = await asyncio.get_running_loop().run_in_executor(None, probe)
        except OSError as error:
            logger.warning("Could not read update package %s: %s", name, error)
            return None, False
        if data is None:
            return cached, False
        if not data or len(data) > MAX_PACKAGE_BYTES:
            if data:
                logger.warning("Update package %s is over %d bytes; ignored",
                               name, MAX_PACKAGE_BYTES)
            return None, False

        package = UpdatePackage(name=name, path=path, stat_key=key)
        lines = parse_package_lines(data.decode('latin-1'))
        package.slots = [base for base, slot in lines if slot]
        seen = set()
        for base, _slot in lines:
            if base and base not in seen:
                seen.add(base)
                package.names.append(base)
        self._packages[name.lower()] = package
        return package, True

    def _send_notices(self, notices: List[str]):
        """RC chat for missing entries, as a task: a stalled RC socket must
        not hold up the requesting player or anyone's PLI 159."""
        rc_manager = getattr(self.filesystem.server, 'rc_manager', None)
        if not notices or rc_manager is None:
            return

        async def send():
            for message in notices:
                try:
                    await rc_manager.broadcast_chat(message)
                except Exception as error:
                    logger.debug("RC notice failed: %s", error)
        task = asyncio.get_running_loop().create_task(send())
        self._notice_tasks.add(task)
        task.add_done_callback(self._notice_tasks.discard)

    def _entry_states_sync(self, entries: Sequence[Tuple[str, List[Path]]],
                           max_size: int) -> List[EntryState]:
        """Worker thread: pick each name's file among its index candidates
        (exact name, still a regular file, as the strict lookup and
        UpdatePackage.cpp:60 require), then size/CRC/mtime it. Names with no
        such file are left out."""
        states: List[EntryState] = []
        for name, candidates in entries:
            path = info = None
            for candidate in candidates:
                if candidate.name != name:
                    continue
                try:
                    found = candidate.stat()
                except OSError:
                    continue
                if stat.S_ISREG(found.st_mode):
                    path, info = candidate, found
                    break
            if path is None:
                continue
            if info.st_size > max_size:
                states.append(EntryState(name, path, info.st_size, 0,
                                         max(0, int(info.st_mtime)), True))
                continue
            key = _stat_key(info)
            cache_key = str(path)
            cached = self._crc_cache.get(cache_key)
            if cached is not None and cached[0] == key:
                crc = cached[1]
            else:
                crc = 0
                try:
                    with open(path, 'rb') as stream:
                        while True:
                            chunk = stream.read(1 << 20)
                            if not chunk:
                                break
                            crc = zlib.crc32(chunk, crc)
                except OSError:
                    continue
                crc &= 0xFFFFFFFF
                self._crc_cache[cache_key] = (key, crc)
            states.append(EntryState(name, path, info.st_size,
                                     crc, max(0, int(info.st_mtime))))
        return states

    async def entry_states(self, package: UpdatePackage) -> List[EntryState]:
        """Current size/CRC/mtime of each entry that resolves through the
        public index right now (one batch lookup on the event loop, at most
        one tree-change check); every stat and read runs in the executor."""
        filesystem = self.filesystem
        loop = asyncio.get_running_loop()
        candidates = filesystem.public_candidates(package.names)
        entries = [(name, candidates.get(name, [])) for name in package.names]
        states = await loop.run_in_executor(
            None, self._entry_states_sync, entries, filesystem.max_file_size)
        # An index entry that no longer holds a file (moved or deleted) is a
        # miss too: allow the one tree-change check and look those up again.
        found = {state.name for state in states}
        stale = [name for name, paths in entries if paths and name not in found]
        if stale and filesystem._maybe_rescan():
            again = filesystem.public_candidates(stale)
            states += await loop.run_in_executor(
                None, self._entry_states_sync,
                [(name, again.get(name, [])) for name in stale],
                filesystem.max_file_size)
            order = {name: index for index, name in enumerate(package.names)}
            states.sort(key=lambda state: order[state.name])
        return states

    # -- throttle --------------------------------------------------------------

    def _take_token(self, player) -> float:
        """Per-player PLI 159 budget: a bucket of REQUEST_BURST requests
        refilled at REQUEST_RATE per second. Takes one token and returns how
        long to wait before answering (0 while the bucket is not empty). The
        bucket may go negative by one: the caller waits that token out."""
        now = time.monotonic()
        try:
            state = self._throttle.get(player)
        except TypeError:
            return 0.0      # not weak-referenceable (a test double)
        if state is None:
            state = self._throttle[player] = [float(REQUEST_BURST), now]
        tokens = min(float(REQUEST_BURST),
                     state[0] + (now - state[1]) * REQUEST_RATE)
        state[0], state[1] = tokens - 1.0, now
        return max(0.0, -state[0] / REQUEST_RATE)

    # -- PLI 159 -------------------------------------------------------------

    @staticmethod
    def parse_request(data: bytes) -> Optional[Tuple[str, int, List[int]]]:
        """(package name, request type, client values) of a PLI 159 body:
        [gchar len][name][guchar type][gint5]* (PlayerClientPackets.cpp:
        1551-1555). None when the body ends before the type byte."""
        if not data:
            return None
        length = max(0, data[0] - 32)
        if len(data) < 1 + length + 1:
            return None
        name = data[1:1 + length].decode('latin-1')
        request_type = data[1 + length] - 32
        values: List[int] = []
        pos = 2 + length
        while len(data) - pos >= 5 and len(values) < MAX_REQUEST_CHECKSUMS:
            b = [data[pos + i] - 32 for i in range(5)]
            values.append(((b[0] << 28) + (b[1] << 21) + (b[2] << 14)
                           + (b[3] << 7) + b[4]) & 0xFFFFFFFF)
            pos += 5
        return name, request_type, values

    async def handle_request(self, player: 'Player', data: bytes):
        """Answer PLI 159: PLO 105 with the bytes needed, each needed file
        through the normal file send path, then PLO 106
        (PlayerClientPackets.cpp:1549-1595). Paced per player (_take_token)."""
        request = self.parse_request(data)
        if request is None:
            logger.debug("Truncated PLI_UPDATEPACKAGEREQUESTFILE dropped")
            return
        # Every request is answered (the reference does), but past the budget
        # only after a wait. A connection's packets are handled one at a time
        # (Player._handle_packets awaits each handler), so this slows only
        # that client's own read loop, at most one request waits per
        # connection, and the wait is at most 1 / REQUEST_RATE seconds. The
        # sleep is cancelled with the connection's task; a client that went
        # away during it gets nothing.
        delay = self._take_token(player)
        if delay > 0:
            await asyncio.sleep(delay)
            if getattr(player, 'connected', True) is False:
                return
        await self._answer(player, *request)

    async def _answer(self, player: 'Player', name: str, request_type: int,
                      values: List[int]):
        # :1557-1559: a reinstall ignores the client's values.
        if request_type == REQUEST_TYPE_REINSTALL:
            values = []

        async with self._lock:
            package, reloaded = await self.load(name)
            states = await self.entry_states(package) if package else []
        if reloaded:
            # UpdatePackage.cpp:63: one RC chat line per missing FILE entry,
            # sent once the lock is released.
            found = {state.name for state in states}
            notices = [f"[Server]: Unable to find file '{base}' "
                       f"in package '{package.name}'"
                       for base in package.names if base not in found]
            for message in notices:
                logger.warning(message)
            self._send_notices(notices)
        for state in states:
            if state.oversize and state.name not in package.oversize_logged:
                package.oversize_logged.add(state.name)
                logger.warning(
                    "Update package %s: %s is over %d bytes; left out",
                    package.name, state.name, self.filesystem.max_file_size)
        states = [state for state in states if not state.oversize]
        by_name = {state.name: state for state in states}
        current = set()
        slots = package.slots if package else []
        for value, slot_name in zip(values, slots):
            state = by_name.get(slot_name)
            if state is not None and value != 0 and value in (
                    state.crc32, state.mod_time):
                current.add(slot_name)
        needed = [state for state in states if state.name not in current]

        total = sum(state.size for state in needed)
        await player.send_raw(build_update_package_size(name, total))
        filesystem = self.filesystem
        for state in needed:
            filesystem.remember_sent_file(player, state.name)
            await filesystem.send_resolved_file(player, state.name, state.path)
        await player.send_raw(build_update_package_done(name))
