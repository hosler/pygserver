"""State components composed by Player.

Player used to carry ~40 flat attributes assigned in one 178-line __init__ that
also built its packet-dispatch table. They are grouped here by what owns them,
and Player keeps every one readable/writable under its original name (see
player._STATE_ALIASES), so the managers, the GS1 host, account persistence and
the test suite are unaffected.

Nothing here talks to the network or to Player: these are plain state holders.
"""

from typing import Dict, List

from .protocol.constants import PLTYPE


class Identity:
    """Who the account is, as far as other players see it."""

    def __init__(self):
        self.account_name = ""
        self.nickname = ""
        self.guild_name = ""
        self.guild_nickname = ""

        # Connection type (CLIENT, RC, NC, NPCSERVER)
        self.connection_type = PLTYPE.CLIENT


class Character:
    """The body in the world: position, stats, gear and animation."""

    def __init__(self):
        # Position (in tiles)
        self.x = 0.0
        self.y = 0.0
        self.direction = 2  # Down
        self.carrysprite = 0
        # Retained until a carried-NPC throw consumes it.  Clients normally
        # clear CARRYNPC separately from sending THROWCARRIED.
        self.npc_id = 0

        # Stats
        self.hearts = 3.0
        self.max_hearts = 3.0
        self.rupees = 0
        self.arrows = 10
        self.bombs = 5
        self.glove_power = 0
        self.sword_power = 1
        self.shield_power = 1

        # Combat stats
        self.kills = 0
        self.deaths = 0

        # Appearance
        self.head_image = "head19.png"
        self.body_image = "body.png"
        self.sword_image = "sword1.png"
        self.shield_image = "shield1.png"
        self.colors = [0, 0, 0, 0, 0]  # Skin, coat, sleeve, shoe, belt

        # MP/AP (PLPROP_MAGICPOINTS=26 / PLPROP_ALIGNMENT=32). Defaults match
        # GServer-v2 (server/include/object/Character.h): mp starts at 0,
        # ap starts at 50 (neutral on the 0-100 good/evil scale).
        self.mp = 0
        self.ap = 50

        # Animation
        self.gani = "idle"
        self.sprite = 0

        # Chat
        self.chat = ""


class Inventory:
    """Persisted per-account state: weapons, flags and custom attributes."""

    def __init__(self):
        self.weapons: List[str] = []
        self.flags: Dict[str, str] = {}
        # GATTRIBS (custom attributes)
        self.gattribs: Dict[int, str] = {}


class Status:
    """Server-imposed state and session bookkeeping."""

    def __init__(self):
        self.logged_in = False
        self.is_frozen = False
        self.is_ghost = False
        self.is_muted = False

        # Admin
        self.admin_rights = 0

        # Session timing
        self.login_time = 0.0
        self.last_packet_time = 0.0
