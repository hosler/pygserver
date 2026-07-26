"""Inbound (PLI) packet handlers, grouped by domain.

| mixin                    | packets                                          |
|--------------------------|--------------------------------------------------|
| MovementHandlers         | warps, player props, adjacent-level preload      |
| CombatHandlers           | bombs, arrows, firespy, hurt, explosions, shoot  |
| ItemHandlers             | ground items, chests, horses                     |
| EntityHandlers           | baddies and the player's weapon list             |
| CommunicationHandlers    | chat, PMs, flags, triggeractions                 |
| FileHandlers             | file/gani/script/class requests                  |
| MiscHandlers             | board edits, profiles, server/misc packets       |

Player inherits all of them; `registry.collect_handler_names` turns the
`@handles` decorations into its dispatch table.
"""

from .chat import CommunicationHandlers
from .combat import CombatHandlers
from .entities import EntityHandlers
from .files import FileHandlers
from .items import ItemHandlers
from .misc import MiscHandlers
from .movement import MovementHandlers
from .registry import collect_handler_names, handles

__all__ = [
    "CommunicationHandlers", "CombatHandlers", "EntityHandlers",
    "FileHandlers", "ItemHandlers", "MiscHandlers", "MovementHandlers",
    "collect_handler_names", "handles",
]
