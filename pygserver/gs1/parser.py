"""GS1 parser — recursive-descent port of GServer-v2's GS1Parser.g4.

Consumes the token list from lexer.tokenize() and produces an AST (ast.py).
Precedence and rule shapes follow the ANTLR grammar; statement-level
assignment vs. expression is resolved with backtracking (the grammar relies on
ANTLR trying assignmentStatement before expression, and '=' doubles as the
equality operator inside expressions).

See memory: gs1-python-port.
"""
from __future__ import annotations

from . import ast
from .lexer import tokenize, Token, EOF

# token type groups
ASSIGN_OPS = {"OP_ASSIGN", "OP_ASSIGN_ADD", "OP_ASSIGN_SUB", "OP_ASSIGN_MUL",
              "OP_ASSIGN_DIV", "OP_ASSIGN_MOD", "OP_ASSIGN_POW"}
EQUALITY_OPS = {"OP_EQUAL", "OP_ASSIGN", "OP_NOTEQ"}
RELATIONAL_OPS = {"OP_LESS", "OP_GREAT", "OP_LESS_EQ", "OP_GREAT_EQ"}
ADDITIVE_OPS = {"OP_ADD", "OP_SUB"}
MULTIPLICATIVE_OPS = {"OP_MUL", "OP_DIV", "OP_MOD"}
UNARY_OPS = {"OP_ADD", "OP_SUB", "OP_LOGICALNOT"}
SPECIAL_LITS = {"ITEM", "CARRY", "DIRECTION", "GENDER", "COLOR", "BADDY"}
STMT_TERMINATORS = {"END", "EOF", "TOKEN_BRACE_RIGHT"}
STRING_PARTS = {"STRING", "MESSAGECODE", "RAWMESSAGECODE"}


class ParseError(Exception):
    def __init__(self, msg, tok: Token):
        super().__init__(f"{msg} (got {tok!r} at offset {tok.pos})")
        self.tok = tok


class Parser:
    def __init__(self, tokens: list[Token], recover: bool = True):
        self.toks = tokens
        self.i = 0
        self.recover = recover
        self.errors: list[ParseError] = []

    def _synchronize(self):
        """Panic-mode recovery: skip to the next ';' (consumed) or '}'/EOF."""
        while not self.at(EOF):
            t = self.next().type
            if t == "END":
                return
            if t == "TOKEN_BRACE_RIGHT":
                self.i -= 1  # let the enclosing block consume it
                return

    # -- token helpers -----------------------------------------------------
    def peek(self, k=0) -> Token:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, *types) -> bool:
        return self.peek().type in types

    def next(self) -> Token:
        t = self.toks[self.i]
        if t.type != EOF:
            self.i += 1
        return t

    def eat(self, ttype) -> Token:
        if self.peek().type != ttype:
            raise ParseError(f"expected {ttype}", self.peek())
        return self.next()

    def accept(self, ttype):
        if self.peek().type == ttype:
            return self.next()
        return None

    # -- entry -------------------------------------------------------------
    def parse_program(self) -> ast.Program:
        body = []
        while not self.at(EOF):
            if self.accept("END"):
                continue
            before = self.i
            try:
                body.append(self.parse_statement())
            except ParseError as e:
                if not self.recover:
                    raise
                self.errors.append(e)
                if self.i == before:  # ensure forward progress
                    self.next()
                self._synchronize()
        return ast.Program(body)

    # block: '{' statement* '}' | statement
    def parse_block(self):
        if self.at("TOKEN_BRACE_LEFT"):
            self.next()
            body = []
            while not self.at("TOKEN_BRACE_RIGHT", EOF):
                if self.accept("END"):
                    continue
                before = self.i
                try:
                    body.append(self.parse_statement())
                except ParseError as e:
                    if not self.recover:
                        raise
                    self.errors.append(e)
                    if self.i == before:
                        self.next()
                    self._synchronize()
            if not self.accept("TOKEN_BRACE_RIGHT") and not self.at(EOF):
                raise ParseError("expected TOKEN_BRACE_RIGHT", self.peek())
            return body  # unclosed block at EOF is tolerated (truncated source)
        return [self.parse_statement()]

    # -- statements --------------------------------------------------------
    def parse_statement(self):
        t = self.peek().type
        if t == "END":
            self.next()
            return ast.ExprStmt(None)
        if t == "TOKEN_BRACE_LEFT":
            # nested bare block (grammar disallows, but real scripts use it)
            return ast.Block(self.parse_block())
        if t == "KW_IF":
            return self.parse_if()
        if t == "KW_FOR":
            return self.parse_for()
        if t == "KW_WHILE":
            return self.parse_while()
        if t == "KW_WITH":
            return self.parse_with()
        if t == "KW_FUNCTION":
            return self.parse_funcdef()
        if t in ("KW_RETURN", "KW_BREAK", "KW_CONTINUE"):
            kind = {"KW_RETURN": "return", "KW_BREAK": "break",
                    "KW_CONTINUE": "continue"}[t]
            self.next()
            self._end_statement()
            return ast.Flow(kind)
        if t == "COMMAND":
            return self.parse_command()
        return self.parse_assignment_or_expr()

    def _end_statement(self):
        if self.at("END"):
            self.next()
        elif not self.at(EOF, "TOKEN_BRACE_RIGHT"):
            raise ParseError("expected ';' or end of statement", self.peek())

    def parse_if(self):
        self.eat("KW_IF")
        self.eat("TOKEN_PAREN_LEFT")
        cond = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        then = self.parse_block()
        els = None
        if self.accept("KW_ELSE"):
            els = self.parse_block()
        return ast.If(cond, then, els)

    def parse_for(self):
        self.eat("KW_FOR")
        self.eat("TOKEN_PAREN_LEFT")
        init = None if self.at("END") else self.parse_assignment_or_expr(no_terminator=True)
        self.eat("END")
        cond = None if self.at("END") else self.parse_expression()
        self.eat("END")
        post = None if self.at("TOKEN_PAREN_RIGHT") else self.parse_assignment_or_expr(no_terminator=True)
        while self.accept("END"):  # tolerate stray trailing ';' before ')'
            pass
        self.eat("TOKEN_PAREN_RIGHT")
        body = self.parse_block()
        return ast.For(init, cond, post, body)

    def parse_while(self):
        self.eat("KW_WHILE")
        self.eat("TOKEN_PAREN_LEFT")
        cond = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.While(cond, self.parse_block())

    def parse_with(self):
        self.eat("KW_WITH")
        self.eat("TOKEN_PAREN_LEFT")
        obj = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.With(obj, self.parse_block())

    def parse_funcdef(self):
        self.eat("KW_FUNCTION")
        name = self._read_identifier_name()
        self.eat("TOKEN_PAREN_LEFT")
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.FuncDef(name, self.parse_block())

    # builtinCommandStatement: COMMAND (arg (',' arg)*)?
    def parse_command(self):
        name = self.next().text
        args = []
        while not self.at(*STMT_TERMINATORS):
            args.append(self.parse_command_arg())
            if not self.accept("TOKEN_COMMA"):
                break
        self._end_statement()
        return ast.Command(name, args)

    def parse_command_arg(self):
        if self.peek().type in SPECIAL_LITS:
            t = self.next()
            return ast.SpecialLit(t.type, t.text)
        return self.parse_expression()

    def parse_assignment_or_expr(self, no_terminator=False):
        # try: identifier_access assignment_operator expression
        mark = self.i
        target = self._try_identifier_access()
        if target is not None and self.peek().type in ASSIGN_OPS:
            op = self.next().text
            value = self.parse_expression()
            node = ast.Assign(target, op, value)
            if not no_terminator:
                self._end_statement()
            return node
        # userFunctionStatement: name '(' ')'
        self.i = mark
        if (self.at("IDENTIFIER") and self.peek().text
                and self.peek(1).type == "TOKEN_PAREN_LEFT"
                and self.peek(2).type == "TOKEN_PAREN_RIGHT"):
            name = self.next().text
            self.next()
            self.next()
            node = ast.UserCall(name)
            if not no_terminator:
                self._end_statement()
            return node
        # otherwise: expression statement
        self.i = mark
        expr = self.parse_expression()
        # tolerate top-level comma lists ('a, b;') by keeping the last expr
        while self.accept("TOKEN_COMMA"):
            expr = self.parse_expression()
        node = ast.ExprStmt(expr)
        if not no_terminator:
            self._end_statement()
        return node

    # =====================================================================
    # Expressions (precedence climbing, mirrors the grammar)
    # =====================================================================
    def parse_expression(self):
        node = self.parse_logic_or()
        while self.accept("TOKEN_QUESTION"):
            a = self.parse_expression()
            self.eat("TOKEN_COLON")
            b = self.parse_expression()
            node = ast.Ternary(node, a, b)
        return node

    def parse_logic_or(self):
        node = self.parse_logic_and()
        while self.at("OP_LOGICALOR"):
            self.next()
            node = ast.BinOp("||", node, self.parse_logic_and())
        return node

    def parse_logic_and(self):
        node = self.parse_equality()
        while self.at("OP_LOGICALAND"):
            self.next()
            node = ast.BinOp("&&", node, self.parse_equality())
        return node

    def parse_equality(self):
        node = self.parse_relational()
        if self.peek().type in EQUALITY_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_relational())
        return node

    def parse_relational(self):
        node = self.parse_additive()
        if self.peek().type in RELATIONAL_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_additive())
        return node

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.peek().type in ADDITIVE_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self):
        node = self.parse_in()
        while self.peek().type in MULTIPLICATIVE_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_in())
        return node

    def parse_in(self):
        node = self.parse_exponent()
        if self.at("OP_IN"):
            self.next()
            if self.at("TOKEN_PIPE", "OP_LESS"):
                node = ast.InExpr(node, self.parse_range())
            else:
                node = ast.InExpr(node, self.parse_primary())
        return node

    def parse_range(self):
        self.next()  # '|' or '<'
        lo = self.parse_expression()
        self.eat("TOKEN_COMMA")
        hi = self.parse_expression()
        if not (self.accept("TOKEN_PIPE") or self.accept("OP_GREAT")):
            raise ParseError("expected '|' or '>' to close range", self.peek())
        return ast.RangeLit(lo, hi)

    def parse_exponent(self):
        node = self.parse_unary()
        while self.at("OP_POW"):
            self.next()
            node = ast.BinOp("^", node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.peek().type in UNARY_OPS:
            op = self.next().text
            return ast.UnaryOp(op, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        node = self.parse_primary()
        if self.at("OP_INC", "OP_DEC"):
            op = self.next().text
            node = ast.Postfix(op, node)
        return node

    def parse_primary(self):
        t = self.peek()
        tt = t.type
        if tt == "TOKEN_PAREN_LEFT":
            self.next()
            node = self.parse_expression()
            # tolerate comma-operator lists in parens, '(a, b)' -> keep last
            while self.accept("TOKEN_COMMA"):
                node = self.parse_expression()
            self.eat("TOKEN_PAREN_RIGHT")
            return node
        if tt in STRING_PARTS:
            return self.parse_string_concat()
        if tt == "FUNCTION":
            return self.parse_builtin_function()
        if tt == "TOKEN_BRACE_LEFT":
            return self.parse_array_literal()
        if tt == "LITERAL":
            self.next()
            return self._literal(t.text)
        if tt in ("ALLFEATURES", "ALLSTATS"):
            self.next()
            return ast.SpecialLit(tt, t.text)
        if tt == "IDENTIFIER":
            node = self.parse_identifier_access()
            # user-function call value: name '(' ')'
            if (isinstance(node, ast.VarRef) and len(node.parts) == 1
                    and not node.parts[0].index
                    and self.at("TOKEN_PAREN_LEFT")
                    and self.peek(1).type == "TOKEN_PAREN_RIGHT"):
                self.next()
                self.next()
                return ast.Call(node.parts[0].name, [])
            return node
        raise ParseError("expected expression", t)

    def parse_string_concat(self):
        parts = []
        while self.peek().type in STRING_PARTS:
            t = self.peek()
            if t.type == "STRING":
                self.next()
                parts.append(ast.Str(t.text))
            elif t.type == "RAWMESSAGECODE":
                self.next()  # synthetic marker, no value
            else:  # MESSAGECODE
                parts.append(self.parse_messagecode())
        return ast.StrConcat(parts)

    def parse_messagecode(self):
        t = self.eat("MESSAGECODE")
        args = []
        if self.accept("TOKEN_PAREN_LEFT"):
            if not self.at("TOKEN_PAREN_RIGHT"):
                args.append(self.parse_expression())
                while self.accept("TOKEN_COMMA"):
                    args.append(self.parse_expression())
            self.eat("TOKEN_PAREN_RIGHT")
        return ast.MessageCode(t.text, args)

    def parse_builtin_function(self):
        name = self.next().text
        self.eat("TOKEN_PAREN_LEFT")
        args = []
        if not self.at("TOKEN_PAREN_RIGHT"):
            args.append(self.parse_expression())
            while self.accept("TOKEN_COMMA"):
                args.append(self.parse_expression())
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.Call(name, args)

    def parse_array_literal(self):
        self.eat("TOKEN_BRACE_LEFT")
        elements = []
        while not self.at("TOKEN_BRACE_RIGHT", EOF):
            if self.accept("TOKEN_COMMA"):
                continue
            if self.accept("END"):
                continue
            elements.append(self.parse_expression())
            self.accept("TOKEN_COMMA")
        self.eat("TOKEN_BRACE_RIGHT")
        return ast.ArrayLit(elements)

    # identifier_access: name ('.' name)* with optional [index]
    def parse_identifier_access(self):
        parts = [self._read_path_part()]
        while self.at("TOKEN_PERIOD"):
            self.next()
            parts.append(self._read_path_part())
        return ast.VarRef(parts)

    def _try_identifier_access(self):
        if not self.at("IDENTIFIER"):
            return None
        try:
            return self.parse_identifier_access()
        except ParseError:
            return None

    def _read_path_part(self):
        # compound_identifier: (IDENTIFIER | messagecode_string | REAL)+ ; a
        # segment may be dynamic, e.g. this.#v(this.a) -> this.<var>
        atoms = []
        static = ""
        dynamic = False
        while True:
            t = self.peek()
            if t.type == "IDENTIFIER":
                self.next()
                if t.text:
                    atoms.append(ast.Str(t.text))
                    static += t.text
            elif t.type == "LITERAL":
                self.next()
                atoms.append(ast.Str(t.text))
                static += t.text
            elif t.type == "MESSAGECODE":
                atoms.append(self.parse_messagecode())
                dynamic = True
            else:
                break
        index = []
        if self.accept("TOKEN_BRACKET_LEFT"):
            index.append(self.parse_expression())
            if self.accept("TOKEN_COMMA"):
                index.append(self.parse_expression())
            self.eat("TOKEN_BRACKET_RIGHT")
        return ast.PathPart("" if dynamic else static, index, atoms)

    def _read_identifier_name(self):
        """Static identifier name only (used by function definitions)."""
        return self._read_path_part().name

    def _literal(self, text):
        low = text.lower()
        if low == "true":
            return ast.Bool(True)
        if low == "false":
            return ast.Bool(False)
        try:
            if low.startswith("0x"):
                return ast.Num(float(int(text, 16)))
            return ast.Num(float(text))
        except ValueError:
            return ast.Num(0.0)


def parse(text: str) -> ast.Program:
    """Lex + parse a GS1 script into a Program AST."""
    return Parser(tokenize(text)).parse_program()


def parse_tokens(tokens: list[Token]) -> ast.Program:
    return Parser(tokens).parse_program()
