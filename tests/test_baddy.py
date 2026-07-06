"""Unit tests for pygserver baddy module (classic per-type stats/image,
walk-cycle animation, and level-file verse strings).

Includes a round-trip test against a hand-parsed chicken4.nw BADDY block:
spawns via the real BaddyManager.add_baddy() path and decodes the resulting
PLO_BADDYPROPS wire bytes with an inline replica of pyReborn's
parse_baddy_props (see pyReborn/pyreborn/packets.py) rather than importing
the sibling repo, per repo boundary.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../reborn-protocol'))

from unittest.mock import AsyncMock, MagicMock

from pygserver.baddy import Baddy, BaddyManager, BaddyType
from pygserver.level import Level

WORLD_DIR = os.path.join(
    os.path.dirname(__file__), '..', '..', 'funtimes-pygserver', 'world')


# GServer-v2 LevelBaddy.cpp baddyPower/baddyImages tables (types 0-9 — the
# only ones a level file can spawn, see level.BADDY_NAME_TO_TYPE).
GSERVER_POWER = {0: 2, 1: 3, 2: 4, 3: 3, 4: 2, 5: 1, 6: 1, 7: 6, 8: 12, 9: 8}
GSERVER_IMAGE = {
    0: "baddygray.png", 1: "baddyblue.png", 2: "baddyred.png",
    3: "baddyblue.png", 4: "baddygray.png", 5: "baddyhare.png",
    6: "baddyoctopus.png", 7: "baddygold.png", 8: "baddylizardon.png",
    9: "baddydragon.png",
}


def decode_baddy_props(data: bytes) -> dict:
    """Inline replica of pyReborn's parse_baddy_props (packets.py) covering
    the prop ids this module emits: ID/X/Y/TYPE/POWERIMAGE/MODE/ANI/DIR/
    VERSESIGHT/VERSEHURT. `data` is the packet body *after* the leading
    PLO_BADDYPROPS gchar (i.e. starts at the baddy id gchar)."""
    props = {}
    pos = 0
    props['id'] = data[pos] - 32
    pos += 1
    while pos < len(data):
        prop_id = data[pos] - 32
        pos += 1
        if prop_id == 1:
            props['x'] = (data[pos] - 32) / 2.0
            pos += 1
        elif prop_id == 2:
            props['y'] = (data[pos] - 32) / 2.0
            pos += 1
        elif prop_id == 3:
            props['type'] = data[pos] - 32
            pos += 1
        elif prop_id == 4:
            power = data[pos] - 32
            pos += 1
            str_len = data[pos] - 32
            pos += 1
            image = data[pos:pos + str_len].decode('latin-1')
            pos += str_len
            props['power'] = power
            props['image'] = image
        elif prop_id == 5:
            props['mode'] = data[pos] - 32
            pos += 1
        elif prop_id == 6:
            props['animation'] = data[pos] - 32
            pos += 1
        elif prop_id == 7:
            props['direction'] = (data[pos] - 32) & 0x03
            pos += 1
        elif prop_id in (8, 9, 10):
            str_len = data[pos] - 32
            pos += 1
            text = data[pos:pos + str_len].decode('latin-1')
            pos += str_len
            props[{8: 'versesight', 9: 'versehurt', 10: 'verseattack'}[prop_id]] = text
        else:
            # default: single byte (matches pyReborn's parse_baddy_props
            # fallback for unhandled prop ids, e.g. the BDPROP.ID entry
            # that's also echoed inside the props list)
            pos += 1
    return props


class TestBaddyDefaultsMatchGServer:
    """Problem A: default image + power (health) per type must match
    GServer-v2's LevelBaddy.cpp baddyImages/baddyPower tables."""

    @pytest.mark.parametrize("type_id", range(10))
    def test_default_image_matches_gserver(self, type_id):
        baddy = Baddy(id=1, level_name="test.nw", baddy_type=BaddyType(type_id), x=1, y=1)
        assert baddy.image == GSERVER_IMAGE[type_id]

    @pytest.mark.parametrize("type_id", range(10))
    def test_default_health_matches_gserver_power(self, type_id):
        baddy = Baddy(id=1, level_name="test.nw", baddy_type=BaddyType(type_id), x=1, y=1)
        assert baddy.health == GSERVER_POWER[type_id]
        assert baddy.max_health == GSERVER_POWER[type_id]

    def test_spawn_broadcasts_type_and_powerimage(self):
        packet = Baddy(id=1, level_name="t.nw", baddy_type=BaddyType.SPIDER,
                        x=5, y=5).build_props_packet()
        decoded = decode_baddy_props(packet[1:-1])  # strip leading PLO byte + trailing newline
        assert decoded['type'] == 5
        assert decoded['power'] == 1
        assert decoded['image'] == "baddyhare.png"


class TestAniToggle:
    """Problem B: walk animation frame toggles 0/1 on ticks that move a baddy."""

    def test_move_towards_target_toggles_ani(self):
        async def main():
            server = MagicMock()
            server.broadcast_to_level = AsyncMock()
            manager = BaddyManager(server)
            baddy = Baddy(id=1, level_name="t.nw", baddy_type=BaddyType.GRAYBALL, x=0, y=0)
            assert baddy.ani == 0

            await manager._move_towards_target(baddy, 10.0, 10.0, delta_time=1.0)
            assert baddy.ani == 1

            await manager._move_towards_target(baddy, 10.0, 10.0, delta_time=1.0)
            assert baddy.ani == 0

        asyncio.run(main())

    def test_wander_toggles_ani(self):
        async def main():
            server = MagicMock()
            server.broadcast_to_level = AsyncMock()
            manager = BaddyManager(server)
            baddy = Baddy(id=1, level_name="t.nw", baddy_type=BaddyType.GRAYBALL, x=30, y=30)

            await manager._wander(baddy, delta_time=1.0)
            assert baddy.ani == 1

        asyncio.run(main())


class TestVerses:
    """Problem C: level-file verses are stored and sent once on first sighting."""

    def test_verses_sent_on_initial_broadcast_only(self):
        baddy = Baddy(id=1, level_name="t.nw", baddy_type=BaddyType.SPIDER, x=1, y=1,
                       verses=["Stop!", "Ouch!", ""])
        initial = decode_baddy_props(
            baddy.build_props_packet(include_verses=True)[1:-1])
        assert initial['versesight'] == "Stop!"
        assert initial['versehurt'] == "Ouch!"

        update = decode_baddy_props(baddy.build_props_packet()[1:-1])
        assert 'versesight' not in update
        assert 'versehurt' not in update


class TestChicken4RoundTrip:
    """Parse the real chicken4.nw BADDY block and spawn it via
    BaddyManager.add_baddy(), then decode the wire packet."""

    def test_chicken4_frog_baddy_round_trips(self):
        level_path = os.path.join(WORLD_DIR, "chicken4.nw")
        if not os.path.isfile(level_path):
            pytest.skip("funtimes-pygserver/world/chicken4.nw not present")
        level = Level.load(level_path)
        defs = level.get_baddy_defs()
        assert defs, "chicken4.nw should have at least one BADDY block"

        first = defs[0]
        assert first['type'] == 5  # frog (BADDY_NAME_TO_TYPE) == GServer FROG
        assert first['verses'][:2] == ["Stop!", "Ouch!"]

        async def main():
            server = MagicMock()
            server.broadcast_to_level = AsyncMock()
            manager = BaddyManager(server)

            baddy = await manager.add_baddy(
                level, first['x'], first['y'], BaddyType(first['type']),
                verses=first['verses'],
            )

            assert baddy.image == "baddyhare.png"
            assert baddy.health == 1

            packet = server.broadcast_to_level.call_args[0][1]
            decoded = decode_baddy_props(packet[1:-1])
            assert decoded['type'] == 5
            assert decoded['power'] == 1
            assert decoded['image'] == "baddyhare.png"
            assert decoded['versesight'] == "Stop!"
            assert decoded['versehurt'] == "Ouch!"

        asyncio.run(main())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
