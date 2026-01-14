"""
pygserver.weapon - Weapon system

Handles weapon definitions and combat.
"""

import logging
from typing import TYPE_CHECKING, Optional, Dict, List

if TYPE_CHECKING:
    from .player import Player

logger = logging.getLogger(__name__)


class Weapon:
    """
    Represents a weapon that players can use.

    Weapons have client-side scripts for visuals and server-side
    scripts for game logic.
    """

    def __init__(self, name: str):
        self.name = name
        self.image = ""
        self.client_script = ""
        self.server_script = ""

    @classmethod
    def from_line(cls, line: str) -> Optional['Weapon']:
        """
        Parse a weapon from add weapon format.

        Format: +weaponname image!<script_code
        """
        if not line.startswith('+'):
            return None

        # Remove leading +
        line = line[1:]

        # Find image (before !) and script (after <)
        name_end = line.find(' ')
        if name_end < 0:
            return None

        name = line[:name_end]
        rest = line[name_end + 1:]

        weapon = cls(name)

        # Split image and script
        if '!' in rest and '<' in rest:
            excl_idx = rest.index('!')
            lt_idx = rest.index('<')
            weapon.image = rest[:excl_idx]
            weapon.client_script = rest[lt_idx + 1:]
        elif '!' in rest:
            weapon.image = rest[:rest.index('!')]
        else:
            weapon.image = rest.strip()

        return weapon


class WeaponManager:
    """
    Manages all weapons on the server.
    """

    def __init__(self):
        self._weapons: Dict[str, Weapon] = {}

        # Add default weapons
        self._add_default_weapons()

    def _add_default_weapons(self):
        """Add built-in default weapons."""
        # Sword
        sword = Weapon("Sword")
        sword.image = "sword.png"
        self._weapons["Sword"] = sword

        # Shield
        shield = Weapon("Shield")
        shield.image = "shield.png"
        self._weapons["Shield"] = shield

        # Bow
        bow = Weapon("Bow")
        bow.image = "bow.png"
        self._weapons["Bow"] = bow

        # Bombs
        bombs = Weapon("Bombs")
        bombs.image = "bombs.png"
        self._weapons["Bombs"] = bombs

    def add_weapon(self, weapon: Weapon):
        """Add a weapon."""
        self._weapons[weapon.name] = weapon

    def get_weapon(self, name: str) -> Optional[Weapon]:
        """Get weapon by name."""
        return self._weapons.get(name)

    def get_all_weapons(self) -> List[Weapon]:
        """Get all weapons."""
        return list(self._weapons.values())

    def build_weapon_packet(self, weapon: Weapon) -> bytes:
        """Build weapon add packet."""
        from .protocol.packets import PacketBuilder
        from .protocol.constants import PLO

        builder = PacketBuilder()
        builder.write_gchar(PLO.NPCWEAPONADD)

        # Format: +name image!<script
        weapon_str = f"+{weapon.name} {weapon.image}"
        if weapon.client_script:
            weapon_str += f"!<{weapon.client_script}"

        builder.write_string(weapon_str)
        builder.write_byte(ord('\n'))

        return builder.build()
