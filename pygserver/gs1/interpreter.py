"""Server-side variable resolution retaining public/private flag prefixes."""

from reborn_protocol.gs1.interp import Interpreter, _getflagkeys
from reborn_protocol.gs1.runtime import NAMESPACES, UNSET
from reborn_protocol.gs1.values import to_str

_FLAG_PREFIXES = ("server.", "serverr.", "client.", "clientr.")


class ServerInterpreter(Interpreter):
    def _resolve(self, ref):
        scope, key, indices, names = super()._resolve(ref)
        if scope in ("server", "client"):
            key = names[0] + "." + key
        return scope, key, indices, names

    @staticmethod
    def _parameter_ref(name):
        if name.startswith(_FLAG_PREFIXES):
            return NAMESPACES[name.partition(".")[0]], name
        return Interpreter._parameter_ref(name)

    def _value_of(self, text):
        if text.startswith(_FLAG_PREFIXES) and text.endswith("="):
            name = text[:-1]
            namespace, _, key = name.partition(".")
            if key and not any(c in key for c in " \t.="):
                value = self.ctx.vars.get(NAMESPACES[namespace], name)
                return None if value is UNSET else to_str(value)
            return None
        return super()._value_of(text)

    def _ex_Call(self, node):
        # getnearestplayers(x, y, condition): the reference hands the host the
        # unevaluated condition and re-runs it per player (ca160f9e,
        # GS1Functions.cpp:597-660), so the third argument is NOT evaluated
        # eagerly here like every other call argument.
        if (node.name == "getnearestplayers" and len(node.args) >= 3
                and node.name not in self.ctx.functions):
            args = [self.eval(arg) for arg in node.args[:2]]
            condition = node.args[2]
            result = self.ctx.host._nearest_players(
                args, self.ctx, lambda: self.eval(condition))
            return result
        if node.name == "getflagkeys" and node.name not in self.ctx.functions:
            args = [self.eval(arg) for arg in node.args]
            prefix = to_str(args[0]) if args else ""
            if prefix.startswith(_FLAG_PREFIXES):
                result = []
                scope = NAMESPACES[prefix.partition(".")[0]]
                for key in sorted(self.ctx.vars.scopes[scope]):
                    if key.startswith(prefix):
                        try:
                            result.append(float(key[len(prefix):]))
                        except ValueError:
                            result.append(0.0)
                return result
            return _getflagkeys(self, args)
        return super()._ex_Call(node)
