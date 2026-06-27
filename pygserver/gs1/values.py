"""GS1 value coercion.

GS1 is loosely typed: a value is a number (float), a string, or an array. The
C++ engine's GameValue can even hold a number AND a string simultaneously; we
model the common single-type case and coerce on demand. See memory:
gs1-python-port.
"""
from __future__ import annotations

import re

_LEADING_NUM = re.compile(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)")


def to_num(v) -> float:
    """Coerce a value to a number. Strings yield their leading numeric prefix."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _LEADING_NUM.match(v)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0
    if isinstance(v, (list, tuple)):
        return float(len(v))
    return 0.0


def fmt_num(x: float) -> str:
    """Format a number the way GS1 prints it (integers without a decimal)."""
    if x != x:  # NaN
        return "0"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    # trim trailing zeros from a fixed representation
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s


def to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return fmt_num(float(v))
    if isinstance(v, (list, tuple)):
        return ",".join(to_str(x) for x in v)
    return str(v)


def to_bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        # a string is truthy if it is non-empty; a numeric string by its value
        m = _LEADING_NUM.fullmatch(v.strip()) if v else None
        if m:
            return to_num(v) != 0
        return v != ""
    if isinstance(v, (list, tuple)):
        return len(v) > 0
    return bool(v)
