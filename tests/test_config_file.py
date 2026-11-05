"""serveroptions.txt parsing."""
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
