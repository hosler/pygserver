"""GS1 lexer — a faithful Python port of GServer-v2's ANTLR GS1Lexer.g4.

GS1 cannot be tokenized context-free: each command/function pushes an
argument-type string (see _tables.py) and the lexer switches *mode* per
argument, so `say hello world` lexes the tail as one STRING rather than three
identifiers. This module reproduces the C++ engine's command-state stack and
its popNextMode / setMode / emitIdentifier machinery.

Output: a flat list of Token(type, text). Token type names match the ANTLR
vocabulary so the parser (ported from GS1Parser.g4) can consume them directly.

See memory: gs1-python-port. Regenerate tables with tools/gen_gs1_tables.py.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from ._tables import COMMANDS, FUNCTIONS, MESSAGECODES

# ---------------------------------------------------------------------------
# Token types (names mirror the ANTLR token vocabulary)
# ---------------------------------------------------------------------------
COMMAND = "COMMAND"
FUNCTION = "FUNCTION"
MESSAGECODE = "MESSAGECODE"
RAWMESSAGECODE = "RAWMESSAGECODE"
STRING = "STRING"
IDENTIFIER = "IDENTIFIER"
LITERAL = "LITERAL"
DIRECTION = "DIRECTION"
ITEM = "ITEM"
COLOR = "COLOR"
GENDER = "GENDER"
BADDY = "BADDY"
CARRY = "CARRY"
ALLSTATS = "ALLSTATS"
ALLFEATURES = "ALLFEATURES"
END = "END"
EOF = "EOF"

# keywords
KW = {
    "with": "KW_WITH", "function": "KW_FUNCTION", "if": "KW_IF",
    "else": "KW_ELSE", "for": "KW_FOR", "while": "KW_WHILE",
    "return": "KW_RETURN", "break": "KW_BREAK", "continue": "KW_CONTINUE",
}

# operators / punctuation, longest first so maximal-munch matching is correct
OPERATORS = [
    (":=", "OP_ASSIGN"), ("+=", "OP_ASSIGN_ADD"), ("-=", "OP_ASSIGN_SUB"),
    ("*=", "OP_ASSIGN_MUL"), ("/=", "OP_ASSIGN_DIV"), ("%=", "OP_ASSIGN_MOD"),
    ("^=", "OP_ASSIGN_POW"), ("==", "OP_EQUAL"), ("!=", "OP_NOTEQ"),
    ("<>", "OP_NOTEQ"), ("<=", "OP_LESS_EQ"), ("=<", "OP_LESS_EQ"),
    (">=", "OP_GREAT_EQ"), ("=>", "OP_GREAT_EQ"), ("++", "OP_INC"),
    ("--", "OP_DEC"), ("&&", "OP_LOGICALAND"), ("||", "OP_LOGICALOR"),
    ("=", "OP_ASSIGN"), ("+", "OP_ADD"), ("-", "OP_SUB"), ("*", "OP_MUL"),
    ("/", "OP_DIV"), ("%", "OP_MOD"), ("^", "OP_POW"), ("<", "OP_LESS"),
    (">", "OP_GREAT"), ("!", "OP_LOGICALNOT"),
]
ASSIGN_OPS = {"OP_ASSIGN", "OP_ASSIGN_ADD", "OP_ASSIGN_SUB", "OP_ASSIGN_MUL",
              "OP_ASSIGN_DIV", "OP_ASSIGN_MOD", "OP_ASSIGN_POW"}
# alias spellings -> canonical symbol (so the AST/interpreter see one form)
_OP_CANON = {":=": "=", "<>": "!=", "=<": "<=", "=>": ">="}

PUNCT = {
    "[": "TOKEN_BRACKET_LEFT", "]": "TOKEN_BRACKET_RIGHT",
    "{": "TOKEN_BRACE_LEFT", "}": "TOKEN_BRACE_RIGHT",
    "(": "TOKEN_PAREN_LEFT", ")": "TOKEN_PAREN_RIGHT",
    ",": "TOKEN_COMMA", "|": "TOKEN_PIPE", "?": "TOKEN_QUESTION",
    ":": "TOKEN_COLON", ".": "TOKEN_PERIOD",
}

# special-literal name sets (fragments DIR/GENDERS/CARRYNAMES/ITEMNAMES/BADDY/COLORS)
DIR_NAMES = {"up", "left", "down", "right"}
GENDER_NAMES = {"male", "female"}
CARRY_NAMES = {"bush", "sign", "vase", "stone", "blackstone", "bomb",
               "hotbomb", "superbomb", "joltbomb", "hotjoltbomb", "none"}
ITEM_NAMES = {"greenrupee", "bluerupee", "redrupee", "bombs", "darts", "heart",
              "glove1", "bow", "bomb", "shield", "sword", "fullheart",
              "superbomb", "battleaxe", "goldensword", "mirrorshield",
              "glove2", "lizardshield", "lizardsword", "goldrupee", "fireball",
              "fireblast", "nukeshot", "joltbomb", "spinattack"}

_REAL = re.compile(r"0x[0-9a-fA-F]+|[0-9]+(?:\.[0-9]+)?|\.[0-9]+")
_IDENT = re.compile(r"[a-zA-Z0-9_]+")
# message code: '#' then a code char-class (longest forms first)
_MC = re.compile(r"#(?:C[0-9]|P1[0-9]?|P2[0-9]?|P30?|P[456789]|[angcmWw1235678NDLFfpbESptKkGsvIeiTURQ])")

# arg-char -> lexer mode (mirrors popNextMode's switch)
MODE_OF = {
    "V": "V", "E": "E", "P": "E", "S": "S", "R": "R", "L": "L", "M": "M",
    "B": "B", "I": "I", "C": "C", "G": "G", "U": "U", "D": "D", "X": "X",
    "Z": "Z", "(": "P1", ")": "P2", "<": "P3",
}
DEFAULT = "DEFAULT"

# pop modes
POP_COMMAND, POP_FUNCTION, POP_ARRAYINDEX = 0, 1, 2

# longest command literals first (so 'setimgpart' beats 'setimg' on maximal munch)
_CMD_BY_LEN = sorted(COMMANDS.items(), key=lambda kv: -len(kv[0]))


@dataclass
class Token:
    type: str
    text: str = ""
    pos: int = -1

    def __repr__(self):
        return f"{self.type}({self.text!r})" if self.text else self.type


@dataclass
class _State:
    arguments: str
    pop_mode: int
    comma_pop: bool = True


class LexError(Exception):
    def __init__(self, msg, pos, line):
        super().__init__(f"{msg} at offset {pos} (line {line})")
        self.pos = pos
        self.line = line


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.n = len(text)
        self.pos = 0
        self.mode = DEFAULT
        self.mode_stack: list[str] = []
        self.states: deque[_State] = deque()
        self.brace_count = 0
        self.before: deque[Token] = deque()
        self.after: deque[Token] = deque()
        self.out: list[Token] = []

    # -- command-state helpers (ports of the C++ @members) ------------------
    def _can_func_pop(self):
        return bool(self.states) and self.states[-1].pop_mode == POP_FUNCTION

    def _can_cmd_pop(self):
        return not self.states or self.states[-1].pop_mode == POP_COMMAND

    def _can_array_pop(self):
        return bool(self.states) and self.states[-1].pop_mode == POP_ARRAYINDEX

    def _can_comma_pop(self):
        return not self.states or self.states[-1].comma_pop

    def _is_next_arg_left_paren(self):
        st = self.states[-1] if self.states else None
        return bool(st and st.arguments and st.arguments[0] == "(")

    def push_command(self, arguments: str):
        self.states.append(_State(arguments, POP_COMMAND, True))
        self.mode_stack.append(self.mode)  # pushMode(dummy) saves current mode
        self.pop_next_mode()

    def push_array_access(self):
        self.states.append(_State("P", POP_ARRAYINDEX, False))
        self.mode_stack.append(self.mode)
        self.pop_next_mode()

    def pop_next_mode(self, terminate_early=False):
        if not self.states:
            return
        st = self.states[-1]
        if terminate_early:
            st.arguments = ""
        if st.arguments == "":
            self.mode = self.mode_stack.pop()
            self.states.pop()
            return
        c = st.arguments[0]
        st.arguments = st.arguments[1:]
        # last string includes commas
        if c in ("S", "R") and (st.arguments == "" or st.arguments[0] == ")"):
            st.comma_pop = False
        self.mode = MODE_OF.get(c, DEFAULT)
        if c == "V":
            self.emit_after(IDENTIFIER)
        elif c in ("S", "R", "L"):
            self.emit_after(STRING)
        elif c == "M":
            self.emit_after(RAWMESSAGECODE)
        elif c == "P":
            st.comma_pop = False

    def check_if_next_mode_optional(self):
        """playersays special case: skip the trailing optional arg if no comma."""
        self.pop_next_mode()
        skip = False
        i = self.pos
        while i < self.n:
            ch = self.text[i]
            if ch == ")":
                skip = True
                break
            if ch == ",":
                break
            i += 1
        if skip:
            self.pop_next_mode()

    # -- token emission ----------------------------------------------------
    def emit_before(self, ttype):
        self.before.append(Token(ttype, "", self.pos))

    def emit_after(self, ttype):
        self.after.append(Token(ttype, "", self.pos))

    @property
    def _line(self):
        return self.text.count("\n", 0, self.pos) + 1

    # -- main loop ---------------------------------------------------------
    def tokenize(self) -> list[Token]:
        while True:
            tok = self.scan_one()
            while self.before:
                self.out.append(self.before.popleft())
            if tok is not None:
                self.out.append(tok)
            while self.after:
                self.out.append(self.after.popleft())
            if tok is not None and tok.type == EOF:
                break
        return self.out

    def scan_one(self):
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        return getattr(self, "_mode_" + self.mode)()

    # -- shared scanning helpers ------------------------------------------
    def _skip_ws(self) -> bool:
        """Skip whitespace/comments. Returns True if anything was skipped."""
        start = self.pos
        t, n = self.text, self.n
        while self.pos < n:
            c = t[self.pos]
            if c in " \t\r\n":
                self.pos += 1
            elif c == "/" and self.pos + 1 < n and t[self.pos + 1] == "/":
                nl = t.find("\n", self.pos)
                self.pos = n if nl < 0 else nl
            elif c == "/" and self.pos + 1 < n and t[self.pos + 1] == "*":
                end = t.find("*/", self.pos + 2)
                self.pos = n if end < 0 else end + 2
            else:
                break
        return self.pos != start

    def _match_messagecode(self):
        """At a '#': emit MESSAGECODE and set up its param mode. Returns Token."""
        m = _MC.match(self.text, self.pos)
        if not m:
            return None
        code = m.group(0)
        self.pos = m.end()
        nxt = self.text[self.pos] if self.pos < self.n else ""
        args = MESSAGECODES.get(code)  # computed codes: #s/#v/#e/#I/#T/#U/#i/#R/#Q
        if args is None:
            # simple code: takes an optional (param) only if '(' follows
            args = "(P)" if nxt == "(" else ""
        if args:
            self.push_command(args)
        return Token(MESSAGECODE, code, m.start())

    def _scan_word(self):
        m = _IDENT.match(self.text, self.pos)
        return m.group(0) if m else None

    def _try_real(self):
        m = _REAL.match(self.text, self.pos)
        if not m:
            return None
        self.pos = m.end()
        return Token(LITERAL, m.group(0), m.start())

    def _try_operator(self):
        t = self.text
        for lit, ttype in OPERATORS:
            if t.startswith(lit, self.pos):
                start = self.pos
                self.pos += len(lit)
                # normalise alias spellings so the AST sees canonical symbols
                return Token(ttype, _OP_CANON.get(lit, lit), start)
        return None

    # =====================================================================
    # DEFAULT mode (top level: commands, keywords, control flow)
    # =====================================================================
    def _mode_DEFAULT(self):
        # ' in ' operator must be matched before whitespace is skipped
        if self.text.startswith(" in ", self.pos):
            self.pos += 4
            return Token("OP_IN", " in ", self.pos - 4)
        if self._skip_ws():
            return None
        c = self.text[self.pos]
        if c == "#":
            mc = self._match_messagecode()
            if mc:
                return mc
        if c == ";":
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == "}":
            self.pos += 1
            return Token("TOKEN_BRACE_RIGHT", "}", self.pos - 1)
        if c == "{":
            self.pos += 1
            return Token("TOKEN_BRACE_LEFT", "{", self.pos - 1)
        if c in "([" and False:  # arrays/parens handled below as punct
            pass
        if c.isalpha() or c == "_":
            return self._default_word()
        if c.isdigit() or (c == "." and self.pos + 1 < self.n and self.text[self.pos + 1].isdigit()):
            r = self._try_real()
            if r:
                return r
        op = self._try_operator()
        if op:
            return op
        if c in PUNCT:
            self.pos += 1
            if c == "[":
                self.push_array_access()
            return Token(PUNCT[c], c, self.pos - 1)
        raise LexError(f"unexpected char {c!r}", self.pos, self._line)

    def _default_word(self):
        word = self._scan_word()
        if word is None:
            raise LexError(f"unexpected char {self.text[self.pos]!r}",
                           self.pos, self._line)
        start = self.pos
        # after '.', a word is a property name, never a command/keyword/function
        # (e.g. this.message, this.set) — commands are only matched at stmt start
        if self.out and self.out[-1].type == "TOKEN_PERIOD":
            self.pos += len(word)
            if word in ("true", "false"):
                return Token(LITERAL, word, start)
            return Token(IDENTIFIER, word, start)
        # command? (longest literal wins; needs_space commands require a space)
        cs = COMMANDS.get(word)
        if cs is not None:
            if cs["needs_space"]:
                after = start + len(word)
                if after < self.n and self.text[after] == " ":
                    self.pos = after + 1
                    self.push_command(cs["args"])
                    return Token(COMMAND, word, start)
                # no space -> not this command, fall through
            else:
                self.pos += len(word)
                self.push_command(cs["args"])
                return Token(COMMAND, word, start)
        if word in FUNCTIONS:
            self.pos += len(word)
            self.push_command(FUNCTIONS[word])
            return Token(FUNCTION, word, start)
        if word in KW:
            self.pos += len(word)
            if word == "function":
                self.push_command("V()")
            return Token(KW[word], word, start)
        if word in ("true", "false"):
            self.pos += len(word)
            return Token(LITERAL, word, start)
        self.pos += len(word)
        return Token(IDENTIFIER, word, start)

    # =====================================================================
    # Expression-like modes (E, V, D, M share most expression scanning)
    # =====================================================================
    def _expr_in_op(self):
        """Match ' in ' operator; returns Token or None (caller handles WS)."""
        if self.text.startswith(" in ", self.pos):
            self.pos += 4
            return Token("OP_IN", " in ", self.pos - 4)
        return None

    def _expr_word(self):
        word = self._scan_word()
        if word is None:  # non-ASCII letter etc. (invalid in code, as in C++)
            raise LexError(f"unexpected char {self.text[self.pos]!r} in expr",
                           self.pos, self._line)
        start = self.pos
        if word in FUNCTIONS:
            self.pos += len(word)
            self.push_command(FUNCTIONS[word])
            return Token(FUNCTION, word, start)
        if word in ("true", "false"):
            self.pos += len(word)
            return Token(LITERAL, word, start)
        self.pos += len(word)
        return Token(IDENTIFIER, word, start)

    def _mode_E(self):
        return self._expr_mode(track_braces=True, allow_assign=True)

    def _mode_D(self):
        # direction mode: DIR names first, otherwise expression-like
        if not self._skip_ws():
            pass
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        word = self._scan_word()
        if word in DIR_NAMES:
            start = self.pos
            self.pos += len(word)
            return Token(DIRECTION, word, start)
        return self._expr_mode(track_braces=False, allow_assign=True, _pre_skipped=True)

    def _mode_M(self):
        # message-code argument list (setcharprop's first arg): expr w/o assign,
        # commas advance args, parens are plain.
        return self._expr_mode(track_braces=False, allow_assign=False, plain_parens=True)

    def _expr_mode(self, track_braces, allow_assign, plain_parens=False, _pre_skipped=False):
        if not _pre_skipped:
            # ' in ' must beat WS; check before skipping a leading space
            io = self._expr_in_op()
            if io:
                return io
            if self._skip_ws():
                return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]

        if c == "}" and self._can_cmd_pop():
            self.emit_before(END)
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_BRACE_RIGHT", "}", self.pos - 1)
        if c == ";" and self._can_cmd_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == ")":
            if not plain_parens and self._can_func_pop() and self.brace_count == 0:
                self.pop_next_mode(True)
                self.pos += 1
                return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
            self.pos += 1
            if track_braces:
                self.brace_count -= 1
            return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
        if c == ",":
            if self._can_comma_pop():
                self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_COMMA", ",", self.pos - 1)
        if c == "(":
            self.pos += 1
            if track_braces:
                self.brace_count += 1
            return Token("TOKEN_PAREN_LEFT", "(", self.pos - 1)
        if c == "]":
            if self._can_array_pop():
                self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_BRACKET_RIGHT", "]", self.pos - 1)
        if c == "[":
            self.pos += 1
            self.push_array_access()
            return Token("TOKEN_BRACKET_LEFT", "[", self.pos - 1)
        if c == "#":
            mc = self._match_messagecode()
            if mc:
                return mc
        if c.isalpha() or c == "_":
            return self._expr_word()
        if c.isdigit() or (c == "." and self.pos + 1 < self.n and self.text[self.pos + 1].isdigit()):
            r = self._try_real()
            if r:
                return r
        op = self._try_operator()
        if op:
            if op.type in ASSIGN_OPS and not allow_assign and op.type != "OP_ASSIGN":
                pass
            return op
        if c in PUNCT:
            self.pos += 1
            return Token(PUNCT[c], c, self.pos - 1)
        raise LexError(f"unexpected char {c!r} in expr", self.pos, self._line)

    def _mode_V(self):
        # variable target: identifier / array access / messagecode, comma & end pop
        if self._skip_ws():
            return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]
        if c == "}" and self._can_cmd_pop():
            self.emit_before(END)
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_BRACE_RIGHT", "}", self.pos - 1)
        if c == "(" and self._is_next_arg_left_paren():
            self.pop_next_mode()
            self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_PAREN_LEFT", "(", self.pos - 1)
        if c == ")" and self._can_func_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
        if c == ";" and self._can_cmd_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == ",":
            self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_COMMA", ",", self.pos - 1)
        if c == "#":
            mc = self._match_messagecode()
            if mc:
                return mc
        if c == "[":
            self.pos += 1
            self.push_array_access()
            return Token("TOKEN_BRACKET_LEFT", "[", self.pos - 1)
        if c in "|?:.":
            self.pos += 1
            return Token(PUNCT[c], c, self.pos - 1)
        if c.isdigit() or (c == "." and self.pos + 1 < self.n and self.text[self.pos + 1].isdigit()):
            r = self._try_real()
            if r:
                return r
        if c.isalpha() or c == "_":
            word = self._scan_word()
            if word is None:
                raise LexError(f"unexpected char {c!r} in var", self.pos, self._line)
            start = self.pos
            self.pos += len(word)
            if word in ("true", "false"):
                return Token(LITERAL, word, start)
            return Token(IDENTIFIER, word, start)
        raise LexError(f"unexpected char {c!r} in var", self.pos, self._line)

    # =====================================================================
    # String modes (S processes message codes, R is raw, L is comma-list)
    # =====================================================================
    def _mode_S(self):
        return self._string_mode(raw=False)

    def _mode_R(self):
        return self._string_mode(raw=True)

    def _string_mode(self, raw):
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]
        if c == "}" and self._can_cmd_pop():
            self.emit_before(END)
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_BRACE_RIGHT", "}", self.pos - 1)
        if c == ")" and not raw and self._can_func_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
        if c == ";" and self._can_cmd_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == "," and self._can_comma_pop():
            self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_COMMA", ",", self.pos - 1)
        if not raw:
            if self.text.startswith("##", self.pos):
                self.pos += 2
                return Token(STRING, "##", self.pos - 2)
            if c == "#":
                mc = self._match_messagecode()
                if mc:
                    return mc
        # consume a run of plain string text up to a delimiter
        return self._consume_string_run(raw)

    def _consume_string_run(self, raw):
        start = self.pos
        t, n = self.text, self.n
        comma_pop = self._can_comma_pop()
        cmd_pop = self._can_cmd_pop()
        func_pop = self._can_func_pop()
        while self.pos < n:
            ch = t[self.pos]
            if not raw and ch == "#":
                break
            if ch == "}" and cmd_pop:
                break
            if ch == ";" and cmd_pop:
                break
            if ch == ")" and not raw and func_pop and not comma_pop:
                break
            if ch == ")" and not raw and func_pop:
                break
            if ch == "," and comma_pop:
                break
            self.pos += 1
        if self.pos == start:
            # nothing consumed -> avoid infinite loop; emit the char as string
            self.pos += 1
        return Token(STRING, t[start:self.pos], start)

    def _mode_L(self):
        # comma-separated string list: like S but commas stay (emit empty STRING
        # around them) and never pop on comma.
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]
        if c == "}" and self._can_cmd_pop():
            self.emit_before(END)
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_BRACE_RIGHT", "}", self.pos - 1)
        if c == ")" and self._can_func_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
        if c == ";" and self._can_cmd_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == ",":
            self.emit_after(STRING)
            self.pos += 1
            return Token("TOKEN_COMMA", ",", self.pos - 1)
        if self.text.startswith("##", self.pos):
            self.pos += 2
            return Token(STRING, "##", self.pos - 2)
        if c == "#":
            mc = self._match_messagecode()
            if mc:
                return mc
        start = self.pos
        t, n = self.text, self.n
        cmd_pop = self._can_cmd_pop()
        func_pop = self._can_func_pop()
        while self.pos < n:
            ch = t[self.pos]
            if ch == "#":
                break
            if ch == ",":
                break
            if ch == "}" and cmd_pop:
                break
            if ch == ";" and cmd_pop:
                break
            if ch == ")" and func_pop:
                break
            self.pos += 1
        if self.pos == start:
            self.pos += 1
        return Token(STRING, t[start:self.pos], start)

    # =====================================================================
    # Special-literal modes (B/I/C/G/U) and code-body (Z), function-paren (1/2/3)
    # =====================================================================
    def _special_mode(self, ttype, names):
        if self._skip_ws():
            return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]
        if c == "}" and self._can_cmd_pop():
            self.emit_before(END)
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_BRACE_RIGHT", "}", self.pos - 1)
        if c == ")" and self._can_func_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
        if c == ";" and self._can_cmd_pop():
            self.pop_next_mode(True)
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == ",":
            self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_COMMA", ",", self.pos - 1)
        word = self._scan_word()
        if word:
            start = self.pos
            self.pos += len(word)
            return Token(ttype, word, start)
        raise LexError(f"unexpected char {c!r} in {ttype}", self.pos, self._line)

    def _mode_B(self):
        return self._special_mode(BADDY, None)

    def _mode_I(self):
        return self._special_mode(ITEM, ITEM_NAMES)

    def _mode_C(self):
        return self._special_mode(COLOR, None)

    def _mode_G(self):
        return self._special_mode(GENDER, GENDER_NAMES)

    def _mode_U(self):
        return self._special_mode(CARRY, CARRY_NAMES)

    def _mode_X(self):
        # storage special case: identifier then pop; or a message code
        if self._skip_ws():
            return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]
        if c == "#":
            mc = self._match_messagecode()
            if mc:
                return mc
        word = self._scan_word()
        if word:
            start = self.pos
            self.pos += len(word)
            self.pop_next_mode()
            return Token(IDENTIFIER, word, start)
        raise LexError(f"unexpected char {c!r} in X", self.pos, self._line)

    def _mode_Z(self):
        # putnpc2 code body: capture a brace-delimited block as STRING text
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        c = self.text[self.pos]
        if c == "{" and self.brace_count == 0:
            self.brace_count += 1
            self.pos += 1
            return None  # hidden opening brace
        if c == ";" and self.brace_count == 0:
            self.pop_next_mode()
            self.pos += 1
            return Token(END, ";", self.pos - 1)
        if c == "}" and self.brace_count == 1:
            self.brace_count -= 1
            self.pop_next_mode()
            self.emit_before(END)
            self.pos += 1
            return None  # hidden closing brace
        if c == "{":
            self.brace_count += 1
            self.pos += 1
            return Token(STRING, "{", self.pos - 1)
        if c == "}":
            self.brace_count -= 1
            self.pos += 1
            return Token(STRING, "}", self.pos - 1)
        if c == ";" and self.brace_count != 0:
            self.pos += 1
            return Token(STRING, ";", self.pos - 1)
        start = self.pos
        t, n = self.text, self.n
        while self.pos < n and t[self.pos] not in "{};":
            self.pos += 1
        if self.pos == start:
            self.pos += 1
        return Token(STRING, t[start:self.pos], start)

    def _mode_P1(self):
        # IN_PARAM_1: expect '(' to open a function call's arg list
        if self._skip_ws():
            return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        if self.text[self.pos] == "(":
            self.states[-1].pop_mode = POP_FUNCTION
            self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_PAREN_LEFT", "(", self.pos - 1)
        raise LexError("expected '(' after function", self.pos, self._line)

    def _mode_P2(self):
        if self._skip_ws():
            return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        if self.text[self.pos] == ")":
            self.pop_next_mode()
            self.pos += 1
            return Token("TOKEN_PAREN_RIGHT", ")", self.pos - 1)
        raise LexError("expected ')'", self.pos, self._line)

    def _mode_P3(self):
        # IN_PARAM_3: '(' opens func args, with optional trailing arg (playersays)
        if self._skip_ws():
            return None
        if self.pos >= self.n:
            return Token(EOF, "", self.pos)
        if self.text[self.pos] == "(":
            self.states[-1].pop_mode = POP_FUNCTION
            self.check_if_next_mode_optional()
            self.pos += 1
            return Token("TOKEN_PAREN_LEFT", "(", self.pos - 1)
        raise LexError("expected '(' after function", self.pos, self._line)


def tokenize(text: str) -> list[Token]:
    """Tokenize a GS1 script into a list of Token(type, text)."""
    return Lexer(text).tokenize()
