"""GS1 interpreter — the tree-walking executor.

Owns control flow, expression evaluation and the flag/var stores; routes
built-in attributes, message codes and side-effecting commands to a Host
(runtime.py). Pure math/string functions are implemented here; game functions
(onwall, getnpc, playersays, ...) fall through to the host.

See memory: gs1-python-port. Phase 4 fleshes out the full command/function set
and diffs against the C++ oracle; this establishes the engine + core.
"""
from __future__ import annotations

import math
import random as _random

from . import ast
from .parser import parse
from .runtime import (Context, Host, MemoryHost, VarStore, UNSET, NAMESPACES,
                      BreakSignal, ContinueSignal, ReturnSignal)
from .values import to_num, to_str, to_bool, fmt_num

# commands the interpreter handles itself (manipulate the var store)
_VAR_COMMANDS = {"set", "unset", "setstring", "addstring", "setarray"}
_MAX_CALL_DEPTH = 100


class Interpreter:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._depth = 0

    # -- entry -------------------------------------------------------------
    def run(self, program: ast.Program):
        # hoist user function definitions first
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                self.ctx.functions[stmt.name] = stmt
        for stmt in program.body:
            if not isinstance(stmt, ast.FuncDef):
                try:
                    self.exec(stmt)
                except (BreakSignal, ContinueSignal, ReturnSignal):
                    pass  # stray control-flow at top level is a no-op

    def run_event(self, program: ast.Program, event: str):
        """Fire an event: set its flag true and run the whole script.

        In GS1 an event name (created, playerenters, playerchats, ...) is a flag
        that reads true only during that event, so `if (playerchats && ...)` and
        `if (created)` gate behaviour. We run every top-level statement; blocks
        for other events see their flag as false and skip themselves.
        """
        self.ctx.active_event = event
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                self.ctx.functions[stmt.name] = stmt
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                continue
            try:
                self.exec(stmt)
            except (BreakSignal, ContinueSignal, ReturnSignal):
                pass  # stray control-flow outside a loop is a no-op

    # -- statements --------------------------------------------------------
    def exec_block(self, body):
        for stmt in body:
            self.exec(stmt)

    def _step(self):
        self.ctx.steps += 1
        if self.ctx.steps > self.ctx.max_steps:
            raise RuntimeError("GS1 step budget exceeded (possible infinite loop)")

    def exec(self, node):
        self._step()
        m = getattr(self, "_st_" + type(node).__name__, None)
        if m is None:
            raise RuntimeError(f"cannot execute node {type(node).__name__}")
        m(node)

    def _st_Block(self, node):
        self.exec_block(node.body)

    def _st_ExprStmt(self, node):
        if node.expr is not None:
            self.eval(node.expr)

    def _st_If(self, node):
        if to_bool(self.eval(node.cond)):
            self.exec_block(node.then)
        elif node.els is not None:
            self.exec_block(node.els)

    def _st_While(self, node):
        while to_bool(self.eval(node.cond)):
            self._step()  # guard empty-body loops too
            try:
                self.exec_block(node.body)
            except BreakSignal:
                break
            except ContinueSignal:
                continue

    def _st_For(self, node):
        if node.init is not None:
            self.exec(node.init)
        while node.cond is None or to_bool(self.eval(node.cond)):
            self._step()  # guard empty-body loops too
            try:
                self.exec_block(node.body)
            except BreakSignal:
                break
            except ContinueSignal:
                pass
            if node.post is not None:
                self.exec(node.post)

    def _st_With(self, node):
        obj = self.eval(node.obj)
        prev = self.ctx.this_obj
        self.ctx.this_obj = obj
        try:
            self.exec_block(node.body)
        finally:
            self.ctx.this_obj = prev

    def _st_FuncDef(self, node):
        self.ctx.functions[node.name] = node

    def _st_Flow(self, node):
        if node.kind == "break":
            raise BreakSignal()
        if node.kind == "continue":
            raise ContinueSignal()
        raise ReturnSignal()

    def _st_UserCall(self, node):
        self._call_user(node.name)

    def _st_Assign(self, node):
        value = self.eval(node.value)
        if node.op != "=":
            cur = self.get_ref(node.target)
            value = self._compound(node.op, cur, value)
        self.set_ref(node.target, value)

    def _st_Command(self, node):
        name = node.name
        if name in _VAR_COMMANDS:
            self._exec_var_command(node)
            return
        if name == "tokenize":
            s = to_str(self.eval(node.args[0])) if node.args else ""
            self.ctx.tokenize_tokens = s.split()
            return
        if name == "tokenize2":
            s = to_str(self.eval(node.args[0])) if node.args else ""
            sep = to_str(self.eval(node.args[1])) if len(node.args) > 1 else " "
            self.ctx.tokenize_tokens = s.split(sep) if sep else list(s)
            return
        args = [self.eval(a) for a in node.args]
        self.ctx.host.call_command(name, args, self.ctx)

    def _exec_var_command(self, node):
        # flags/strings/arrays live in the var store, NOT in host built-ins
        name, args = node.name, node.args
        if name == "set":
            ref = args[0]
            if self._store_get(ref) is UNSET_VAL:  # set only marks presence
                self._store_set(ref, 1.0)
        elif name == "unset":
            self.unset_ref(args[0])
        elif name == "setstring":
            val = to_str(self.eval(args[1])) if len(args) > 1 else ""
            if val == "":
                self.unset_ref(args[0])
            else:
                self._store_set(args[0], val)
        elif name == "addstring":
            self._array_append(args[0], to_str(self.eval(args[1])) if len(args) > 1 else "")
        elif name == "setarray":
            size = int(to_num(self.eval(args[1]))) if len(args) > 1 else 0
            self._store_set(args[0], [0.0] * max(0, size))

    # -- expressions -------------------------------------------------------
    def eval(self, node):
        m = getattr(self, "_ex_" + type(node).__name__, None)
        if m is None:
            raise RuntimeError(f"cannot evaluate node {type(node).__name__}")
        return m(node)

    def _ex_Num(self, node):
        return node.value

    def _ex_Bool(self, node):
        return 1.0 if node.value else 0.0

    def _ex_Str(self, node):
        return node.value

    def _ex_StrConcat(self, node):
        # compound strings are trimmed both sides by the engine
        # (GS1Visitor::visitCompoundString -> trimMutate), which strips the
        # command/argument separator space; internal spacing is preserved.
        return "".join(self._str_part(p) for p in node.parts).strip()

    def _str_part(self, p):
        if isinstance(p, ast.Str):
            return p.value
        if isinstance(p, ast.MessageCode):
            return self._eval_messagecode(p)
        return to_str(self.eval(p))

    def _ex_MessageCode(self, node):
        return self._eval_messagecode(node)

    def _eval_messagecode(self, node):
        code = node.code
        a = node.args
        # computed / string-manipulation codes (faithful to GS1MessageCodes.cpp)
        if code in ("#v", "#s", "#U"):
            return to_str(self.eval(a[0])) if a else ""
        if code == "#T":  # trim
            return to_str(self.eval(a[0])).strip() if a else ""
        if code == "#e":  # substr(start, len, str)
            if len(a) >= 3:
                start = max(0, int(math.floor(to_num(self.eval(a[0])))))
                length = max(0, int(math.floor(to_num(self.eval(a[1])))))
                return to_str(self.eval(a[2]))[start:start + length]
            return ""
        if code == "#I":  # csv item by index
            if len(a) >= 2:
                csv = to_str(self.eval(a[0]))
                idx = int(math.floor(to_num(self.eval(a[1]))))
                items = csv.split(",") if csv else []
                return items[idx] if 0 <= idx < len(items) else ""
            return ""
        if code == "#K":  # char from ascii code
            c = min(255, max(0, int(math.floor(to_num(self.eval(a[0])))))) if a else 0
            return chr(c)
        if code == "#t":  # tokenize token by index
            idx = int(math.floor(to_num(self.eval(a[0])))) if a else 0
            toks = self.ctx.tokenize_tokens
            return toks[idx] if 0 <= idx < len(toks) else ""
        if code == "#R":  # random pick among args
            return to_str(self.eval(_random.choice(a))) if a else ""
        # character / context codes (#a account, #n nick, #c chat, #1-8, #C, ...)
        args = [self.eval(x) for x in a]
        return to_str(self.ctx.host.message_code(code, args, self.ctx))

    def _ex_VarRef(self, node):
        v = self.get_ref(node)
        return 0.0 if v is UNSET_VAL else v

    def _ex_ArrayLit(self, node):
        return [self.eval(e) for e in node.elements]

    def _ex_SpecialLit(self, node):
        return node.value

    def _ex_RangeLit(self, node):
        return (self.eval(node.lo), self.eval(node.hi))

    def _ex_UnaryOp(self, node):
        v = self.eval(node.operand)
        if node.op == "-":
            return -to_num(v)
        if node.op == "+":
            return to_num(v)
        if node.op == "!":
            return 0.0 if to_bool(v) else 1.0
        return v

    def _ex_Postfix(self, node):
        cur = to_num(self.get_ref(node.operand))
        new = cur + 1 if node.op == "++" else cur - 1
        self.set_ref(node.operand, new)
        return cur

    def _ex_Ternary(self, node):
        return self.eval(node.a) if to_bool(self.eval(node.cond)) else self.eval(node.b)

    def _ex_InExpr(self, node):
        v = to_num(self.eval(node.value))
        rng = node.rng
        if isinstance(rng, ast.RangeLit):
            lo = to_num(self.eval(rng.lo))
            hi = to_num(self.eval(rng.hi))
            return 1.0 if lo <= v <= hi else 0.0
        container = self.eval(rng)
        if isinstance(container, (list, tuple)):
            return 1.0 if any(to_num(x) == v for x in container) else 0.0
        return 0.0

    def _ex_BinOp(self, node):
        op = node.op
        if op == "&&":
            return 1.0 if (to_bool(self.eval(node.left)) and to_bool(self.eval(node.right))) else 0.0
        if op == "||":
            return 1.0 if (to_bool(self.eval(node.left)) or to_bool(self.eval(node.right))) else 0.0
        a = self.eval(node.left)
        b = self.eval(node.right)
        # GS1 arithmetic/comparison is numeric (strings coerce to their numeric
        # value); string compares use strequals(). == / != also compare arrays.
        if op == "+":
            return to_num(a) + to_num(b)
        if op == "-":
            return to_num(a) - to_num(b)
        if op == "*":
            return to_num(a) * to_num(b)
        if op == "/":
            d = to_num(b)
            return to_num(a) / d if d != 0 else 0.0
        if op == "%":
            d = to_num(b)
            return math.fmod(to_num(a), d) if d != 0 else 0.0
        if op == "^":
            try:
                return float(to_num(a) ** to_num(b))
            except (ValueError, OverflowError):
                return 0.0
        if op in ("==", "="):
            return 1.0 if self._eq(a, b) else 0.0
        if op == "!=":
            return 0.0 if self._eq(a, b) else 1.0
        if op == "<":
            return 1.0 if to_num(a) < to_num(b) else 0.0
        if op == ">":
            return 1.0 if to_num(a) > to_num(b) else 0.0
        if op == "<=":
            return 1.0 if to_num(a) <= to_num(b) else 0.0
        if op == ">=":
            return 1.0 if to_num(a) >= to_num(b) else 0.0
        raise RuntimeError(f"unknown operator {op}")

    @staticmethod
    def _eq(a, b):
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return list(a) == list(b)
        return abs(to_num(a) - to_num(b)) < 1e-9

    def _compound(self, op, cur, value):
        base = op[0]
        if base == "+":
            return to_num(cur) + to_num(value)
        if base == "-":
            return to_num(cur) - to_num(value)
        if base == "*":
            return to_num(cur) * to_num(value)
        if base == "/":
            d = to_num(value)
            return to_num(cur) / d if d != 0 else 0.0
        if base == "%":
            d = to_num(value)
            return math.fmod(to_num(cur), d) if d != 0 else 0.0
        if base == "^":
            try:
                return float(to_num(cur) ** to_num(value))
            except (ValueError, OverflowError):
                return 0.0
        return value

    # -- function calls ----------------------------------------------------
    def _ex_Call(self, node):
        name = node.name
        if name in self.ctx.functions:
            return self._call_user(name)
        fn = _PURE.get(name)
        if fn is not None:
            return fn(self, [self.eval(a) for a in node.args])
        v = self.ctx.host.call_function(name, [self.eval(a) for a in node.args], self.ctx)
        return 0.0 if v is UNSET else v

    def _call_user(self, name):
        fn = self.ctx.functions.get(name)
        if fn is None:
            return 0.0
        if self._depth >= _MAX_CALL_DEPTH:
            raise RuntimeError("GS1 max call depth exceeded")
        self._depth += 1
        try:
            self.exec_block(fn.body)
        except ReturnSignal:
            pass
        finally:
            self._depth -= 1
        return 0.0

    # -- variable resolution ----------------------------------------------
    def _part_name(self, part):
        if part.atoms:
            return "".join(
                p.value if isinstance(p, ast.Str) else self._eval_messagecode(p)
                for p in part.atoms)
        return part.name

    def _resolve(self, ref):
        names = [self._part_name(p) for p in ref.parts]
        index = None
        for p in ref.parts:
            if p.index:
                index = int(to_num(self.eval(p.index[0])))
                break
        first = names[0]
        if first in NAMESPACES and len(names) > 1:
            return NAMESPACES[first], ".".join(names[1:]), index, names
        return None, ".".join(names), index, names

    def get_ref(self, ref):
        scope, key, index, names = self._resolve(ref)
        if scope is not None:
            v = self.ctx.vars.get(scope, key, index)
            return UNSET_VAL if v is UNSET else v
        # bare name that matches the firing event -> its flag reads true
        if key and key == self.ctx.active_event:
            return 1.0
        # bare: built-in attribute first, then player flag/var
        idxlist = [index] if index is not None else []
        v = self.ctx.host.get_builtin(key, idxlist, self.ctx)
        if v is not UNSET:
            return v
        v = self.ctx.vars.get(None, key, index)
        return UNSET_VAL if v is UNSET else v

    def set_ref(self, ref, value):
        scope, key, index, names = self._resolve(ref)
        if scope is not None:
            self.ctx.vars.set(scope, key, value, index)
            return
        idxlist = [index] if index is not None else []
        if self.ctx.host.set_builtin(key, value, idxlist, self.ctx):
            return
        self.ctx.vars.set(None, key, value, index)

    def unset_ref(self, ref):
        scope, key, index, names = self._resolve(ref)
        self.ctx.vars.unset(scope, key)

    def _store_get(self, ref):
        """Read a flag/var from the var store only (ignores host built-ins)."""
        scope, key, index, names = self._resolve(ref)
        v = self.ctx.vars.get(scope, key, index)
        return UNSET_VAL if v is UNSET else v

    def _store_set(self, ref, value):
        """Write a flag/var to the var store only (ignores host built-ins)."""
        scope, key, index, names = self._resolve(ref)
        self.ctx.vars.set(scope, key, value, index)

    def _array_append(self, ref, value):
        cur = self._store_get(ref)
        if not isinstance(cur, list):
            cur = [] if cur is UNSET_VAL else [cur]
        cur.append(value)
        self._store_set(ref, cur)


UNSET_VAL = UNSET  # alias for readability in this module


# -- pure built-in functions ------------------------------------------------
def _f_random(self, a):
    lo = to_num(a[0]) if a else 0.0
    hi = to_num(a[1]) if len(a) > 1 else lo
    if hi < lo:
        lo, hi = hi, lo
    return _random.uniform(lo, hi)


def _f1(fn):
    return lambda self, a: fn(to_num(a[0]) if a else 0.0)


def _safe(fn, default=0.0):
    def g(x):
        try:
            return fn(x)
        except (ValueError, OverflowError, ZeroDivisionError):
            return default
    return g


# vecx/vecy direction tables (dir 0=up,1=left,2=down,3=right)
_VECX = (0.0, -1.0, 0.0, 1.0)
_VECY = (-1.0, 0.0, 1.0, 0.0)


def _ascii(a):
    s = to_str(a[0]) if a else ""
    return float(ord(s[0]) & 0xFF) if s else 0.0


def _getangle(a):
    dx = to_num(a[0]) if a else 0.0
    dy = to_num(a[1]) if len(a) > 1 else 0.0
    if dx == 0 and dy == 0:
        return 0.0
    ang = math.atan2(-dy, dx)  # game flips Y
    if ang < 0:
        ang += 2 * math.pi
    return ang


def _getdir(a):
    # intended direction from a delta (the C++ clamp here is buggy; we do the
    # sensible thing — corpus scripts expect a real direction)
    dx = to_num(a[0]) if a else 0.0
    dy = to_num(a[1]) if len(a) > 1 else 0.0
    ix = max(-1, min(1, round(dx)))
    iy = max(-1, min(1, round(dy)))
    if ix == 0 and iy == -1:
        return 0.0
    if ix == -1 and iy == 0:
        return 1.0
    if ix == 1 and iy == 0:
        return 3.0
    return 2.0  # down (also the default)


def _aindexof(a):
    if len(a) < 2 or not isinstance(a[1], (list, tuple)):
        return -1.0
    val = to_num(a[0])
    for i, x in enumerate(a[1]):
        if to_num(x) == val:
            return float(i)
    return -1.0


def _lindexof(a):
    if len(a) < 2:
        return -1.0
    needle = to_str(a[0]).strip()
    for i, item in enumerate(to_str(a[1]).split(",")):
        if item.strip() == needle:
            return float(i)
    return -1.0


_PURE = {
    # math
    "random": _f_random,
    "abs": _f1(abs),
    "int": _f1(lambda x: float(int(x))),       # truncate toward zero
    "sin": _f1(math.sin),
    "cos": _f1(math.cos),
    "tan": _f1(math.tan),
    "arctan": _f1(math.atan),
    "exp": _f1(_safe(math.exp)),
    "log": _f1(_safe(math.log)),
    "sqrt": _f1(_safe(math.sqrt)),
    "max": lambda self, a: max((to_num(x) for x in a), default=0.0),
    "min": lambda self, a: min((to_num(x) for x in a), default=0.0),
    "getangle": lambda self, a: _getangle(a),
    "getdir": lambda self, a: _getdir(a),
    "vecx": lambda self, a: _VECX[int(math.floor(to_num(a[0]) if a else 0)) % 4],
    "vecy": lambda self, a: _VECY[int(math.floor(to_num(a[0]) if a else 0)) % 4],
    # strings
    "ascii": lambda self, a: _ascii(a),
    "keycode": lambda self, a: _ascii(a),
    "strlen": lambda self, a: float(len(to_str(a[0]))) if a else 0.0,
    "strtofloat": lambda self, a: to_num(a[0]) if a else 0.0,
    "strequals": lambda self, a: 1.0 if len(a) > 1 and to_str(a[0]) == to_str(a[1]) else 0.0,
    "strcontains": lambda self, a: 1.0 if len(a) > 1 and to_str(a[1]) in to_str(a[0]) else 0.0,
    "startswith": lambda self, a: 1.0 if len(a) > 1 and to_str(a[0]).startswith(to_str(a[1])) else 0.0,
    # indexof(substring, str) -> position of substring in str (note arg order)
    "indexof": lambda self, a: float(to_str(a[1]).find(to_str(a[0]))) if len(a) > 1 else -1.0,
    "sarraylen": lambda self, a: float(to_str(a[0]).count(",") + 1) if a else 0.0,
    "lindexof": lambda self, a: _lindexof(a),
    # arrays
    "arraylen": lambda self, a: float(len(a[0])) if a and isinstance(a[0], (list, tuple)) else 0.0,
    "aindexof": lambda self, a: _aindexof(a),
}


# -- convenience entry points ----------------------------------------------
def run(source: str, host: Host = None, ctx: Context = None) -> Context:
    """Parse and execute a GS1 script; return the resulting context."""
    ctx = ctx or Context(host or MemoryHost())
    Interpreter(ctx).run(parse(source))
    return ctx


def run_event(source: str, event: str, host: Host = None, ctx: Context = None) -> Context:
    ctx = ctx or Context(host or MemoryHost())
    Interpreter(ctx).run_event(parse(source), event)
    return ctx
