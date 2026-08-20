import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.auxiliary import derive_linear_gloss
from golani_texture_localizer.bundles import (
    _expand_to_bc_blocks,
    _coverage_values,
    _mip_chain,
    _prune_stale_preserved_auxiliary_outputs,
    _review_projection_contract,
    _roundtrip_limits,
    _sha256_file,
    _validate_derived_material_output,
    _validate_derived_material_pixels,
    _validate_auxiliary_mip_invariants,
    _validate_auxiliary_plan_manifest,
    _validate_compressed_auxiliary_invariants,
    _verified_source_bundle,
)
from golani_texture_localizer.paths import ProjectPaths


def test_gloss_8px_roundtrip_limit_uses_noop_calibration() -> None:
    mae, p99, maximum = _roundtrip_limits("gloss", 8, 8, 6.0)

    assert mae == 8.0
    assert p99 == 64.0
    assert maximum == 128.0


def test_gloss_16px_roundtrip_limit_keeps_default() -> None:
    assert _roundtrip_limits("gloss", 16, 16, 6.0)[0] == 6.0


def test_review_projection_contract_requires_current_repeat_wrap() -> None:
    inventory = {
        "records": [
            {
                "target_id": "can",
                "bundle_key": "diffuse.bundle",
                "path_id": 1,
                "width": 4,
                "height": 4,
                "wrap_u": 0,
                "wrap_v": 0,
            },
            {
                "bundle_key": "aux.bundle",
                "path_id": 2,
                "texture": "can_gloss",
                "role": "gloss",
                "width": 4,
                "height": 4,
                "format": 10,
                "wrap_u": 1,
                "wrap_v": 0,
            },
        ]
    }
    material_data = {
        "bindings": [
            {
                "material_bundle_key": "material.bundle",
                "material_assets_file": "CAB-MATERIAL",
                "material_path_id": 7,
                "material": "can-material",
                "property": "_MainTex",
                "texture_bundle_key": "diffuse.bundle",
                "path_id": 1,
                "scale": [1.0, 1.0],
                "offset": [0.0, 0.0],
            },
            {
                "material_bundle_key": "material.bundle",
                "material_assets_file": "CAB-MATERIAL",
                "material_path_id": 7,
                "material": "can-material",
                "property": "_SpecMap",
                "texture_bundle_key": "aux.bundle",
                "path_id": 2,
                "scale": [1.0, 1.0],
                "offset": [0.0, 0.0],
            },
        ]
    }
    entry = {
        "identity": {
            "texture_bundle_key": "aux.bundle",
            "path_id": 2,
            "texture": "can_gloss",
            "role": "gloss",
            "width": 4,
            "height": 4,
            "format": 10,
        }
    }

    with pytest.raises(ValueError, match="Repeat wrap"):
        _review_projection_contract(
            inventory,
            target_id="can",
            material_data=material_data,
            contract_key="can-material::_SpecMap",
            contract_entry=entry,
        )

    inventory["records"][1]["wrap_u"] = 0
    entry["identity"]["format"] = 12
    with pytest.raises(ValueError, match="identity"):
        _review_projection_contract(
            inventory,
            target_id="can",
            material_data=material_data,
            contract_key="can-material::_SpecMap",
            contract_entry=entry,
        )


def test_repack_rejects_unverified_derived_material_channels() -> None:
    output = {
        "policy": "neutralize_old_text",
        "metrics": {
            "changed_outside_mask": 0,
            "changed_unselected_channels": 0,
            "mode_preserved": True,
            "selected_channels": ["R"],
        },
    }

    _validate_derived_material_output(output, expected_channels=["R"])

    output["metrics"]["changed_unselected_channels"] = 1
    with pytest.raises(ValueError, match="changed_unselected_channels"):
        _validate_derived_material_output(output)

    output["metrics"]["changed_unselected_channels"] = 0
    with pytest.raises(ValueError, match="현재 재질 계약과 달라요"):
        _validate_derived_material_output(output, expected_channels=["G"])


def test_repack_requires_every_current_auxiliary_plan_and_output_count() -> None:
    key = ("maps.bundle", 7, "normal")
    expected = {
        key: {
            "policy": "neutralize_old_text",
            "old_text_mask_sha256": "a" * 64,
            "operation_signature": "signed",
            "target_ids": ["mayo"],
        }
    }
    row = {
        "texture_bundle_key": key[0],
        "path_id": key[1],
        "role": key[2],
        **expected[key],
    }

    with pytest.raises(ValueError, match="missing"):
        _validate_auxiliary_plan_manifest(expected, [], [], 0)
    with pytest.raises(ValueError, match="derived_count"):
        _validate_auxiliary_plan_manifest(expected, [row], [{}], 0)

    plans, outputs = _validate_auxiliary_plan_manifest(expected, [row], [{}], 1)

    assert plans[key]["policy"] == "neutralize_old_text"
    assert outputs == [{}]


def test_repack_accepts_only_fully_measured_master_alpha_derivation(tmp_path: Path) -> None:
    artifacts = {}
    for name in (
        "neutral_base",
        "projected_master_alpha",
        "effect_mask",
        "projected_protected",
        "projected_seam_guard",
    ):
        path = tmp_path / f"{name}.png"
        path.write_bytes(name.encode("ascii"))
        artifacts[name] = {"path": str(path), "sha256": _sha256_file(path)}
    output = {
        "policy": "neutralize_and_derive",
        "role": "gloss",
        "metrics": {
            "changed_outside_effect_union": 0,
            "changed_inside_protected": 0,
            "changed_inside_seam_guard": 0,
            "changed_outside_effect_mask": 0,
            "changed_unselected_channels": 0,
            "mode_preserved": True,
            "selected_channels": ["R"],
            "projection_signature": "continuous-alpha-same-st-integer-area:v1",
            "algorithm_signature": "linear-gloss-delta-from-master-alpha:v1",
            "channel_deltas": {"R": 24.0},
            "alignment": [
                {
                    "region_id": "front",
                    "center_error_texels": 0.25,
                    "bbox_edge_error_texels": 0.5,
                    "rotation_error_deg": 0.0,
                }
            ],
        },
        "derivation": {
            "producer": "linear-gloss-delta-from-master-alpha:v1",
            "effect_parameters": {"channel_deltas": {"R": 24.0}},
            "master_lettering": [
                {
                    "region_id": "front",
                    "selected_lettering_sha256": "a" * 64,
                    "lettering_mask_sha256": "b" * 64,
                }
            ],
            **artifacts,
        },
    }

    _validate_derived_material_output(output, expected_channels=["R"])

    output["metrics"]["changed_inside_seam_guard"] = 1
    with pytest.raises(ValueError, match="changed_inside_seam_guard"):
        _validate_derived_material_output(output, expected_channels=["R"])

    output["metrics"]["changed_inside_seam_guard"] = 0
    output["metrics"]["channel_deltas"]["R"] = float("nan")
    with pytest.raises(ValueError, match="Gloss delta"):
        _validate_derived_material_output(output, expected_channels=["R"])


def test_repack_recomputes_derived_pixels_instead_of_trusting_manifest(tmp_path: Path) -> None:
    paths = ProjectPaths.create(tmp_path, tmp_path / "workspace")
    paths.derived.mkdir(parents=True)
    source = np.full((4, 4, 4), 100, dtype=np.uint8)
    source_path = paths.workspace / "source.png"
    Image.fromarray(source, "RGBA").save(source_path)
    old_mask = np.zeros((4, 4), dtype=np.uint8)
    old_mask_path = tmp_path / "old-mask.png"
    Image.fromarray(old_mask, "L").save(old_mask_path)
    alpha = np.zeros((4, 4), dtype=np.uint8)
    alpha[1:3, 1:3] = 255
    neutral = source.copy()
    derived, effect, role_metrics = derive_linear_gloss(
        neutral,
        alpha,
        channel_deltas={"R": 20.0},
    )
    artifact_values = {
        "neutral_base": (neutral, "RGBA"),
        "projected_master_alpha": (alpha, "L"),
        "effect_mask": (effect.astype(np.uint8) * 255, "L"),
        "projected_protected": (np.zeros((4, 4), dtype=np.uint8), "L"),
        "projected_seam_guard": (np.zeros((4, 4), dtype=np.uint8), "L"),
    }
    artifacts = {}
    for name, (values, mode) in artifact_values.items():
        path = paths.derived / f"{name}.png"
        Image.fromarray(values, mode).save(path)
        artifacts[name] = {"path": str(path), "sha256": _sha256_file(path)}
    derived_path = paths.derived / "gloss.png"
    Image.fromarray(derived, "RGBA").save(derived_path)
    changed = np.any(derived != source, axis=2)
    output = {
        "policy": "neutralize_and_derive",
        "role": "gloss",
        "source_png": str(source_path),
        "derived_png": str(derived_path),
        "old_text_mask": "old-mask.png",
        "old_text_mask_sha256": _sha256_file(old_mask_path),
        "metrics": {
            **role_metrics,
            "mode_preserved": True,
            "changed_pixels": int(changed.sum()),
            "changed_outside_effect_union": 0,
            "changed_inside_protected": 0,
            "changed_inside_seam_guard": 0,
            "alignment": [
                {
                    "region_id": "front",
                    "center_error_texels": 0.0,
                    "bbox_edge_error_texels": 0.0,
                    "rotation_error_deg": 0.0,
                }
            ],
            "projection_signature": "continuous-alpha-same-st-integer-area:v1",
        },
        "derivation": {
            "producer": "linear-gloss-delta-from-master-alpha:v1",
            "master_lettering": [
                {
                    "region_id": "front",
                    "selected_lettering_sha256": "a" * 64,
                    "lettering_mask_sha256": "b" * 64,
                }
            ],
            "effect_parameters": {"channel_deltas": {"R": 20.0}},
            **artifacts,
        },
    }

    _validate_derived_material_output(
        output,
        expected_channels=["R"],
        derived_root=paths.derived,
    )
    _validate_derived_material_pixels(output, expected_channels=["R"], paths=paths)

    wrong_master = alpha.copy()
    wrong_master[0, 0] = 255
    with pytest.raises(ValueError, match="현재 승인 lettering"):
        _validate_derived_material_pixels(
            output,
            expected_channels=["R"],
            paths=paths,
            expected_projected_alpha=wrong_master,
        )

    tampered = derived.copy()
    tampered[0, 0, 0] += 1
    Image.fromarray(tampered, "RGBA").save(derived_path)
    with pytest.raises(ValueError, match="재실행 결과|union 밖"):
        _validate_derived_material_pixels(output, expected_channels=["R"], paths=paths)


def test_auxiliary_mip_contract_measures_shared_bc_seam_instead_of_blanket_block() -> None:
    source = np.full((8, 8, 4), 100, dtype=np.uint8)
    edited = source.copy()
    edited[0, 0, 0] = 140
    effect = np.zeros((8, 8), dtype=bool)
    effect[0, 0] = True
    seam = np.zeros((8, 8), dtype=bool)
    seam[0, 1] = True
    coverage = Image.new("L", (8, 8), 255)

    levels = _validate_auxiliary_mip_invariants(
        Image.fromarray(source, "RGBA"),
        Image.fromarray(edited, "RGBA"),
        role="gloss",
        count=4,
        coverage=coverage,
        selected_channels=["R"],
        effect_union=effect,
        protected=np.zeros_like(effect),
        seam_guard=np.zeros_like(effect),
    )
    assert all(len(group) == 4 for group in levels)

    merged_levels = _validate_auxiliary_mip_invariants(
        Image.fromarray(source, "RGBA"),
        Image.fromarray(edited, "RGBA"),
        role="gloss",
        count=4,
        coverage=coverage,
        selected_channels=["R"],
        effect_union=effect,
        protected=np.zeros_like(effect),
        seam_guard=seam,
    )
    empty_levels = [np.zeros_like(value) for value in merged_levels[2]]
    reports = _validate_compressed_auxiliary_invariants(
        merged_levels[0],
        merged_levels[0],
        merged_levels[1],
        merged_levels[0],
        merged_levels[0],
        merged_levels[1],
        empty_levels,
        merged_levels[2],
        merged_levels[3],
        merged_levels[4],
        merged_levels[5],
        role="gloss",
        selected_channels=["R"],
        block_size=4,
        max_mae=6.0,
    )

    assert reports[0]["protected_roundtrip_max"] == 0
    assert all(report["protected_roundtrip_max"] <= 48 for report in reports)


def test_bc_effect_expansion_handles_partial_edge_blocks() -> None:
    mask = np.zeros((5, 6), dtype=bool)
    mask[4, 5] = True

    expanded = _expand_to_bc_blocks(mask, 4)

    assert expanded[:4].sum() == 0
    assert expanded[4:, 4:].all()


def test_compressed_gloss_contract_blocks_disappearing_effect() -> None:
    source = np.full((4, 4, 4), 100, dtype=np.uint8)
    intended = source.copy()
    intended[..., 0] = 120
    mask = np.ones((4, 4), dtype=bool)
    empty = np.zeros_like(mask)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="효과가 사라졌어요"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(source),
            [empty],
            [mask],
            [empty],
            [empty],
            [mask],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


@pytest.mark.parametrize("delta", [1, 2])
def test_compressed_gloss_contract_preserves_exact_small_effect(delta: int) -> None:
    source = np.full((4, 4, 4), 100, dtype=np.uint8)
    intended = source.copy()
    intended[..., 0] += delta
    effect = np.ones((4, 4), dtype=bool)
    empty = np.zeros_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    reports = _validate_compressed_auxiliary_invariants(
        images(source),
        images(source),
        images(intended),
        images(source),
        images(source),
        images(intended),
        [empty],
        [effect],
        [empty],
        [empty],
        [effect],
        role="gloss",
        selected_channels=["R"],
        block_size=4,
        max_mae=6.0,
    )

    assert reports[0]["stages"]["new-effect"]["support_recall"] == 1.0


def test_compressed_gloss_contract_blocks_strong_bleed_from_weak_effect() -> None:
    source = np.full((32, 32, 4), 100, dtype=np.uint8)
    intended = source.copy()
    edited = source.copy()
    effect = np.zeros((32, 32), dtype=bool)
    effect[8:24, 9:25] = True
    intended[..., 0][effect] = 101
    edited[..., 0][effect] = 101
    edited[8:24, 8, 0] = 160
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="bleed"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(edited),
            [empty],
            [effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_checks_direct_deployed_scalar() -> None:
    source = np.zeros((8, 8, 4), dtype=np.uint8)
    source[..., 3] = 255
    intended = source.copy()
    intended[0, 0, 0] = 20
    source_roundtrip = source.copy()
    source_roundtrip[..., 0] = 60
    edited_roundtrip = source_roundtrip.copy()
    edited_roundtrip[0, 0, 0] = 80
    effect = np.zeros((8, 8), dtype=bool)
    effect[0, 0] = True
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="scalar ROI"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source_roundtrip),
            images(source_roundtrip),
            images(edited_roundtrip),
            [empty],
            [effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_old_effect_reappearing_in_final_payload() -> None:
    source = np.zeros((4, 4, 4), dtype=np.uint8)
    source[..., 3] = 255
    source[0, 0, 0] = 20
    neutral = source.copy()
    neutral[0, 0, 0] = 0
    edited = neutral.copy()
    final_roundtrip = edited.copy()
    final_roundtrip[0, 0, 0] = 20
    old_effect = np.zeros((4, 4), dtype=bool)
    old_effect[0, 0] = True
    new_effect = np.zeros((4, 4), dtype=bool)
    empty = np.zeros_like(old_effect)
    covered = np.ones_like(old_effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="old-effect 잔류 에너지"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(neutral),
            images(edited),
            images(source),
            images(neutral),
            images(final_roundtrip),
            [old_effect],
            [new_effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_partial_old_effect_reappearance() -> None:
    source = np.zeros((32, 32, 4), dtype=np.uint8)
    source[..., 3] = 255
    old_effect = np.zeros((32, 32), dtype=bool)
    old_effect[4:14, 4:14] = True
    source[..., 0][old_effect] = 20
    neutral = source.copy()
    neutral[..., 0][old_effect] = 0
    final_roundtrip = neutral.copy()
    old_positions = np.argwhere(old_effect)
    for y, x in old_positions[:40]:
        final_roundtrip[y, x, 0] = 20
    empty = np.zeros_like(old_effect)
    covered = np.ones_like(old_effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="old-effect 잔류 에너지"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(neutral),
            images(neutral),
            images(source),
            images(neutral),
            images(final_roundtrip),
            [old_effect],
            [empty],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_coarse_old_effect_reappearance() -> None:
    source0 = np.zeros((64, 64, 4), dtype=np.uint8)
    source1 = np.zeros((32, 32, 4), dtype=np.uint8)
    source0[..., 3] = 255
    source1[..., 3] = 255
    old0 = np.zeros((64, 64), dtype=bool)
    old0[4:14, 4:14] = True
    old1 = np.zeros((32, 32), dtype=bool)
    old1[4, 4:14] = True
    source0[..., 0][old0] = 20
    source1[..., 0][old1] = 20
    neutral0 = source0.copy()
    neutral1 = source1.copy()
    neutral0[..., 0][old0] = 0
    neutral1[..., 0][old1] = 0
    final1 = neutral1.copy()
    final1[..., 0][old1] = 20
    empty0 = np.zeros_like(old0)
    empty1 = np.zeros_like(old1)
    covered0 = np.ones_like(old0)
    covered1 = np.ones_like(old1)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="old-effect 잔류 에너지"):
        _validate_compressed_auxiliary_invariants(
            images(source0, source1),
            images(neutral0, neutral1),
            images(neutral0, neutral1),
            images(source0, source1),
            images(neutral0, neutral1),
            images(neutral0, final1),
            [old0, old1],
            [empty0, empty1],
            [empty0, empty1],
            [empty0, empty1],
            [covered0, covered1],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_single_coarse_old_effect_reappearance() -> None:
    source0 = np.zeros((8, 8, 4), dtype=np.uint8)
    source1 = np.zeros((4, 4, 4), dtype=np.uint8)
    source0[..., 3] = 255
    source1[..., 3] = 255
    old0 = np.zeros((8, 8), dtype=bool)
    old1 = np.zeros((4, 4), dtype=bool)
    old0[0, 0] = True
    old1[0, 0] = True
    source0[0, 0, 0] = 20
    source1[0, 0, 0] = 20
    neutral0 = source0.copy()
    neutral1 = source1.copy()
    neutral0[0, 0, 0] = 0
    neutral1[0, 0, 0] = 0
    final1 = neutral1.copy()
    final1[0, 0, 0] = 20
    empty0 = np.zeros_like(old0)
    empty1 = np.zeros_like(old1)
    covered0 = np.ones_like(old0)
    covered1 = np.ones_like(old1)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="old-effect 잔류 에너지"):
        _validate_compressed_auxiliary_invariants(
            images(source0, source1),
            images(neutral0, neutral1),
            images(neutral0, neutral1),
            images(source0, source1),
            images(neutral0, neutral1),
            images(neutral0, final1),
            [old0, old1],
            [empty0, empty1],
            [empty0, empty1],
            [empty0, empty1],
            [covered0, covered1],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_coarse_old_effect_edge_bleed() -> None:
    source0 = np.full((64, 64, 4), 200, dtype=np.uint8)
    source1 = np.full((32, 32, 4), 200, dtype=np.uint8)
    source0[..., 3] = 255
    source1[..., 3] = 255
    old0 = np.zeros((64, 64), dtype=bool)
    old1 = np.zeros((32, 32), dtype=bool)
    old0[8:24, 9:25] = True
    old1[8:16, 9:17] = True
    neutral0 = source0.copy()
    neutral1 = source1.copy()
    neutral0[..., 0][old0] = 100
    neutral1[..., 0][old1] = 180
    bled1 = neutral1.copy()
    for y, x in [(y, 8) for y in range(8, 16)][:5] + [
        (y, 17) for y in range(8, 16)
    ][:5]:
        bled1[y, x, 0] = 75
    empty0 = np.zeros_like(old0)
    empty1 = np.zeros_like(old1)
    covered0 = np.ones_like(old0)
    covered1 = np.ones_like(old1)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="(과도한 bleed|국소 최대 bleed)"):
        _validate_compressed_auxiliary_invariants(
            images(source0, source1),
            images(neutral0, neutral1),
            images(neutral0, neutral1),
            images(source0, source1),
            images(neutral0, bled1),
            images(neutral0, bled1),
            [old0, old1],
            [empty0, empty1],
            [empty0, empty1],
            [empty0, empty1],
            [covered0, covered1],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_strong_bleed_from_small_coarse_old_effect() -> None:
    source0 = np.full((32, 32, 4), 200, dtype=np.uint8)
    source1 = np.full((16, 16, 4), 200, dtype=np.uint8)
    source0[..., 3] = 255
    source1[..., 3] = 255
    old0 = np.zeros((32, 32), dtype=bool)
    old1 = np.zeros((16, 16), dtype=bool)
    old0[2:6, 3:9] = True
    old1[4:6, 5:9] = True
    neutral0 = source0.copy()
    neutral1 = source1.copy()
    neutral0[..., 0][old0] = 0
    neutral1[..., 0][old1] = 180
    bled1 = neutral1.copy()
    bled1[4, 4, 0] = 75
    bled1[5, 9, 0] = 75
    empty0 = np.zeros_like(old0)
    empty1 = np.zeros_like(old1)
    covered0 = np.ones_like(old0)
    covered1 = np.ones_like(old1)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="(과도한 bleed|국소 최대 bleed)"):
        _validate_compressed_auxiliary_invariants(
            images(source0, source1),
            images(neutral0, neutral1),
            images(neutral0, neutral1),
            images(source0, source1),
            images(neutral0, bled1),
            images(neutral0, bled1),
            [old0, old1],
            [empty0, empty1],
            [empty0, empty1],
            [empty0, empty1],
            [covered0, covered1],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_lost_small_coarse_new_effect() -> None:
    source0 = np.full((32, 32, 4), 100, dtype=np.uint8)
    source1 = np.full((16, 16, 4), 100, dtype=np.uint8)
    intended0 = source0.copy()
    intended1 = source1.copy()
    actual1 = source1.copy()
    effect0 = np.zeros((32, 32), dtype=bool)
    effect1 = np.zeros((16, 16), dtype=bool)
    effect0[2:12, 2:14] = True
    effect1[2:7, 2:8] = True
    intended0[..., 0][effect0] = 120
    intended1[..., 0][effect1] = 120
    actual1[..., 0][effect1] = 120
    for y, x in np.argwhere(effect1)[:8]:
        actual1[y, x, 0] = 100
    empty0 = np.zeros_like(effect0)
    empty1 = np.zeros_like(effect1)
    covered0 = np.ones_like(effect0)
    covered1 = np.ones_like(effect1)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="support 보존율"):
        _validate_compressed_auxiliary_invariants(
            images(source0, source1),
            images(source0, source1),
            images(intended0, intended1),
            images(source0, source1),
            images(source0, source1),
            images(intended0, actual1),
            [empty0, empty1],
            [effect0, effect1],
            [empty0, empty1],
            [empty0, empty1],
            [covered0, covered1],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_severely_weakened_effect() -> None:
    source = np.full((4, 4, 4), 100, dtype=np.uint8)
    intended = source.copy()
    intended[..., 0] = 120
    weakened = source.copy()
    weakened[..., 0] = 108
    effect = np.ones((4, 4), dtype=bool)
    empty = np.zeros_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="강도가 허용 범위를"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(weakened),
            [empty],
            [effect],
            [empty],
            [empty],
            [effect],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_local_polarity_reversal() -> None:
    source = np.full((8, 8, 4), 100, dtype=np.uint8)
    intended = source.copy()
    actual = source.copy()
    effect = np.zeros((8, 8), dtype=bool)
    effect[:4, :4] = True
    intended[..., 0][effect] = 110
    actual[..., 0][effect] = 110
    effect_positions = np.argwhere(effect)
    for y, x in effect_positions[:7]:
        actual[y, x, 0] = 90
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="국소 효과 방향"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(actual),
            [empty],
            [effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_one_texel_translation() -> None:
    source = np.full((32, 32, 4), 100, dtype=np.uint8)
    intended = source.copy()
    shifted = source.copy()
    intended_effect = np.zeros((32, 32), dtype=bool)
    intended_effect[4:24, 5:25] = True
    shifted_effect = np.zeros((32, 32), dtype=bool)
    shifted_effect[4:24, 6:26] = True
    intended[..., 0][intended_effect] = 120
    shifted[..., 0][shifted_effect] = 120
    empty = np.zeros_like(intended_effect)
    covered = np.ones_like(intended_effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="edge/중심"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(shifted),
            [empty],
            [intended_effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


@pytest.mark.parametrize("level", [0, 4])
def test_compressed_gloss_contract_blocks_high_energy_edge_bleed(level: int) -> None:
    source = np.full((128, 128, 4), 100, dtype=np.uint8)
    intended = source.copy()
    bled = source.copy()
    effect = np.zeros((128, 128), dtype=bool)
    effect[20:40, 17:37] = True
    intended[..., 0][effect] = 120
    bled[..., 0][effect] = 120
    bled[20:40, 16, 0] = 200
    bled[20:40, 37, 0] = 200
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    source_images = [Image.fromarray(source, "RGBA") for _ in range(level + 1)]
    intended_images = [
        Image.fromarray(source if index < level else intended, "RGBA")
        for index in range(level + 1)
    ]
    bled_images = [
        Image.fromarray(source if index < level else bled, "RGBA")
        for index in range(level + 1)
    ]
    masks = lambda final: [empty.copy() for _ in range(level)] + [final]

    with pytest.raises(
        ValueError, match="(과도한 bleed|국소 최대 bleed|edge bleed)"
    ):
        _validate_compressed_auxiliary_invariants(
            source_images,
            source_images,
            intended_images,
            source_images,
            source_images,
            bled_images,
            masks(empty),
            masks(effect),
            masks(empty),
            masks(empty),
            [covered.copy() for _ in range(level + 1)],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_isolated_maximum_new_effect_bleed() -> None:
    source = np.full((64, 64, 4), 100, dtype=np.uint8)
    intended = source.copy()
    edited = source.copy()
    effect = np.zeros((64, 64), dtype=bool)
    effect[16:48, 17:49] = True
    intended[..., 0][effect] = 120
    edited[..., 0][effect] = 120
    edited[16, 16, 0] = 200
    edited[47, 49, 0] = 200
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="국소 최대 bleed"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(edited),
            [empty],
            [effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_gloss_contract_blocks_additive_final_edge_bleed() -> None:
    source = np.full((64, 64, 4), 100, dtype=np.uint8)
    neutral = source.copy()
    edited = source.copy()
    neutral_roundtrip = source.copy()
    edited_roundtrip = source.copy()
    effect = np.zeros((64, 64), dtype=bool)
    effect[16:48, 17:49] = True
    neutral[..., 0][effect] = 80
    neutral_roundtrip[..., 0][effect] = 80
    neutral_roundtrip[16, 16, 0] = 140
    edited_roundtrip[16, 16, 0] = 180
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="최종 edge bleed"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(neutral),
            images(edited),
            images(source),
            images(neutral_roundtrip),
            images(edited_roundtrip),
            [effect],
            [effect],
            [empty],
            [empty],
            [covered],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_normal_contract_checks_invalid_bleed_inside_effect_blocks() -> None:
    source = np.full((64, 64, 4), 128, dtype=np.uint8)
    intended = source.copy()
    bled = source.copy()
    effect = np.zeros((64, 64), dtype=bool)
    effect[20:40, 17:37] = True
    intended[..., 3][effect] = 148
    bled[..., 3][effect] = 148
    bled[20, 16, 1] = 218
    bled[20, 16, 3] = 218
    bled[20, 37, 1] = 218
    bled[20, 37, 3] = 218
    empty = np.zeros_like(effect)
    covered = np.ones_like(effect)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="단위 원"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(bled),
            [empty],
            [effect],
            [empty],
            [empty],
            [covered],
            role="normal",
            selected_channels=["G", "A"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_contract_checks_deployed_protected_roi_at_coarse_mip() -> None:
    source0 = np.full((8, 8, 4), 100, dtype=np.uint8)
    source1 = np.full((4, 4, 4), 100, dtype=np.uint8)
    edited0 = source0.copy()
    edited0[0, 0, 0] = 120
    edited1 = source1.copy()
    edited1[0, 0, 0] = 120
    damaged1 = edited1.copy()
    damaged1[3, 3, 2] = 160
    new0 = np.zeros((8, 8), dtype=bool)
    new0[0, 0] = True
    new1 = np.zeros((4, 4), dtype=bool)
    new1[0, 0] = True
    protected0 = np.zeros((8, 8), dtype=bool)
    protected0[7, 7] = True
    protected1 = np.zeros((4, 4), dtype=bool)
    protected1[3, 3] = True
    empty0 = np.zeros_like(new0)
    empty1 = np.zeros_like(new1)
    covered0 = np.ones_like(new0)
    covered1 = np.ones_like(new1)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="protected/seam ROI"):
        _validate_compressed_auxiliary_invariants(
            images(source0, source1),
            images(source0, source1),
            images(edited0, edited1),
            images(source0, source1),
            images(source0, source1),
            images(edited0, damaged1),
            [empty0, empty1],
            [new0, new1],
            [protected0, protected1],
            [empty0, empty1],
            [covered0, covered1],
            role="gloss",
            selected_channels=["R"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_normal_contract_blocks_invalid_dxt5nm_xy() -> None:
    source = np.full((4, 4, 4), 128, dtype=np.uint8)
    intended = source.copy()
    intended[..., 1] = 200
    intended[..., 3] = 200
    invalid = source.copy()
    invalid[..., 1] = 218
    invalid[..., 3] = 218
    mask = np.ones((4, 4), dtype=bool)
    empty = np.zeros_like(mask)
    images = lambda values: [Image.fromarray(values, "RGBA")]

    with pytest.raises(ValueError, match="단위 원"):
        _validate_compressed_auxiliary_invariants(
            images(source),
            images(source),
            images(intended),
            images(source),
            images(source),
            images(invalid),
            [empty],
            [mask],
            [empty],
            [empty],
            [mask],
            role="normal",
            selected_channels=["G", "A"],
            block_size=4,
            max_mae=6.0,
        )


def test_compressed_normal_contract_blocks_invalid_protected_dxt5nm_xy() -> None:
    source = np.full((4, 4, 4), 128, dtype=np.uint8)
    source[0, 1, 1] = 217
    source[0, 1, 3] = 217
    intended = source.copy()
    intended[0, 0, 3] = 148
    invalid = intended.copy()
    invalid[0, 1, 1] = 218
    invalid[0, 1, 3] = 218
    effect = np.zeros((4, 4), dtype=bool)
    effect[0, 0] = True
    protected = np.zeros((4, 4), dtype=bool)
    protected[0, 1] = True
    empty = np.zeros_like(protected)
    images = lambda first, second: [
        Image.fromarray(first, "RGBA"),
        Image.fromarray(second, "RGBA"),
    ]

    with pytest.raises(ValueError, match="protected/seam.*단위 원"):
        _validate_compressed_auxiliary_invariants(
            images(source, source),
            images(source, source),
            images(source, intended),
            images(source, source),
            images(source, source),
            images(source, invalid),
            [empty, empty],
            [empty, effect],
            [empty, protected],
            [empty, empty],
            [protected, protected],
            role="normal",
            selected_channels=["G", "A"],
            block_size=4,
            max_mae=6.0,
        )


def test_source_bundle_must_match_inventory_hash(tmp_path: Path) -> None:
    bundle_key = "assets/content/item.bundle"
    source = tmp_path / bundle_key
    source.parent.mkdir(parents=True)
    source.write_bytes(b"inventory version")
    records = [{"bundle_key": bundle_key, "bundle_sha256": _sha256_file(source)}]

    assert _verified_source_bundle(bundle_key, tmp_path, None, records) == source.resolve()

    source.write_bytes(b"changed after inventory")
    with pytest.raises(ValueError, match="변경됐어요"):
        _verified_source_bundle(bundle_key, tmp_path, None, records)


def test_override_bundle_must_match_registered_hash(tmp_path: Path) -> None:
    source = tmp_path / "original.bundle"
    source.write_bytes(b"verified original")
    override = {"path": str(source), "sha256": _sha256_file(source)}

    assert _verified_source_bundle("item.bundle", tmp_path, override, []) == source.resolve()

    source.write_bytes(b"changed original")
    with pytest.raises(ValueError, match="변경됐어요"):
        _verified_source_bundle("item.bundle", tmp_path, override, [])


def test_override_bundle_allows_different_live_texture_payload(tmp_path: Path) -> None:
    bundle_key = "assets/content/item.bundle"
    live = tmp_path / bundle_key
    live.parent.mkdir(parents=True)
    live.write_bytes(b"already edited live texture payload")
    source = tmp_path / "original.bundle"
    source.write_bytes(b"verified original texture payload")
    override = {"path": str(source), "sha256": _sha256_file(source)}
    records = [{"bundle_key": bundle_key, "bundle_sha256": _sha256_file(live)}]

    assert _verified_source_bundle(
        bundle_key, tmp_path, override, records
    ) == source.resolve()


def test_missing_override_uses_matching_live_bundle(tmp_path: Path) -> None:
    bundle_key = "assets/content/item.bundle"
    live = tmp_path / bundle_key
    live.parent.mkdir(parents=True)
    live.write_bytes(b"verified original")
    override = {
        "path": str(tmp_path / "deleted-backup.bundle"),
        "sha256": _sha256_file(live),
    }

    assert _verified_source_bundle(bundle_key, tmp_path, override, []) == live.resolve()


def test_uv_coverage_uses_conservative_integer_downscale_for_auxiliary_map() -> None:
    values = np.zeros((4, 4), dtype=np.uint8)
    values[0, 0] = 255
    values[1, 3] = 255
    values[3, 2] = 255

    result = _coverage_values(Image.fromarray(values, "L"), (2, 2))

    np.testing.assert_array_equal(
        result,
        np.asarray([[True, True], [False, True]], dtype=bool),
    )


def test_auxiliary_mips_preserve_channels_not_changed_by_material_edit() -> None:
    values = np.arange(8 * 8 * 4, dtype=np.uint8).reshape((8, 8, 4))

    normal = values.copy()
    normal[2:6, 2:6, 1] = 200
    normal[2:6, 2:6, 3] = 180
    source_normal_mips = _mip_chain(Image.fromarray(values, "RGBA"), "normal", 4)
    changed_normal_mips = _mip_chain(Image.fromarray(normal, "RGBA"), "normal", 4)
    for source, changed in zip(source_normal_mips, changed_normal_mips, strict=True):
        np.testing.assert_array_equal(
            np.asarray(source)[..., [0, 2]],
            np.asarray(changed)[..., [0, 2]],
        )

    gloss = values.copy()
    gloss[2:6, 2:6, 0] = 220
    source_gloss_mips = _mip_chain(Image.fromarray(values, "RGBA"), "gloss", 4)
    changed_gloss_mips = _mip_chain(Image.fromarray(gloss, "RGBA"), "gloss", 4)
    for source, changed in zip(source_gloss_mips, changed_gloss_mips, strict=True):
        np.testing.assert_array_equal(
            np.asarray(source)[..., 1:],
            np.asarray(changed)[..., 1:],
        )


@pytest.mark.parametrize("size", [(3, 2), (2, 1), (8, 8)])
def test_uv_coverage_rejects_unsafe_size_conversion(size: tuple[int, int]) -> None:
    coverage = Image.new("L", (4, 4), 255)

    with pytest.raises(ValueError, match="UV coverage"):
        _coverage_values(coverage, size)


def test_partial_repack_prunes_proven_stale_preserved_auxiliary_bundle(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    bundle_key = "assets/item/textures.bundle"
    output = paths.bundles / bundle_key
    output.parent.mkdir(parents=True)
    output.write_bytes(b"stale derived normal")
    roundtrip = paths.reports / "roundtrip" / "item" / "sample_nrm.png"
    roundtrip.parent.mkdir(parents=True)
    roundtrip.write_bytes(b"stale preview")
    report = paths.reports / "bundles" / "assets@item@textures.bundle.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {
                "output_bundle": str(output.resolve()),
                "output_sha256": _sha256_file(output),
                "textures": [
                    {"texture": "sample_nrm", "roundtrip": str(roundtrip.resolve())}
                ],
            }
        ),
        encoding="utf-8",
    )
    inventory = {
        "records": [
            {
                "bundle_key": "assets/item/diffuse.bundle",
                "path_id": 1,
                "target_id": "sample",
            },
            {
                "bundle_key": bundle_key,
                "path_id": 2,
                "texture": "sample_nrm",
                "role": "normal",
            },
        ],
        "materials": [
            {
                "bundle_key": "assets/item/model.bundle",
                "assets_file": "CAB-MODEL",
                "path_id": 3,
                "texture_slots": [
                    {
                        "property": "_MainTex",
                        "texture_bundle_key": "assets/item/diffuse.bundle",
                        "path_id": 1,
                    },
                    {
                        "property": "_BumpMap",
                        "texture_bundle_key": bundle_key,
                        "path_id": 2,
                    },
                ],
            }
        ],
    }
    plans = {
        (bundle_key, 2, "normal"): {
            "policy": "preserve",
            "target_ids": ["sample"],
        }
    }

    pruned = _prune_stale_preserved_auxiliary_outputs(
        paths, inventory, plans, {"sample"}, {"assets/item/diffuse.bundle"}
    )

    assert [value["bundle_key"] for value in pruned] == [bundle_key]
    assert not output.exists()
    assert not report.exists()
    assert not roundtrip.exists()


def test_partial_repack_keeps_auxiliary_bundle_shared_with_unselected_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    bundle_key = "assets/shared/textures.bundle"
    output = paths.bundles / bundle_key
    output.parent.mkdir(parents=True)
    output.write_bytes(b"shared output")
    report = paths.reports / "bundles" / "assets@shared@textures.bundle.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {
                "output_bundle": str(output.resolve()),
                "output_sha256": _sha256_file(output),
                "textures": [{"texture": "shared_nrm"}],
            }
        ),
        encoding="utf-8",
    )
    inventory = {
        "records": [
            {"bundle_key": "a.bundle", "path_id": 1, "target_id": "selected"},
            {"bundle_key": "b.bundle", "path_id": 2, "target_id": "other"},
            {
                "bundle_key": bundle_key,
                "path_id": 9,
                "texture": "shared_nrm",
                "role": "normal",
            },
        ],
        "materials": [
            {
                "bundle_key": "a.model",
                "assets_file": "CAB-A-MODEL",
                "path_id": 10,
                "texture_slots": [
                    {"property": "_MainTex", "texture_bundle_key": "a.bundle", "path_id": 1},
                    {"property": "_BumpMap", "texture_bundle_key": bundle_key, "path_id": 9},
                ],
            },
            {
                "bundle_key": "b.model",
                "assets_file": "CAB-B-MODEL",
                "path_id": 11,
                "texture_slots": [
                    {"property": "_MainTex", "texture_bundle_key": "b.bundle", "path_id": 2},
                    {"property": "_BumpMap", "texture_bundle_key": bundle_key, "path_id": 9},
                ],
            },
        ],
    }
    plans = {
        (bundle_key, 9, "normal"): {
            "policy": "preserve",
            "target_ids": ["selected"],
        }
    }

    pruned = _prune_stale_preserved_auxiliary_outputs(
        paths, inventory, plans, {"selected"}, {"a.bundle"}
    )

    assert pruned == []
    assert output.is_file()
    assert report.is_file()
