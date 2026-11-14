"""
pygserver.config - Server configuration

Defines server settings and paths.
"""

import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from pathlib import Path

from .protocol.constants import LevelItemType

logger = logging.getLogger(__name__)


# serveroptions.txt and foldersconfig.txt travel as raw bytes on the wire
# (RC_SERVEROPTIONSGET/SET, RC_FOLDERCONFIGGET/SET), which this server decodes
# as latin-1; the files are read and written the same way so every byte
# round-trips.
OPTIONS_FILE_ENCODING = 'latin-1'
# Characters str.splitlines() treats as a line end, beyond '\n' itself.
LINE_BREAK_CHARS = frozenset('\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029')


def atomic_write_text(path, text: str,
                      encoding: str = OPTIONS_FILE_ENCODING) -> None:
    """Replace a text file in one step without changing what it is.

    The write lands on a symlink's target rather than replacing the link,
    keeps the existing file's permission bits (a new file gets 0666 less the
    umask, like open()), and goes through a temporary file in the target's
    own folder that is fsynced before the rename and removed on any error.
    Text that does not encode raises before the disk is touched.
    """
    data = text.encode(encoding)
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode: Optional[int] = stat.S_IMODE(os.stat(real).st_mode)
    except FileNotFoundError:
        mode = None
    for _attempt in range(16):
        temporary = real.parent / f".{real.name}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"no free temporary name beside {real}")
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, real)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        directory = os.open(real.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


# Item names as the reference's LevelItem::ItemNames spells them
# (GServer-v2 LevelItem.h:56-63); an item can also be named by its number
# (LevelItem.cpp getItemId(string)).
ITEM_IDS_BY_NAME = {item.name.lower(): int(item)
                    for item in LevelItemType if item >= 0}

# Server.h:514-515.
DEFAULT_BUSH_ITEM_TYPES = ("greenrupee", "bluerupee", "heart", "bombs")
DEFAULT_DEATH_ITEM_TYPES = ("greenrupee", "bluerupee", "redrupee",
                            "goldrupee", "bombs", "darts")
# Server.cpp:605-606: greenrupee 25, bluerupee 2, bombs 5, heart 5, rest 0.
DEFAULT_SPAWN_RATES = {
    int(LevelItemType.GREENRUPEE): 25, int(LevelItemType.BLUERUPEE): 2,
    int(LevelItemType.BOMBS): 5, int(LevelItemType.HEART): 5,
}
UINT32_MAX = 0xFFFFFFFF


def item_id_for_name(name: str) -> int:
    """Item id for a name or number, -1 when unknown (LevelItem.cpp getItemId)."""
    name = name.strip().lower()
    if name in ITEM_IDS_BY_NAME:
        return ITEM_IDS_BY_NAME[name]
    if name.isascii() and name.isdigit() and int(name) < len(ITEM_IDS_BY_NAME):
        return int(name)
    return -1


def _split_item_list(value: str) -> List[str]:
    """Settings::getList: comma split, empty entries dropped (Settings.h:230-240)."""
    return [part.strip().lower() for part in value.split(',') if part.strip()]


def _uint_setting(key: str, text: str, bits: int = 32) -> int:
    """Settings::get<uintN_t> (Settings.h:202-221, StringUtils.h:1320-1346).

    A value that is not plain digits reads as 0 and the server carries on: the
    option is present, so the caller's `value_or(default)` never applies. (The
    default only comes back when the option is absent.) One warning is logged
    per bad line. Digits past the type's range clamp to its maximum, as the
    loader always did for these keys; a leading minus reads as 0.
    """
    text = text.strip()
    if text.isascii() and text.isdigit():
        return min(int(text), (1 << bits) - 1)
    if text and not (text[0] == '-' and text[1:].isascii() and text[1:].isdigit()):
        logger.warning("serveroptions.txt: %s = %r is not a number; reading it as 0",
                       key, text)
    return 0


def _true_false_or_default(value: str, default: bool) -> bool:
    """Settings.h:208-216: only the words true/false count; else the default."""
    word = value.strip().lower()
    return word == 'true' if word in ('true', 'false') else default


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

    # GServer-v2/server/include/Server.h:173-174 (thresholds in seconds).
    apsystem: bool = True
    dontupdateratingd: bool = False
    aptime: List[int] = field(default_factory=lambda: [30, 90, 300, 600, 1200])

    # Gear power limits. Names and defaults are GServer-v2's own server options
    # (SettingCache declarations, server/include/Server.h:156-158).
    sword_limit: int = 3
    shield_limit: int = 3
    heal_swords: bool = False
    heart_limit: int = 3  # GServer-v2/server/include/Server.h:166.
    # Client PLI_PUTNPC is opt-in, matching the reference server's
    # get<bool>(...).value_or(false).
    putnpc_enabled: bool = False

    # World file system (Server.cpp:769-786): `nofoldersconfig = true` skips
    # foldersconfig.txt and makes the `sharefolder` folders public; with the
    # default `false`, sharefolder is ignored (filesystem.py share_dirs).
    nofoldersconfig: bool = False
    sharefolder: List[str] = field(default_factory=list)
    # PLO_BIGMAP / PLO_MINIMAP text sent at login when it splits into four
    # comma tokens (PlayerClient.cpp:494-512), kept verbatim here.
    bigmap: str = ""
    minimap: str = ""
    # Classic map text files (Server.cpp:954-970). Parsed only; no adjacency.
    maps: List[str] = field(default_factory=list)

    # Script mode: GServer-v2/server/src/scripting/Script.cpp:430-434;
    # option name from GServer-v2/bin/servers/default/config/serveroptions.txt:305.
    serverside: bool = True
    # Send links / signs to the client even with the NPC server present
    # (Server.h:154-155, PlayerClient.cpp:1495-1500). A missing key reads
    # false in the reference; from_file applies that. A bare ServerConfig()
    # carries the shipped default config's values (serveroptions.txt:325,328).
    clientsidelinks: bool = True
    clientsidesigns: bool = True
    baddyitems: bool = False
    # updateboard2 keeps its change (Server.h:137-138, Level.cpp:1852-1875);
    # `levelsautosave` additionally saves it to the level file.
    savelevels: bool = False
    levelsautosave: bool = False
    # Item drops (Server.h:123-124,514-515; PlayerClient.cpp:1898-1998).
    # `deathitemtypes` / `bushitemtypes` stay None while the option is absent,
    # which means the reference default list.
    dropitemsdead: bool = True
    mindeathgralats: int = 1
    maxdeathgralats: int = 50
    deathitemtypes: Optional[List[str]] = None
    bushitems: bool = True
    vasesdrop: bool = True
    bushitemtypes: Optional[List[str]] = None
    # `spawnrate<item>` values by the text after `spawnrate`; see
    # rebuild_bush_drops for when they are read.
    spawn_rates: Dict[str, int] = field(default_factory=dict)
    # Server.h:171 enableexbodycolors: widens the colour clamp (protocol/colors.py).
    enableexbodycolors: bool = False
    # Server.h:172: rejects client Z changes (PlayerProps.cpp:495-500).
    lockplayerz: bool = False
    # Server.h:165: Player::addWeapon(LevelItemType) is a no-op when off.
    defaultweapons: bool = True

    # `servermode` (Server.cpp:196-200): normal, kingdoms or newworld; the last
    # two announce themselves with PLO 176 / 177 at login (Player.cpp:748-752).
    servermode: str = "normal"
    # `generation` (Server.h:509): unset or `modern` sends compiled GS2 client
    # scripts for level NPCs (PLO_NPCBYTECODE); `classic`/`newmain` never do.
    generation: str = ""
    # `default_scriptengine[_client|_server]` (NPCServer.cpp:60-62): the engine
    # for a script half with no `//#GS1` / `//#GS2` first line. Unset keeps
    # pygserver's own source-shape detection.
    default_scriptengine: str = ""
    default_scriptengine_client: str = ""
    default_scriptengine_server: str = ""

    # Level tileset selection (Server.h:510-511, Server.cpp:1384-1405): which
    # tile-type table collision reads. `newtilesets` flips every level to the
    # new-format table, `newtilesetlevels` (comma list of globs/prefixes) only
    # the matching ones.
    newtilesets: bool = False
    newtilesetlevels: List[str] = field(default_factory=list)

    # Server.h:178: gani names (comma list, no attributes) whose being set on a
    # player makes the server test the sword rectangle for NPC `washit`
    # (PlayerProps.cpp:373). An empty value turns the feature off.
    playerswordganis: List[str] = field(default_factory=lambda: ["sword"])

    # scriptengine-gs1.txt `explodedwithjolt` (ScriptEngineGS1.h:173): the
    # joltbomb flag on exploded events. Default true, like the reference.
    explodedwithjolt: bool = True

    # Builtin triggeractions (GServer-v2/server/include/Server.h:143-150).
    triggerhack_weapons: bool = False
    triggerhack_groups: bool = True
    triggerhack_guilds: bool = False
    triggerhack_rc: bool = False
    triggerhack_levels: bool = False
    triggerhack_props: bool = False
    triggerhack_execscript: bool = False
    triggerhack_files: bool = False

    # Ambient world population: number of wandering VillagerNPCs to spawn on
    # the start level at boot (0 disables). See npcs/villager.py.
    villager_count: int = 8

    # Server.h:161: whether an RC without staff + set-rights may change a
    # player's gralats (PlayerProps.cpp:1205-1210).
    normaladminscanchangegralats: bool = True

    # Player.cpp:701: `onlystaff` refuses every non-staff login and makes the
    # list server report HQ level 0 (ServerList.cpp:276,479).
    onlystaff: bool = False
    # Server.h:139: client-sent `server.` flag sets and deletes are dropped
    # (PlayerClientPackets.cpp:665,707).
    dontaddserverflags: bool = False
    # Server.h:163: weapons granted at login and kept from client deletion
    # (PlayerClient.cpp:606, PlayerClientPackets.cpp:883).
    protectedweapons: List[str] = field(default_factory=list)

    # Server.h:125 / PlayerClientPackets.cpp:352: a client item drop is refused.
    disableitemdropping: bool = False
    # Server.h:132 / PlayerClientPackets.cpp:845: client explosions are ignored.
    noexplosions: bool = False
    # Server.h:131 / PlayerClientPackets.cpp:1310: relay of client triggeractions
    # to the rest of the level.
    sendplayertriggers: bool = True
    # LevelBaddy.cpp:270,302: seconds a dead baddy stays dead.
    baddyrespawntime: int = 60
    # Player props sync box on a gmap, in tiles each way (Server.h:127;
    # Level::findPlayersInRangeForSync, Level.cpp:3265-3318).
    syncdistancex: int = 192
    syncdistancey: int = 192
    # Server.h:134: seconds until a 2x2 board change (a cut bush) is restored
    # (board_changes.py).
    respawntime: int = 15

    # The serveroptions.txt this config was loaded from; RC option edits are
    # written back here. Empty for an in-memory config, which then persists
    # nothing.
    options_path: str = ""

    def __post_init__(self):
        self.rebuild_bush_drops(rates_from_options=False)

    # -- item drop tables ------------------------------------------------------

    def rebuild_bush_drops(self, rates_from_options: bool) -> None:
        """Snapshot the (item id, rate) roll table (Server.cpp:603-621).

        The reference rebuilds it only when `bushitemtypes` is (re)parsed, so a
        `spawnrate<item>` option changes nothing until then. With the option
        absent at the first load the table is built from the defaults alone
        (the callback ran before the options file was read, Settings.h trackOne):
        callers pass rates_from_options=False for that case.
        """
        names = (self.bushitemtypes if self.bushitemtypes is not None
                 else DEFAULT_BUSH_ITEM_TYPES)
        table: List[Tuple[int, int]] = []
        for name in names:
            name = name.strip().lower()
            item = item_id_for_name(name)
            if item < 0:
                continue
            rate = DEFAULT_SPAWN_RATES.get(item, 0)
            if rates_from_options and name in self.spawn_rates:
                rate = self.spawn_rates[name]
            table.append((item, rate))
        self._bush_drops = table

    @property
    def bush_drops(self) -> List[Tuple[int, int]]:
        """The bush roll table, in configured order."""
        return list(self._bush_drops)

    def death_drop_items(self) -> frozenset:
        """Item ids a dying player may drop (Server.cpp:624-635)."""
        names = (self.deathitemtypes if self.deathitemtypes is not None
                 else DEFAULT_DEATH_ITEM_TYPES)
        return frozenset(i for i in map(item_id_for_name, names) if i >= 0)

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
        config.options_path = str(config_file)

        config._apply_script_engine_gs1(
            server_path / "config" / "scriptengine-gs1.txt")

        # Set paths relative to server directory
        config.base_dir = str(server_path)
        config.levels_dir = str(server_path / "world")
        config.accounts_dir = str(server_path / "accounts")
        config.weapons_dir = str(server_path / "weapons")
        config.npcs_dir = str(server_path / "npcs")

        return config

    def _apply_script_engine_gs1(self, path) -> None:
        """Read the options this server honours from scriptengine-gs1.txt
        (same `key=value`, `#` comment format as serveroptions.txt)."""
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    if key.strip().lower() == 'explodedwithjolt':
                        self.explodedwithjolt = (
                            value.split('#', 1)[0].strip().lower() == 'true')
        except OSError:
            pass  # absent file: reference defaults

    @classmethod
    def from_file(cls, path: str) -> 'ServerConfig':
        """
        Load configuration from a serveroptions.txt file.

        Format is key = value pairs, one per line.
        """
        config = cls()
        config.options_path = str(path)
        try:
            # The encoding RC_SERVEROPTIONSSET writes the file with.
            with open(path, 'r', encoding=OPTIONS_FILE_ENCODING) as f:
                # A key absent from a config file reads false
                # (Server.h:154-155); only a listed key turns it on. Written
                # through vars() so rc._OptionProbe, which records setattr
                # calls, does not report these as the probed option.
                vars(config).update(clientsidelinks=False, clientsidesigns=False)
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip().lower()
                        value = value.strip()
                        # Real serveroptions files annotate numeric/boolean
                        # values in place (`triggerhack_weapons = true# ...`,
                        # cf. GServer-v2 server/src/utilities/Settings.cpp:50).
                        # Free-text options keep their '#' (`name = Server #1`).
                        scalar = value.split('#', 1)[0].strip()

                        if key == 'name':
                            config.name = value
                        elif key == 'description':
                            config.description = value
                        elif key == 'language':
                            config.language = value
                        elif key == 'url':
                            config.url = value
                        elif key == 'serverport':
                            config.port = int(scalar)
                        elif key == 'villager_count':
                            # pygserver-only option: how many VillagerNPCs
                            # spawn on the start level (server.py:291).
                            config.villager_count = int(scalar)
                        elif key == 'serverip':
                            config.serverip = value
                        elif key == 'localip':
                            config.localip = value
                        elif key == 'listip':
                            config.listip = value
                        elif key == 'listport':
                            config.listport = int(scalar)
                        elif key == 'staff':
                            # Settings.h getList splits with ignoreEmpty = true
                            # (StringUtils.h:618-650): blank entries never exist.
                            config.staff = [
                                s.strip() for s in value.split(',') if s.strip()]
                        elif key == 'noverifylogin':
                            # noverifylogin=true means verify_login=False (don't verify)
                            config.verify_login = scalar.lower() != 'true'
                        elif key == 'startlevel':
                            config.start_level = value
                        elif key == 'startx':
                            config.start_x = float(scalar)
                        elif key == 'starty':
                            config.start_y = float(scalar)
                        elif key == 'maxplayers':
                            config.max_players = int(scalar)
                        elif key == 'gmaps':
                            config.gmaps = [g.strip() for g in value.split(',') if g.strip()]
                        elif key == 'swordlimit':
                            config.sword_limit = int(scalar)
                        elif key == 'shieldlimit':
                            config.shield_limit = int(scalar)
                        elif key == 'healswords':
                            config.heal_swords = scalar.lower() == 'true'
                        elif key == 'heartlimit':
                            config.heart_limit = int(scalar)
                        elif key == 'dontupdateratingd':
                            config.dontupdateratingd = scalar.lower() == 'true'
                        elif key == 'apsystem':
                            config.apsystem = scalar.lower() == 'true'
                        elif key in ('aptime0', 'aptime1', 'aptime2', 'aptime3', 'aptime4'):
                            config.aptime[int(key[-1])] = int(scalar)
                        elif key == 'newtilesets':
                            config.newtilesets = scalar.lower() == 'true'
                        elif key == 'playerswordganis':
                            config.playerswordganis = [
                                g.strip() for g in value.split(',') if g.strip()]
                        elif key == 'newtilesetlevels':
                            config.newtilesetlevels = [
                                g.strip() for g in value.split(',') if g.strip()]
                        elif key == 'putnpcenabled':
                            config.putnpc_enabled = scalar.lower() == 'true'
                        elif key == 'sendplayertriggers':
                            # Settings.h:208-216: only the words true/false
                            # count; anything else keeps the default (true).
                            if scalar.lower() in ('true', 'false'):
                                config.sendplayertriggers = scalar.lower() == 'true'
                        elif key == 'baddyrespawntime':
                            # LevelBaddy.cpp:270,302 reads an unsigned 32-bit value.
                            config.baddyrespawntime = _uint_setting(key, scalar)
                        elif key in ('syncdistancex', 'syncdistancey'):
                            # SettingCache<uint32_t> (Server.h:127).
                            setattr(config, key, _uint_setting(key, scalar))
                        elif key == 'respawntime':
                            # Server.h:134: board-change respawn, uint16_t.
                            config.respawntime = _uint_setting(key, scalar, 16)
                        elif key == 'protectedweapons':
                            config.protectedweapons = [
                                w.strip() for w in value.split(',') if w.strip()]
                        elif key == 'nofoldersconfig':
                            config.nofoldersconfig = scalar.lower() == 'true'
                        elif key in ('sharefolder', 'maps'):
                            setattr(config, key, [
                                s.strip() for s in value.split(',') if s.strip()])
                        elif key in ('bigmap', 'minimap'):
                            setattr(config, key, value)
                        elif key in ('servermode', 'generation',
                                     'default_scriptengine',
                                     'default_scriptengine_client',
                                     'default_scriptengine_server'):
                            setattr(config, key, scalar.lower())
                        elif key in ('dropitemsdead', 'bushitems', 'vasesdrop'):
                            setattr(config, key, _true_false_or_default(scalar, True))
                        elif key in ('mindeathgralats', 'maxdeathgralats'):
                            # uint32_t in the reference (PlayerClient.cpp:1907-8).
                            setattr(config, key, _uint_setting(key, scalar))
                        elif key in ('deathitemtypes', 'bushitemtypes'):
                            setattr(config, key, _split_item_list(scalar))
                        elif key.startswith('spawnrate') and len(key) > 9:
                            # A new dict, so a caller that records assignments
                            # (rc.py) sees the key; read at rebuild_bush_drops.
                            config.spawn_rates = {
                                **config.spawn_rates,
                                key[9:]: _uint_setting(key, scalar)}
                        elif key in (
                            'serverside',
                            'clientsidelinks', 'clientsidesigns',
                            'savelevels', 'levelsautosave',
                            'baddyitems',
                            'lockplayerz',
                            'defaultweapons', 'enableexbodycolors',
                            'normaladminscanchangegralats',
                            'onlystaff', 'dontaddserverflags',
                            'disableitemdropping', 'noexplosions',
                            'triggerhack_weapons', 'triggerhack_groups',
                            'triggerhack_guilds', 'triggerhack_rc',
                            'triggerhack_levels', 'triggerhack_props',
                            'triggerhack_execscript', 'triggerhack_files',
                        ):
                            setattr(config, key, scalar.lower() == 'true')
        except FileNotFoundError:
            pass  # Use defaults

        config.rebuild_bush_drops(
            rates_from_options=config.bushitemtypes is not None)
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

    def apply_max_hearts(self, hearts: int) -> int:
        """Apply the reference's heartlimit and hard maximum of 20 hearts."""
        return max(0, min(int(hearts), self.heart_limit, 20))

    def option_lines(self) -> List[str]:
        """The serveroptions.txt lines `to_file` writes."""
        lines = [
            "# pygserver configuration",
            f"name = {self.name}",
            f"description = {self.description}",
            f"serverport = {self.port}",
            f"staff = {','.join(self.staff)}",
            f"noverifylogin = {str(not self.verify_login).lower()}",
            f"startlevel = {self.start_level}",
            f"startx = {self.start_x}",
            f"starty = {self.start_y}",
            f"maxplayers = {self.max_players}",
            f"heartlimit = {self.heart_limit}",
            f"apsystem = {str(self.apsystem).lower()}",
            f"dontupdateratingd = {str(self.dontupdateratingd).lower()}",
            f"putnpcenabled = {str(self.putnpc_enabled).lower()}",
        ]
        lines += [f"aptime{band} = {seconds}"
                  for band, seconds in enumerate(self.aptime)]
        if self.servermode and self.servermode != "normal":
            lines.append(f"servermode = {self.servermode}")
        for key in ('generation', 'default_scriptengine',
                    'default_scriptengine_client',
                    'default_scriptengine_server'):
            if getattr(self, key):
                lines.append(f"{key} = {getattr(self, key)}")
        for key in (
            'serverside', 'clientsidelinks', 'clientsidesigns',
            'savelevels', 'levelsautosave',
            'baddyitems', 'dropitemsdead', 'bushitems', 'vasesdrop',
            'enableexbodycolors',
            'lockplayerz',
            'defaultweapons',
            'onlystaff', 'dontaddserverflags',
            'disableitemdropping', 'noexplosions', 'sendplayertriggers',
            'triggerhack_weapons', 'triggerhack_groups',
            'triggerhack_guilds', 'triggerhack_rc', 'triggerhack_levels',
            'triggerhack_props', 'triggerhack_execscript', 'triggerhack_files',
        ):
            lines.append(f"{key} = {str(getattr(self, key)).lower()}")
        lines.append(f"baddyrespawntime = {self.baddyrespawntime}")
        lines.append(f"respawntime = {self.respawntime}")
        lines.append(f"syncdistancex = {self.syncdistancex}")
        lines.append(f"syncdistancey = {self.syncdistancey}")
        lines.append(f"mindeathgralats = {self.mindeathgralats}")
        lines.append(f"maxdeathgralats = {self.maxdeathgralats}")
        for key in ('deathitemtypes', 'bushitemtypes'):
            if getattr(self, key) is not None:
                lines.append(f"{key} = {','.join(getattr(self, key))}")
        lines += [f"spawnrate{name} = {rate}"
                  for name, rate in self.spawn_rates.items()]
        if self.protectedweapons:
            lines.append(f"protectedweapons = {','.join(self.protectedweapons)}")
        return lines

    def to_file(self, path: str):
        """Save configuration to file."""
        with open(path, 'w', encoding=OPTIONS_FILE_ENCODING,
                  errors='replace') as f:
            for line in self.option_lines():
                f.write(line + "\n")

    @staticmethod
    def update_options_file(path: str, updates) -> None:
        """Write `key = value` pairs into an existing options file in place.

        Every line whose key is updated is rewritten (the loader applies lines
        in order, so a duplicate left stale would win); every other line,
        comment and key is kept; new keys are appended. The file is replaced
        atomically. Keys and values are refused if they could start a new line
        or change what the loader reads as the key.
        """
        pending: Dict[str, str] = {}
        for key, value in updates:
            key = str(key).strip().lower()
            value = str(value).strip()
            if (not key or key.startswith('#') or '=' in key
                    or any(c.isspace() or not c.isprintable() for c in key)
                    or any(c in value for c in '\n\0')
                    or LINE_BREAK_CHARS.intersection(value)):
                raise ValueError(f"invalid option line for {key!r}")
            pending[key] = value
        if not pending:
            return
        try:
            text = Path(path).read_text(encoding=OPTIONS_FILE_ENCODING)
        except FileNotFoundError:
            text = ""
        lines = text.split('\n')
        if lines and lines[-1] == '':
            lines.pop()
        written = set()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            key = stripped.split('=', 1)[0].strip().lower()
            if key in pending:
                lines[index] = f"{key} = {pending[key]}"
                written.add(key)
        lines += [f"{key} = {value}" for key, value in pending.items()
                  if key not in written]
        atomic_write_text(path, '\n'.join(lines) + '\n')
