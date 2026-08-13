from __future__ import annotations

from golani_texture_localizer.materials import _all_consumers, _auxiliary_slots


def test_actual_material_binding_keeps_shared_ratcola_maps() -> None:
    bindings = [
        {
            "bundle_key": "items.bundle",
            "material": "item_ratcola",
            "path_id": 10,
            "texture_slots": [
                {
                    "property": "_MainTex",
                    "file_id": 0,
                    "path_id": 1,
                    "texture": "rat_diff",
                    "texture_bundle_key": "items.bundle",
                },
                {
                    "property": "_BumpMap",
                    "file_id": 0,
                    "path_id": 2,
                    "texture": "tar_nrm",
                    "texture_bundle_key": "items.bundle",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                },
                {
                    "property": "_SpecMap",
                    "file_id": 0,
                    "path_id": 3,
                    "texture": "tar_gloss",
                    "texture_bundle_key": "items.bundle",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                },
            ],
        }
    ]

    assert {(row["role"], row["texture"]) for row in _auxiliary_slots(bindings)} == {
        ("normal", "tar_nrm"),
        ("gloss", "tar_gloss"),
    }


def test_shared_consumers_are_not_hidden_by_family_name() -> None:
    inventory = {
        "materials": [
            {
                "bundle_key": "items.bundle",
                "material": "rat",
                "path_id": 10,
                "texture_slots": [
                    {
                        "property": "_BumpMap",
                        "file_id": 0,
                        "path_id": 7,
                        "texture_bundle_key": "items.bundle",
                    }
                ],
            },
            {
                "bundle_key": "items.bundle",
                "material": "tar",
                "path_id": 11,
                "texture_slots": [
                    {
                        "property": "_BumpMap",
                        "file_id": 0,
                        "path_id": 7,
                        "texture_bundle_key": "items.bundle",
                    }
                ],
            },
        ]
    }

    assert [value["material"] for value in _all_consumers(inventory, "items.bundle", 7)] == [
        "rat",
        "tar",
    ]
