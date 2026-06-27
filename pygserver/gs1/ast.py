"""GS1 AST node definitions.

These mirror GS1Parser.g4's structure/precedence but are shaped for execution by
the Phase-3 visitor rather than to reproduce ANTLR's parse tree verbatim.
See memory: gs1-python-port.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# -- top level / statements -------------------------------------------------
@dataclass
class Program:
    body: list


@dataclass
class Block:
    body: list


@dataclass
class If:
    cond: Any
    then: list
    els: Optional[list] = None


@dataclass
class For:
    init: Any
    cond: Any
    post: Any
    body: list


@dataclass
class While:
    cond: Any
    body: list


@dataclass
class With:
    obj: Any
    body: list


@dataclass
class FuncDef:
    name: str
    body: list


@dataclass
class Flow:
    kind: str  # 'return' | 'break' | 'continue'


@dataclass
class Command:
    name: str
    args: list


@dataclass
class UserCall:
    name: str


@dataclass
class Assign:
    target: Any  # VarRef
    op: str
    value: Any


@dataclass
class ExprStmt:
    expr: Any


# -- expressions ------------------------------------------------------------
@dataclass
class Ternary:
    cond: Any
    a: Any
    b: Any


@dataclass
class BinOp:
    op: str
    left: Any
    right: Any


@dataclass
class UnaryOp:
    op: str
    operand: Any


@dataclass
class Postfix:
    op: str
    operand: Any


@dataclass
class InExpr:
    value: Any
    rng: Any


@dataclass
class Call:
    name: str  # builtin function
    args: list


@dataclass
class MessageCode:
    code: str
    args: list = field(default_factory=list)


@dataclass
class StrConcat:
    parts: list  # list of Str | MessageCode


@dataclass
class Str:
    value: str


@dataclass
class Num:
    value: float


@dataclass
class Bool:
    value: bool


@dataclass
class PathPart:
    name: str  # static name, or "" when the segment is dynamic (see atoms)
    index: list = field(default_factory=list)  # 0, 1 or 2 index expressions
    atoms: list = field(default_factory=list)  # Str|MessageCode parts for dynamic names


@dataclass
class VarRef:
    parts: list  # list[PathPart]


@dataclass
class ArrayLit:
    elements: list


@dataclass
class SpecialLit:
    kind: str  # ITEM | CARRY | DIRECTION | GENDER | COLOR | BADDY
    value: str


@dataclass
class RangeLit:
    lo: Any
    hi: Any
