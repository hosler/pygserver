"""Tests for the GS1 parser (pygserver.gs1.parser).

Structural AST checks plus a corpus guard: every lexable script must produce an
AST (panic-mode recovery guarantees this), and the clean-parse rate (scripts
with zero recovered errors) must stay at the Phase-2 baseline (~98% of lexable).
"""
import os

import pytest

from reborn_protocol.gs1 import ast
from reborn_protocol.gs1.parser import parse, Parser
from reborn_protocol.gs1.lexer import tokenize

CORPUS = os.path.join(os.path.dirname(__file__), "gs1_corpus")


def test_assignment():
    prog = parse("x = 30;")
    assert isinstance(prog.body[0], ast.Assign)
    assert prog.body[0].op == "="
    assert isinstance(prog.body[0].value, ast.Num)


def test_quoted_string_assignment_produces_strconcat():
    # `this.chat = "Welcome!";` (chicken_house1.nw) — quoted literal RHS.
    prog = parse('this.chat = "Welcome!";')
    node = prog.body[0]
    assert isinstance(node, ast.Assign)
    assert isinstance(node.value, ast.StrConcat)
    assert node.value.parts == [ast.Str("Welcome!")]


def test_quoted_string_in_if_condition():
    prog = parse('if (player.chat == "/start game") { setlevel2("x.nw",1,1); }')
    cond = prog.body[0].cond
    assert isinstance(cond, ast.BinOp)
    assert cond.right == ast.StrConcat(parts=[ast.Str("/start game")])


def test_if_else():
    prog = parse("if (a) { x=1; } else { x=2; }")
    node = prog.body[0]
    assert isinstance(node, ast.If)
    assert node.els is not None


def test_else_if_chain():
    prog = parse("if (a) x=1; else if (b) x=2; else x=3;")
    node = prog.body[0]
    assert isinstance(node.els[0], ast.If)  # else-branch is a single nested if


def test_for_loop():
    prog = parse("for (i=0; i<10; i++) { x=i; }")
    node = prog.body[0]
    assert isinstance(node, ast.For)
    assert isinstance(node.init, ast.Assign)


def test_command_with_args():
    prog = parse("setimgpart block.png,0,0,32,32;")
    cmd = prog.body[0]
    assert isinstance(cmd, ast.Command) and cmd.name == "setimgpart"
    assert len(cmd.args) == 5


def test_string_arg_concatenates_messagecode():
    prog = parse("message Hello #v(playerx) there;")
    arg = prog.body[0].args[0]
    assert isinstance(arg, ast.StrConcat)
    assert any(isinstance(p, ast.MessageCode) for p in arg.parts)


def test_setcharprop_messagecode_first_arg():
    prog = parse("setcharprop #c,Hello;")
    cmd = prog.body[0]
    assert cmd.name == "setcharprop"
    assert len(cmd.args) == 2


def test_builtin_function_call():
    prog = parse("x = random(1, 5);")
    val = prog.body[0].value
    assert isinstance(val, ast.Call) and val.name == "random"
    assert len(val.args) == 2


def test_dotted_property_access():
    prog = parse("this.money = this.money + 5;")
    tgt = prog.body[0].target
    assert isinstance(tgt, ast.VarRef)
    assert [p.name for p in tgt.parts] == ["this", "money"]


def test_property_named_like_command():
    # 'message' is a command keyword but here is a property after '.'
    prog = parse("this.message = 1;")
    assert [p.name for p in prog.body[0].target.parts] == ["this", "message"]


def test_dynamic_variable_name_with_messagecode():
    # this.#v(this.a) -> dynamic segment; must parse without error
    p = Parser(tokenize("unset this.#v(this.a);"))
    prog = p.parse_program()
    assert not p.errors
    part = prog.body[0].args[0].parts[1]
    assert part.atoms and any(isinstance(a, ast.MessageCode) for a in part.atoms)


def test_in_range_operator():
    prog = parse("if (this.x in |1,5|) hide;")
    cond = prog.body[0].cond
    assert isinstance(cond, ast.InExpr)
    assert isinstance(cond.rng, ast.RangeLit)


def test_ternary():
    prog = parse("x = (a > 5) ? 10 : 20;")
    assert isinstance(prog.body[0].value, ast.Ternary)


def test_array_literal():
    prog = parse("temp.arr = {1, 2, 3};")
    assert isinstance(prog.body[0].value, ast.ArrayLit)
    assert len(prog.body[0].value.elements) == 3


def test_function_def_and_call():
    prog = parse("function Foo() { hide; } Foo();")
    assert isinstance(prog.body[0], ast.FuncDef)
    assert isinstance(prog.body[1], ast.UserCall)


def test_recovery_on_malformed_statement():
    # a malformed middle statement must not lose the surrounding good ones
    p = Parser(tokenize("x = 1; this.shh<=) message bad; y = 2;"))
    prog = p.parse_program()
    assert p.errors  # recorded the error
    kinds = [type(s).__name__ for s in prog.body]
    assert "Assign" in kinds  # x=1 and y=2 survived


@pytest.mark.skipif(not os.path.isdir(CORPUS), reason="gs1_corpus not present")
def test_corpus_parse_rate():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    from gs1_corpus_profile import iter_npc_scripts

    lexable = clean = 0
    for _fn, script in iter_npc_scripts(CORPUS):
        try:
            toks = tokenize(script)
        except Exception:
            continue
        lexable += 1
        p = Parser(toks)
        prog = p.parse_program()          # recovery => never raises
        assert isinstance(prog, ast.Program)
        if not p.errors:
            clean += 1
    assert lexable > 1000
    rate = clean / lexable
    # Phase 2 baseline ~98.9% clean of lexable; guard against regressions.
    assert rate >= 0.98, f"clean parse-rate regressed: {rate:.3%}"
