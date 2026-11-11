"""State components composed by Player.

Player used to carry ~40 flat attributes assigned in one 178-line __init__ that
also built its packet-dispatch table. They are grouped here by what owns them,
and Player keeps every one readable/writable under its original name (see
player._STATE_ALIASES), so the managers, the GS1 host, account persistence and
the test suite are unaffected.

Nothing here talks to the network or to Player: these are plain state holders.
"""

from typing import Dict, List

from .protocol.colors import default_colors
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
        # Wire position in tiles: NPC-relative when attached, level-local otherwise.
        self.x = 0.0
        self.y = 0.0
        # PLPROP_Z/Z2 in tiles (Character.h localPixelZ / 16).
        self.z = 0.0
        self.direction = 2  # Down
        self.carrysprite = -1
        # Retained until a carried-NPC throw consumes it.  Clients normally
        # clear CARRYNPC separately from sending THROWCARRIED.
        self.npc_id = 0
        self.attach_npc = (0, 0)

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
        self.elo_rating = 1500.0
        self.elo_deviation = 350.0
        self.last_spar_time = 0

        # Appearance
        self.head_image = "head19.png"
        self.body_image = "body.png"
        self.sword_image = "sword1.png"
        self.shield_image = "shield1.png"
        self.colors = default_colors()  # Character.h:43; Skin, coat, sleeve, shoe, belt, 3 new-world slots

        # MP/AP (PLPROP_MAGICPOINTS=26 / PLPROP_ALIGNMENT=32). Defaults match
        # GServer-v2 (server/include/object/Character.h): mp starts at 0,
        # ap starts at 50 (neutral on the 0-100 good/evil scale).
        self.mp = 0
        self.ap = 50
        self.ap_counter = 0

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
        # Set only once a game-client login has been fully accepted and its
        # account loaded (Player.cpp:771-773, PlayerClient.cpp:449,659 m_loaded).
        # Every save of the player's state into its account checks it, so a
        # refused, dropped or unverified connection can never write one.
        self.account_loaded = False
        self.is_frozen = False
        self.is_ghost = False
        self.is_muted = False

        # Admin
        self.admin_rights = 0

        # Session timing
        self.login_time = 0.0
        self.last_packet_time = 0.0
