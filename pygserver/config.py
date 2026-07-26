"""
pygserver.config - Server configuration

Defines server settings and paths.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
from pathlib import Path


@dataclass
class ServerConfig:
    """Server configuration settings."""

    # Server identity
    name: str = "pygserver"
    description: str = "Python Reborn Server"
    language: str = "English"
    url: str = "http://www.graal.in/"

    # Network
    host: str = "0.0.0.0"
    port: int = 14900
    serverip: str = "AUTO"  # IP to advertise (AUTO = detect)
    localip: str = "AUTO"   # Local IP (AUTO = detect)

    # List server connection
    listip: str = "listserver.graal.in"
    listport: int = 14900
    enable_listserver: bool = False  # Enable list server registration
    hq_password: str = ""            # HQ password
    hq_level: int = 1                # HQ access level

    # Base directory (server root)
    base_dir: str = "."

    # Paths (relative to base_dir)
    levels_dir: str = "levels"
    weapons_dir: str = "weapons"
    npcs_dir: str = "npcs"
    accounts_dir: str = "accounts"

    # GMAP support
    gmaps: List[str] = field(default_factory=list)

    # Staff
    staff: List[str] = field(default_factory=list)

    # Options
    verify_login: bool = False  # Set True for production
    start_level: str = "onlinestartlocal.nw"
    start_x: float = 30.0
    start_y: float = 30.5

    # Gameplay
    max_players: int = 100
    heartbeat_interval: float = 5.0  # seconds

    # Gear power limits. Names and defaults are GServer-v2's own server options
    # (SettingCache declarations, server/include/Server.h:156-158).
    sword_limit: int = 3
    shield_limit: int = 3
    heal_swords: bool = False

    # Ambient world population: number of wandering VillagerNPCs to spawn on
    # the start level at boot (0 disables). See npcs/villager.py.
    villager_count: int = 8

    @classmethod
    def from_server_dir(cls, server_dir: str) -> 'ServerConfig':
        """
        Load configuration from a server directory (like funtimes/).

        Expects structure:
        - server_dir/config/serveroptions.txt
        - server_dir/world/ (levels)
        - server_dir/accounts/
        - server_dir/weapons/
        - server_dir/npcs/
        """
        server_path = Path(server_dir)
        config_file = server_path / "config" / "serveroptions.txt"

        if config_file.exists():
            config = cls.from_file(str(config_file))
        else:
            config = cls()

        # Set paths relative to server directory
        config.base_dir = str(server_path)
        config.levels_dir = str(server_path / "world")
        config.accounts_dir = str(server_path / "accounts")
        config.weapons_dir = str(server_path / "weapons")
        config.npcs_dir = str(server_path / "npcs")

        return config

    @classmethod
    def from_file(cls, path: str) -> 'ServerConfig':
        """
        Load configuration from a serveroptions.txt file.

        Format is key = value pairs, one per line.
        """
        config = cls()
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip().lower()
                        value = value.strip()

                        if key == 'name':
                            config.name = value
                        elif key == 'description':
                            config.description = value
                        elif key == 'language':
                            config.language = value
                        elif key == 'url':
                            config.url = value
                        elif key == 'serverport':
                            config.port = int(value)
                        elif key == 'serverip':
                            config.serverip = value
                        elif key == 'localip':
                            config.localip = value
                        elif key == 'listip':
                            config.listip = value
                        elif key == 'listport':
                            config.listport = int(value)
                        elif key == 'staff':
                            config.staff = [s.strip() for s in value.split(',')]
                        elif key == 'noverifylogin':
                            # noverifylogin=true means verify_login=False (don't verify)
                            config.verify_login = value.lower() != 'true'
                        elif key == 'startlevel':
                            config.start_level = value
                        elif key == 'startx':
                            config.start_x = float(value)
                        elif key == 'starty':
                            config.start_y = float(value)
                        elif key == 'maxplayers':
                            config.max_players = int(value)
                        elif key == 'gmaps':
                            config.gmaps = [g.strip() for g in value.split(',') if g.strip()]
                        elif key == 'swordlimit':
                            config.sword_limit = int(value)
                        elif key == 'shieldlimit':
                            config.shield_limit = int(value)
                        elif key == 'healswords':
                            config.heal_swords = value.lower() == 'true'
        except FileNotFoundError:
            pass  # Use defaults

        return config

    # -- gear limits ----------------------------------------------------------
    #
    # A client is free to claim any sword/shield power in PLI_PLAYERPROPS, so the
    # value has to be clamped before it is stored. The reference server does that
    # in props::Limits (server/src/utilities/PropertySerializers.cpp:652-664),
    # driven by these options rather than by the hard Limits::MaxSwordPower /
    # MaxShieldPower constants (which applySwordPower/applyShieldPower never
    # consult). Clamping belongs here and not in the wire parser: the parse of
    # bare powers 1..4 and of the biased power+30 / power+10 forms has to stay
    # faithful for the packet to stay aligned.

    def apply_sword_power(self, power: int) -> int:
        """Clamp a claimed sword power to `swordlimit`.

        The floor is 0 unless `healswords` is on, which allows negative powers
        down to -swordlimit (a sword that heals instead of damaging).
        """
        minimum = -self.sword_limit if self.heal_swords else 0
        return max(minimum, min(int(power), self.sword_limit))

    def apply_shield_power(self, power: int) -> int:
        """Clamp a claimed shield power to `shieldlimit` (floor 0)."""
        return max(0, min(int(power), self.shield_limit))

    def to_file(self, path: str):
        """Save configuration to file."""
        with open(path, 'w') as f:
            f.write(f"# pygserver configuration\n")
            f.write(f"name = {self.name}\n")
            f.write(f"description = {self.description}\n")
            f.write(f"port = {self.port}\n")
            f.write(f"staff = {','.join(self.staff)}\n")
            f.write(f"noverifylogin = {str(not self.verify_login).lower()}\n")
            f.write(f"startlevel = {self.start_level}\n")
            f.write(f"startx = {self.start_x}\n")
            f.write(f"starty = {self.start_y}\n")
            f.write(f"maxplayers = {self.max_players}\n")
