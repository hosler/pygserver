"""
pygserver.level_cache - the per-player record of levels left this session

The reference keeps, per client, the time it last LEFT each level
(PlayerClient::m_cachedStaticLevels / m_cachedDynamicLevels,
GServer-v2 server/include/player/PlayerClient.h:63-68,219-220). Despite the
reference's `lastEnteredTime` name, only leaving writes it
(PlayerClient::leaveSubLevel, PlayerClient.cpp:1406-1444); entering writes
nothing. It answers two questions on the next entry:

- static: "does the client already hold this board?" A level with no record
  always gets its board (PlayerClient.cpp:1471);
- dynamic: "what changed since the player left?" NPC props and move-queue
  entries stamped at or after the leave time go out (Level.cpp:1466-1488).

pygserver loads one Level per level file, a gmap segment included, so one
record keyed by level name serves both. The record lives as long as the
connection (cleared in Player._cleanup, PlayerClient.cpp:184-185) and is reset
for every player when the level is reloaded (Level.cpp:952-970).
"""

import time
from typing import Dict, Optional


def clock() -> float:
    """The time stamped into the record, NPC props and moves: wall-clock
    seconds with sub-second precision, like the reference's per-frame
    system_clock (Server.cpp:358). Tests replace this module attribute."""
    return time.time()


class VisitedLevels:
    """Level name -> the time the player last left it."""

    def __init__(self):
        self._left: Dict[str, float] = {}

    def record_leave(self, level_name: str, when: Optional[float] = None) -> float:
        when = clock() if when is None else when
        self._left[level_name] = when
        return when

    def left_at(self, level_name: str) -> Optional[float]:
        """When the player last left `level_name`, None if it has not."""
        return self._left.get(level_name)

    def visited(self, level_name: str) -> bool:
        return level_name in self._left

    def reset(self, level_name: str) -> None:
        """Forget one level (resetLevelCache, PlayerClient.cpp:1674-1706)."""
        self._left.pop(level_name, None)

    def clear(self) -> None:
        self._left.clear()


def reset_for_all_players(server, level_name: str) -> None:
    """A reloaded level is new to every client on the server
    (Level::reload, Level.cpp:962-968)."""
    for player in list(getattr(server, 'players', {}).values()):
        record = getattr(player, 'visited_levels', None)
        if record is not None:
            record.reset(level_name)
