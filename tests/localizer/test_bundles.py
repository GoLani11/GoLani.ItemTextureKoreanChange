import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.bundles import (
    _coverage_values,
    _mip_chain,
    _prune_stale_preserved_auxiliary_outputs,
    _roundtrip_limits,
    _sha256_file,
    _validate_derived_material_output,
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
                "path_id": 10,
                "texture_slots": [
                    {"property": "_MainTex", "texture_bundle_key": "a.bundle", "path_id": 1},
                    {"property": "_BumpMap", "texture_bundle_key": bundle_key, "path_id": 9},
                ],
            },
            {
                "bundle_key": "b.model",
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
