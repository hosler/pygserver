"""GS1 (Graal Script 1) interpreter for pygserver.

A faithful Python port of the GServer-v2 C++ ANTLR engine
(GServer-v2/server/src/scripting/gs1/). See memory: gs1-python-port.

Build order: lexer (mode-driven) -> parser/AST -> visitor/executor ->
commands+functions+messagecodes -> events/flags + NPCManager wiring.

Lexer tables (COMMANDS/FUNCTIONS/MESSAGECODES) live in _tables.py and are
auto-generated from the grammar by tools/gen_gs1_tables.py.
"""

from ._tables import COMMANDS, FUNCTIONS, MESSAGECODES
from .lexer import tokenize, Token, LexError
from .parser import parse, Parser, ParseError
from .runtime import Context, Host, MemoryHost, VarStore
from .interp import Interpreter, run, run_event

__all__ = [
    "COMMANDS", "FUNCTIONS", "MESSAGECODES",
    "tokenize", "Token", "LexError",
    "parse", "Parser", "ParseError",
    "Context", "Host", "MemoryHost", "VarStore",
    "Interpreter", "run", "run_event",
]
