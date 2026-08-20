from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.inventory import _material_targets
from golani_texture_localizer.materials import (
    _all_consumers,
    _auxiliary_slots,
    _binding_signature,
    _canonical_master_lettering,
    _consumer_target_ids,
    derive_approved_materials,
    _material_mask_descriptor,
    _master_lettering_alpha,
    _neutralize_map,
    _packed_normal_lighting,
    _register_auxiliary_plan,
    _slot_consumers,
    _verify_auxiliary_contract_entry,
    _verify_derivation_contract,
)
from golani_texture_localizer.paths import ProjectPaths
from golani_texture_localizer.review import sha256_file


def test_derive_requires_current_inventory_schema(tmp_path, monkeypatch) -> None:
    paths = ProjectPaths.create(tmp_path, tmp_path / "workspace")
    monkeypatch.setattr(
        "golani_texture_localizer.materials.load_inventory",
        lambda _path: {"schema_version": 2, "records": []},
    )

    with pytest.raises(ValueError, match="extract를 다시 실행"):
        derive_approved_materials(SimpleNamespace(targets=[]), paths)


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


def test_master_lettering_uses_hash_pinned_continuous_alpha(tmp_path) -> None:
    paths = ProjectPaths.create(tmp_path, tmp_path / "workspace")
    lettering_path = tmp_path / "lettering.png"
    mask_path = tmp_path / "lettering-mask.png"
    rgba = np.zeros((4, 4, 4), dtype=np.uint8)
    rgba[1:3, 1:3, :3] = 255
    rgba[1, 1, 3] = 64
    rgba[1, 2, 3] = 128
    rgba[2, 1, 3] = 192
    rgba[2, 2, 3] = 255
    mask = (rgba[..., 3] > 0).astype(np.uint8) * 255
    Image.fromarray(rgba, "RGBA").save(lettering_path)
    Image.fromarray(mask, "L").save(mask_path)
    edit_data = {
        "compositor": {
            "regions": [
                {
                    "region_id": "front",
                    "selected_lettering": {
                        "path": "lettering.png",
                        "sha256": sha256_file(lettering_path),
                    },
                    "lettering_mask": {
                        "path": "lettering-mask.png",
                        "sha256": sha256_file(mask_path),
                    },
                }
            ]
        }
    }

    combined, regions, records = _master_lettering_alpha(paths, edit_data, (4, 4))

    assert np.array_equal(combined, rgba[..., 3])
    assert np.array_equal(regions["front"], rgba[..., 3])
    assert records == [
        {
            "region_id": "front",
            "selected_lettering_sha256": sha256_file(lettering_path),
            "lettering_mask_sha256": sha256_file(mask_path),
        }
    ]


def test_master_lettering_signature_is_canonical_and_rejects_duplicate_regions() -> None:
    first = {
        "region_id": "back",
        "selected_lettering_sha256": "a" * 64,
        "lettering_mask_sha256": "b" * 64,
    }
    second = {
        "region_id": "front",
        "selected_lettering_sha256": "c" * 64,
        "lettering_mask_sha256": "d" * 64,
    }

    assert _canonical_master_lettering([second, first]) == [first, second]
    with pytest.raises(ValueError, match="중복"):
        _canonical_master_lettering([first, first])


def test_actual_material_binding_keeps_shared_ratcola_maps() -> None:
    bindings = [
        {
            "bundle_key": "items.bundle",
            "assets_file": "CAB-ITEMS",
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
            "assets_file": "CAB-ITEMS",
            "path_id": 10,
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
            "assets_file": "CAB-ITEMS",
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
                "assets_file": "CAB-ITEMS",
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
                "assets_file": "CAB-ITEMS",
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
                "assets_file": "CAB-MODEL",
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


def test_derived_shared_consumer_requires_every_diffuse_slot_to_map_to_target() -> None:
    inventory = {
        "records": [
            {"bundle_key": "items.bundle", "path_id": 1, "target_id": "can"}
        ],
        "materials": [
            {
                "bundle_key": "items.bundle",
                "assets_file": "CAB-ITEMS",
                "material": "item",
                "path_id": 10,
                "texture_slots": [
                    {
                        "property": "_MainTex",
                        "texture_bundle_key": "items.bundle",
                        "path_id": 1,
                    },
                    {
                        "property": "_BaseMap",
                        "texture_bundle_key": "other.bundle",
                        "path_id": 99,
                    },
                ],
            }
        ],
    }
    consumers = [
        {
            "material_bundle_key": "items.bundle",
            "material_assets_file": "CAB-ITEMS",
            "material_path_id": 10,
        }
    ]

    assert _consumer_target_ids(inventory, consumers) == {"__unmapped_consumer__"}


def test_same_material_path_in_different_assets_files_keeps_unmapped_consumer() -> None:
    records = [
        {"bundle_key": "diffuse.bundle", "path_id": 1, "target_id": "sample"}
    ]
    materials = [
        {
            "bundle_key": "model.bundle",
            "assets_file": "CAB-MAPPED",
            "material": "mapped",
            "path_id": 7,
            "texture_slots": [
                {
                    "property": "_MainTex",
                    "texture_bundle_key": "diffuse.bundle",
                    "path_id": 1,
                },
                {
                    "property": "_BumpMap",
                    "texture_bundle_key": "normal.bundle",
                    "path_id": 9,
                },
            ],
        },
        {
            "bundle_key": "model.bundle",
            "assets_file": "CAB-UNMAPPED",
            "material": "unmapped",
            "path_id": 7,
            "texture_slots": [
                {
                    "property": "_MainTex",
                    "texture_bundle_key": "other.bundle",
                    "path_id": 2,
                },
                {
                    "property": "_BumpMap",
                    "texture_bundle_key": "normal.bundle",
                    "path_id": 9,
                },
            ],
        },
    ]
    inventory = {"records": records, "materials": materials}
    consumers = _all_consumers(inventory, "normal.bundle", 9)

    assert _material_targets(records, materials) == {
        ("model.bundle", "CAB-MAPPED", 7): ["sample"]
    }
    assert {value["material_assets_file"] for value in consumers} == {
        "CAB-MAPPED",
        "CAB-UNMAPPED",
    }
    assert _consumer_target_ids(inventory, consumers) == {
        "sample",
        "__unmapped_consumer__",
    }


def test_duplicate_material_contract_key_groups_only_identical_auxiliary_st() -> None:
    def binding(assets_file: str, path_id: int, offset: list[float]) -> dict:
        return {
            "bundle_key": "model.bundle",
            "assets_file": assets_file,
            "material": "duplicate",
            "path_id": path_id,
            "texture_slots": [
                {
                    "property": "_BumpMap",
                    "path_id": 9,
                    "texture": "shared_normal",
                    "texture_bundle_key": "normal.bundle",
                    "scale": [1.0, 1.0],
                    "offset": offset,
                }
            ],
        }

    assert len(
        _auxiliary_slots(
            [binding("CAB-FIRST", 7, [0.0, 0.0]), binding("CAB-SECOND", 7, [0.0, 0.0])]
        )
    ) == 2
    with pytest.raises(ValueError, match="서로 다른 보조맵/ST"):
        _auxiliary_slots(
            [binding("CAB-FIRST", 7, [0.0, 0.0]), binding("CAB-SECOND", 7, [0.25, 0.0])]
        )


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


def test_dxt1_gloss_contract_rejects_unrepresentable_alpha_channel(tmp_path) -> None:
    source = tmp_path / "gloss.png"
    source.write_bytes(b"dxt1-gloss")
    slot = {
        "texture_bundle_key": "maps.bundle",
        "path_id": 8,
        "texture": "item_G",
        "role": "gloss",
        "scale": [1.0, 1.0],
        "offset": [0.0, 0.0],
    }
    record = {
        "bundle_key": "maps.bundle",
        "path_id": 8,
        "texture": "item_G",
        "width": 512,
        "height": 512,
        "format": 10,
    }
    entry = {
        "identity": {
            "texture_bundle_key": "maps.bundle",
            "path_id": 8,
            "texture": "item_G",
            "role": "gloss",
            "width": 512,
            "height": 512,
            "format": 10,
            "uv_scale": [1.0, 1.0],
            "uv_offset": [0.0, 0.0],
        },
        "source_map": {"path": "gloss.png", "sha256": sha256_file(source)},
        "whole_map_generated": False,
        "channel_contract": {"packing": "custom", "used_channels": ["A"]},
        "neutralization_signature": "patch-copy:v1",
    }

    with pytest.raises(ValueError, match="DXT1 Gloss"):
        _verify_auxiliary_contract_entry(
            entry,
            slot,
            record,
            source,
            {"method": "patch"},
            tmp_path,
        )


def test_valid_normal_derivation_contract_uses_nonflat_parameter_probe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "golani_texture_localizer.materials._verify_effect_measurement",
        lambda *args, **kwargs: None,
    )
    paths = ProjectPaths.create(tmp_path, tmp_path / "workspace")
    scale = [1.0, 1.0]
    offset = [0.0, 0.0]
    projection = {
        "signature": "continuous-alpha-same-st-integer-area:v1",
        "source_size": [4, 4],
        "target_size": [4, 4],
        "diffuse_uv_scale": scale,
        "diffuse_uv_offset": offset,
        "auxiliary_uv_scale": scale,
        "auxiliary_uv_offset": offset,
        "v_axis": "png-top-left+unity-v-up",
        "texel_center_sampling": True,
    }
    derivation = {
        "schema_version": 1,
        "producer": "dxt5nm-rnm-height-from-master-alpha:v1",
        "physical_component": "all-selected-lettering-alpha",
        "master_region_ids": ["front"],
        "projection": projection,
        "alignment_limits": {
            "center_error_texels": 0.5,
            "bbox_edge_error_texels": 1.0,
            "rotation_error_deg": 0.0,
        },
        "effect_parameters": {
            "height_scale_texels": 1.0,
            "polarity": 1,
            "bevel_passes": 1,
        },
        "effect_measurement": {},
    }

    assert (
        _verify_derivation_contract(
            paths,
            {"effect_kind": "master-alpha-relief", "derivation": derivation},
            role="normal",
            used_channels=(1, 3),
            diffuse_record={"width": 4, "height": 4, "wrap_u": 0, "wrap_v": 0},
            diffuse_slot={"scale": scale, "offset": offset},
            auxiliary_record={"width": 4, "height": 4, "wrap_u": 0, "wrap_v": 0},
            auxiliary_slot={"scale": scale, "offset": offset},
            master_records=[{"region_id": "front"}],
        )
        == derivation
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


def test_auxiliary_plan_deduplicates_same_target_shared_material_slots() -> None:
    plans = {}
    slot = {"texture_bundle_key": "maps.bundle", "path_id": 9, "role": "gloss"}

    assert _register_auxiliary_plan(
        plans,
        slot,
        target_id="beer",
        policy="neutralize_old_text",
        old_text_mask_sha256="a" * 64,
        operation_signature="same-contract",
    )
    assert not _register_auxiliary_plan(
        plans,
        slot,
        target_id="beer",
        policy="neutralize_old_text",
        old_text_mask_sha256="a" * 64,
        operation_signature="same-contract",
    )

    assert plans[("maps.bundle", 9, "gloss")]["target_ids"] == ["beer"]


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


def test_neutralized_dxt5nm_quantization_stays_inside_unit_disk(tmp_path) -> None:
    source = np.full((2, 2, 4), 128, dtype=np.uint8)
    source_path = tmp_path / "source-normal.png"
    Image.fromarray(source, "RGBA").save(source_path)
    patch = source.copy()
    patch[..., 1] = 0
    patch[..., 3] = 0
    mask = np.ones((2, 2), dtype=bool)

    result, _ = _neutralize_map(
        source_path,
        mask,
        "normal",
        patch=patch,
        channels=(1, 3),
    )
    values = np.asarray(result, dtype=np.uint8)
    x = values[..., 3].astype(np.float32) / 127.5 - 1.0
    y = values[..., 1].astype(np.float32) / 127.5 - 1.0

    assert float(np.sqrt(x * x + y * y).max()) <= 1.0
