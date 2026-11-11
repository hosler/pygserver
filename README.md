# pygserver - Python Game Server for Reborn

A Python implementation of the Reborn game server, supporting v6.037 protocol with Python NPC scripting.

## Features

- **v6.037 Protocol Support**: Full ENCRYPT_GEN_5 encryption/compression
- **Python NPC Scripting**: Write NPC behaviors in Python
- **Asyncio-based**: Efficient async networking
- **Compatible with pyReborn**: Connect with the pyReborn pygame client
- **List Server Integration**: Optional registration with Reborn list server for:
  - Server discovery and player browsing
  - Account verification (when enabled)
  - Cross-server messaging
  - Automatic reconnection with exponential backoff

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
├── __main__.py           # `python -m pygserver` entry point
├── server.py             # Main GameServer class (asyncio)
├── player.py             # Player connection handling
├── level.py              # Level loading and management
├── npc.py                # NPC system with Python scripting
├── npc_motion.py         # NPC move queue (GS1 `move`, PLO_MOVE/MOVE2)
├── weapon.py             # Weapon definitions
├── gs2.py                # GS2 clientside bytecode (compile on load, serve)
├── world.py              # World/GMAP management
├── config.py             # Server configuration
├── listserver.py         # List server client (registration/verification)
├── combat.py             # Combat system (bombs, arrows, damage)
├── items.py              # Item system (ground items, chests)
├── baddy.py              # Baddy/enemy system
├── horse.py              # Horse/mount system
├── rc.py                 # RC (Remote Control) admin system
├── nc.py                 # NC (NPC Control) system
├── filesystem.py         # File serving system
├── account.py            # Account management and persistence
└── protocol/
    ├── packets.py        # Packet builders (uses reborn-protocol)
    ├── codec.py          # Re-export shim → reborn_protocol.codec
    ├── constants.py      # Re-export shim → reborn_protocol.constants
    └── encryption.py     # Re-export shim → reborn_protocol.encryption
```

The server uses the shared `reborn-protocol` library for:
- Encryption/codec (ENCRYPT_GEN_5, packet framing)
- Protocol constants (PLI/PLO/SVI/SVO packet IDs, PLPROP/NPCPROP property IDs)

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
# Server Identity
name = My Server
description = A Python-powered game server
language = English
url = http://www.example.com/

# Network
serverport = 14900
serverip = AUTO
localip = AUTO

# List Server (Optional)
listip = listserver.graal.in
listport = 14900
# Set enable_listserver = true in Python config to activate

# Access Control
staff = admin,moderator
noverifylogin = true

# Starting Location
startlevel = onlinestartlocal.nw
startx = 30
starty = 30.5

# Limits
maxplayers = 100
```

### Gameplay options

Each is read from `serveroptions.txt` with the reference server's name and
default, and written back by `ServerConfig.to_file`:

| Option | Default | Effect |
|---|---|---|
| `baddyitems` | `false` | A dying baddy drops an item (`LevelBaddy.cpp:284`) |
| `lockplayerz` | `false` | Client Z changes are rejected (`PlayerProps.cpp:495-500`) |
| `defaultweapons` | `true` | Pickups may add the built-in weapons (`Server.h:165`); off, they are a no-op |
| `putnpcenabled` | `false` | Allows the client `PLI_PUTNPC` packet |
| `servermode` | `normal` | `kingdoms` sends PLO 176, `newworld` sends PLO 177, right at the head of every client/RC/NC login (`Player.cpp:746-752`); `normal` sends neither. `newworld` also widens the COLORS prop to 8 bytes. Read once at start-up (`Server.cpp:194-195`): an RC write is saved but takes effect at the next start |
| `generation` | unset | Unset or `modern`: a level NPC whose client half is GS2 and compiles ships it as PLO_NPCBYTECODE (131) after its props, with the SCRIPT prop zeroed and CLASS listing its joined classes (`Level.cpp:1473-1480`). `classic` / `newmain`: text SCRIPT only, never 131. A GS1 or uncompilable client half always stays text |
| `default_scriptengine`, `_client`, `_server` | unset | Engine (`gs1` / `gs2`) for a script half with no `//#GS1` / `//#GS2` first line; the first line wins, then the specific option, then the generic one. Unset: pygserver sniffs the source shape |
| `playerswordganis` | `sword` | Comma list of gani names that make the server fire `washit` on NPCs inside the sword rectangle in front of the player (`PlayerProps.cpp:373`); empty turns it off. One swing fires one `washit` even when the client also reports the hit |
| `onlystaff` | `false` | Every login whose account is not in `staff` is refused with "This server is currently restricted to staff only." (`Player.cpp:701-706`; RC and NC keep their own staff-and-IP gate), and the list server is told HQ level 0 (`ServerList.cpp:276,479`); an RC edit re-sends it |
| `dontaddserverflags` | `false` | A client `PLI_FLAGSET` / `PLI_FLAGDEL` on a flag whose name contains `server.` is swallowed (`PlayerClientPackets.cpp:663-668,705-710`); only matters with `serverside = false`, as clients never reach server flags otherwise |
| `protectedweapons` | empty | Comma list of weapons added at login when missing and refused for client `PLI_NPCWEAPONDEL` (`PlayerClient.cpp:605-610`, `PlayerClientPackets.cpp:883`); names compare case-insensitively with edge whitespace ignored, and an entry naming no known weapon grants nothing |
| `disableitemdropping` | `false` | A client `PLI_ITEMADD` is answered with `PLO_ITEMDEL` and spawns nothing (`PlayerClientPackets.cpp:352-354`) |
| `noexplosions` | `false` | A client `PLI_EXPLOSION` is dropped (`PlayerClientPackets.cpp:845`) |
| `sendplayertriggers` | `true` | Off keeps client triggeractions from being relayed to the level; NPC events still run (`PlayerClientPackets.cpp:1310`) |
| `baddyrespawntime` | `60` | Seconds a dead baddy waits before respawning (`LevelBaddy.cpp:270,302`) |
| `respawntime` | `15` | Seconds until a 2x2 board change (a cut bush, a broken vase) is restored and the level is told (`Server.h:134`, `Level.cpp:1668-1672`, `LevelBoardChange.cpp:39-50`); `0` never restores. Other changes stay until the level is reloaded. A player entering the level is sent the base board and then every change in order. Client board changes never reach the level file (`Level.cpp:999-1030` saves the base tiles); on a gmap they, and their respawn, also reach the eight surrounding segments. Not in the reference: a level holds at most 1024 changes / 8192 changed tiles; at the bound respawned changes that no longer differ from the base are dropped first, otherwise the new change is refused (see `board_changes.py`) |
| `dropitemsdead` | `true` | A player whose STATUS prop goes to dead drops gralats, arrows and bombs where he stands, and the death counter rises (`PlayerClient.cpp:1898-1998`, `PlayerProps.cpp:574-580`); a sparring zone does neither. Gralats, arrows and bombs are deducted first, so with no rupee type allowed the gralats are simply lost |
| `mindeathgralats`, `maxdeathgralats` | `1`, `50` | Range of gralats dropped (capped by what the player holds); max `0` drops none. A min above max is treated as equal to max, where the reference leaves it undefined |
| `deathitemtypes` | `greenrupee,bluerupee,redrupee,goldrupee,bombs,darts` | Item names (or numbers) a death may drop; without `darts` / `bombs` no arrows / bombs go (`Server.cpp:624-635`) |
| `bushitems` | `true` | Cutting bushes, grass or swamp may lay an item (`PlayerClientPackets.cpp:112,154`); clients below 2.1 lay their own |
| `bushitemtypes` | `greenrupee,bluerupee,heart,bombs` | Items one cut rolls through, in order; rate is `spawnrate<item>`, defaulting to greenrupee 25, bluerupee 2, bombs 5, heart 5, others 0, summed cumulatively over a roll of 1-100 (`Server.cpp:603-621,1438-1453`). The `spawnrate` options are read only when `bushitemtypes` is set, as in the reference |
| `spawnrate<item>` | see above | Percent chance for that item in the bush roll |
| `vasesdrop` | `true` | Breaking a vase always lays a heart (`PlayerClientPackets.cpp:159`) |
| `enableexbodycolors` | `false` | Lets body colour values run past the 20 classic colours up to 170 when `generation` is modern or unset (`PropertySerializers.h:798-804`) |

Item drops go to every client in the level segment the item lands in, including
the one that caused them. Unlike the reference, a level holds at most 100 ground
items; that cap is the only bound on how many items a stream of cut packets can
lay, because the drop roll tests the level's base tile, which board changes
never rewrite, so every cut rolls again (`PlayerClientPackets.cpp:144-148`).
Level items expire server-side without a packet (`Level.cpp:711-716`); clients
run their own timer and answer with `PLI_ITEMDEL`, which is relayed to the
level's other players and removes the item if it is still there.
A number in `serveroptions.txt` that is not plain digits reads as 0 for
`mindeathgralats`, `maxdeathgralats`, `baddyrespawntime`, `respawntime` and
`spawnrate<item>`, with one warning in the log (`Settings.h:202-221`).
`itemdropevents`, `itemdropeventsonlyforgralats` and the item-NPC substitution
that needs an NPC server are not implemented.

### RC and NC Access

An RC, RC2 or NC login is admitted only when both hold (as on the reference
server, GServer-v2 `Player.cpp:685-694`):

1. the account is listed in the `staff` option (case-insensitive), and
2. the connecting socket address matches one entry of the account's
   admin-IP range: comma-separated, `*` matches any run and `?` one
   character, no spaces. An empty range, and the `0.0.0.0` every new account
   starts with, admit nobody. IPv4-mapped IPv6 peers match as plain IPv4.

Staff membership grants no rights. Every RC action checks only the rights
stored on the account (`PLPERM` bits). A staff member with no rights can log
in but cannot do anything. The NC login does not check the NPC-control right.
If an RC user removes that right from an account, the server drops the live
NC session of that account.

To grant access by hand, stop the server and edit `accounts/<name>.json`:

```json
"admin_rights": 1048575,
"admin_ip": "127.0.0.1,192.168.1.*"
```

`1048575` (`0xFFFFF`) is every right. Once one account can log in with the
set-rights right, use RC's player-rights editor for the rest.

### Files clients can download

`PLI_WANTFILE` and `PLI_UPDATEFILE` look a file up by name alone,
case-insensitively, under the public folders and search them recursively:
`world/` (the reference's file system root, GServer-v2 `Server.cpp:491-530`),
`levels/ gani/ ganis/ images/ sounds/ bodies/ heads/ swords/ shields/ hats/
global/`, and the first folder of every `config/foldersconfig.txt` pattern.
`accounts/`, `config/`, `logs/`, `weapons/`, `npcs/`, `scripts/`, `guilds/`,
`serverflags.txt` and anything outside the server folder are never served, and
a symlink that leaves its folder is refused.

### List Server Setup

To enable list server registration:

```python
from pygserver import GameServer, ServerConfig

config = ServerConfig.from_file("serveroptions.txt")
config.enable_listserver = True  # Enable registration
config.hq_password = "your-hq-password"  # Optional HQ password
config.hq_level = 1  # HQ access level (1-3)

server = GameServer(config)
```

When enabled, the server will:
- Register with the list server on startup
- Send player join/leave notifications
- Support account verification (if `verify_login = True`)
- Automatically reconnect if disconnected

## GS2 clientside scripting

Weapons in `weapons/weapon<name>.txt` (GRAWP001 format) and script classes in
`scripts/<name>.txt` get their `//#CLIENTSIDE` half compiled to GS2 bytecode at
boot and served to clients on `PLI_UPDATESCRIPT` / `PLI_UPDATECLASS`:

- **Compiler**: the real GServer-v2 `gs2test` binary, found via `$GS2TEST_BIN`,
  the sibling `reborn-protocol/tests/tools/gs2test`, or `PATH`. Compiled output
  is cached beside the source as `<name>.gs2bc`, which doubles as a
  precompiled-input path when no compiler is installed.
- **No compiler, no `.gs2bc`**: scripts simply have no bytecode and the server
  keeps serving classic GS1 text exactly as before.
- **Serverside** stays Python/GS1 — only the clientside half is compiled.

`cd pyReborn && python -m game_tester --gs2` exercises this end to end.

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
