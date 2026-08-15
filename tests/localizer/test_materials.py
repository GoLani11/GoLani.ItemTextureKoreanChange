from __future__ import annotations

import pytest

from golani_texture_localizer.materials import (
    _all_consumers,
    _auxiliary_slots,
    _material_mask_descriptor,
    _register_auxiliary_plan,
    _slot_consumers,
)


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


def test_auxiliary_consumer_lookup_uses_auxiliary_bundle_not_diffuse_bundle() -> None:
    inventory = {
        "materials": [
            {
                "bundle_key": "model.bundle",
                "material": "shared",
                "path_id": 10,
                "texture_slots": [
                    {
                        "property": "_BumpMap",
                        "path_id": 7,
                        "texture_bundle_key": "normal.bundle",
                    }
                ],
            }
        ]
    }
    slot = {"path_id": 7, "texture_bundle_key": "normal.bundle"}

    assert [value["material"] for value in _slot_consumers(inventory, slot)] == ["shared"]


def test_shared_auxiliary_map_rejects_different_target_masks() -> None:
    plans = {}
    slot = {"texture_bundle_key": "maps.bundle", "path_id": 9, "role": "normal"}

    assert _register_auxiliary_plan(
        plans,
        slot,
        target_id="ratcola",
        policy="neutralize_old_text",
        old_text_mask_sha256="a" * 64,
    )
    with pytest.raises(ValueError, match="target마다 달라요"):
        _register_auxiliary_plan(
            plans,
            slot,
            target_id="tarcola",
            policy="neutralize_old_text",
            old_text_mask_sha256="b" * 64,
        )


def test_neutralize_policy_requires_explicit_material_mask() -> None:
    with pytest.raises(ValueError, match="material_masks"):
        _material_mask_descriptor({}, "material::_BumpMap")

    descriptor = _material_mask_descriptor(
        {
            "material_masks": {
                "material::_BumpMap": {
                    "path": "workspace/reviews/item/material-old-text-mask.png",
                    "sha256": "a" * 64,
                    "method": "inpaint",
                }
            }
        },
        "material::_BumpMap",
    )

    assert descriptor["method"] == "inpaint"
