"""
Example NPC Script for pygserver

This NPC greets players and responds to chat.
"""


class GreeterNPC:
    """A friendly NPC that greets players."""

    def on_created(self, npc):
        """Called when the NPC is created."""
        npc.image = "pics1.png"
        npc.x = 32.0
        npc.y = 32.0
        npc.say("Welcome to pygserver!")
        print(f"GreeterNPC created at ({npc.x}, {npc.y})")

    def on_player_enters(self, npc, player):
        """Called when a player enters the level."""
        npc.say(f"Hello, {player.nickname}!")

    def on_player_chats(self, npc, player, message):
        """Called when a player sends a chat message."""
        message_lower = message.lower()

        if "hello" in message_lower or "hi" in message_lower:
            npc.say(f"Hi {player.nickname}!")
        elif "help" in message_lower:
            npc.say("Try exploring the level!")
        elif "bye" in message_lower:
            npc.say("Goodbye!")

    def on_timeout(self, npc):
        """Called when the timer expires."""
        pass  # Not using timer in this example


class WanderingNPC:
    """An NPC that wanders around randomly."""

    def on_created(self, npc):
        """Initialize the wandering NPC."""
        npc.image = "pics1.png"
        npc.x = 40.0
        npc.y = 40.0
        npc.set_timer(3.0)  # Move every 3 seconds

    def on_timeout(self, npc):
        """Move in a random direction."""
        import random
        dx = random.choice([-1, 0, 1])
        dy = random.choice([-1, 0, 1])

        # Keep within bounds (2-61 to avoid walls)
        new_x = max(2, min(61, npc.x + dx))
        new_y = max(2, min(61, npc.y + dy))

        npc.x = new_x
        npc.y = new_y

        # Update direction based on movement
        if dx > 0:
            npc.direction = 3  # Right
        elif dx < 0:
            npc.direction = 1  # Left
        elif dy > 0:
            npc.direction = 2  # Down
        elif dy < 0:
            npc.direction = 0  # Up

        npc.set_timer(3.0)  # Reset timer
