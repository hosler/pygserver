"""GS1 runtime: variable store, host interface, execution context.

The interpreter (interp.py) owns control flow, expression evaluation and the
flag/var stores. Everything that touches actual game state — built-in player/NPC
attributes (playerx, hearts, ...), arrays (players[], npcs[]), message codes
that need player context, and side-effecting commands (say, setimg, hide, ...) —
goes through a Host. pygserver supplies a real Host in Phase 5; MemoryHost lets
us execute and test scripts standalone now.

Variable model (from GS1Variables.h): namespaced flags/vars —
  this. / thiso. (npc)   temp. (per-run)   local. (npc-side)
  client. / clientr. (client flags)   server. / serverr. (server flags)
  level. (level)   global. (server, unsaved)   bare name -> player flag/attr
See memory: gs1-python-port.
"""
from __future__ import annotations

from .values import to_num, to_str, to_bool

# recognised namespace prefixes -> internal scope key
NAMESPACES = {
    "this": "this", "thiso": "thiso", "temp": "temp", "local": "local",
    "client": "client", "clientr": "client", "server": "server",
    "serverr": "server", "level": "level", "global": "global",
}

UNSET = object()  # sentinel: variable does not exist


# -- control-flow signals ---------------------------------------------------
class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class ReturnSignal(Exception):
    pass


class Host:
    """Interface the interpreter calls for game state and side effects.

    All methods have safe no-op defaults so a partial host still runs scripts.
    """

    def get_builtin(self, name, indices, ctx):
        """Return a built-in player/NPC/level attribute or array element.

        Return runtime.UNSET if this host does not know the name (the
        interpreter then falls back to a plain player variable)."""
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        """Try to set a built-in attribute. Return True if handled."""
        return False

    def call_command(self, name, args, ctx) -> None:
        """Perform a side-effecting command (say, setimg, hide, ...)."""

    def call_function(self, name, args, ctx):
        """Evaluate a built-in function not handled by the interpreter core.

        Return runtime.UNSET if unknown (interpreter yields 0)."""
        return UNSET

    def message_code(self, code, args, ctx) -> str:
        """Expand a message code (e.g. #a account name) to a string."""
        return ""


class MemoryHost(Host):
    """In-memory host for standalone execution and tests.

    Built-ins live in a flat dict; commands are recorded in `log`; a few common
    functions (onwall/getnpc/...) return benign defaults.
    """

    def __init__(self, attrs=None):
        self.attrs = dict(attrs or {})
        self.log = []  # list of (command_name, [arg values])

    def get_builtin(self, name, indices, ctx):
        if name in self.attrs:
            v = self.attrs[name]
            if indices and isinstance(v, (list, tuple)):
                i = int(to_num(indices[0]))
                return v[i] if 0 <= i < len(v) else 0.0
            return v
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        # only handle names declared as built-ins (pre-seeded); everything else
        # falls through so the interpreter stores it as a plain player var
        if name not in self.attrs:
            return False
        if indices:
            i = int(to_num(indices[0]))
            if i < 0:
                return True
            arr = self.attrs[name]
            if isinstance(arr, list):
                while len(arr) <= i:
                    arr.append(0.0)
                arr[i] = value
                return True
        self.attrs[name] = value
        return True

    def call_command(self, name, args, ctx) -> None:
        self.log.append((name, list(args)))

    def call_function(self, name, args, ctx):
        return UNSET

    def message_code(self, code, args, ctx) -> str:
        return ""


_SCOPE_KEYS = ("this", "thiso", "temp", "local", "client", "server",
               "level", "global")


class VarStore:
    """Per-scope flag/var storage for one script execution.

    `scopes` and `player_flags` may be supplied as external backing dicts so
    state persists in the right place: NPC-owned dicts for this/thiso/local,
    the player's flag dict for bare names, server/level dicts for server/level,
    etc. Anything not supplied gets a fresh in-memory dict.
    """

    def __init__(self, scopes=None, player_flags=None):
        self.scopes = scopes if scopes is not None else {}
        for k in _SCOPE_KEYS:
            self.scopes.setdefault(k, {})
        self.player_flags = player_flags if player_flags is not None else {}

    def get(self, scope, key, index=None):
        table = self.scopes.get(scope, self.player_flags) if scope else self.player_flags
        v = table.get(key, UNSET)
        if v is UNSET:
            return UNSET
        if index is not None and isinstance(v, list):
            i = int(index)
            return v[i] if 0 <= i < len(v) else 0.0
        return v

    def set(self, scope, key, value, index=None):
        table = self.scopes.get(scope, self.player_flags) if scope else self.player_flags
        if index is not None:
            i = int(index)
            if i < 0:
                return  # GS1 ignores negative array indices
            arr = table.get(key)
            if not isinstance(arr, list):
                arr = []
                table[key] = arr
            while len(arr) <= i:
                arr.append(0.0)
            arr[i] = value
        else:
            table[key] = value

    def unset(self, scope, key):
        table = self.scopes.get(scope, self.player_flags) if scope else self.player_flags
        table.pop(key, None)


class Context:
    """Execution context for one NPC/script run."""

    def __init__(self, host: Host, vars: VarStore = None, this_obj=None,
                 player=None, functions=None):
        self.host = host
        self.vars = vars or VarStore()
        self.this_obj = this_obj      # the NPC ("this") handle, host-defined
        self.player = player          # the acting player handle, host-defined
        self.functions = functions or {}  # name -> FuncDef (user functions)
        self.active_event = None      # the firing event name (its flag reads 1)
        self.tokenize_tokens = []     # set by `tokenize`, read by #t(i)
        self.steps = 0                # statement budget guard (infinite loops)
        self.max_steps = 200_000
