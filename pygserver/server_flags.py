"""Persistent server flags shared by scripts and administrative clients."""

import os
from collections import UserDict
from pathlib import Path


def serialize_flag(name, value):
    # True stays bool like player flags; GServer-v2 ScriptContainers.cpp:156 writes it bare.
    if value is True:
        return name
    if isinstance(value, str):
        return f"{name}={value}"
    return None


class ServerFlags(UserDict):
    def __init__(self, server):
        super().__init__()
        self.server = server
        self.path = Path(server.config.base_dir) / "config" / "serverflags.txt"
        self.dirty = False
        self.load()

    def load(self):
        # GServer-v2 Server.cpp:787,849: trimmed lines; bare/empty values become true.
        if not self.path.exists():
            return
        flags = {}
        for line in self.path.read_text(encoding="latin-1").splitlines():
            name, _, value = line.strip().partition("=")
            name = name.strip()
            if name:
                flags.setdefault(name, value.strip() or True)
        self.data = flags
        self.dirty = False

    def save(self):
        if not self.dirty:
            return
        # GServer-v2 Server.cpp:1070: sorted, serializable flags only.
        lines = [serialize_flag(name, self.data[name]) for name in sorted(self.data)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".txt.tmp")
        try:
            temporary.write_text("".join(line + "\n" for line in lines if line is not None),
                                 encoding="latin-1")
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()
        self.dirty = False

    def put(self, name, value, notify=True):
        # GServer-v2 Server.cpp:1925-1935 stores a bare PLI_FLAGSET as true.
        if value == "":
            value = True
        self.data[name] = value
        self._changed(name, value, notify=notify)

    def remove(self, name, notify=True):
        del self.data[name]
        self._changed(name, None, deleted=True, notify=notify)

    def __setitem__(self, name, value):
        self.put(name, value)

    def __delitem__(self, name):
        self.remove(name)

    def _changed(self, name, value, deleted=False, notify=True):
        self.dirty = True
        if notify:
            from .gs1.execution import _schedule
            _schedule(self.server._broadcast_flag(name, value, deleted))
