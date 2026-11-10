"""Version gates for game-client packet layouts."""

from reborn_protocol.codec import Gen2Codec


def client_version(player):
    """Default to the supported v6 client; retain classic transport support."""
    version = getattr(player, 'client_version', None)
    if isinstance(version, (int, float)):
        return version
    session = getattr(player, 'session', None)
    return 2.22 if isinstance(getattr(session, 'codec', None), Gen2Codec) else 6.037
