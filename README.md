# pygserver - Python Game Server for Reborn

A Python implementation of the Reborn game server, supporting v6.037 protocol with Python NPC scripting.

## Features

- **v6.037 Protocol Support**: Full ENCRYPT_GEN_5 encryption/compression
- **Python NPC Scripting**: Write NPC behaviors in Python
- **Asyncio-based**: Efficient async networking
- **Compatible with pyReborn**: Connect with the pyReborn pygame client

## Quick Start

```bash
# Install
pip install -e .

# Run server
python run_server.py

# Or as a module
python -m pygserver
```

Then connect with pyReborn:
```bash
cd ../pyReborn
python -m pyreborn.example_pygame <username> <password> localhost 14900
```

## Project Structure

```
pygserver/
├── __init__.py           # Package exports
├── server.py             # Main GameServer class (asyncio)
├── player.py             # Player connection handling
├── level.py              # Level loading and management
├── npc.py                # NPC system with Python scripting
├── weapon.py             # Weapon definitions
├── world.py              # World/GMAP management
├── config.py             # Server configuration
├── protocol/
│   ├── encryption.py     # ENCRYPT_GEN_5 implementation
│   ├── codec.py          # Packet encoding/decoding
│   ├── constants.py      # Packet IDs (PLI_*, PLO_*)
│   └── packets.py        # Packet parsing/building
└── scripting/
    └── __init__.py       # Python scripting docs
```

## Python NPC Scripting

NPCs are scripted in Python. Create a `.py` file in the `npcs/` directory:

```python
class GreeterNPC:
    """A friendly NPC that greets players."""

    def on_created(self, npc):
        """Called when the NPC is created."""
        npc.image = "pics1.png"
        npc.x = 32.0
        npc.y = 32.0
        npc.say("Welcome!")
        npc.set_timer(5.0)

    def on_player_enters(self, npc, player):
        """Called when a player enters the level."""
        npc.say(f"Hello, {player.nickname}!")

    def on_player_chats(self, npc, player, message):
        """Called when a player chats."""
        if "hello" in message.lower():
            npc.say(f"Hi {player.nickname}!")

    def on_timeout(self, npc):
        """Called when timer expires."""
        npc.move(1, 0)  # Move right
        npc.set_timer(5.0)
```

### NPC Events

- `on_created(npc)` - NPC initialized
- `on_timeout(npc)` - Timer expired
- `on_player_enters(npc, player)` - Player entered level
- `on_player_leaves(npc, player)` - Player left level
- `on_player_chats(npc, player, message)` - Player sent chat
- `on_player_touches(npc, player)` - Player touched NPC

### NPC API

```python
# Properties
npc.id          # NPC ID
npc.name        # NPC name
npc.x, npc.y    # Position (tiles)
npc.direction   # Facing (0=up, 1=left, 2=down, 3=right)
npc.image       # Sprite image
npc.gani        # Animation name
npc.message     # Chat message
npc.flags       # Custom flags dict

# Methods
npc.move(dx, dy)              # Move by offset
npc.warp(level, x, y)         # Warp to location
npc.set_image(image)          # Set sprite
npc.set_ani(animation)        # Set animation
npc.set_timer(seconds)        # Set timeout
npc.say(text)                 # Show message
npc.hide() / npc.show()       # Visibility
npc.destroy()                 # Remove NPC
npc.get_flag(name)            # Get flag
npc.set_flag(name, value)     # Set flag
```

## Configuration

Create `serveroptions.txt`:

```ini
name = My Server
port = 14900
staff = admin,moderator
noverifylogin = true
startlevel = onlinestartlocal.nw
startx = 30
starty = 30.5
maxplayers = 100
```

## Protocol Support

- **Version**: 6.037 (G3D0311C)
- **Encryption**: ENCRYPT_GEN_5 (XOR cipher with compression)
- **Compression**: Dynamic (uncompressed/zlib/bz2)

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest
```

## Architecture

Based on GServer-v2 C++ architecture:

| C++ Class | Python Module | Purpose |
|-----------|---------------|---------|
| TServer | server.py | Main server loop |
| TPlayer | player.py | Player connections |
| TLevel | level.py | Level management |
| TNPC | npc.py | NPC system |
| TWeapon | weapon.py | Weapons |
| TMap | world.py | World/GMAP |

## License

MIT License - See LICENSE file.
