"""
pygserver.gs2 - GS2 clientside bytecode: compile on load, serve on request.

pygserver's own scripting is Python/GS1, so the serverside half of a script is
none of this module's business. What it does is take the `//#CLIENTSIDE` half of
a weapon or script-class source, hand it to the real GServer-v2 GS2 compiler
(`gs2test`, the same binary reborn-protocol's compiler-conformance test pins),
and hold the resulting bytecode ready for PLI_UPDATESCRIPT / PLI_UPDATECLASS.

The compiler is optional. Without bytecode, classic GS1 weapons retain their
source text; failed GS2 scripts have no clientside payload.

Animation scripts go the same way: a .gani's SCRIPT block is compiled whole and
served as PLO_GANISCRIPT bytecode (server/src/animation/GameAni.cpp).

Splitting and header/checksum layout follow GServer-v2's own implementation
(server/src/scripting/Script.cpp minify()/split(), server/src/object/Weapon.cpp
setScript(), server/src/scripting/ScriptClass.cpp setScript()).
"""

import logging
import os
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from reborn_protocol.gs2.compiler import Gs2Compiler as GS2Compiler, find_compiler
from reborn_protocol.gs2.detect import looks_like_gs2

from .text_safety import strip_control_chars
from .protocol.packets import (
    build_default_weapon,
    build_gani_script,
    build_load_gani,
    build_load_script_bytecode,
    build_load_script_header,
    build_npc_weapon_add,
    build_npc_weapon_add_scripted,
    build_npc_weapon_script,
    build_raw_data_announcement,
)

if TYPE_CHECKING:
    from .player import Player
    from .server import GameServer

logger = logging.getLogger(__name__)

CLIENTSIDE_MARKER = "//#CLIENTSIDE"

#: Empty-bytecode header the client gets for a class it asked for and we don't
#: have; without it a weapon's join() of a missing class never settles
#: (PlayerClientPackets.cpp msgPLI_UPDATECLASS, which answers with
#: PLO_WEAPONBYTECODE2 rather than PLO_LOADSCRIPT on purpose).
_ZERO_KEY = "\x20" * 10
_ZERO_CRC = "\x20" * 5


# =============================================================================
# Source splitting
# =============================================================================

def minify(source: str) -> str:
    """Strip comments and blank lines from a script source.

    Line trimming only kicks in past the clientside marker, matching
    Script::minify: serverside code is fed to a compiler that may care about
    leading whitespace, clientside code is not.
    """
    lines: List[str] = []
    in_serverside = True
    for line in source.split('\n'):
        if line.endswith('\r'):
            line = line[:-1]
        comment = line.find('//')
        if comment != -1:
            if comment + 2 < len(line) and line[comment + 2] != '#':
                line = line[:comment]
            elif CLIENTSIDE_MARKER in line:
                in_serverside = False
        if not in_serverside:
            line = line.strip()
        if line:
            lines.append(line)

    minified = '\n'.join(lines)
    start = 0
    while True:
        start = minified.find('/*', start)
        if start == -1:
            break
        end = minified.find('*/', start)
        if end == -1:
            break
        minified = minified[:start] + minified[end + 2:]
    return minified.strip()


def split_clientside(source: str) -> Tuple[str, str]:
    """Split a minified source into (serverside, clientside) at the marker.

    Unlike Script::split we leave the clientside line terminators as '\\n'
    instead of mangling them to '\\xa7'; that mangling exists for the GS1 text
    wire format, and the compiler's own baseline fixtures are '\\n'-separated.
    """
    minified = minify(source)
    sep = minified.find(CLIENTSIDE_MARKER)
    if sep == -1:
        return '', minified
    end_of_line = minified.find('\n', sep)
    if end_of_line == -1:
        return minified[:sep].strip(), ''
    return minified[:sep].strip(), minified[end_of_line + 1:].strip()


# =============================================================================
# Scripts
# =============================================================================

def to_csv(fields: List[str]) -> str:
    """Join header fields the way string::toCSV does: a field containing a
    quote, comma or backslash is wrapped in quotes with those chars doubled."""
    parts = []
    for field_value in fields:
        if any(c in field_value for c in '",\\'):
            parts.append('"' + ''.join(c * 2 if c in '"\\' else c
                                       for c in field_value) + '"')
        else:
            parts.append(field_value)
    return ','.join(parts)


def parse_csv_tolerant(value: str) -> List[str]:
    """Decode the wire CSV dialect, falling back to a raw comma split.

    Quoted fields double embedded quotes and backslashes.  A malformed quoted
    field is ordinary raw trigger text, never a reason to drop the trigger.
    """
    fields: List[str] = []
    pos = 0
    while pos <= len(value):
        if pos == len(value):
            fields.append("")
            break
        if value[pos] != '"':
            end = value.find(",", pos)
            if end < 0:
                fields.append(value[pos:])
                break
            fields.append(value[pos:end])
            pos = end + 1
            continue

        pos += 1
        field: List[str] = []
        while pos < len(value):
            char = value[pos]
            if char in ('"', '\\') and pos + 1 < len(value) \
                    and value[pos + 1] == char:
                field.append(char)
                pos += 2
                continue
            if char == '"':
                pos += 1
                if pos == len(value):
                    fields.append("".join(field))
                    return fields
                if value[pos] != ",":
                    return value.split(",")
                fields.append("".join(field))
                pos += 1
                break
            field.append(char)
            pos += 1
        else:
            return value.split(",")
    return fields


def _gint5(value: int) -> str:
    return ''.join(chr(((value >> shift) & 0x7F) + 32)
                   for shift in (28, 21, 14, 7, 0))


@dataclass
class GS2Script:
    """One compiled clientside script (a weapon or a script class)."""

    kind: str                       # 'weapon' or 'class'
    name: str
    image: str = ''
    source: str = ''
    clientside: str = ''
    bytecode: bytes = b''
    des_key: str = ''
    checksum: int = 0
    header: str = ''
    header_with_crc: str = ''
    joined_classes: str = ''
    server_program: Optional[Any] = None
    server_runtime: Optional[Any] = None
    source_path: Optional[Path] = None
    #: Last time the weapon's definition changed (Weapon::modTime): the file's
    #: mtime when loaded, the wall clock when created or edited. A level NPC's
    #: toweapons only replaces the weapon when this is older than the level.
    mod_time: float = 0.0

    def build_headers(self, source: str):
        """Derive the DES key from source and build the CRC32 headers.

        Weapons hash bytecode (server/src/object/Weapon.cpp:351-370);
        Classes retain source CRC (server/src/scripting/ScriptClass.cpp:40-42).
        """
        raw = source.encode('latin-1', errors='replace')
        digest = zlib.crc32(raw) & 0xFFFFFFFF
        # GServer keys off std::hash of the source, which we can't reproduce and
        # don't need to: the client only stores the key. Any stable 8 bytes will
        # do, as long as it's the same 10-char two-GINT5 shape.
        self.des_key = _gint5(digest) + _gint5(zlib.adler32(raw) & 0xFFFFFFFF)
        self.checksum = (digest if self.kind == 'class'
                         else zlib.crc32(self.bytecode) & 0xFFFFFFFF)
        parts = [self.kind, self.name, '1', self.des_key]
        if self.kind == 'class':
            self.header = to_csv(parts + [_gint5(self.checksum)])
            self.header_with_crc = self.header
        else:
            self.header = to_csv(parts)
            self.header_with_crc = to_csv(parts + [str(self.checksum)])


def _header_value(value: str) -> str:
    """A weapon-file header value (REALNAME/IMAGE) on one line.

    The file is line-oriented, so a line break (or any control character) in a
    name or image would start a new directive, e.g. a SCRIPT block that loads
    on the next restart. Those characters are dropped rather than escaped:
    the format has no escape.
    """
    return strip_control_chars(value)


def parse_weapon_file(text: str) -> Optional[Tuple[str, str, str]]:
    """Parse a GRAWP001 weapon file into (name, image, script).

    Format per Weapon::loadWeapon: a GRAWP001 magic line, then REALNAME/IMAGE
    lines and a SCRIPT block terminated by a bare SCRIPTEND.
    """
    text = text.replace('\r', '')
    lines = text.split('\n')
    if not lines or lines[0] != 'GRAWP001':
        return None

    name = image = ''
    script_lines: List[str] = []
    i = 1
    while i < len(lines):
        line = lines[i]
        i += 1
        command, _, rest = line.partition(' ')
        if command == 'REALNAME':
            name = rest
        elif command == 'IMAGE':
            image = rest
        elif command == 'SCRIPT':
            while i < len(lines):
                line = lines[i]
                i += 1
                if line == 'SCRIPTEND':
                    break
                script_lines.append(line)

    if not name:
        return None
    return name, image, '\n'.join(script_lines)


# =============================================================================
# Ganis
# =============================================================================

@dataclass
class GS2Gani:
    """One .gani's compiled animation script."""

    name: str
    setbackto: str = ''
    script: str = ''
    bytecode: bytes = b''
    checksum: int = 0


def parse_gani_file(text: str) -> Tuple[str, str]:
    """Pull (setbackto, script) out of a .gani file per GameAni::load.

    Everything else in the file (SPRITE/ANI/DEFAULT*) is rendering data the
    client reads from its own downloaded copy, so only these two lines matter
    here. Note the SCRIPT block is compiled whole, including its
    `//#CLIENTSIDE` line: a gani has no serverside half to split off
    (ScriptSystem::getCompiledClientScript).
    """
    setbackto = ''
    script_lines: List[str] = []
    lines = text.replace('\r', '').split('\n')
    i = 0
    while i < len(lines):
        parts = lines[i].split()
        i += 1
        if not parts:
            continue
        if parts[0] == 'SETBACKTO' and len(parts) >= 2:
            setbackto = parts[1]
        elif parts[0] == 'SCRIPT':
            while i < len(lines) and not lines[i].startswith('SCRIPTEND'):
                script_lines.append(lines[i])
                i += 1
            i += 1
    return setbackto, '\n'.join(script_lines)


# =============================================================================
# Manager
# =============================================================================

class GS2ScriptManager:
    """Loads GS2 weapons/classes off disk and serves their bytecode."""

    def __init__(self, server: 'GameServer', base_path: str = "."):
        self.server = server
        self.base_path = Path(base_path)
        self.compiler = GS2Compiler()
        self.weapons: Dict[str, GS2Script] = {}
        self.classes: Dict[str, GS2Script] = {}
        self.ganis: Dict[str, GS2Gani] = {}

    # -- loading -------------------------------------------------------------

    def load(self):
        """Scan weapons/ and scripts/ and compile every clientside half."""
        weapons_dir = getattr(self.server.config, 'weapons_dir', None) or 'weapons'
        self._load_dir(Path(weapons_dir), 'weapon')
        self._load_dir(self.base_path / 'scripts', 'class')

        scripts = list(self.weapons.values()) + list(self.classes.values())
        compiled = sum(1 for s in scripts if s.bytecode)
        if compiled:
            logger.info(f"GS2: {compiled} compiled script(s) "
                        f"({len(self.weapons)} weapon(s), {len(self.classes)} class(es)); "
                        f"{len(scripts) - compiled} served as GS1")
        elif not self.compiler.available:
            logger.info("GS2: no gs2test compiler and no precompiled .gs2bc; "
                        "serving GS1 scripts only")

    def _load_dir(self, directory: Path, kind: str):
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.txt")):
            try:
                self._load_file(path, kind)
            except Exception as e:
                logger.warning(f"Failed to load {kind} script {path.name}: {e}")

    def _load_file(self, path: Path, kind: str):
        source = path.read_text(encoding='latin-1', errors='replace')
        if kind == 'weapon':
            parsed = parse_weapon_file(source)
            if parsed is None:
                return
            name, image, script_source = parsed
        else:
            name, image, script_source = path.stem, '', source

        serverside, clientside = split_clientside(script_source)
        script = GS2Script(kind=kind, name=name, image=image,
                           source=script_source, clientside=clientside,
                           source_path=path, mod_time=path.stat().st_mtime)
        if kind == 'weapon' and serverside:
            from .gs1_host import compile_gs1
            from .npc import NPC
            script.server_program = compile_gs1(serverside)
            if script.server_program is not None:
                script.server_runtime = NPC(0, name)
                script.server_runtime.gs1_program = script.server_program
                script.server_runtime.gs1_source = serverside
                manager = getattr(self.server, "npc_manager", None)
                if manager is not None:
                    manager.register_weapon_runtime(script.server_runtime)
        # Script.cpp:480-513 requires an NPC server (Server.cpp:973-986).
        if getattr(self.server.config, 'serverside', False):
            script.bytecode = self._bytecode_for(path, clientside, name) or b''
            if not script.bytecode and looks_like_gs2(clientside):
                script.clientside = ''
        script.build_headers(script_source)

        target = self.weapons if kind == 'weapon' else self.classes
        target[name.lower()] = script

    def _bytecode_for(self, path: Path, clientside: str, name: str) -> Optional[bytes]:
        """Compiled bytecode for a clientside half, via the on-disk cache when
        it is at least as new as the source (which is also the only input path
        when the compiler binary is missing)."""
        cache_path = path.with_suffix('.gs2bc')
        if cache_path.is_file() and cache_path.stat().st_mtime >= path.stat().st_mtime:
            return cache_path.read_bytes()

        bytecode = self.compiler.compile(clientside, name)
        if bytecode:
            try:
                cache_path.write_bytes(bytecode)
            except OSError as e:
                logger.debug(f"Could not cache bytecode for {name}: {e}")
        return bytecode

    # -- lookup --------------------------------------------------------------

    def get_weapon(self, name: str) -> Optional[GS2Script]:
        return self.weapons.get(name.lower())

    @staticmethod
    def _atomic_write(path: Path, text: str):
        """Replace a source file without exposing a partially written file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.",
                                         dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="latin-1", errors="replace") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _encoded_weapon_filename(name: str) -> str:
        return ''.join(
            char if char.isascii() and (char.isalnum() or char == '.')
            else f"%{ord(char):03d}"
            for char in f"weapon{name}.txt"
        )

    def upsert_weapon(self, name: str, image: str, source: str) -> GS2Script:
        """Persist and load an NC-created or replaced weapon."""
        name, image = _header_value(name), _header_value(image)
        current = self.get_weapon(name)
        directory = Path(getattr(self.server.config, "weapons_dir", None)
                         or "weapons")
        path = (current.source_path if current and current.source_path
                else directory / self._encoded_weapon_filename(name))
        lines = ["GRAWP001", f"REALNAME {name}", f"IMAGE {image}"]
        if source:
            lines.extend(("SCRIPT", source, "SCRIPTEND"))
        self._atomic_write(path, '\n'.join(lines) + '\n')
        if current and current.server_runtime is not None:
            manager = getattr(self.server, "npc_manager", None)
            if manager is not None:
                manager.unregister_weapon_runtime(current.server_runtime)
        self._load_file(path, 'weapon')
        return self.get_weapon(name)  # type: ignore[return-value]

    def delete_weapon(self, name: str) -> bool:
        weapon = self.weapons.pop(name.lower(), None)
        if weapon is None:
            return False
        if weapon.server_runtime is not None:
            manager = getattr(self.server, "npc_manager", None)
            if manager is not None:
                manager.unregister_weapon_runtime(weapon.server_runtime)
        if weapon.source_path is not None:
            weapon.source_path.unlink(missing_ok=True)
            weapon.source_path.with_suffix('.gs2bc').unlink(missing_ok=True)
        return True

    def upsert_classic_weapon(self, name: str, image: str,
                              source: str) -> GS2Script:
        """Create/update a persistent classic weapon from an NPC script."""
        name, image = _header_value(name), _header_value(image)
        weapon = self.get_weapon(name)
        if weapon is not None and weapon.source == source:
            weapon.mod_time = time.time()
            return weapon

        serverside, clientside = split_clientside(source)
        weapon = GS2Script(kind="weapon", name=name, image=image,
                           source=source,
                           clientside=clientside)
        if serverside:
            from .gs1_host import compile_gs1
            from .npc import NPC
            weapon.server_program = compile_gs1(serverside)
            if weapon.server_program is not None:
                weapon.server_runtime = NPC(0, name)
                weapon.server_runtime.gs1_program = weapon.server_program
                weapon.server_runtime.gs1_source = serverside
                manager = getattr(self.server, "npc_manager", None)
                if manager is not None:
                    manager.register_weapon_runtime(weapon.server_runtime)
        weapon.build_headers(source)
        weapon.mod_time = time.time()
        self.weapons[name.lower()] = weapon
        self._save_classic_weapon(weapon, source)
        return weapon

    def _save_classic_weapon(self, weapon: GS2Script, source: str):
        directory = Path(getattr(self.server.config, "weapons_dir", None)
                         or "weapons")
        encoded = self._encoded_weapon_filename(weapon.name)
        lines = ["GRAWP001", f"REALNAME {_header_value(weapon.name)}",
                 f"IMAGE {_header_value(weapon.image)}"]
        if source:
            lines.extend(("SCRIPT", source, "SCRIPTEND"))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / encoded).write_text(
                '\n'.join(lines) + '\n', encoding='latin-1',
                errors='replace')
        except OSError as exc:
            logger.warning("Could not save weapon %s: %s", weapon.name, exc)

    def get_class(self, name: str) -> Optional[GS2Script]:
        return self.classes.get(name.lower())

    def upsert_class(self, name: str, source: str) -> GS2Script:
        """Persist and load an NC-created or replaced script class."""
        current = self.get_class(name)
        encoded = ''.join(
            char if char.isascii() and (char.isalnum() or char in '._-')
            else f"%{ord(char):03d}" for char in name)
        path = (current.source_path if current and current.source_path
                else self.base_path / 'scripts' / f'{encoded}.txt')
        self._atomic_write(path, source)
        self._load_file(path, 'class')
        return self.get_class(name)  # type: ignore[return-value]

    def delete_class(self, name: str) -> bool:
        script = self.classes.pop(name.lower(), None)
        if script is None:
            return False
        if script.source_path is not None:
            script.source_path.unlink(missing_ok=True)
            script.source_path.with_suffix('.gs2bc').unlink(missing_ok=True)
        return True

    def get_gani(self, name: str) -> Optional[GS2Gani]:
        """Compile a .gani's script, memoised by name.

        Loaded on demand rather than scanned at startup, the way
        ResourceManager::findOrAddResource does it - ganis are asked for by
        name and only a handful of a server's set is ever requested. Unlike
        that one we do NOT memoise misses, since the names come straight off
        the wire and a client naming files that don't exist would otherwise
        grow the table without bound.
        """
        if name.lower().endswith('.gani'):
            name = name[:-5]
        cached = self.ganis.get(name.lower())
        if cached is not None:
            return cached

        filesystem = getattr(self.server, 'filesystem', None)
        raw = filesystem.read_file(f"{name}.gani") if filesystem is not None else None
        if raw is None:
            return None

        setbackto, script = parse_gani_file(raw.decode('latin-1', errors='replace'))
        gani = GS2Gani(name=name, setbackto=setbackto, script=script)
        gani.bytecode = self.compiler.compile(script, name) or b''
        gani.checksum = zlib.crc32(gani.bytecode) & 0xFFFFFFFF
        self.ganis[name.lower()] = gani
        return gani

    # -- wire ----------------------------------------------------------------

    async def send_weapon_bytecode(self, player: 'Player', name: str) -> bool:
        """Answer PLI_UPDATESCRIPT with PLO_WEAPONBYTECODE2.

        Wrapped in PLO_RAWDATA: bytecode is binary and any 0x0a byte in it
        would otherwise end the packet early (Weapon::sendByteCodeToPlayer).
        """
        script = self.get_weapon(name)
        if script is None or not script.bytecode:
            return False
        packet = build_npc_weapon_script(script.header, script.bytecode)
        await player.send_raw(build_raw_data_announcement(len(packet)) + packet)
        logger.debug(f"GS2: sent weapon {script.name} bytecode "
                     f"({len(script.bytecode)} bytes) to player {player.id}")
        return True

    async def send_class_bytecode(self, player: 'Player', name: str,
                                  checksum: int = 0):
        """Answer PLI_UPDATECLASS with PLO_LOADSCRIPT (in PLO_RAWDATA).

        A missing class gets an empty-bytecode stub; unchanged classes and
        classes without bytecode get no payload.
        """
        script = self.get_class(name)
        if script is None:
            stub = to_csv(['class', name, '1', _ZERO_KEY, _ZERO_CRC])
            await player.send_raw(build_npc_weapon_script(stub, b''))
            return
        if checksum == script.checksum or not script.bytecode:
            return
        packet = build_load_script_bytecode(script.header, script.bytecode)
        await player.send_raw(build_raw_data_announcement(len(packet)) + packet)
        logger.debug(f"GS2: sent class {script.name} bytecode "
                     f"({len(script.bytecode)} bytes) to player {player.id}")

    async def send_gani(self, player: 'Player', name: str, checksum: int = 0):
        """Answer PLI_UPDATEGANI: the compiled script as PLO_GANISCRIPT when
        the client's copy is stale, then always PLO_LOADGANI.

        A gani we can't find gets no answer at all (msgPLI_UPDATEGANI returns
        early on a missing animation) - the client keeps rendering the .gani
        file it downloaded through the ordinary file path.
        """
        gani = self.get_gani(name)
        if gani is None:
            return
        if gani.bytecode and gani.checksum != checksum:
            packet = build_gani_script(gani.name, gani.bytecode)
            await player.send_raw(build_raw_data_announcement(len(packet)) + packet)
            logger.debug(f"GS2: sent gani {gani.name} bytecode "
                         f"({len(gani.bytecode)} bytes) to player {player.id}")
        await player.send_raw(build_load_gani(gani.name, gani.setbackto))

    async def announce_weapon(self, player: 'Player', name: str) -> bool:
        """Push a compiled weapon to a player: PLO_NPCWEAPONADD carrying the
        image and joined-class list, then the PLO_LOADSCRIPT header. The client
        pulls the bytecode itself with PLI_UPDATESCRIPT. Built-in item weapons
        (bow, bomb, ...) are a bare PLO_DEFAULTWEAPON id instead."""
        from .items import DEFAULT_WEAPON_IDS
        default = DEFAULT_WEAPON_IDS.get(name)
        if default is not None and self.get_weapon(name) is None:
            await player.send_raw(build_default_weapon(int(default)))
            return True
        script = self.get_weapon(name)
        if script is None:
            return False
        if not script.bytecode:
            await player.send_raw(build_npc_weapon_add(
                script.name, script.image, script.clientside))
            return True
        await player.send_raw(build_npc_weapon_add_scripted(
            script.name, script.image, script.joined_classes))
        await player.send_raw(build_load_script_header(script.header_with_crc))
        return True

    async def announce_weapons(self, player: 'Player'):
        """Announce every compiled weapon the player already owns."""
        for name in list(getattr(player, 'weapons', []) or []):
            await self.announce_weapon(player, name)
