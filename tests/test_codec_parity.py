"""Byte-level parity checks for the local and shared packet codecs."""

import pytest

from pygserver.protocol.packets import (
    PacketBuilder as LocalBuilder,
    PacketReader as LocalReader,
)
from reborn_protocol.codec import (
    PacketBuilder as SharedBuilder,
    PacketReader as SharedReader,
)


@pytest.mark.parametrize(
    ("method", "values"),
    [
        ("write_byte", (0, 1, 255)),
        ("write_gchar", (0, 1, 223)),
        ("write_gshort", (0, 1, 16383)),
        ("write_gint3", (0, 1, 2097151)),
        ("write_gint4", (0, 1, 471347295)),
        ("write_gint5", (0, 1, 0xFFFFFFFF)),
    ],
)
def test_numeric_writer_parity(method, values):
    for value in values:
        local = LocalBuilder()
        shared = SharedBuilder()
        getattr(local, method)(value)
        getattr(shared, method)(value)
        assert local.build() == shared.build()


@pytest.mark.parametrize("value", ("", "abc", "caf\xe9", "\u2603"))
def test_string_writer_parity(value):
    for method in ("write_string", "write_gstring", "write_gstring_short"):
        local = LocalBuilder()
        shared = SharedBuilder()
        getattr(local, method)(value)
        getattr(shared, method)(value)
        assert local.build() == shared.build()


def test_bytes_position_and_build_parity():
    local = LocalBuilder().write_bytes(b"\x00\xff").write_position2(-1.5)
    shared = SharedBuilder().write_bytes(b"\x00\xff").write_position2(-1.5)
    assert local.build() == shared.build()


@pytest.mark.parametrize(
    ("writer", "reader", "values"),
    [
        ("write_byte", "read_byte", (0, 1, 255)),
        ("write_gchar", "read_gchar", (0, 1, 223)),
        ("write_gshort", "read_gshort", (0, 1, 16383)),
        ("write_gint3", "read_gint3", (0, 1, 2097151)),
        ("write_gint5", "read_gint5", (0, 1, 0xFFFFFFFF)),
    ],
)
def test_numeric_reader_parity(writer, reader, values):
    for value in values:
        data = getattr(LocalBuilder(), writer)(value).build()
        local = LocalReader(data)
        shared = SharedReader(data)
        assert getattr(local, reader)() == getattr(shared, reader)()
        assert local.pos == shared.pos


def test_string_reader_and_cursor_parity():
    data = b"abcXYZ"
    local = LocalReader(data)
    shared = SharedReader(data)
    assert local.read_string(3) == shared.read_string(3)
    assert local.read_bytes(1) == b"X"
    shared.skip(1)
    assert local.peek_byte() == shared.peek_byte()
    assert local.remaining() == shared.remaining()
    assert local.has_data() == shared.has_data()

    for method, data in (
        ("read_gstring", LocalBuilder().write_gstring("abc").build()),
        ("read_gstring_short", LocalBuilder().write_gstring_short("abc").build()),
    ):
        local = LocalReader(data)
        shared = SharedReader(data)
        assert getattr(local, method)() == getattr(shared, method)()
        assert local.pos == shared.pos


@pytest.mark.parametrize("reader", ("read_gshort", "read_gint3", "read_gint5"))
def test_truncated_numeric_reader_divergence_is_documented(reader):
    data = b"!"
    local = LocalReader(data)
    shared = SharedReader(data)
    assert getattr(local, reader)() == getattr(shared, reader)() == 0
    assert local.pos == 0
    assert shared.pos == len(data)


@pytest.mark.parametrize(
    ("method", "value"),
    [
        ("write_gchar", -1),
        ("write_gchar", 224),
        ("write_gshort", -1),
        ("write_gshort", 28767),
        ("write_gshort", 28768),
        ("write_gint3", -1),
        ("write_gint3", 3682399),
        ("write_gint3", 3682400),
        ("write_gint5", -1),
        ("write_gint5", 0x100000000),
    ],
)
def test_out_of_range_writer_divergence_is_documented(method, value):
    local = LocalBuilder()
    shared = SharedBuilder()
    getattr(local, method)(value)
    getattr(shared, method)(value)
    assert local.build() != shared.build()


def test_oversize_string_writer_divergence_is_documented():
    for method, value in (
        ("write_gstring", "x" * 224),
        ("write_gstring_short", "x" * 28768),
    ):
        local = LocalBuilder()
        shared = SharedBuilder()
        getattr(local, method)(value)
        getattr(shared, method)(value)
        assert local.build() != shared.build()


def test_malformed_reader_and_cursor_divergences_are_documented():
    local = LocalReader(b"\x00\x00")
    shared = SharedReader(b"\x00\x00")
    assert local.read_gshort() < 0
    assert shared.read_gshort() == 0

    local = LocalReader(b"abc")
    shared = SharedReader(b"abc")
    assert local.read_string(-1) == "ab"
    assert shared.read_string(-1) == ""
    assert local.pos == -1
    assert shared.pos == 0

    local = LocalReader(b"abc")
    shared = SharedReader(b"abc")
    local.skip(10)
    shared.skip(10)
    assert local.pos == 10
    assert shared.pos == 3


def test_five_byte_reader_arithmetic_divergence_is_documented():
    data = b"\x00\x00\x00\x00\x00"
    local = LocalReader(data)
    shared = SharedReader(data)
    assert local.read_gint5() == -32
    assert shared.read_gint5() == 4227330016


def test_local_signed_and_newline_extensions():
    data = LocalBuilder().write_gchar_signed(-1).write_newline().build()
    reader = LocalReader(data)
    assert reader.read_gchar_signed() == -1
    assert reader.read_byte() == 0x0A
