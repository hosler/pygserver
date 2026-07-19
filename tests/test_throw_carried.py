"""Server-authoritative carried-object throw regressions."""

import asyncio
from unittest.mock import MagicMock

import pytest

from pygserver.combat import CarryObjectSprite, CombatManager
from pygserver.level import Level
from pygserver.npc import NPCManager
from pygserver.player import Player
from pygserver.protocol.constants import PLPROP


class FakeServer:
    def __init__(self):
        self.npc_manager = NPCManager(self)
        self.combat_manager = CombatManager(self)
        self.broadcasts = []

    async def broadcast_to_level(self, name, packet, exclude=None):
        self.broadcasts.append((name, packet, exclude))


class FakePlayer:
    def __init__(self, server, level):
        self.server = server
        self.level = level
        self.id = 1
        self.x = 10.0
        self.y = 10.0
        self.direction = 3
        self.carrysprite = CarryObjectSprite.BUSH
        self.npc_id = 0
        self.flags = {}


def test_throw_lands_after_five_server_iterations():
    async def main():
        server = FakeServer()
        level = Level("throw.nw")
        player = FakePlayer(server, level)
        calls = 0
        original = server.npc_manager.get_npcs_on_level

        def counted(current_level):
            nonlocal calls
            calls += 1
            return original(current_level)

        server.npc_manager.get_npcs_on_level = counted
        await server.combat_manager.handle_throw_carried(
            player, player.direction, player.carrysprite
        )
        assert calls == 5  # two client frames per 0.1-second server iteration
        assert server.broadcasts[0][2] == {player.id}

    asyncio.run(main())


@pytest.mark.parametrize("sprite,flag", [
    (CarryObjectSprite.BUSH, "bush"),
    (CarryObjectSprite.STONE, "stone"),
    (CarryObjectSprite.VASE, "vase"),
    (CarryObjectSprite.SIGN, "sign"),
    (CarryObjectSprite.BLACKSTONE, "blackstone"),
    (CarryObjectSprite.NPC, "npc"),
])
def test_throw_hits_npc_and_scopes_pelt_flag(sprite, flag):
    async def main():
        server = FakeServer()
        level = Level("throw.nw")
        player = FakePlayer(server, level)
        # First rightward iteration moves the 2x2 box to x=12.2.
        target = server.npc_manager.create_npc(level=level, x=12.5, y=11)
        server.npc_manager.attach_gs1(
            target,
            f"if (waspelt) {{ this.hit = 1; this.right = peltwith{flag}; }}",
        )
        await server.combat_manager.handle_throw_carried(player, 3, sprite)
        assert target.hearts == 2.0
        assert target.gs1_scopes["this"]["hit"] == 1.0
        assert target.gs1_scopes["this"]["right"] == 1.0

    asyncio.run(main())


def test_carried_npc_moves_nine_tiles_and_fires_wasthrown():
    async def main():
        server = FakeServer()
        level = Level("throw.nw")
        player = FakePlayer(server, level)
        carried = server.npc_manager.create_npc(level=level, x=0, y=0)
        server.npc_manager.attach_gs1(
            carried, "if (wasthrown) { this.thrown = 1; }"
        )
        player.npc_id = carried.id
        await server.combat_manager.handle_throw_carried(
            player, 3, CarryObjectSprite.NPC
        )
        assert server.combat_manager._thrown_npc_tasks
        await asyncio.gather(*server.combat_manager._thrown_npc_tasks)
        assert carried.x == pytest.approx(player.x + 9.0)
        assert carried.y == pytest.approx(player.y)
        assert carried.gs1_scopes["this"]["thrown"] == 1.0

    asyncio.run(main())


def test_carrysprite_player_property_is_tracked():
    async def main():
        server = MagicMock()
        server.npc_manager = None
        player = Player(server, 1, MagicMock(), MagicMock())
        payload = bytes((int(PLPROP.CARRYSPRITE) + 32,
                         int(CarryObjectSprite.BLACKSTONE) + 32))
        await player._handle_player_props(payload)
        assert player.carrysprite == CarryObjectSprite.BLACKSTONE

    asyncio.run(main())
