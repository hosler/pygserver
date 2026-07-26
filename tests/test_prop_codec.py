"""SWORDPOWER/SHIELDPOWER wire decode, against GServer-v2's
PropertySwordPower/PropertyShieldPower::deserialize."""

from pygserver.protocol.constants import PLPROP
from pygserver.protocol.packets import parse_player_props


def _gchar(value: int) -> bytes:
    return bytes([value + 32])


def _prop(prop_id: int, *payload: bytes) -> bytes:
    return _gchar(int(prop_id)) + b"".join(payload)


def _gstring(text: str) -> bytes:
    return _gchar(len(text)) + text.encode('latin-1')


def test_sword_power_with_custom_image_is_debiased():
    data = _prop(PLPROP.SWORDPOWER, _gchar(30 + 3), _gstring("blade.png"))
    props = parse_player_props(data)
    assert props[PLPROP.SWORDPOWER] == 3
    assert props['sword_image'] == "blade.png"


def test_shield_power_with_custom_image_is_debiased():
    data = _prop(PLPROP.SHIELDPOWER, _gchar(10 + 3), _gstring("guard.png"))
    props = parse_player_props(data)
    assert props[PLPROP.SHIELDPOWER] == 3
    assert props['shield_image'] == "guard.png"


def test_bare_sword_powers_parse_unchanged():
    for power in (1, 2, 3, 4):
        props = parse_player_props(_prop(PLPROP.SWORDPOWER, _gchar(power)))
        assert props[PLPROP.SWORDPOWER] == power
        assert props['sword_image'] == f"sword{power}.png"


def test_bare_shield_powers_parse_unchanged():
    # Deserialize hands out a default image up to power 4 even though serialize
    # only emits the bare form up to 3 - the asymmetry we mirror on purpose.
    for power in (1, 2, 3, 4):
        props = parse_player_props(_prop(PLPROP.SHIELDPOWER, _gchar(power)))
        assert props[PLPROP.SHIELDPOWER] == power
        assert props['shield_image'] == f"shield{power}.png"


def test_zero_power_has_no_image():
    assert parse_player_props(_prop(PLPROP.SWORDPOWER, _gchar(0)))['sword_image'] == ""
    assert parse_player_props(_prop(PLPROP.SHIELDPOWER, _gchar(0)))['shield_image'] == ""


def test_mid_range_bare_power_does_not_swallow_following_props():
    """A bare power between the bare range and the threshold carries no image;
    reading one desynced every prop after it."""
    data = (_prop(PLPROP.SWORDPOWER, _gchar(7))
            + _prop(PLPROP.SHIELDPOWER, _gchar(6))
            + _prop(PLPROP.CURLEVEL, _gstring("onlinestartlocal.nw")))
    props = parse_player_props(data)
    assert props[PLPROP.SWORDPOWER] == 7
    assert props[PLPROP.SHIELDPOWER] == 6
    assert props[PLPROP.CURLEVEL] == "onlinestartlocal.nw"


def test_shield_power_without_image_bytes_is_tolerated():
    """PropertyShieldPower::deserialize's bytesLeft()==0 early return."""
    props = parse_player_props(_prop(PLPROP.SHIELDPOWER, _gchar(10 + 2)))
    assert props[PLPROP.SHIELDPOWER] == 2
    assert props['shield_image'] == ""


def test_sword_custom_image_without_extension_gets_gif():
    data = _prop(PLPROP.SWORDPOWER, _gchar(30 + 1), _gstring("myblade"))
    assert parse_player_props(data)['sword_image'] == "myblade.gif"
