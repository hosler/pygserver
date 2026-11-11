"""Suite-wide isolation for process-global state."""

import pytest

from pygserver.protocol import colors


@pytest.fixture(autouse=True)
def _unbind_colors_server():
    """colors_wire_width() follows the newest GameServer's config. Drop that
    binding around every test so a server built with `servermode = newworld`
    cannot widen COLORS for a later test that builds packets without one."""
    colors._server = None
    yield
    colors._server = None
