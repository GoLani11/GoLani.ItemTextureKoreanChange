from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.materials import (
    _all_consumers,
    _auxiliary_slots,
    _binding_signature,
    _material_mask_descriptor,
    _neutralize_map,
    _packed_normal_lighting,
    _register_auxiliary_plan,
    _slot_consumers,
    _verify_auxiliary_contract_entry,
)
from golani_texture_localizer.review import sha256_file


def test_packed_normal_lighting_uses_alpha_and_green_channels() -> None:
    flat = np.full((2, 2, 4), 128, dtype=np.uint8)
    flat[..., 0] = 3
    flat[..., 2] = 250
    tilted = flat.copy()
    tilted[0, 0, 3] = 220

    flat_lighting = _packed_normal_lighting(flat, (1.0, 0.0, 1.0))
    tilted_lighting = _packed_normal_lighting(tilted, (1.0, 0.0, 1.0))

    assert np.all(flat_lighting[..., 0] == flat_lighting[..., 1])
    assert tilted_lighting[0, 0, 0] != flat_lighting[0, 0, 0]
    assert np.array_equal(tilted_lighting[1, 1], flat_lighting[1, 1])


def test_packed_normal_lighting_rejects_zero_light() -> None:
    with pytest.raises(ValueError, match="0"):
        _packed_normal_lighting(np.zeros((1, 1, 4), dtype=np.uint8), (0.0, 0.0, 0.0))


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


def test_material_binding_signature_includes_uv_st() -> None:
    bindings = [
        {
            "bundle_key": "items.bundle",
            "material": "label",
            "texture_slots": [
                {
                    "property": "_SpecMap",
                    "path_id": 3,
                    "texture": "label_G",
                    "texture_bundle_key": "items.bundle",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                }
            ],
        }
    ]
    original = _binding_signature(bindings)

    bindings[0]["texture_slots"][0]["offset"] = [0.25, 0.0]

    assert _binding_signature(bindings) != original


def test_auxiliary_slots_reject_same_texture_with_two_material_roles() -> None:
    bindings = [
        {
            "bundle_key": "items.bundle",
            "material": "ambiguous",
            "path_id": 10,
            "texture_slots": [
                {
                    "property": "_BumpMap",
                    "path_id": 7,
                    "texture": "shared_map",
                    "texture_bundle_key": "maps.bundle",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                },
                {
                    "property": "_SpecMap",
                    "path_id": 7,
                    "texture": "shared_map",
                    "texture_bundle_key": "maps.bundle",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                },
            ],
        }
    ]

    with pytest.raises(ValueError, match="Normal/Gloss 역할"):
        _auxiliary_slots(bindings)


def test_unassigned_optional_auxiliary_slots_are_ignored() -> None:
    bindings = [
        {
            "bundle_key": "items.bundle",
            "material": "flat",
            "path_id": 10,
            "texture_slots": [
                {
                    "property": "_BumpMap",
                    "file_id": 0,
                    "path_id": 0,
                    "texture": None,
                    "texture_bundle_key": None,
                },
                {
                    "property": "_MetallicGlossMap",
                    "file_id": 0,
                    "path_id": 0,
                    "texture": None,
                    "texture_bundle_key": None,
                },
            ],
        }
    ]

    assert _auxiliary_slots(bindings) == []


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


def test_auxiliary_contract_is_bound_to_source_identity_and_uv_st(tmp_path) -> None:
    source = tmp_path / "normal.png"
    source.write_bytes(b"packed-normal")
    slot = {
        "texture_bundle_key": "maps.bundle",
        "path_id": 7,
        "texture": "item_N",
        "role": "normal",
        "scale": [1.0, 1.0],
        "offset": [0.0, 0.0],
    }
    record = {
        "bundle_key": "maps.bundle",
        "path_id": 7,
        "texture": "item_N",
        "width": 512,
        "height": 512,
        "format": 12,
    }
    entry = {
        "identity": {
            "texture_bundle_key": "maps.bundle",
            "path_id": 7,
            "texture": "item_N",
            "role": "normal",
            "width": 512,
            "height": 512,
            "format": 12,
            "uv_scale": [1.0, 1.0],
            "uv_offset": [0.0, 0.0],
        },
        "source_map": {"path": "normal.png", "sha256": sha256_file(source)},
        "whole_map_generated": False,
    }

    _verify_auxiliary_contract_entry(entry, slot, record, source, None, tmp_path)

    entry["identity"]["uv_offset"] = [0.25, 0.0]
    with pytest.raises(ValueError, match="identity나 UV ST"):
        _verify_auxiliary_contract_entry(entry, slot, record, source, None, tmp_path)

    entry["identity"]["uv_offset"] = [0.0, 0.0]
    entry["channel_contract"] = {
        "packing": "dxt5nm-x-a-y-g",
        "used_channels": ["G", "A"],
    }
    entry["neutralization_signature"] = "opencv-telea:v1:radius=1"
    material_mask = {"method": "patch"}
    with pytest.raises(ValueError, match="중립화 알고리즘 계약"):
        _verify_auxiliary_contract_entry(
            entry,
            slot,
            record,
            source,
            material_mask,
            tmp_path,
        )


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


def test_shared_auxiliary_map_rejects_different_channel_contracts() -> None:
    plans = {}
    slot = {"texture_bundle_key": "maps.bundle", "path_id": 9, "role": "gloss"}

    _register_auxiliary_plan(
        plans,
        slot,
        target_id="first",
        policy="neutralize_old_text",
        old_text_mask_sha256="a" * 64,
        operation_signature="channels=R",
    )
    with pytest.raises(ValueError, match="정책/마스크가 target마다 달라요"):
        _register_auxiliary_plan(
            plans,
            slot,
            target_id="second",
            policy="neutralize_old_text",
            old_text_mask_sha256="a" * 64,
            operation_signature="channels=RGB",
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


def test_patch_policy_requires_hash_pinned_patch() -> None:
    with pytest.raises(ValueError, match="patch path"):
        _material_mask_descriptor(
            {
                "material_masks": {
                    "material::_SpecMap": {
                        "path": "workspace/reviews/item/material-mask.png",
                        "sha256": "a" * 64,
                        "method": "patch",
                    }
                }
            },
            "material::_SpecMap",
        )

    descriptor = _material_mask_descriptor(
        {
            "material_masks": {
                "material::_SpecMap": {
                    "path": "workspace/reviews/item/material-mask.png",
                    "sha256": "a" * 64,
                    "method": "patch",
                    "patch": "workspace/reviews/item/material-patch.png",
                    "patch_sha256": "b" * 64,
                }
            }
        },
        "material::_SpecMap",
    )

    assert descriptor["patch_sha256"] == "b" * 64


def test_neutralize_map_applies_patch_only_inside_mask(tmp_path) -> None:
    source = np.full((4, 4, 4), 100, dtype=np.uint8)
    source[..., 3] = 255
    source_path = tmp_path / "source.png"
    Image.fromarray(source, "RGBA").save(source_path)
    patch = np.full((4, 4, 4), 200, dtype=np.uint8)
    mask = np.zeros((4, 4), dtype=bool)
    mask[1:3, 1:3] = True

    result, metrics = _neutralize_map(source_path, mask, "gloss", patch=patch)
    values = np.asarray(result)

    assert np.all(values[mask] == 200)
    assert np.array_equal(values[~mask], source[~mask])
    assert metrics["changed_outside_mask"] == 0
    assert metrics["changed_unselected_channels"] == 0
    assert metrics["method"] == "patch"


def test_neutralize_map_changes_only_verified_gloss_channels(tmp_path) -> None:
    source = np.full((4, 4, 4), 100, dtype=np.uint8)
    source[..., 3] = 255
    source_path = tmp_path / "source.png"
    Image.fromarray(source, "RGBA").save(source_path)
    patch = np.full((4, 4, 4), 200, dtype=np.uint8)
    mask = np.zeros((4, 4), dtype=bool)
    mask[1:3, 1:3] = True

    result, metrics = _neutralize_map(
        source_path,
        mask,
        "gloss",
        patch=patch,
        channels=(0,),
    )
    values = np.asarray(result)

    assert np.all(values[mask, 0] == 200)
    assert np.array_equal(values[..., 1:], source[..., 1:])
    assert metrics["selected_channels"] == ["R"]
    assert metrics["changed_unselected_channels"] == 0
