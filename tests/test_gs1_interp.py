"""Tests for the GS1 interpreter (pygserver.gs1.interp).

Executes scripts against MemoryHost and asserts on resulting variable state and
the recorded command log, plus a corpus execution-robustness guard.
"""
import os

import pytest

from reborn_protocol.gs1 import parse
from reborn_protocol.gs1.interp import Interpreter, run, run_event
from reborn_protocol.gs1.runtime import Context, MemoryHost

CORPUS = os.path.join(os.path.dirname(__file__), "gs1_corpus")


def probe(ctx, expr):
    """Read a value back by evaluating an expression in ctx."""
    return Interpreter(ctx).eval(parse(expr + ";").body[0].expr)


def test_arithmetic_and_for_loop():
    ctx = run("this.sum=0; for (i=1;i<=5;i++){ this.sum+=i; }")
    assert probe(ctx, "this.sum") == 15.0


def test_operator_precedence():
    ctx = run("this.x = 2 + 3 * 4;")
    assert probe(ctx, "this.x") == 14.0


def test_while_with_break():
    ctx = run("this.i=0; while (1) { this.i++; if (this.i >= 3) break; }")
    assert probe(ctx, "this.i") == 3.0


def test_flags_set_unset():
    ctx = run("set won;")
    assert probe(ctx, "won") == 1.0
    ctx = run("set won; unset won;")
    assert probe(ctx, "won") in (0.0, "")


def test_setstring_and_empty_unsets():
    ctx = run("setstring this.name,Hero;")
    assert probe(ctx, "this.name") == "Hero"
    ctx = run("setstring this.name,Hero; setstring this.name,;")
    assert probe(ctx, "this.name") in (0.0, "")


def test_namespaces_independent():
    ctx = run("this.x=1; temp.x=2; server.x=3;")
    assert probe(ctx, "this.x") == 1.0
    assert probe(ctx, "temp.x") == 2.0
    assert probe(ctx, "server.x") == 3.0


def test_alias_operators():
    ctx = run("this.a = (5 <> 4); this.b = (5 => 5); this.c = (3 =< 4);")
    assert probe(ctx, "this.a") == 1.0  # <> is !=
    assert probe(ctx, "this.b") == 1.0  # => is >=
    assert probe(ctx, "this.c") == 1.0  # =< is <=


def test_pure_functions():
    ctx = run("this.a=abs(-7); this.b=int(3.9); this.c=strlen(hello); "
              "this.d=strequals(foo,foo); this.e=strcontains(hello,ell);")
    assert probe(ctx, "this.a") == 7.0
    assert probe(ctx, "this.b") == 3.0
    assert probe(ctx, "this.c") == 5.0
    assert probe(ctx, "this.d") == 1.0
    assert probe(ctx, "this.e") == 1.0


def test_ternary_and_in_range():
    ctx = run("this.x = (3 in |1,5|) ? 100 : 0;")
    assert probe(ctx, "this.x") == 100.0
    ctx = run("this.x = (9 in |1,5|) ? 100 : 0;")
    assert probe(ctx, "this.x") == 0.0


def test_plus_is_numeric_not_concat():
    # GS1 '+' is numeric (oracle: GS1Visitor::visitExpressionAdditive);
    # numeric strings coerce, non-numeric coerce to 0
    ctx = run("this.n = 2 + 3; this.m = strtofloat(10) + strtofloat(5);")
    assert probe(ctx, "this.n") == 5.0
    assert probe(ctx, "this.m") == 15.0


def test_equality_is_numeric():
    # '==' compares numerically (string compares use strequals)
    ctx = run("this.a = (5 == 5); this.b = (5 == 6);")
    assert probe(ctx, "this.a") == 1.0
    assert probe(ctx, "this.b") == 0.0


def test_command_routed_to_host_with_messagecode():
    h = MemoryHost(attrs={"playerx": 42})
    ctx = Context(h)
    Interpreter(ctx).run(parse("message Pos #v(playerx) ok;"))
    assert h.log == [("message", ["Pos 42 ok"])]  # compound strings are trimmed


def test_builtin_attribute_get_set():
    h = MemoryHost(attrs={"playerrupees": 0})
    ctx = Context(h)
    Interpreter(ctx).run(parse("playerrupees = playerrupees + 50;"))
    assert h.attrs["playerrupees"] == 50.0


def test_dynamic_variable_name():
    ctx = run("this.a=3; this.#v(this.a)=99; this.out=this.#v(this.a);")
    assert probe(ctx, "this.out") == 99.0


def test_user_function_call():
    h = MemoryHost()
    ctx = Context(h)
    Interpreter(ctx).run(parse("function Greet(){ say2 hello; } Greet();"))
    assert ("say2", ["hello"]) in h.log


def test_event_dispatch_only_matching_handler():
    h = MemoryHost()
    src = "if (created){ set wasinit; } if (playerenters){ say2 hi; }"
    ctx = run_event(src, "created", host=h)
    assert probe(ctx, "wasinit") == 1.0
    assert h.log == []  # playerenters handler not fired


def test_array_literal_and_index():
    ctx = run("temp.a = {10, 20, 30}; this.v = temp.a[1];")
    assert probe(ctx, "this.v") == 20.0


def test_infinite_loop_is_guarded():
    ctx = Context(MemoryHost())
    ctx.max_steps = 5000
    with pytest.raises(RuntimeError):
        Interpreter(ctx).run(parse("while (1) { this.x++; }"))


@pytest.mark.skipif(not os.path.isdir(CORPUS), reason="gs1_corpus not present")
def test_corpus_execution_robustness():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    from gs1_corpus_profile import iter_npc_scripts
    from reborn_protocol.gs1.parser import Parser
    from reborn_protocol.gs1.lexer import tokenize

    events = ["created", "playerenters", "playertouchsme", "playerchats"]
    ran = crash = 0
    for _fn, script in iter_npc_scripts(CORPUS):
        try:
            prog = Parser(tokenize(script)).parse_program()
        except Exception:
            continue
        try:
            for ev in events:
                c = Context(MemoryHost(attrs={"playerx": 30, "playery": 30}))
                Interpreter(c).run_event(prog, ev)
            ran += 1
        except RuntimeError:
            crash += 1  # step-budget guard on uncapped loops (mock-host artifact)
        except Exception:
            crash += 1
    assert ran > 1000
    # well over 99.9% of scripts execute without an unexpected exception
    assert crash / (ran + crash) < 0.002, f"too many exec crashes: {crash}/{ran+crash}"
