"""Version gates for game-client packet layouts."""

from reborn_protocol.codec import Gen2Codec

# Login version token -> client version, the nonempty rows of gs2lib's
# clientVersions[] (IUtil.cpp:60-130, in CLVER_* order) paired with the
# matching clientVersionString[] number. The reference keeps the table index
# (CLVER_*, getVersionID IUtil.cpp:271) and compares indices; the numbers here
# keep the same order, so the existing `>=` gates behave alike. Skipped: the
# NW 2.02 token (CLVER id 2 sits below every number) and the iPhone / Worlds
# tokens map to 6.037, the last number, since their ids follow CLVER_6_037.
# reborn-protocol carries no such table (pyReborn has its own client-side
# VERSIONS), so it lives here until the codec library grows one.
CLIENT_VERSION_TOKENS = {
    'GSERV007': 1.21, 'GSERV009': 1.25, 'GSERV011': 1.27, 'GSERV013': 1.28,
    'GSERV014': 1.30, 'GSERV016': 1.31, 'GSERV018': 1.32,
    'GNW20020': 1.33, 'GNW22030': 1.34, 'GNW13040': 1.35, 'GNW10050': 1.36,
    'GNW21050': 1.37, 'GNW22060': 1.38, 'GNW02070': 1.381, 'GNW07080': 1.39,
    'GNW12080': 1.391, 'GNW13110': 1.411,
    'GNW24011': 2.0, 'GNW23021': 2.01, 'GNW15041': 2.02, 'GNW20041': 2.022,
    'GNW06051': 2.03, 'GNW24061': 2.04, 'GNW03081': 2.05, 'GNW31101': 2.1,
    'GNW01012': 2.12, 'GNW23012': 2.13, 'GNW30042': 2.14, 'GNW19052': 2.15,
    'GNW20052': 2.151, 'GNW12102': 2.16, 'GNW22122': 2.17, 'GNW21033': 2.18,
    'GNW15053': 2.19, 'GNW28063': 2.2, 'GNW01113': 2.21, 'GNW03014': 2.22,
    'GNW14015': 2.3, 'GNW28015': 2.31,
    'G3D16053': 3.0, 'G3D27063': 3.01, 'G3D03014': 3.041,
    'G3D28095': 4.0211, 'G3D09125': 4.034, 'G3D17026': 4.042,
    'G3D26076': 4.110, 'G3D20126': 4.208,
    'G3D22067': 5.07, 'G3D14097': 5.12, 'G3D26090': 5.31,
    'G3D3007A': 6.015, 'G3D2505C': 6.034, 'G3D0311C': 6.037,
    'G3D0511C': 6.037,
    'G3D04048': 6.037, 'G3D18010': 6.037, 'G3D29090': 6.037,
    'G3D2504D': 6.037, 'G3D2204D': 6.037,
}


def version_for_token(token):
    """The client version for a login version token, None when the token is
    not in the table (getVersionID answers CLVER_UNKNOWN)."""
    return CLIENT_VERSION_TOKENS.get(token)


def client_version(player):
    """Default to the supported v6 client; retain classic transport support."""
    version = getattr(player, 'client_version', None)
    if isinstance(version, (int, float)):
        return version
    session = getattr(player, 'session', None)
    return 2.22 if isinstance(getattr(session, 'codec', None), Gen2Codec) else 6.037
