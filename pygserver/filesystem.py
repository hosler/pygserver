"""
pygserver.filesystem - File serving system

Handles file requests, large file transfers, and RC file browser.
Based on GServer-v2 file serving implementation.
"""

import asyncio
import logging
import os
import re
import hashlib
import time
import zlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, BinaryIO

from .protocol.constants import PLO
from .text_safety import has_control_chars
from .protocol.packets import (
    PacketBuilder,
    build_file,
    build_raw_data_announcement,
    build_file_send_failed,
    build_file_uptodate,
    build_large_file_start,
    build_large_file_end,
    build_large_file_size,
)

if TYPE_CHECKING:
    from .server import GameServer
    from .player import Player

logger = logging.getLogger(__name__)


@dataclass
class FileInfo:
    """Information about a file for directory listings."""
    name: str
    size: int
    is_directory: bool
    modified_time: float = 0.0


@dataclass
class PendingUpload:
    """Tracks a large file upload in progress."""
    filename: str
    expected_size: int
    received_data: bytearray = field(default_factory=bytearray)
    player_id: int = 0

    @property
    def complete(self) -> bool:
        return len(self.received_data) >= self.expected_size


@dataclass
class PendingDownload:
    """Tracks a large file download in progress."""
    filename: str
    total_size: int
    sent_bytes: int = 0
    player_id: int = 0


class FileSystem:
    """
    Manages file serving for game clients.

    Handles:
    - File requests (levels, ganis, images, sounds)
    - Large file transfers (chunked)
    - File checksum verification
    - RC file browser operations
    """

    def __init__(self, server: 'GameServer', base_path: str = "."):
        self.server = server
        self.base_path = Path(base_path)

        # File type directories
        self.file_dirs: Dict[str, Path] = {
            'levels': self.base_path / 'levels',
            'gani': self.base_path / 'gani',
            'images': self.base_path / 'images',
            'sounds': self.base_path / 'sounds',
            'scripts': self.base_path / 'scripts',
            'world': self.base_path / 'world',
        }

        # Pending transfers
        self._uploads: Dict[int, PendingUpload] = {}  # player_id -> PendingUpload
        self._downloads: Dict[int, PendingDownload] = {}  # player_id -> PendingDownload

        # File cache (filename -> (checksum, data))
        self._cache: Dict[str, Tuple[int, bytes]] = {}
        self._cache_max_size = 50 * 1024 * 1024  # 50MB cache
        self._cache_current_size = 0

        # Settings
        # Both match GServer-v2 (server/src/player/Player.cpp: `fileData.size()
        # > 32000` selects the large-file path, and each chunk is clamped to
        # 32000). Ours was 64KB, so a file between 32001 and 65536 bytes went
        # out as one ordinary PLO_FILE where the reference server would have
        # chunked it - and pyReborn's `--tier1 large_file_transfer`, whose
        # fixture is 45000 bytes, never entered this path at all and passed
        # without exercising the thing it is named for.
        self.large_file_threshold = 32000
        self.chunk_size = 32000
        self.max_file_size = 10 * 1024 * 1024  # 10MB max

        # Lowercase name -> files under the public roots (see _find_file).
        self._public_index: Dict[str, List[Path]] = {}
        self._public_index_time = float('-inf')
        # (public roots, {directory: mtime_ns}) recorded by the last scan, and
        # whether any of those mtimes was too fresh to trust (see
        # _public_tree_changed).
        self._public_signature: Optional[tuple] = None
        self._public_signature_racy = True

        # Create directories
        self._ensure_directories()

    def _ensure_directories(self):
        """Ensure all file directories exist."""
        for dir_path in self.file_dirs.values():
            dir_path.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # File Request Handling
    # =========================================================================

    async def handle_want_file(self, player: 'Player', filename: str):
        """
        Handle PLI_WANTFILE - Client requesting a file.

        Args:
            player: Player requesting file
            filename: Requested filename
        """
        logger.debug(f"Player {player.id} wants file: {filename}")

        # Find file
        file_path = self._find_file(filename)
        if not file_path:
            logger.warning(f"File not found: {filename}")
            packet = build_file_send_failed(filename)
            await player.send_raw(packet)
            return

        # Check file size
        file_size = file_path.stat().st_size
        if file_size > self.max_file_size:
            logger.warning(f"File too large: {filename} ({file_size} bytes)")
            packet = build_file_send_failed(filename)
            await player.send_raw(packet)
            return

        # Send file
        if file_size > self.large_file_threshold:
            await self._send_large_file(player, filename, file_path)
        else:
            await self._send_file(player, filename, file_path)

    async def handle_update_file(
            self, player: 'Player', mod_time: int, filename: str):
        """Send a file only when its modification time has changed."""
        file_path = self._find_file(filename)
        if file_path and mod_time == int(file_path.stat().st_mtime):
            await player.send_raw(build_file_uptodate(filename))
            return
        await self.handle_want_file(player, filename)

    async def handle_verify_want_send(self, player: 'Player', checksum: int, filename: str):
        """
        Handle PLI_VERIFYWANTSEND - Client wants file if checksum differs.

        Args:
            player: Player requesting file
            checksum: Client's checksum of the file
            filename: Requested filename
        """
        logger.debug(f"Player {player.id} verify file: {filename} (checksum={checksum})")

        # Find file
        file_path = self._find_file(filename)
        if not file_path:
            packet = build_file_send_failed(filename)
            await player.send_raw(packet)
            return

        # Calculate server checksum
        server_checksum = self._calculate_checksum(file_path)

        if server_checksum == checksum:
            # File is up to date
            packet = build_file_uptodate(filename)
            await player.send_raw(packet)
        else:
            # Send updated file
            await self.handle_want_file(player, filename)

    async def _send_file(self, player: 'Player', filename: str, file_path: Path):
        """
        Send a small file to player.

        Args:
            player: Player to send to
            filename: Filename to report
            file_path: Path to file
        """
        try:
            # Blocking disk I/O off the event loop (run_in_executor rather than
            # asyncio.to_thread since this project targets Python 3.8+, which
            # predates to_thread).
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, file_path.read_bytes)

            # PLO_FILE carries raw bytes (with newlines), so announce its full
            # length via PLO_RAWDATA first; the codec compresses the stream.
            file_packet = build_file(filename, data)
            announcement = build_raw_data_announcement(len(file_packet))
            await player.send_raw(announcement + file_packet)

            logger.debug(f"Sent file {filename} to player {player.id} ({len(data)} bytes)")

        except Exception as e:
            logger.error(f"Error sending file {filename}: {e}")
            packet = build_file_send_failed(filename)
            await player.send_raw(packet)

    async def _send_large_file(self, player: 'Player', filename: str, file_path: Path):
        """
        Send a large file to player in chunks.

        Args:
            player: Player to send to
            filename: Filename to report
            file_path: Path to file
        """
        try:
            file_size = file_path.stat().st_size

            # Send start packet
            packet = build_large_file_start(filename)
            await player.send_raw(packet)

            # Send size
            packet = build_large_file_size(file_size)
            await player.send_raw(packet)

            # Track download
            self._downloads[player.id] = PendingDownload(
                filename=filename,
                total_size=file_size,
                player_id=player.id
            )

            # Send file in chunks, with the blocking open()/read() calls each
            # pushed to a worker thread (run_in_executor, not asyncio.to_thread
            # - this project targets Python 3.8+) so a large file transfer
            # doesn't stall the event loop for every other connection. The
            # await player.send_raw() calls between chunks already yield, so
            # this keeps that chunk-at-a-time behavior.
            loop = asyncio.get_event_loop()
            f = await loop.run_in_executor(None, open, file_path, 'rb')
            try:
                while True:
                    chunk = await loop.run_in_executor(None, f.read, self.chunk_size)
                    if not chunk:
                        break

                    # Repeat the complete file header so each raw chunk dispatches as PLO_FILE.
                    file_packet = build_file(filename, chunk)
                    announcement = build_raw_data_announcement(len(file_packet))
                    await player.send_raw(announcement + file_packet)

                    self._downloads[player.id].sent_bytes += len(chunk)
            finally:
                await loop.run_in_executor(None, f.close)

            # Send end packet
            packet = build_large_file_end(filename)
            await player.send_raw(packet)

            # Clean up
            del self._downloads[player.id]

            logger.info(f"Sent large file {filename} to player {player.id} ({file_size} bytes)")

        except Exception as e:
            logger.error(f"Error sending large file {filename}: {e}")
            if player.id in self._downloads:
                del self._downloads[player.id]
            packet = build_file_send_failed(filename)
            await player.send_raw(packet)

    # =========================================================================
    # Upload Handling
    # =========================================================================

    async def handle_large_file_start(self, player: 'Player', size: int, filename: str):
        """
        Handle start of large file upload.

        Args:
            player: Player uploading
            size: Expected file size
            filename: Target filename
        """
        self._uploads[player.id] = PendingUpload(
            filename=filename,
            expected_size=size,
            player_id=player.id
        )
        logger.info(f"Large file upload started: {filename} ({size} bytes)")

    async def handle_upload_data(self, player: 'Player', data: bytes):
        """
        Handle file upload data chunk.

        Args:
            player: Player uploading
            data: Data chunk
        """
        upload = self._uploads.get(player.id)
        if not upload:
            return

        upload.received_data.extend(data)

        if upload.complete:
            await self._finalize_upload(player, upload)

    async def handle_large_file_end(self, player: 'Player'):
        """
        Handle end of large file upload.

        Args:
            player: Player uploading
        """
        upload = self._uploads.get(player.id)
        if upload:
            await self._finalize_upload(player, upload)

    async def _finalize_upload(self, player: 'Player', upload: PendingUpload):
        """
        Finalize a file upload.

        Args:
            player: Player who uploaded
            upload: Upload information
        """
        try:
            # Determine target path
            file_path = self._get_upload_path(upload.filename)
            if not file_path:
                logger.error(f"Invalid upload path: {upload.filename}")
                return

            # Write file
            with open(file_path, 'wb') as f:
                f.write(upload.received_data)

            logger.info(f"File upload complete: {upload.filename} ({len(upload.received_data)} bytes)")

        except Exception as e:
            logger.error(f"Error finalizing upload {upload.filename}: {e}")

        finally:
            del self._uploads[player.id]

    def _get_upload_path(self, filename: str) -> Optional[Path]:
        """Get safe upload path for a filename."""
        if not filename or '\x00' in filename:
            return None

        relative = Path(filename.replace('\\', '/'))
        if relative.is_absolute() or '..' in relative.parts:
            return None

        base = self.base_path.resolve()
        candidate = (base / relative).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return None
        return candidate

    # =========================================================================
    # RC File Browser
    # =========================================================================

    def list_directory(self, path: str = "") -> List[FileInfo]:
        """
        List files in a directory.

        Args:
            path: Relative path from base

        Returns:
            List of FileInfo objects
        """
        files = []

        # Sanitize path
        path = path.replace('..', '').strip('/')
        dir_path = self.base_path / path if path else self.base_path

        if not dir_path.exists() or not dir_path.is_dir():
            return files

        try:
            for entry in sorted(dir_path.iterdir()):
                stat = entry.stat()
                files.append(FileInfo(
                    name=entry.name,
                    size=stat.st_size if entry.is_file() else 0,
                    is_directory=entry.is_dir(),
                    modified_time=stat.st_mtime
                ))
        except Exception as e:
            logger.error(f"Error listing directory {path}: {e}")

        return files

    def move_file(self, src: str, dst: str) -> bool:
        """
        Move/rename a file.

        Args:
            src: Source path
            dst: Destination path

        Returns:
            True if successful
        """
        try:
            src_path = self.base_path / src.strip('/')
            dst_path = self.base_path / dst.strip('/')

            if not src_path.exists():
                return False

            src_path.rename(dst_path)
            logger.info(f"Moved file: {src} -> {dst}")
            return True

        except Exception as e:
            logger.error(f"Error moving file {src} -> {dst}: {e}")
            return False

    def delete_file(self, path: str) -> bool:
        """
        Delete a file.

        Args:
            path: File path

        Returns:
            True if successful
        """
        try:
            file_path = self.base_path / path.strip('/')

            if not file_path.exists():
                return False

            if file_path.is_file():
                file_path.unlink()
                logger.info(f"Deleted file: {path}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error deleting file {path}: {e}")
            return False

    def delete_folder(self, path: str) -> bool:
        """
        Delete a folder.

        Args:
            path: Folder path

        Returns:
            True if successful
        """
        try:
            folder_path = self.base_path / path.strip('/')

            if not folder_path.exists() or not folder_path.is_dir():
                return False

            # Remove recursively
            import shutil
            shutil.rmtree(folder_path)
            logger.info(f"Deleted folder: {path}")
            return True

        except Exception as e:
            logger.error(f"Error deleting folder {path}: {e}")
            return False

    # =========================================================================
    # Utility Methods
    # =========================================================================

    # Folders a game client may read from by name. The reference binds the whole
    # `world/` tree recursively and answers PLI_WANTFILE / PLI_UPDATEFILE from
    # FileCategory::ALL, i.e. every regular file under it, looked up by file
    # name alone and case-insensitively (Server.cpp:491-530 loadFolderConfig,
    # FileSystem.cpp:76-116 bind, :442-454 infoi, Player.cpp:577 sendFile,
    # PlayerClientPackets.cpp:962). foldersconfig.txt only sorts those files
    # into categories; its patterns are relative to `world/`.
    # Our worlds also keep content in sibling folders of `world/` (the
    # `images/ gani/ sounds/` of the native layout, and `bodies/ heads/ swords/
    # shields/ hats/` that pyReborn's LocalWorld links beside it), so the
    # public roots are `world/` plus these names plus the first folder of every
    # foldersconfig.txt pattern, each searched recursively. Never public:
    # PRIVATE_DIRS, i.e. accounts/, config/, logs/, weapons/, npcs/,
    # scripts/ (the script SOURCE) and the other server-side folders, nor
    # serverflags.txt or anything outside the server folder.
    CLIENT_FILE_DIRS = (
        'world', 'levels', 'gani', 'ganis', 'images', 'sounds',
        'bodies', 'heads', 'swords', 'shields', 'hats', 'global')
    PRIVATE_DIRS = frozenset((
        'accounts', 'config', 'logs', 'weapons', 'npcs', 'scripts', 'guilds',
        'execscripts', 'translations', 'documents', 'snapshots', 'tools'))

    # Where PLI_PUTNPC looks for its script: FileCategory::FILE by bare name
    # (PlayerClientPackets.cpp:769), i.e. the world folder. The private
    # scripts/ folder is never read for a client-chosen name.
    NPC_SCRIPT_DIRS = ('world',)

    # A name missing from the index triggers a re-scan only if a public
    # directory changed since the last scan (a stat per known directory), and
    # at most this often, so a file added while the server runs is found
    # without a request flood re-walking the tree on the event loop.
    INDEX_RESCAN_SECONDS = 1.0
    # A directory modified this recently when it was scanned may take a further
    # change inside the same timestamp tick, so the next miss re-scans again.
    _MTIME_SETTLE_NS = 2_000_000_000

    @staticmethod
    def is_bare_filename(name) -> bool:
        """True for a plain file name: no separator, `..`, NUL, control
        character or leading dot."""
        if not isinstance(name, str) or not name or len(name) > 255:
            return False
        if name.startswith('.') or '..' in name:
            return False
        if '/' in name or '\\' in name:
            return False
        return not has_control_chars(name)

    # -- foldersconfig.txt ------------------------------------------------------
    #
    # Server.cpp:497-512 files every pattern under `world/`, so a pattern can
    # never name a folder outside it. Here a pattern's first folder becomes a
    # sibling public root (see above), so it is held to what a world can
    # legitimately hold: a CLIENT_FILE_DIRS name or a folder that exists under
    # `world/`, never a dot-folder or a PRIVATE_DIRS name, and never a folder
    # that resolves outside the server folder. The same checks gate the RC
    # set (rc.py) and every read of the file, which may be hand-edited.
    FOLDERS_CONFIG_MAX_LINES = 1024
    FOLDERS_CONFIG_MAX_LINE = 1024
    # The file is split on '\n' only. A line still holding another character
    # str.splitlines() breaks on, or any other C0/C1 control, is dropped whole:
    # a later reader must never see two lines where this one saw one.
    _FOLDERS_CONFIG_BAD_CHARS = re.compile('[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029]')
    _folders_config_cache: Optional[tuple] = None

    @staticmethod
    def split_folders_config_line(line: str) -> Optional[Tuple[str, str]]:
        """(lowercase category, glob) of one line, None for a blank or
        comment-only line. `#` starts a comment (Server.cpp:491-494)."""
        body = line.split('#', 1)[0].strip(' \t')
        if not body:
            return None
        parts = re.split('[ \t]+', body, maxsplit=1)
        glob = parts[1].strip(' \t').replace('\\', '/') if len(parts) > 1 else ''
        return parts[0].lower(), glob

    def _folders_config_first_allowed(self, first: str) -> bool:
        """A pattern's first folder is either a CLIENT_FILE_DIRS name or a
        folder that exists inside `world/` (where the reference files every
        pattern, Server.cpp:497-512)."""
        if not first or first.startswith('.'):
            return False
        if first.lower() in {name.lower() for name in self.CLIENT_FILE_DIRS}:
            return True
        world = self.base_path / 'world'
        try:
            if not (world / first).is_dir():
                return False
            (world / first).resolve().relative_to(world.resolve())
        except (OSError, ValueError, RuntimeError):
            return False
        return True

    def _folders_config_root(self, first: str) -> Optional[Path]:
        """The sibling public root a pattern's first folder names, or None.
        On top of `_folders_config_first_allowed`: never a PRIVATE_DIRS name
        (a `npcs/*` line still sorts `world/npcs`), and never a folder that
        resolves outside the server folder."""
        if (set(first) & set('*?[') or not self._folders_config_first_allowed(first)
                or first.lower() in self.PRIVATE_DIRS):
            return None
        root = self.base_path / first
        if first.lower() in {name.lower() for name in self.CLIENT_FILE_DIRS}:
            # Public whatever the config says; these may be links to shared
            # asset folders (pyReborn's LocalWorld), so no containment check.
            return root
        try:
            root.resolve().relative_to(self.base_path.resolve())
        except (OSError, ValueError, RuntimeError):
            return None
        return root

    def folders_config_glob_allowed(self, glob: str) -> bool:
        """Whether a foldersconfig.txt pattern stays inside the folders a
        client may already read: no absolute path, drive, dot segment (`.`,
        `..`, `.git`), and a first folder `_folders_config_first_allowed`
        accepts. A pattern with no folder (`*.png`) or a wildcard first
        segment names `world/` itself and adds no root."""
        if glob.startswith('/') or ':' in glob:
            return False
        segments = glob.split('/')
        if any(segment.startswith('.') for segment in segments):
            return False
        if len(segments) == 1 or set(segments[0]) & set('*?['):
            return True
        return self._folders_config_first_allowed(segments[0])

    def folders_config_line_allowed(self, line: str) -> bool:
        if (len(line) > self.FOLDERS_CONFIG_MAX_LINE
                or self._FOLDERS_CONFIG_BAD_CHARS.search(line)):
            return False
        parsed = self.split_folders_config_line(line)
        return parsed is None or self.folders_config_glob_allowed(parsed[1])

    def folders_config_path(self) -> Path:
        return self.base_path / 'config' / 'foldersconfig.txt'

    def folders_config_entries(self) -> List[Tuple[str, str]]:
        """(category, glob) of every allowed foldersconfig.txt line.

        Parsed once per version of the file, keyed on its (mtime_ns, size,
        inode); `invalidate_folders_config` drops the cached copy.
        """
        path = self.folders_config_path()
        try:
            info = os.stat(path)
        except OSError:
            self._folders_config_cache = None
            return []
        key = (info.st_mtime_ns, info.st_size, info.st_ino)
        cached = self._folders_config_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            with open(path, 'rb') as stream:
                text = stream.read(self.FOLDERS_CONFIG_MAX_LINES
                                   * (self.FOLDERS_CONFIG_MAX_LINE + 1)).decode('latin-1')
        except OSError:
            return []
        entries: List[Tuple[str, str]] = []
        for line in text.split('\n')[:self.FOLDERS_CONFIG_MAX_LINES]:
            if line.endswith('\r'):
                line = line[:-1]
            if not self.folders_config_line_allowed(line):
                continue
            parsed = self.split_folders_config_line(line)
            if parsed is not None:
                entries.append(parsed)
        self._folders_config_cache = (key, entries)
        return entries

    def invalidate_folders_config(self):
        """Forget the parsed foldersconfig.txt and the public index built from
        it, so a narrowed config stops serving its old roots at once."""
        self._folders_config_cache = None
        self._public_index = {}
        self._public_signature = None
        self._public_index_time = float('-inf')

    def _folders_config_dirs(self) -> List[str]:
        """First folder of each allowed foldersconfig.txt pattern
        (`heads/*` -> heads). A glob with no folder names `world/` itself."""
        names: List[str] = []
        for _category, glob in self.folders_config_entries():
            if '/' not in glob:
                continue
            first = glob.split('/', 1)[0].strip()
            if (first not in names
                    and self._folders_config_root(first) is not None):
                names.append(first)
        return names

    def public_dirs(self) -> List[Path]:
        """Existing public root folders, in search order (`world/` first)."""
        names = list(self.CLIENT_FILE_DIRS)
        names += [n for n in self._folders_config_dirs() if n not in names]
        roots = []
        for name in names:
            if name.lower() in self.PRIVATE_DIRS:
                continue
            root = self.base_path / name
            if root.is_dir():
                roots.append(root)
        return roots

    def _rebuild_public_index(self) -> Dict[str, List[Path]]:
        """Walk every public root and key each regular file by lowercase name."""
        index: Dict[str, List[Path]] = {}
        roots = self.public_dirs()
        mtimes: Dict[str, int] = {}
        scan_start = time.time_ns()
        for root in roots:
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                try:
                    # Before this directory's files are classified, so a change
                    # after the listing shows up as a newer mtime next time.
                    mtimes[dirpath] = os.stat(dirpath).st_mtime_ns
                except OSError:
                    pass
                dirnames.sort()
                for filename in sorted(filenames):
                    if not self.is_bare_filename(filename):
                        continue
                    path = Path(dirpath) / filename
                    try:
                        if not path.is_file():
                            continue
                        path.resolve().relative_to(resolved_root)
                    except (OSError, ValueError):
                        continue    # a link leaving the folder
                    index.setdefault(filename.lower(), []).append(path)
        self._public_index = index
        self._public_signature = (tuple(roots), mtimes)
        self._public_signature_racy = any(
            scan_start - m < self._MTIME_SETTLE_NS for m in mtimes.values())
        self._public_index_time = time.monotonic()
        return index

    def _public_tree_changed(self) -> bool:
        """Whether a public folder was added, removed or modified since the
        last scan. Adding, removing or renaming an entry bumps its directory's
        mtime (a new subfolder bumps its parent's), so a stat of each directory
        the scan saw is enough; no file is opened or resolved."""
        if self._public_signature is None or self._public_signature_racy:
            return True
        roots, mtimes = self._public_signature
        if tuple(self.public_dirs()) != roots:
            return True
        for dirpath, mtime in mtimes.items():
            try:
                if os.stat(dirpath).st_mtime_ns != mtime:
                    return True
            except OSError:
                return True
        return False

    def _lookup_public(self, filename: str) -> Optional[Path]:
        candidates = self._public_index.get(filename.lower(), ())
        for path in candidates:
            if path.name == filename and path.is_file():
                return path
        for path in candidates:
            if path.is_file():
                return path
        return None

    def _find_file(self, filename: str, dirs=None,
                   strict: bool = False) -> Optional[Path]:
        """Resolve a client-supplied file name to a file inside a public folder.

        The name is reduced to its final component (the reference does the
        same, Player.cpp:577) unless `strict`, in which case anything that is
        not already a bare name is refused. By default every public root
        (`public_dirs`) is searched recursively and case-insensitively, with an
        exact-case match preferred; the server root, a full relative path,
        accounts/, config/, weapons/ and serverflags.txt cannot be reached. A
        symlink leaving its folder is refused. With `dirs`, only those folders
        directly under base_path are searched, non-recursively (PLI_PUTNPC).
        """
        if not isinstance(filename, str):
            return None
        if not strict:
            filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        if not self.is_bare_filename(filename):
            return None

        if dirs is not None:
            for dirname in dirs:
                directory = self.base_path / dirname
                file_path = directory / filename
                try:
                    if not file_path.is_file():
                        continue
                    file_path.resolve().relative_to(directory.resolve())
                except (OSError, ValueError):
                    continue
                return file_path
            return None

        found = self._lookup_public(filename)
        if found is None and (time.monotonic() - self._public_index_time
                              >= self.INDEX_RESCAN_SECONDS
                              and self._public_tree_changed()):
            self._rebuild_public_index()
            found = self._lookup_public(filename)
        return found

    def _calculate_checksum(self, file_path: Path) -> int:
        """
        Calculate CRC32 checksum of a file.

        Args:
            file_path: Path to file

        Returns:
            CRC32 checksum
        """
        try:
            with open(file_path, 'rb') as f:
                return zlib.crc32(f.read()) & 0xFFFFFFFF
        except Exception:
            return 0

    def file_exists(self, filename: str) -> bool:
        """Check if a file exists."""
        return self._find_file(filename) is not None

    def get_file_size(self, filename: str) -> int:
        """Get the size of a file."""
        file_path = self._find_file(filename)
        if file_path:
            return file_path.stat().st_size
        return 0

    def read_file(self, filename: str) -> Optional[bytes]:
        """
        Read a file's contents.

        Args:
            filename: Filename to read

        Returns:
            File contents, or None if not found
        """
        file_path = self._find_file(filename)
        if not file_path:
            return None

        try:
            with open(file_path, 'rb') as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error reading file {filename}: {e}")
            return None

    def write_file(self, filename: str, data: bytes) -> bool:
        """
        Write data to a file.

        Args:
            filename: Filename to write
            data: Data to write

        Returns:
            True if successful
        """
        file_path = self._get_upload_path(filename)
        if not file_path:
            return False

        try:
            with open(file_path, 'wb') as f:
                f.write(data)
            return True
        except Exception as e:
            logger.error(f"Error writing file {filename}: {e}")
            return False

    async def send_file(self, player: 'Player', filename: str):
        """
        Send a file to a player.

        Args:
            player: Player to send to
            filename: File to send
        """
        file_path = self._find_file(filename)
        if not file_path:
            packet = build_file_send_failed(filename)
            await player.send_raw(packet)
            return

        file_size = file_path.stat().st_size
        if file_size > self.large_file_threshold:
            await self._send_large_file(player, filename, file_path)
        else:
            await self._send_file(player, filename, file_path)
