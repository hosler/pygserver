"""
pygserver.filesystem - File serving system

Handles file requests, large file transfers, and RC file browser.
Based on GServer-v2 file serving implementation.
"""

import asyncio
import logging
import os
import hashlib
import zlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, BinaryIO

from .protocol.constants import PLO
from .protocol.packets import (
    PacketBuilder,
    build_file,
    build_file_send_failed,
    build_file_uptodate,
    build_large_file_start,
    build_large_file_end,
    build_large_file_size,
    build_gani_script,
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
        self.large_file_threshold = 64 * 1024  # 64KB
        self.chunk_size = 32 * 1024  # 32KB chunks for large files
        self.max_file_size = 10 * 1024 * 1024  # 10MB max

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
            with open(file_path, 'rb') as f:
                data = f.read()

            # Compress if beneficial
            compressed = zlib.compress(data, 6)
            if len(compressed) < len(data):
                data = compressed

            packet = build_file(filename, data)
            await player.send_raw(packet)

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

            # Send file in chunks
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(self.chunk_size)
                    if not chunk:
                        break

                    # Send chunk as raw data
                    builder = PacketBuilder()
                    builder.write_gchar(PLO.RAWDATA)
                    builder.write_gint3(len(chunk))
                    builder.write_byte(ord('\n'))
                    await player.send_raw(builder.build())
                    await player.send_raw(chunk)

                    self._downloads[player.id].sent_bytes += len(chunk)

            # Send end packet
            packet = build_large_file_end()
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
    # Gani/Script Handling
    # =========================================================================

    async def handle_update_gani(self, player: 'Player', filename: str):
        """
        Handle PLI_UPDATEGANI - Client requesting gani.

        Args:
            player: Player requesting gani
            filename: Gani filename
        """
        file_path = self._find_file(filename)
        if not file_path:
            return

        try:
            with open(file_path, 'r', encoding='latin-1') as f:
                script = f.read()

            packet = build_gani_script(filename, script)
            await player.send_raw(packet)

        except Exception as e:
            logger.error(f"Error sending gani {filename}: {e}")

    async def handle_update_script(self, player: 'Player', filename: str):
        """
        Handle PLI_UPDATESCRIPT - Client requesting script.

        Args:
            player: Player requesting script
            filename: Script filename
        """
        await self.handle_update_gani(player, filename)

    async def handle_update_class(self, player: 'Player', classname: str):
        """
        Handle PLI_UPDATECLASS - Client requesting class script.

        Args:
            player: Player requesting class
            classname: Class name
        """
        if hasattr(self.server, 'class_manager'):
            cls = self.server.class_manager.get_class(classname)
            if cls:
                packet = build_gani_script(classname, cls.script)
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
        # Sanitize filename
        filename = filename.replace('..', '').replace('//', '/')

        # Determine directory by extension
        ext = Path(filename).suffix.lower()
        if ext in ['.nw', '.graal', '.zelda']:
            base = self.file_dirs['levels']
        elif ext == '.gani':
            base = self.file_dirs['gani']
        elif ext in ['.png', '.gif', '.bmp']:
            base = self.file_dirs['images']
        elif ext in ['.wav', '.mp3', '.ogg']:
            base = self.file_dirs['sounds']
        else:
            base = self.base_path

        return base / Path(filename).name

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

    def _find_file(self, filename: str) -> Optional[Path]:
        """
        Find a file by name in file directories.

        Args:
            filename: Filename to find

        Returns:
            Path to file, or None if not found
        """
        # Clean filename
        filename = filename.replace('..', '').strip('/')
        name = Path(filename).name

        # Check each directory
        for dir_path in self.file_dirs.values():
            file_path = dir_path / name
            if file_path.exists() and file_path.is_file():
                return file_path

        # Check base path
        file_path = self.base_path / name
        if file_path.exists() and file_path.is_file():
            return file_path

        # Check with full path
        file_path = self.base_path / filename
        if file_path.exists() and file_path.is_file():
            return file_path

        return None

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
