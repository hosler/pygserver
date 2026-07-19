"""Checks for the classic level-sign wire encoding."""

from pygserver.protocol.packets import build_level_sign


def test_literal_hash_uses_classic_code_86():
    packet = build_level_sign(54, 47, "a#bc")

    assert packet[3:-1] == bytes((58, 118, 59, 60, 128))
