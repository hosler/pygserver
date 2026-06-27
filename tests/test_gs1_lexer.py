"""Tests for the GS1 lexer (pygserver.gs1.lexer).

Golden token-stream checks for the context-sensitive mode machinery, plus a
corpus parse-rate guard so later phases don't regress tokenization. The corpus
(tests/gs1_corpus/, ~29.8k NPC scripts from Graal Classic 2004) is gitignored;
the corpus test self-skips when it isn't present.
"""
import os

import pytest

from reborn_protocol.gs1.lexer import tokenize, LexError

CORPUS = os.path.join(os.path.dirname(__file__), "gs1_corpus")


def types(src):
    return [t.type for t in tokenize(src) if t.type != "EOF"]


def texts(src):
    """Non-empty token (type, text) pairs — empty synthetic tokens dropped."""
    return [(t.type, t.text) for t in tokenize(src) if t.type != "EOF" and t.text]


def test_basic_assignment():
    assert texts("x = 30;") == [
        ("IDENTIFIER", "x"), ("OP_ASSIGN", "="), ("LITERAL", "30"), ("END", ";")]


def test_command_string_arg_is_one_token():
    # setimg takes a string ('S'): the tail is a single STRING, not identifiers
    assert texts("setimg block.png;") == [
        ("COMMAND", "setimg"), ("STRING", " block.png"), ("END", ";")]


def test_say_takes_expression_not_string():
    # per the grammar, 'say' is 'E' (expression); 'say2' is the raw string form
    ts = types("say playerx;")
    assert ts[0] == "COMMAND"
    assert "IDENTIFIER" in ts  # playerx lexed as an identifier, not a string


def test_setcharprop_mode_switch():
    # 'MS': first arg is a message code, second a string
    assert texts("setcharprop #c,Hello World;") == [
        ("COMMAND", "setcharprop"), ("MESSAGECODE", "#c"), ("TOKEN_COMMA", ","),
        ("STRING", "Hello World"), ("END", ";")]


def test_nested_function_call_commas_dont_pop_outer():
    ts = types("this.x = random(1, 5);")
    assert ts.count("FUNCTION") == 1
    assert ("LITERAL", "1") in texts("this.x = random(1, 5);")


def test_message_code_with_computed_param():
    ts = texts("message Hello #v(playerx);")
    assert ("MESSAGECODE", "#v") in ts
    assert ("IDENTIFIER", "playerx") in ts


def test_control_flow_keywords():
    ts = types("if (a) { y=1; } else { y=2; }")
    assert ts[0] == "KW_IF"
    assert "KW_ELSE" in ts


def test_for_loop():
    ts = types("for (i=0; i<10; i++) { x=i; }")
    assert ts[:2] == ["KW_FOR", "TOKEN_PAREN_LEFT"]
    assert "OP_INC" in ts


def test_color_code_c8_accepted():
    # 2004 corpus uses #C8 even though the base grammar lists #C0-7
    assert ("MESSAGECODE", "#C8") in texts("setcharprop #C8,body3.png;")


def test_non_ascii_in_code_raises_clean_lexerror():
    with pytest.raises(LexError):
        tokenize("x = \xe1bc;")  # accented letter is invalid in code


@pytest.mark.skipif(not os.path.isdir(CORPUS), reason="gs1_corpus not present")
def test_corpus_parse_rate():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    from gs1_corpus_profile import iter_npc_scripts

    ok = total = 0
    for _fn, script in iter_npc_scripts(CORPUS):
        total += 1
        try:
            tokenize(script)
            ok += 1
        except Exception:
            pass
    assert total > 1000, "corpus looks empty"
    rate = ok / total
    # Phase 1 baseline is ~99.0%; guard against regressions.
    assert rate >= 0.985, f"tokenization parse-rate regressed: {rate:.3%}"
