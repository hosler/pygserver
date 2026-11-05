"""GS1 writes to the player's persistent, owner-synchronized client flags."""

from reborn_protocol.gs1.runtime import UNSET, VarStore
from reborn_protocol.gs1.values import to_str

from ..protocol.packets import build_flag_del, build_flag_set


class PlayerVarStore(VarStore):
    def __init__(self, player, **kwargs):
        super().__init__(**kwargs)
        self.player = player

    def set(self, scope, key, value, index=None):
        super().set(scope, key, value, index)
        if scope == "client":
            stored = self.get(scope, key)
            if stored is not UNSET:
                self._send(build_flag_set(key, True if stored is True else to_str(stored)))

    def unset(self, scope, key):
        super().unset(scope, key)
        if scope == "client":
            self._send(build_flag_del(key))

    def _send(self, packet):
        # GServer-v2 Player.cpp:979-1012 sends server writes only to the owner, for both prefixes.
        sender = getattr(self.player, "send_raw", None)
        if sender is not None:
            from .execution import _schedule
            _schedule(sender(packet))
