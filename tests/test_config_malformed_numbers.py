"""A malformed number in serveroptions.txt never aborts start-up.

Reference (GServer-v2 origin/beta4): Settings::get<uintN_t> (utilities/Settings.h
:202-221) returns string::toNumber (utilities/StringUtils.h:1320-1346), which is
0 for anything that is not plain digits. The option is present, so the
`.value_or(default)` at the call sites (LevelBaddy.cpp:270, PlayerClient.cpp
:1907-1908, Server.cpp:618) never applies: a bad value reads as 0, the default
only comes back when the option is absent.
"""

import logging

import pytest

from pygserver.config import ServerConfig
from pygserver.rc import _probe_option

KEYS = ["maxdeathgralats", "mindeathgralats", "baddyrespawntime", "respawntime"]


def load(tmp_path, text):
    path = tmp_path / "serveroptions.txt"
    path.write_text(text)
    return ServerConfig.from_file(str(path))


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("bad", ["5x", "x", "1.5", "+7", "1e3", "5 5", "0x10"])
def test_a_malformed_number_reads_as_zero_with_one_warning(tmp_path, caplog, key, bad):
    with caplog.at_level(logging.WARNING, logger="pygserver.config"):
        config = load(tmp_path, f"name = kept\n{key} = {bad}\n")
    assert getattr(config, key) == 0
    assert config.name == "kept"                      # the rest still loaded
    assert len([r for r in caplog.records if key in r.getMessage()]) == 1


def test_spawnrate_malformed_reads_as_zero(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="pygserver.config"):
        config = load(tmp_path, "spawnrateheart = 5x\nspawnratebombs = 7\n")
    assert config.spawn_rates == {"heart": 0, "bombs": 7}
    assert len(caplog.records) == 1


@pytest.mark.parametrize("key,value", [("maxdeathgralats", "12"), ("respawntime", "30"),
                                        ("baddyrespawntime", "0"), ("mindeathgralats", "007")])
def test_well_formed_numbers_still_load(tmp_path, caplog, key, value):
    with caplog.at_level(logging.WARNING, logger="pygserver.config"):
        config = load(tmp_path, f"{key} = {value}   # note\n")
    assert getattr(config, key) == int(value)
    assert caplog.records == []


def test_negative_and_oversized_values_keep_their_clamps(tmp_path):
    config = load(tmp_path, "maxdeathgralats = -4\nrespawntime = 99999\n"
                            "baddyrespawntime = 99999999999\n")
    assert (config.maxdeathgralats, config.respawntime,
            config.baddyrespawntime) == (0, 0xFFFF, 0xFFFFFFFF)


def test_an_rc_write_of_a_malformed_number_is_a_zero_not_a_crash():
    assert _probe_option("maxdeathgralats", "5x")[0] == {"maxdeathgralats": 0}
    assert _probe_option("respawntime", "abc")[0] == {"respawntime": 0}
