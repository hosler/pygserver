"""
pygserver.gs2 - GS2 clientside bytecode: compile on load, serve on request.

pygserver's own scripting is Python/GS1, so the serverside half of a script is
none of this module's business. What it does is take the `//#CLIENTSIDE` half of
a weapon or script-class source, hand it to the real GServer-v2 GS2 compiler
(`gs2test`, the same binary reborn-protocol's compiler-conformance test pins),
and hold the resulting bytecode ready for PLI_UPDATESCRIPT / PLI_UPDATECLASS.

The compiler is a native binary and is not a dependency of this server: with no
binary and no precompiled `.gs2bc` beside the source, a script simply has no
bytecode and the server keeps serving it as classic GS1 text exactly as before.

Animation scripts go the same way: a .gani's SCRIPT block is compiled whole and
served as PLO_GANISCRIPT bytecode (server/src/animation/GameAni.cpp).

Splitting and header/checksum layout follow GServer-v2's own implementation
(server/src/scripting/Script.cpp minify()/split(), server/src/object/Weapon.cpp
setScript(), server/src/scripting/ScriptClass.cpp setScript()).
"""

import logging
import os
import shutil
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from .protocol.packets import (
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
#: PLO_NPCWEAPONSCRIPT rather than PLO_LOADSCRIPT on purpose).
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
# Compiler
# =============================================================================

def find_compiler() -> Optional[str]:
    """Locate the gs2test binary: $GS2TEST_BIN, the sibling reborn-protocol
    checkout's cached build, or PATH. None if there isn't a runnable one."""
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        os.environ.get("GS2TEST_BIN"),
        str(repo_root / "reborn-protocol" / "tests" / "tools" / "gs2test"),
        shutil.which("gs2test"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class GS2Compiler:
    """Wrapper around the gs2test binary. Absent binary = compile() returns None."""

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary if binary is not None else find_compiler()

    @property
    def available(self) -> bool:
        return self.binary is not None

    def compile(self, source: str, name: str = "script") -> Optional[bytes]:
        if not self.available or not source.strip():
            return None
        with tempfile.TemporaryDirectory(prefix="pygserver-gs2-") as tmp:
            src_path = Path(tmp) / "in.gs2"
            out_path = Path(tmp) / "out.gs2bc"
            src_path.write_text(source, encoding='latin-1', errors='replace')
            try:
                proc = subprocess.run(
                    [self.binary, str(src_path), "-o", str(out_path)],
                    capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning(f"GS2 compile of {name} failed to run: {e}")
                return None
            if proc.returncode != 0 or not out_path.exists():
                # Not an error condition: a GS1 clientside half is expected to
                # fail here and is served as text instead. The caller counts.
                logger.debug(f"GS2 compile of {name} failed: "
                             f"{(proc.stderr or proc.stdout).strip()[:300]}")
                return None
            return out_path.read_bytes()


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

    def build_headers(self, source: str):
        """Derive the DES key, CRC32 and CSV headers from the full source.

        The two kinds encode their checksum differently in the reference
        implementation: Weapon::setScript appends it as a decimal string
        (CString's long long ctor), ScriptClass::setScript as a GINT5.
        """
        raw = source.encode('latin-1', errors='replace')
        digest = zlib.crc32(raw) & 0xFFFFFFFF
        # GServer keys off std::hash of the source, which we can't reproduce and
        # don't need to: the client only stores the key. Any stable 8 bytes will
        # do, as long as it's the same 10-char two-GINT5 shape.
        self.des_key = _gint5(digest) + _gint5(zlib.adler32(raw) & 0xFFFFFFFF)
        self.checksum = digest
        parts = [self.kind, self.name, '1', self.des_key]
        if self.kind == 'class':
            self.header = to_csv(parts + [_gint5(self.checksum)])
            self.header_with_crc = self.header
        else:
            self.header = to_csv(parts)
            self.header_with_crc = to_csv(parts + [str(self.checksum)])


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

        _, clientside = split_clientside(script_source)
        script = GS2Script(kind=kind, name=name, image=image,
                           source=script_source, clientside=clientside)
        script.build_headers(script_source)
        script.bytecode = self._bytecode_for(path, clientside, name) or b''

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

    def upsert_classic_weapon(self, name: str, image: str,
                              source: str) -> GS2Script:
        """Create/update a persistent classic weapon from an NPC script."""
        weapon = self.get_weapon(name)
        if weapon is not None and weapon.source == source:
            return weapon

        _, clientside = split_clientside(source)
        weapon = GS2Script(kind="weapon", name=name, image=image,
                           source=source,
                           clientside=clientside)
        weapon.build_headers(source)
        self.weapons[name.lower()] = weapon
        self._save_classic_weapon(weapon, source)
        return weapon

    def _save_classic_weapon(self, weapon: GS2Script, source: str):
        directory = Path(getattr(self.server.config, "weapons_dir", None)
                         or "weapons")
        encoded = ''.join(
            char if char.isascii() and (char.isalnum() or char == '.')
            else f"%{ord(char):03d}"
            for char in f"weapon{weapon.name}.txt"
        )
        lines = ["GRAWP001", f"REALNAME {weapon.name}",
                 f"IMAGE {weapon.image}"]
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
        """Answer PLI_UPDATESCRIPT with PLO_NPCWEAPONSCRIPT.

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

        Always answers: a class we don't have gets the empty-bytecode stub, an
        unchanged one gets nothing (the client's copy is current).
        """
        script = self.get_class(name)
        if script is None or not script.bytecode:
            stub = to_csv(['class', name, '1', _ZERO_KEY, _ZERO_CRC])
            await player.send_raw(build_npc_weapon_script(stub, b''))
            return
        if checksum == script.checksum:
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
        pulls the bytecode itself with PLI_UPDATESCRIPT."""
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
