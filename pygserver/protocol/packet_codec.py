"""Compatibility extensions around the shared packet codec."""

from reborn_protocol.codec import (
    PacketBuilder as SharedPacketBuilder,
    PacketReader as SharedPacketReader,
)


class PacketReader(SharedPacketReader):
    """Shared reader with preserved server-specific edge behavior."""

    def read_gchar_signed(self) -> int:
        """Read a signed GCHAR without clamping."""
        return self.read_byte() - 32

    def read_gshort(self) -> int:
        """Read a two-byte encoded integer."""
        # Divergence: codec.py:79 consumes a truncated buffer and clamps
        # malformed negative results; this reader historically does neither.
        if self.pos + 1 >= len(self.data):
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        self.pos += 2
        return (b1 << 7) + b2

    def read_gint3(self) -> int:
        """Read a three-byte encoded integer."""
        # Divergence: codec.py:96 consumes a truncated buffer and clamps
        # malformed negative results; this reader historically does neither.
        if self.pos + 2 >= len(self.data):
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        b3 = self.data[self.pos + 2] - 32
        self.pos += 3
        return (b1 << 14) + (b2 << 7) + b3

    def read_gint5(self) -> int:
        """Read a five-byte encoded integer."""
        # Divergence: codec.py:129 consumes truncated input, uses addition,
        # and masks the result to 32 bits; this reader keeps its bitwise fold.
        if self.pos + 4 >= len(self.data):
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        b3 = self.data[self.pos + 2] - 32
        b4 = self.data[self.pos + 3] - 32
        b5 = self.data[self.pos + 4] - 32
        self.pos += 5
        return (b1 << 28) | (b2 << 21) | (b3 << 14) | (b4 << 7) | b5

    def read_string(self, length: int) -> str:
        """Read a fixed-length string."""
        # Divergence: codec.py:149 clamps negative lengths to zero; this
        # reader preserves slicing and cursor movement for negative lengths.
        if self.pos + length > len(self.data):
            length = len(self.data) - self.pos
        data = self.data[self.pos:self.pos + length]
        self.pos += length
        return data.decode(self.encoding, errors="replace")

    def read_bytes(self, length: int) -> bytes:
        """Read raw bytes."""
        if self.pos + length > len(self.data):
            length = len(self.data) - self.pos
        data = self.data[self.pos:self.pos + length]
        self.pos += length
        return data

    def skip(self, count: int) -> None:
        """Skip bytes."""
        # Divergence: codec.py:196 caps the cursor at the buffer length; this
        # reader historically permits the cursor to move beyond either end.
        self.pos += count


class PacketBuilder(SharedPacketBuilder):
    """Shared builder with preserved server-specific edge behavior."""

    def __init__(self):
        super().__init__()
        self.data = self._data

    def write_gchar(self, value: int) -> "PacketBuilder":
        """Write a one-byte encoded integer."""
        # Divergence: codec.py:226 clamps to 0..223; this writer wraps.
        self._data.append((value + 32) & 0xFF)
        return self

    def write_gchar_signed(self, value: int) -> "PacketBuilder":
        """Write a signed one-byte encoded integer."""
        return self.write_gchar(value)

    def write_gshort(self, value: int) -> "PacketBuilder":
        """Write a two-byte encoded integer."""
        # Divergence: codec.py:235 clamps to 0..28767 and permits carry in
        # the low lane; this writer wraps the top lane and masks the low lane.
        self._data.append(((value >> 7) + 32) & 0xFF)
        self._data.append(((value & 0x7F) + 32) & 0xFF)
        return self

    def write_gint3(self, value: int) -> "PacketBuilder":
        """Write a three-byte encoded integer."""
        # Divergence: codec.py:247 clamps to 0..3682399 and permits carry in
        # inner lanes; this writer wraps the top lane and masks lower lanes.
        self._data.append(((value >> 14) + 32) & 0xFF)
        self._data.append((((value >> 7) & 0x7F) + 32) & 0xFF)
        self._data.append(((value & 0x7F) + 32) & 0xFF)
        return self

    def write_gint5(self, value: int) -> "PacketBuilder":
        """Write a five-byte encoded integer."""
        # Divergence: codec.py:284 clamps to the unsigned 32-bit range; this
        # writer wraps the top lane and masks each of the four lower lanes.
        self._data.append(((value >> 28) + 32) & 0xFF)
        self._data.append((((value >> 21) & 0x7F) + 32) & 0xFF)
        self._data.append((((value >> 14) & 0x7F) + 32) & 0xFF)
        self._data.append((((value >> 7) & 0x7F) + 32) & 0xFF)
        self._data.append(((value & 0x7F) + 32) & 0xFF)
        return self

    def write_gstring(self, value: str) -> "PacketBuilder":
        """Write a one-byte-length-prefixed string."""
        # Divergence: codec.py:313 truncates encoded data to 223 bytes; this
        # writer retains the full data after its wrapping length prefix.
        encoded = value.encode("latin-1", errors="replace")
        self.write_gchar(len(encoded))
        self._data.extend(encoded)
        return self

    def write_gstring_short(self, value: str) -> "PacketBuilder":
        """Write a two-byte-length-prefixed string."""
        # Divergence: codec.py:321 truncates encoded data to 28767 bytes; this
        # writer retains the full data after its wrapping length prefix.
        encoded = value.encode("latin-1", errors="replace")
        self.write_gshort(len(encoded))
        self._data.extend(encoded)
        return self

    def write_newline(self) -> "PacketBuilder":
        """Write a packet terminator."""
        self._data.append(0x0A)
        return self
