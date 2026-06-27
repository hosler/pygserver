"""
Villager NPC for pygserver.

A player-like ambient character that wanders its home area, faces the way it
walks, and chats back when greeted. Used by the server's world-population step
to fill a level with life. Demonstrates the live-update NPC API:

    - set_character(head, body, colors) -> looks like a real player
    - set_nickname(name)                -> name floats above its head
    - x/y/direction/gani/say()          -> broadcast to nearby players each tick
"""

import random

# A few palettes (8 color indices each: skin, coat, sleeves, shoes, belt, ...).
_PALETTES = [
    [2, 13, 8, 18, 1, 0, 0, 0],
    [2, 20, 19, 4, 7, 0, 0, 0],
    [3, 5, 5, 18, 11, 0, 0, 0],
    [1, 28, 27, 2, 9, 0, 0, 0],
]

_NAMES = [
    "Aldric", "Bryn", "Cora", "Doran", "Elsie",
    "Finn", "Greta", "Hollis", "Ivo", "Juna",
]

_GREETINGS = [
    "Fine day, isn't it?",
    "Mind the baddies south of here.",
    "Welcome, traveler.",
    "I've lived here all my life.",
    "Heard there's treasure in the caves.",
]


class VillagerNPC:
    """An ambient, wandering, player-like villager."""

    HOME_RADIUS = 6  # how far it strays from its spawn tile

    def on_created(self, npc):
        # Pick a stable identity from the NPC id so each villager differs.
        seed = npc.id
        npc.set_nickname(_NAMES[seed % len(_NAMES)])
        npc.set_character(
            head=f"head{(seed % 20):d}.png",
            body="body.png",
            colors=_PALETTES[seed % len(_PALETTES)],
        )
        npc.set_ani("idle")

        # Remember home so we wander around it rather than off the map.
        npc.set_flag("home_x", str(npc.x))
        npc.set_flag("home_y", str(npc.y))

        npc.set_timer(self._next_delay())

    def on_timeout(self, npc):
        home_x = float(npc.get_flag("home_x") or npc.x)
        home_y = float(npc.get_flag("home_y") or npc.y)

        # 1-in-4 ticks the villager just stands and idles.
        if random.random() < 0.25:
            npc.set_ani("idle")
            npc.set_timer(self._next_delay())
            return

        # Step one tile in a random cardinal direction, staying near home.
        direction = random.randint(0, 3)
        dirs = {0: (0, -1), 1: (-1, 0), 2: (0, 1), 3: (1, 0)}
        dx, dy = dirs[direction]

        new_x = npc.x + dx
        new_y = npc.y + dy
        if abs(new_x - home_x) <= self.HOME_RADIUS:
            npc.x = max(1.0, min(62.0, new_x))
        if abs(new_y - home_y) <= self.HOME_RADIUS:
            npc.y = max(1.0, min(62.0, new_y))

        npc.face(direction)
        npc.set_ani("walk")

        # Occasionally mutter something.
        if random.random() < 0.10:
            npc.say(random.choice(_GREETINGS))

        npc.set_timer(self._next_delay())

    def on_player_enters(self, npc, player):
        name = getattr(player, "nickname", None) or "stranger"
        npc.say(f"Hello, {name}!")

    def on_player_chats(self, npc, player, message):
        msg = message.lower()
        if any(w in msg for w in ("hi", "hello", "hey")):
            npc.say(random.choice(_GREETINGS))
        elif "bye" in msg:
            npc.say("Safe travels!")
        elif "name" in msg:
            npc.say(f"They call me {npc.nickname}.")

    def _next_delay(self) -> float:
        """Randomized step interval so villagers don't move in lockstep."""
        return random.uniform(1.2, 2.8)
