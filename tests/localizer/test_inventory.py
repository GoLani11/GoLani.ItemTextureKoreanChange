import json
from pathlib import Path

import pytest

from golani_texture_localizer.inventory import _apply_source_overrides, sha256_file
from golani_texture_localizer.paths import ProjectPaths


def test_source_override_survives_inventory_rescan(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    paths.source_overrides.mkdir(parents=True)
    source_png = paths.source_overrides / "mre.png"
    source_png.write_bytes(b"verified source pixels")
    source_bundle = tmp_path / "item_mre_assets.bundle"
    source_bundle.write_bytes(b"verified source bundle")
    bundle_key = "assets/content/item_mre_assets.bundle"
    records = [{"bundle_key": bundle_key, "target_id": "mre", "source_png": None}]
    overrides = {
        bundle_key: {
            "path": str(source_bundle),
            "sha256": sha256_file(source_bundle),
            "target_id": "mre",
            "source_png_sha256": sha256_file(source_png),
        }
    }

    _apply_source_overrides(records, overrides, paths)

    assert records[0]["source_png"] == str(source_png.resolve())
    assert records[0]["source_origin"] == "verified_override"
    assert records[0]["source_sha256"] == sha256_file(source_png)


def test_source_override_rejects_changed_png(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    paths.source_overrides.mkdir(parents=True)
    source_png = paths.source_overrides / "mre.png"
    source_png.write_bytes(b"changed pixels")
    source_bundle = tmp_path / "item_mre_assets.bundle"
    source_bundle.write_bytes(b"verified source bundle")
    bundle_key = "assets/content/item_mre_assets.bundle"
    records = [{"bundle_key": bundle_key, "target_id": "mre", "source_png": None}]
    overrides = {
        bundle_key: {
            "path": str(source_bundle),
            "sha256": sha256_file(source_bundle),
            "target_id": "mre",
            "source_png_sha256": "0" * 64,
        }
    }

    with pytest.raises(ValueError, match="PNG 해시"):
        _apply_source_overrides(records, overrides, paths)


def test_inventory_v1_loads_without_fabricating_material_bindings(tmp_path: Path) -> None:
    from golani_texture_localizer.inventory import load_inventory

    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema_version": 1, "records": []}), encoding="utf-8")

    assert load_inventory(path)["materials"] == []
