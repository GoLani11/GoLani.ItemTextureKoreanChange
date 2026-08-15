import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from golani_texture_localizer.inventory import (
    _apply_source_overrides,
    _material_targets,
    _slot,
    sha256_file,
)
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

    inventory = load_inventory(path)

    assert inventory["materials"] == []
    assert inventory["renderers"] == []


def test_material_targets_follow_only_actual_main_texture_slots() -> None:
    records = [
        {"bundle_key": "textures.bundle", "path_id": 7, "target_id": "sample"},
        {"bundle_key": "textures.bundle", "path_id": 8, "target_id": None},
    ]
    materials = [
        {
            "bundle_key": "model.bundle",
            "path_id": 11,
            "texture_slots": [
                {
                    "property": "_MainTex",
                    "texture_bundle_key": "textures.bundle",
                    "path_id": 7,
                },
                {
                    "property": "_BumpMap",
                    "texture_bundle_key": "textures.bundle",
                    "path_id": 8,
                },
            ],
        },
        {
            "bundle_key": "model.bundle",
            "path_id": 12,
            "texture_slots": [
                {
                    "property": "_BumpMap",
                    "texture_bundle_key": "textures.bundle",
                    "path_id": 7,
                }
            ],
        },
    ]

    assert _material_targets(records, materials) == {("model.bundle", 11): ["sample"]}


def test_slot_resolves_external_texture_from_serialized_file_table() -> None:
    pointer = SimpleNamespace(m_FileID=1, m_PathID=42)
    environment_value = SimpleNamespace(
        m_Texture=pointer,
        m_Scale=SimpleNamespace(x=1, y=1),
        m_Offset=SimpleNamespace(x=0, y=0),
    )
    assets_file = SimpleNamespace(
        externals=[SimpleNamespace(name="CAB-TEXTURES")]
    )

    slot = _slot(
        "_BumpMap",
        environment_value,
        {},
        "model.bundle",
        assets_file=assets_file,
        textures_by_bundle={"textures.bundle": {42: "sample_nrm"}},
        bundle_keys_by_assets_file={"cab-textures": {"textures.bundle"}},
    )

    assert slot["texture"] == "sample_nrm"
    assert slot["texture_bundle_key"] == "textures.bundle"
    assert slot["external_assets_file"] == "CAB-TEXTURES"


def test_slot_leaves_ambiguous_external_texture_unresolved() -> None:
    pointer = SimpleNamespace(m_FileID=1, m_PathID=42)
    environment_value = SimpleNamespace(
        m_Texture=pointer,
        m_Scale=SimpleNamespace(x=1, y=1),
        m_Offset=SimpleNamespace(x=0, y=0),
    )
    assets_file = SimpleNamespace(
        externals=[SimpleNamespace(name="CAB-DUPLICATE")]
    )

    slot = _slot(
        "_SpecMap",
        environment_value,
        {},
        "model.bundle",
        assets_file=assets_file,
        textures_by_bundle={
            "first.bundle": {42: "first_gloss"},
            "second.bundle": {42: "second_gloss"},
        },
        bundle_keys_by_assets_file={
            "cab-duplicate": {"first.bundle", "second.bundle"}
        },
    )

    assert slot["texture"] is None
    assert slot["texture_bundle_key"] is None
