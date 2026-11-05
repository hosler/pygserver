"""Pure alignment point timers and PvP penalties."""

from typing import Optional, Sequence, Tuple


def ap_threshold(ap: int, aptimes: Sequence[int]) -> int:
    """Return the timer for the current AP band."""
    # GServer-v2/server/src/player/PlayerClient.cpp:252-261.
    for band, upper in enumerate((20, 40, 60, 80)):
        if ap < upper:
            return aptimes[band]
    return aptimes[4]


def regen_step(ap: int, ap_counter: int,
               aptimes: Sequence[int]) -> Tuple[int, int, bool]:
    """Advance one eligible second; changed describes AP, not the timer."""
    # GServer-v2/server/src/player/PlayerClient.cpp:245-261: reaching zero
    # consumes a tick; the next tick increments AP and uses the NEW AP band.
    if ap_counter > 0:
        return ap, ap_counter - 1, False
    new_ap = ap + 1 if ap < 100 else ap
    # Even at 100 AP the reference resets the counter, without sending AP.
    return new_ap, ap_threshold(new_ap, aptimes), new_ap != ap


def kill_penalty(killer_ap: int, victim_ap: int,
                 aptimes: Sequence[int]) -> Optional[Tuple[int, int]]:
    """Return the killer's new AP and timer, or None when exempt."""
    # GServer-v2/server/src/player/packets/PlayerClientPackets.cpp:470-487.
    if killer_ap <= 0 or victim_ap <= 19:
        return None
    new_ap = max(0, killer_ap - ((killer_ap // 20) + 1) * (victim_ap // 20))
    return new_ap, ap_threshold(new_ap, aptimes)
