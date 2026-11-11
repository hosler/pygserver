"""Wire width of the COLORS prop (PLPROP 13 / NPCPROP 19), set by `servermode`.

The reference never looks at the client version: PropertyColors::getColorCount
(PropertySerializers.h:793-796) is 8 only when the server runs
`servermode = newworld`, otherwise 5 (normal AND kingdoms). Every path that
sends a player or NPC COLORS prop (login, other-player props, RC player props,
GS commands, trigger actions) goes through that one serialize(), and the
player-prop reader (PLI_PLAYERPROPS, RC player-props set) uses the same count
(PropertySerializers.h:752, `onlyDeserialize5 == false`). Only the NPC form
reads a fixed 5 (`PropertyColors<true>`, NPC.h:646; NPC.cpp:1343-1350).

Storage is always 8 slots (Character.h:43 `std::array<uint8_t, 8> colors`).
A prop read builds a fresh zero-filled array, fills the slots the wire carried
and replaces the stored array wholesale (PlayerProps.cpp:453-460,
PlayerProps.cpp:1191-1192 constructPropFor -> make_shared, `values{}`), so the
slots a 5-byte read did not carry become 0. `normalize_colors` mirrors that.

Every value is clamped to the colour table size on write and read
(PropertySerializers.h:737-760, getMaxColorValue :798-804): 20 classic colours,
plus the 150 HTML colours when `generation` is modern (unset counts as modern
here, the same rule as gs2.py npc_bytecode_enabled) and `enableexbodycolors`
is on. The bound is the table size itself, as in the reference, so the
inclusive clamp lets the value equal the count.

New characters start from Character.h:43 `{2, 0, 10, 4, 18, 18, 18, 18}`; the
default account file (`COLORS 2,0,10,4,18`) overlays only its first five slots
(FlatFileAccountLoader.cpp:361-369), so slots 5-7 stay 18.

The mode is fixed when the server starts: Server.cpp:194-199 reads `servermode`
once ("This requires a full server restart to change"). GameServer copies it
into `server.server_mode` at construction and the width follows that copy, so
an RC write of the option (which only edits the live option store and the
file) cannot change the width mid-session and desync the props stream.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from reborn_protocol.props import COLORS_CLASSIC, COLORS_NEWWORLD

#: Number of stored colour slots (Character.h:43).
COLORS_STORED = COLORS_NEWWORLD

#: Table sizes behind the clamp (Character.h:157 ClassicColors::COUNT, :350 HTMLColors::COUNT).
CLASSIC_COLORS_COUNT = 20
HTML_COLORS_COUNT = 150

#: Starting colours of a new character (Character.h:43).
DEFAULT_COLORS = (2, 0, 10, 4, 18, 18, 18, 18)

#: Wire width of the NPC COLORS prop on READ (PropertyColors<true>).
NPC_COLORS_READ_WIDTH = COLORS_CLASSIC

_server: Optional[Any] = None


def normalize_server_mode(value) -> str:
    """The mode a `servermode` option value selects (Server.cpp:194-199):
    "kingdoms" or "newworld" (case-insensitive), anything else "normal"."""
    mode = str(value or "").strip().lower()
    return mode if mode in ("kingdoms", "newworld") else "normal"


def bind_server(server) -> None:
    """Remember which server's start-up mode answers colors_wire_width().

    The newest GameServer wins (one per process outside of tests). Its
    `server_mode` attribute is looked up per call; that attribute is set once
    at construction and never follows the option store.
    """
    global _server
    _server = server


def colors_wire_width(config=None) -> int:
    """5, or 8 when the server mode is newworld (PropertySerializers.h:793-796).

    With a `config` given, that config's `servermode` decides (for callers that
    build packets for a config of their own). Otherwise the bound server's
    start-up `server_mode` does; with no bound server this is the default-mode
    width, 5.
    """
    if config is not None:
        mode = normalize_server_mode(getattr(config, "servermode", ""))
    else:
        mode = normalize_server_mode(getattr(_server, "server_mode", ""))
    return COLORS_NEWWORLD if mode == "newworld" else COLORS_CLASSIC


def overlay_colors(values: Sequence[int]) -> List[int]:
    """Stored form for a loaded account: the reference starting colours with
    the given slots written over the front (FlatFileAccountLoader.cpp:361-369
    copies the file's tokens into a Character that already holds the
    defaults, so a short list keeps the default tail)."""
    slots = default_colors()
    count = min(len(values), COLORS_STORED) if isinstance(values, (list, tuple)) else 0
    slots[:count] = normalize_colors(values)[:count]
    return slots


def normalize_colors(values: Sequence[int]) -> List[int]:
    """The stored form: exactly COLORS_STORED slots, short input zero-padded.

    Also the result of a wire read, where the slots the wire did not carry end
    up 0 (module docstring).
    """
    slots: List[int] = []
    for value in (values if isinstance(values, (list, tuple)) else ())[:COLORS_STORED]:
        try:
            slots.append(int(value))
        except (TypeError, ValueError):
            slots.append(0)
    slots += [0] * (COLORS_STORED - len(slots))
    return slots



def default_colors() -> List[int]:
    """A fresh list of the reference starting colours (Character.h:43)."""
    return list(DEFAULT_COLORS)


def colors_max_value(config=None) -> int:
    """Largest colour value the wire carries (PropertySerializers.h:798-804)."""
    if config is None:
        config = getattr(_server, "config", None)
    generation = str(getattr(config, "generation", "") or "").strip().lower()
    if (generation in ("", "modern")
            and getattr(config, "enableexbodycolors", False) is True):
        return CLASSIC_COLORS_COUNT + HTML_COLORS_COUNT
    return CLASSIC_COLORS_COUNT


def clamp_colors(values: Sequence[int], config=None) -> List[int]:
    """Clamp every colour to 0..colors_max_value() (PropertySerializers.h:744,758).

    Raises TypeError/ValueError on a non-numeric entry, like the encoder does.
    """
    limit = colors_max_value(config)
    return [max(0, min(int(value), limit)) for value in values]
