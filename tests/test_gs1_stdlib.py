"""Tests for GS1 built-in functions and message codes.

Semantics verified against GServer-v2 GS1Functions.cpp / GS1MessageCodes.cpp.
Note: a function argument's type (P expression / S string / V variable) is fixed
by the lexer, so e.g. ascii(P) sees an expression while indexof(SS) sees strings.
"""
from reborn_protocol.gs1.interp import run, Interpreter
from reborn_protocol.gs1 import parse


def probe(ctx, expr):
    return Interpreter(ctx).eval(parse(expr + ";").body[0].expr)


# -- functions --------------------------------------------------------------
def test_math_functions():
    ctx = run("this.a=abs(-5); this.b=int(3.9); this.c=int(-3.9); this.d=max(2,9);")
    assert probe(ctx, "this.a") == 5.0
    assert probe(ctx, "this.b") == 3.0
    assert probe(ctx, "this.c") == -3.0   # truncates toward zero
    assert probe(ctx, "this.d") == 9.0


def test_vec_and_dir():
    ctx = run("this.vx=vecx(3); this.vy=vecy(0); this.d=getdir(1,0);")
    assert probe(ctx, "this.vx") == 1.0    # right -> x=1
    assert probe(ctx, "this.vy") == -1.0   # up -> y=-1
    assert probe(ctx, "this.d") == 3.0     # (1,0) -> right


def test_string_functions():
    ctx = run("this.i=indexof(ell,hello); this.eq=strequals(foo,foo); "
              "this.sw=startswith(he,hello); this.len=strlen(hello);")
    assert probe(ctx, "this.i") == 1.0     # indexof(substring, str)
    assert probe(ctx, "this.eq") == 1.0
    assert probe(ctx, "this.sw") == 1.0
    assert probe(ctx, "this.len") == 5.0


def test_ascii_of_string_var():
    ctx = run("setstring this.s,Apple; this.a=ascii(this.s);")
    assert probe(ctx, "this.a") == 65.0    # 'A'


def test_list_and_array_functions():
    ctx = run("setstring this.csv,a,b,c; this.sl=sarraylen(this.csv); "
              "this.li=lindexof(b,this.csv);")
    assert probe(ctx, "this.sl") == 3.0
    assert probe(ctx, "this.li") == 1.0
    ctx = run("temp.arr={5,7,9}; this.al=arraylen(temp.arr); this.ai=aindexof(7,temp.arr);")
    assert probe(ctx, "this.al") == 3.0
    assert probe(ctx, "this.ai") == 1.0


# -- message codes ----------------------------------------------------------
def test_messagecode_substr_and_csv():
    ctx = run("setstring this.x,#e(0,5,Hello World); setstring this.csv,a,b,c; "
              "setstring this.y,#I(this.csv,2);")
    assert probe(ctx, "this.x") == "Hello"
    assert probe(ctx, "this.y") == "c"


def test_messagecode_trim_char_value():
    ctx = run("setstring this.t,#T(  hi  ); setstring this.k,#K(65); setstring this.v,#v(3+4);")
    assert probe(ctx, "this.t") == "hi"
    assert probe(ctx, "this.k") == "A"
    assert probe(ctx, "this.v") == "7"


def test_tokenize_and_token_code():
    ctx = run("tokenize alpha beta gamma; setstring this.t,#t(2);")
    assert probe(ctx, "this.t") == "gamma"
    # tokenize2 takes DELIMITERS first, then text (fn_tokenize2; oracle:
    # `tokenize2 -,a-b-c` -> 3 tokens a/b/c).
    ctx = run("tokenize2 -,a-b-c; setstring this.t,#t(1);")
    assert probe(ctx, "this.t") == "b"


def test_messagecode_string_of_var():
    ctx = run("this.n = 42; setstring this.s,val is #s(this.n) end;")
    assert probe(ctx, "this.s") == "val is 42 end"


def test_indexof_and_lindexof_are_case_sensitive():
    # Unlike strequals/strcontains/startswith (GServer-v2's equalsi/findi,
    # case-insensitive), fn_indexof uses plain std::string::find and
    # fn_lindexof compares trimmed items with `==` — both case-sensitive.
    ctx = run("this.hit=indexof(ELL,hello); this.miss=indexof(ell,hello);")
    assert probe(ctx, "this.hit") == -1.0     # wrong case never matches
    assert probe(ctx, "this.miss") == 1.0
    ctx = run("setstring this.csv,A,b,c; this.hit=lindexof(A,this.csv); "
              "this.miss=lindexof(a,this.csv);")
    assert probe(ctx, "this.hit") == 0.0
    assert probe(ctx, "this.miss") == -1.0


def test_sin_clamped_to_zero_pi_range_cos_is_not():
    # GServer-v2's fn_sin (GS1Functions.cpp) only computes std::sin(value) for
    # value in [0, pi]; anything outside returns 0 (fn_cos has no such
    # restriction). Bomber's eye_bomber mallet-UI Draw() folds its phase
    # into [0,1] before multiplying by pi specifically to stay in this
    # window, so the corpus relies on the clamp being real.
    ctx = run("this.a=sin(-0.1); this.b=sin(3.5); this.c=sin(1.5707963267948966);"
              " this.d=cos(-0.1); this.e=cos(3.5);")
    assert probe(ctx, "this.a") == 0.0     # negative -> clamped
    assert probe(ctx, "this.b") == 0.0     # > pi -> clamped
    assert abs(probe(ctx, "this.c") - 1.0) < 1e-9   # pi/2 in range -> sin(pi/2)=1
    assert probe(ctx, "this.d") != 0.0      # cos has no range restriction
    assert probe(ctx, "this.e") != 0.0
