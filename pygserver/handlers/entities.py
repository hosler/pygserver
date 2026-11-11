"""Baddies, client-created level NPCs, and the player's weapon list."""

import fnmatch
import logging
import os
from pathlib import Path

from ..protocol.constants import PLI, PLO, PLTYPE, NPCPROP, BDPROP, BDMODE, LevelItemType
from ..protocol.packets import (
    PacketReader, build_baddy_props, build_default_weapon,
    build_npc_del, build_npc_del2, build_npc_props,
)
from ..text_safety import has_control_chars, sanitize_value, strip_control_chars
from .registry import handles

logger = logging.getLogger(__name__)


def _level_mod_time(level) -> float:
    """Modification time of a level's file (Level::modTime, LevelLoader.cpp:
    268); 0 for a level with no file behind it."""
    path = getattr(level, 'file_path', None)
    try:
        return os.stat(path).st_mtime if path else 0.0
    except OSError:
        return 0.0


def _folders_config_entries(filesystem):
    """(category, glob) lines of config/foldersconfig.txt, after the comment
    and whitespace rules of Server::loadFolderConfig (Server.cpp:497-512).
    The file system parses and caches them per version of the file, and
    drops any line whose glob could leave the public folders."""
    return filesystem.folders_config_entries()


def _glob_matches(path: str, glob: str) -> bool:
    """string::match (StringUtils.h:1622-1700): case-sensitive, `*` also
    crosses `/`, `?` is one character, nothing else is special."""
    return fnmatch.fnmatchcase(path, glob.replace('[', '[[]'))


def find_putnpc_script(filesystem, name):
    """The PLI_PUTNPC script file, FileCategory::FILE by bare name
    (PlayerClientPackets.cpp:768).

    With a foldersconfig.txt, only files whose world-relative path matches a
    `file <glob>` line qualify (FileSystem.cpp:398-411, 750-764), drawn from
    the public folders a client can already fetch by bare name. Without one,
    the lookup stays in the world folder. A path or a name outside those
    folders is never read (the reference's direct-path check at
    FileSystem.cpp:475-481 is left out on purpose).
    """
    entries = _folders_config_entries(filesystem)
    if not entries:
        return filesystem._find_file(
            name, dirs=filesystem.NPC_SCRIPT_DIRS, strict=True)
    globs = [glob for category, glob in entries if category == 'file' and glob]
    if not globs or filesystem._find_file(name, strict=True) is None:
        return None
    base = Path(filesystem.base_path)
    world = base / 'world'
    candidates = list(filesystem._public_index.get(name.lower(), ()))
    candidates.sort(key=lambda path: path.name != name)
    for path in candidates:
        try:
            relative = path.relative_to(world)
        except ValueError:
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
        if (path.is_file()
                and any(_glob_matches(relative.as_posix(), glob) for glob in globs)):
            return path
    return None


class EntityHandlers:
    """Mixin: PLI_BADDY* and the weapon add/delete packets."""

    @handles(PLI.BADDYPROPS)
    async def _handle_baddy_props(self, data: bytes):
        """Handle PLI_BADDYPROPS packet."""
        if not self.level or not self.level.is_player_leader(self):
            return

        reader = PacketReader(data)
        baddy_id = reader.read_gchar()
        props = {}
        while reader.has_data():
            prop_id = reader.read_gchar()
            if prop_id == BDPROP.ID:
                props[prop_id] = reader.read_gchar()
            elif prop_id in (BDPROP.X, BDPROP.Y):
                props[prop_id] = reader.read_gchar() / 2.0
            elif prop_id == BDPROP.TYPE:
                props[prop_id] = reader.read_gchar()
            elif prop_id == BDPROP.POWERIMAGE:
                props[prop_id] = (reader.read_gchar(),
                                  strip_control_chars(reader.read_gstring()))
            elif prop_id == BDPROP.MODE:
                props[prop_id] = reader.read_gchar()
            elif prop_id in (BDPROP.ANI, BDPROP.DIR):
                props[prop_id] = reader.read_gchar()
            elif prop_id in (BDPROP.VERSESIGHT, BDPROP.VERSEHURT,
                              BDPROP.VERSEATTACK):
                props[prop_id] = strip_control_chars(reader.read_gstring())
            else:
                break

        manager = getattr(self.server, 'baddy_manager', None)
        baddy = manager.get_baddy(self.level.name, baddy_id) if manager else None
        if not baddy:
            return

        if BDPROP.POWERIMAGE in props:
            baddy.health = props[BDPROP.POWERIMAGE][0]
        if props.get(BDPROP.MODE) == BDMODE.DEAD:
            await manager.handle_baddy_death(baddy, self, exclude={self.id})
        else:
            await self.server.broadcast_to_level(
                self.level.name, build_baddy_props(baddy_id, props),
                exclude={self.id},
            )

    @handles(PLI.BADDYHURT)
    async def _handle_baddy_hurt(self, data: bytes):
        """Handle PLI_BADDYHURT packet.

        Wire format (GServer-v2 msgPLI_BADDYHURT, PlayerClientPackets.cpp:
        523-539, commit e0cd07af9bb4be09c54c0335f222dd0eacb71c1):
            [GUChar baddyId][GChar hurtDX][GChar hurtDY][GUChar damage,
            half-hearts]
        hurtDX/hurtDY are commented there as "midpoint: 64" - the same
        recentering idiom as read_gchar_signed() (byte - 32) with an extra
        -64 on top, i.e. value = read_gchar_signed() - 64 (mirrors GServer's
        PropertyHurtDxDy<MidPoint>::deserialize: dx = readGChar() - MidPoint).
        GServer-v2 itself treats these as a client-trust artifact and never
        parses them server-side (it just relays the raw packet to the
        baddy's leader) - pygserver is authoritative for baddy damage
        (BaddyManager.handle_baddy_hurt already computes its own knockback
        direction from baddy/player position), so hurt_dx/hurt_dy are parsed
        here and intentionally dropped rather than fed into knockback.

        Backward tolerance: older pyReborn builds sent the legacy 2-field
        [baddy_id][damage] payload with no knockback fields - fall back to
        that when the packet is too short for the 4-field format.
        """
        if not self.level:
            return
        reader = PacketReader(data)
        if len(data) >= 4:
            baddy_id = reader.read_gchar()
            hurt_dx = reader.read_gchar_signed() - 64  # noqa: F841 (parsed, unused - see docstring)
            hurt_dy = reader.read_gchar_signed() - 64  # noqa: F841
            damage = reader.read_gchar()
        else:
            logger.debug(
                f"PLI_BADDYHURT: {len(data)}-byte packet too short for the "
                "4-field format, falling back to legacy [id][damage]"
            )
            baddy_id = reader.read_gchar()
            damage = reader.read_gchar() if reader.remaining() else 1

        if hasattr(self.server, 'baddy_manager'):
            await self.server.baddy_manager.handle_baddy_hurt(self, baddy_id, damage)

    @handles(PLI.BADDYADD)
    async def _handle_baddy_add(self, data: bytes):
        """Handle {x*2}{y*2}{type}{power in half-hearts}{image=rest}."""
        if not self.level:
            return

        reader = PacketReader(data)
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0
        baddy_type = reader.read_gchar()
        power = min(reader.read_gchar(), 12)
        image = strip_control_chars(
            reader.remaining().decode('latin-1', errors='replace'))
        if image and not Path(image).suffix:
            image += ".gif"

        if hasattr(self.server, 'baddy_manager'):
            from ..baddy import BaddyType
            baddy = await self.server.baddy_manager.add_baddy(
                self.level, x, y, BaddyType(baddy_type),
                respawn_enabled=False,
                power=power,
                image=image,
            )

    @handles(PLI.PUTNPC)
    async def _handle_putnpc(self, data: bytes):
        """Create a level NPC from {image}{scriptfile}{x*2}{y*2}."""
        if (not self.level or getattr(self.server.config, 'serverside', True)
                or not getattr(self.server.config, 'putnpc_enabled', False)):
            return

        reader = PacketReader(data)
        image = strip_control_chars(reader.read_gstring())
        scriptfile = reader.read_gstring()
        x = reader.read_gchar() / 2.0
        y = reader.read_gchar() / 2.0

        filesystem = getattr(self.server, 'filesystem', None)
        manager = getattr(self.server, 'npc_manager', None)
        # The name is client-supplied, so it must be a bare name resolved in
        # a public folder: never a path, which would let a client broadcast
        # accounts/ or config/ files as NPC code.
        script_path = (find_putnpc_script(filesystem, scriptfile)
                       if filesystem else None)
        if script_path is None or manager is None:
            return

        code = script_path.read_text(encoding='latin-1').replace('\r', '')
        npc = manager.create_npc(level=self.level, x=x, y=y)
        npc.image = image
        manager.attach_gs1(npc, code)
        await self.server.broadcast_to_level(
            self.level.name, npc.build_props_packet()
        )

    @handles(PLI.NPCWEAPONDEL)
    async def _handle_npc_weapon_del(self, data: bytes):
        """Handle PLI_NPCWEAPONDEL packet."""
        reader = PacketReader(data)
        weapon_name = reader.remaining().decode('latin-1', errors='replace')

        if weapon_name in self.weapons:
            self.weapons.remove(weapon_name)

    @handles(PLI.WEAPONADD)
    async def _handle_weapon_add(self, data: bytes):
        """Handle PLI_WEAPONADD packet (client requesting to add weapon)."""
        # PlayerClientPackets.cpp:890-945: every form is binary and mode-gated.
        if getattr(self.server.config, 'serverside', True):
            return
        reader = PacketReader(data)
        kind = reader.read_gchar()
        manager = getattr(self.server, 'gs2_manager', None)
        if kind == 0:
            # Player.cpp:1087-1092: default weapons are gated by this option.
            if not getattr(self.server.config, 'defaultweapons', True):
                return
            item_id = reader.read_gchar_signed()
            try:
                name = LevelItemType(item_id).name.lower()
            except ValueError:
                return
            if item_id < 0:
                return
            self.add_weapon(name)
            await self.send_raw(build_default_weapon(item_id))
            return
        npc = self.server.npc_manager.get_npc(reader.read_gint3())
        if npc is None or npc.level is None or manager is None:
            return
        # NPC.cpp:2120-2144 extracts the first literal toweapons argument.
        source = npc.gs1_source
        start = source.find('toweapons')
        if start < 0:
            return
        start += len('toweapons')
        ends = [pos for delimiter in (';', '}')
                if (pos := source.find(delimiter, start)) >= 0]
        if not ends:
            return
        name = source[start:min(ends)].strip()
        if not name:
            return
        if has_control_chars(name):
            return
        old = manager.get_weapon(name)
        # PlayerClientPackets.cpp:922-935: a missing weapon is created; an
        # existing one is replaced only when it is older than the level the
        # NPC sits in, so touching the NPC cannot undo a newer (e.g. NC) edit.
        if old is None or old.mod_time < _level_mod_time(npc.level):
            weapon = manager.upsert_classic_weapon(name, npc.image, source)
        else:
            weapon = old
        if old is not None and old is not weapon:
            for other in self.server.players.values():
                if name in other.weapons:
                    await manager.announce_weapon(other, name)
        self.add_weapon(name)
        await manager.announce_weapon(self, name)

    @handles(PLI.NPCPROPS)
    async def _handle_npc_props(self, data: bytes):
        # PlayerClientPackets.cpp:189-210: same level, no leader/owner test.
        if getattr(self.server.config, 'serverside', True):
            return
        reader = PacketReader(data)
        npc = self.server.npc_manager.get_npc(reader.read_gint3())
        if npc is None or npc.level is not self.level:
            return
        from reborn_protocol.props import NPC_PROPS, decode_value, payload_len
        raw = reader.remaining()
        updates = {}
        position = 0
        while position < len(raw):
            prop_id = raw[position] - 32
            position += 1
            descriptor = NPC_PROPS.get(prop_id)
            if descriptor is None:
                await self.disconnect('Corrupted packet received.')
                return
            width = payload_len(descriptor, raw, position, colors_len=8)
            if position + width > len(raw):
                await self.disconnect('Corrupted packet received.')
                return
            value, position = decode_value(descriptor, raw, position, colors_len=8)
            # Choke point for client-settable NPC strings (see text_safety).
            updates.update(npc.apply_client_prop(prop_id, sanitize_value(value)))
        if updates:
            await self.server.broadcast_to_world(
                npc.level.name, build_npc_props(npc.id, updates), exclude={self.id})

    @handles(PLI.NPCDEL)
    async def _handle_npc_del(self, data: bytes):
        # PlayerClientPackets.cpp:783-794: no level, ownership or staff test.
        if getattr(self.server.config, 'serverside', True):
            return
        npc = self.server.npc_manager.get_npc(PacketReader(data).read_gint3())
        if npc is None:
            return
        level = npc.level
        await self.server.npc_manager.destroy_npc(npc, notify=False)
        if level is not None:
            # Server.cpp:1610-1638 also invalidates copies outside this level.
            for other in list(self.server.players.values()):
                if not other.connection_type & PLTYPE.ANYCLIENT:
                    continue
                packet = (build_npc_del(npc.id) if other.level is level else
                          build_npc_del2(level.name, npc.id))
                await other.send_raw(packet)
