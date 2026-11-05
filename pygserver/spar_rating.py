"""Pure sparring ratings and inactivity deviation decay."""

import math
from ctypes import CDLL, c_float
from ctypes.util import find_library
from typing import Tuple


# Rounding double pow to float can differ from the reference's native powf.
_powf = CDLL(find_library('m') or 'msvcrt').powf
_powf.argtypes = (c_float, c_float)
_powf.restype = c_float


def _float(value: float) -> float:
    """Round an intermediate to the reference's single precision."""
    return c_float(value).value


def _inverse(value: float) -> float:
    """Preserve infinite dSpar when an expectation rounds to one."""
    return 1.0 / value if value else math.inf


def glicko_update(loser_rating: float, loser_dev: float,
                  winner_rating: float, winner_dev: float
                  ) -> Tuple[float, float, float, float]:
    """Return winner rating/deviation, then loser rating/deviation.

    GServer-v2/server/src/player/packets/PlayerClientPackets.cpp:433-449.
    Keep the reference's gSpar/ESpar/dSpar order, including its deviation
    indexing. Integer-exponent pow calls use doubles; float operations and
    powf/std::pow(float, float) results round to single precision.
    """
    lose, lose_dev, win, win_dev = map(
        _float, (loser_rating, loser_dev, winner_rating, winner_dev))
    q = _float(0.0057565)
    pi = _float(3.14159265)
    g_spar = [
        _float(1.0 / math.pow(
            1.0 + 3.0 * math.pow(q, 2) * math.pow(dev, 2)
            / math.pow(pi, 2), 0.5))
        for dev in (win_dev, lose_dev)
    ]
    e_spar = []
    for g, own, other in ((g_spar[1], win, lose), (g_spar[0], lose, win)):
        exponent = _float(_float(-g * _float(own - other)) / 400.0)
        power = _powf(10.0, exponent)
        e_spar.append(_float(1.0 / _float(1.0 + power)))
    d_spar = [
        _float(_inverse(math.pow(q, 2) * math.pow(g, 2)
                        * expected * _float(1.0 - expected)))
        for g, expected in zip(g_spar, e_spar)
    ]
    results = []
    for rating, dev, g, expected, d, score in zip(
            (win, lose), (win_dev, lose_dev), g_spar, e_spar, d_spar,
            (1.0, 0.0)):
        precision = _float(_float(1.0 / _powf(dev, 2.0))
                           + _float(_inverse(d)))
        adjustment = _float(_float(q / precision)
                            * _float(g * _float(score - expected)))
        new_rating = _float(rating + adjustment)
        new_dev = _powf(_float(1.0 / precision), 0.5)
        results.extend((min(4000.0, max(0.0, new_rating)),
                        min(350.0, max(50.0, new_dev))))
    return tuple(results)


def decay_deviation(deviation: float, days: int) -> float:
    """Increase deviation after whole days, capped at 350.

    GServer-v2/server/src/player/PlayerClient.cpp:453-471.
    """
    deviation = _float(deviation)
    c = _float(44.721)
    variance = _float(_float(deviation * deviation)
                      + _float(_float(c * c) * _float(days)))
    return min(350.0, _float(math.sqrt(variance)) if variance >= 0 else math.nan)
