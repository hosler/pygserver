"""PLO_NPCWEAPONADD structured wire-format tests."""

from pygserver.protocol.packets import build_npc_weapon_add


def test_weapon_add_includes_image_and_script_property_ids():
    assert build_npc_weapon_add("Beer", "", "") == b"A$Beer  !  \n"


def test_weapon_add_encodes_image_and_script():
    assert build_npc_weapon_add("Bow", "bow.png", "shoot();") == (
        b"A#Bow 'bow.png! (shoot();\n"
    )
