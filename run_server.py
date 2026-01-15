#!/usr/bin/env python3
"""
pygserver - Example server runner

Usage:
    python run_server.py [server_dir]

Examples:
    python run_server.py                    # Run with default config
    python run_server.py ../funtimes        # Run with funtimes server
    python run_server.py /path/to/server    # Run with absolute path

Or:
    python -m pygserver [server_dir]
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent to path for development
sys.path.insert(0, str(Path(__file__).parent))

from pygserver.config import ServerConfig
from pygserver.server import GameServer


async def main():
    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )

    # Load config
    server_dir = sys.argv[1] if len(sys.argv) > 1 else None

    if server_dir:
        server_path = Path(server_dir)
        if server_path.is_dir():
            # Load from server directory (like funtimes/)
            config = ServerConfig.from_server_dir(server_dir)
        elif server_path.is_file():
            # Load from config file
            config = ServerConfig.from_file(server_dir)
        else:
            print(f"Error: {server_dir} not found")
            sys.exit(1)
    else:
        config = ServerConfig()
        # Default development settings
        config.name = "pygserver Development"
        config.verify_login = False

    # Ensure directories exist
    for dir_name in [config.levels_dir, config.npcs_dir, config.accounts_dir]:
        Path(dir_name).mkdir(exist_ok=True)

    # Create example level if none exist
    levels_path = Path(config.levels_dir)
    if not list(levels_path.glob("*.nw")):
        create_example_level(levels_path / config.start_level)

    # Create example NPC script
    npcs_path = Path(config.npcs_dir)
    example_npc = npcs_path / "example_npc.py"
    if not example_npc.exists():
        create_example_npc_script(example_npc)

    print(f"""
========================================
      pygserver - Python Reborn Server
========================================
  Name:     {config.name}
  Port:     {config.port}
  Levels:   {config.levels_dir}
  NPCs:     {config.npcs_dir}
  Protocol: v6.037 (ENCRYPT_GEN_5)
========================================

Connect with pyReborn:
  python -m pyreborn.example_pygame <user> <pass> localhost {config.port}

Press Ctrl+C to stop the server.
""")

    # Create and start server
    server = GameServer(config)
    try:
        await server.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await server.stop()


def create_example_level(path: Path):
    """Create a simple example level file."""
    # Create a minimal NW level file
    # Format: GLEVNW01 header + BOARD section + tiles

    tiles = bytearray(8192)  # 64x64 tiles, 2 bytes each

    # Fill with grass (tile 0)
    for i in range(4096):
        tiles[i * 2] = 0
        tiles[i * 2 + 1] = 0

    # Add some border walls (tile 22 = blocking)
    for x in range(64):
        # Top row
        idx = x * 2
        tiles[idx] = 22
        tiles[idx + 1] = 0
        # Bottom row
        idx = (63 * 64 + x) * 2
        tiles[idx] = 22
        tiles[idx + 1] = 0

    for y in range(64):
        # Left column
        idx = (y * 64) * 2
        tiles[idx] = 22
        tiles[idx + 1] = 0
        # Right column
        idx = (y * 64 + 63) * 2
        tiles[idx] = 22
        tiles[idx + 1] = 0

    # Write level file
    with open(path, 'wb') as f:
        # Header
        f.write(b'GLEVNW01')
        # Board section
        f.write(b'BOARD ')
        f.write(tiles)
        # End
        f.write(b'\n')

    print(f"Created example level: {path}")


def create_example_npc_script(path: Path):
    """Create an example NPC script."""
    script = '''"""
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
'''

    with open(path, 'w') as f:
        f.write(script)

    print(f"Created example NPC script: {path}")


if __name__ == "__main__":
    asyncio.run(main())
