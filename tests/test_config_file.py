"""serveroptions.txt parsing."""
import pytest

from pygserver.config import ServerConfig


def test_inline_hash_comment_ends_scalar_values_only(tmp_path):
    # Real server configs annotate scalar values in place (cf. GServer-v2
    # server/src/utilities/Settings.cpp:50); free text keeps its '#'.
    path = tmp_path / "serveroptions.txt"
    path.write_text(
        "# leading comment line\n"
        "triggerhack_weapons = true# gr.addweapon, gr.deleteweapon\n"
        "heartlimit = 3 # hearts\n"
        "name = Server #1\n"
        "description = Join #general\n")
    config = ServerConfig.from_file(str(path))
    assert config.triggerhack_weapons is True
    assert config.heart_limit == 3
    assert config.name == "Server #1"
    assert config.description == "Join #general"


def test_villager_count_is_read_from_serveroptions(tmp_path):
    """villager_count is a pygserver-only option (config.py); the loader
    must honour it so a world can tone down the start-level crowd."""
    from pygserver.config import ServerConfig
    path = tmp_path / "serveroptions.txt"
    path.write_text("name = x\nvillager_count = 2# two is plenty\n")
    assert ServerConfig.from_file(str(path)).villager_count == 2


@pytest.mark.parametrize('value,enabled', [('true', True), ('false', False)])
def test_baddyitems_option_defaults_false_and_round_trips(tmp_path, value, enabled):
    path = tmp_path / 'serveroptions.txt'
    assert ServerConfig.from_file(str(path)).baddyitems is False
    path.write_text(f'baddyitems = {value} # item drops\n')

    config = ServerConfig.from_file(str(path))

    assert config.baddyitems is enabled
    config.to_file(str(path))
    assert ServerConfig.from_file(str(path)).baddyitems is enabled


def test_gameplay_options_default_to_the_reference_and_round_trip(tmp_path):
    # Server.h:165,172, LevelBaddy.cpp:284 and the PLI_PUTNPC opt-in.
    path = tmp_path / 'serveroptions.txt'
    config = ServerConfig.from_file(str(path))
    assert (config.baddyitems, config.lockplayerz,
            config.defaultweapons, config.putnpc_enabled) == (False, False, True, False)

    path.write_text('baddyitems = true\nlockplayerz = true\n'
                    'defaultweapons = false\nputnpcenabled = true\n'
                    'serverport = 14123\n')
    config = ServerConfig.from_file(str(path))
    assert (config.baddyitems, config.lockplayerz,
            config.defaultweapons, config.putnpc_enabled) == (True, True, False, True)

    config.to_file(str(path))
    again = ServerConfig.from_file(str(path))
    assert (again.baddyitems, again.lockplayerz, again.defaultweapons,
            again.putnpc_enabled, again.port) == (True, True, False, True, 14123)
