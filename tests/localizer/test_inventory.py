from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import golani_texture_localizer.inventory as inventory_module
from golani_texture_localizer.inventory import (
    _apply_source_overrides,
    _auxiliary_pointer_identities,
    _external_materials,
    _inventory_material_pointer_graph_evidence,
    _material_dependency_snapshot,
    _material_pointer_graph_evidence,
    _material_pointer_identity,
    _material_targets,
    _slot,
    scan_collection,
    sha256_file,
    verify_material_dependency_snapshot,
    verify_source_override_material_graph,
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
    materials = []
    overrides = {
        bundle_key: {
            "path": str(source_bundle),
            "sha256": sha256_file(source_bundle),
            "target_id": "mre",
            "source_png_sha256": sha256_file(source_png),
            "material_pointer_graph": _material_pointer_graph_evidence(
                materials, bundle_key
            ),
        }
    }

    _apply_source_overrides(records, overrides, paths, materials)

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
    materials = []
    overrides = {
        bundle_key: {
            "path": str(source_bundle),
            "sha256": sha256_file(source_bundle),
            "target_id": "mre",
            "source_png_sha256": "0" * 64,
            "material_pointer_graph": _material_pointer_graph_evidence(
                materials, bundle_key
            ),
        }
    }

    with pytest.raises(ValueError, match="PNG 해시"):
        _apply_source_overrides(records, overrides, paths, materials)


def test_source_override_without_material_graph_is_safely_migrated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        inventory_module,
        "_raw_material_pointer_graph_from_bundle",
        lambda *_args: [],
    )

    _apply_source_overrides(records, overrides, paths, [])

    assert overrides[bundle_key]["material_pointer_graph"] == (
        _material_pointer_graph_evidence([], bundle_key)
    )


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
            "assets_file": "CAB-MODEL",
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
            "assets_file": "CAB-MODEL",
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

    assert _material_targets(records, materials) == {
        ("model.bundle", "CAB-MODEL", 11): ["sample"]
    }


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


def test_material_pointer_identity_distinguishes_serialized_assets_files() -> None:
    local = SimpleNamespace(m_FileID=0, m_PathID=7)
    external_object = SimpleNamespace(
        type=SimpleNamespace(name="Material"),
        assets_file=SimpleNamespace(name="CAB-SECOND"),
        path_id=7,
    )
    external = SimpleNamespace(
        m_FileID=1,
        m_PathID=7,
        deref=lambda: external_object,
    )

    assert _material_pointer_identity(
        local, SimpleNamespace(name="CAB-FIRST")
    ) == ("CAB-FIRST", 7)
    assert _material_pointer_identity(
        external, SimpleNamespace(name="CAB-FIRST")
    ) == ("CAB-SECOND", 7)


def test_scan_rejects_texture_path_collision_across_assets_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import UnityPy

    bundle_key = "assets/collision.bundle"
    bundle_root = tmp_path / "Windows"
    bundle_path = bundle_root / bundle_key
    bundle_path.parent.mkdir(parents=True)
    bundle_path.write_bytes(b"bundle")

    def texture_object(assets_file: str, name: str) -> SimpleNamespace:
        texture = SimpleNamespace(m_Name=name)
        return SimpleNamespace(
            type=SimpleNamespace(name="Texture2D"),
            assets_file=SimpleNamespace(name=assets_file),
            path_id=7,
            read=lambda: texture,
        )

    environment = SimpleNamespace(
        objects=[
            texture_object("CAB-FIRST", "first"),
            texture_object("CAB-SECOND", "second"),
        ]
    )
    monkeypatch.setattr(UnityPy, "load", lambda *_args: environment)
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    profile = SimpleNamespace(
        bundles=[SimpleNamespace(key=bundle_key, label="collision")],
        targets=[],
        ignored_diffuse_tokens=(),
    )

    with pytest.raises(ValueError, match="Texture2D path_id 7.*충돌"):
        scan_collection(profile, bundle_root, paths, extract=False)


def _raw_material(bundle_key: str = "textures.bundle") -> dict[str, object]:
    return {
        "bundle_key": bundle_key,
        "assets_file": "CAB-MATERIALS",
        "path_id": 17,
        "material": "diagnostic only",
        "texture_slots": [
            {
                "property": "_MainTex",
                "file_id": 0,
                "path_id": 31,
                "external_assets_file": None,
                "scale": [1.0, 1.0],
                "offset": [0.0, 0.0],
            },
            {
                "property": "_BumpMap",
                "file_id": 1,
                "path_id": 42,
                "external_assets_file": "CAB-NORMALS",
                "scale": [0.5, 0.75],
                "offset": [0.25, -0.5],
            },
        ],
    }


def test_material_pointer_graph_is_order_independent_and_ignores_names() -> None:
    bundle_key = "textures.bundle"
    first = _raw_material(bundle_key)
    second = deepcopy(first)
    second["material"] = "renamed diagnostic label"
    second["texture_slots"].reverse()

    assert _material_pointer_graph_evidence([first], bundle_key) == (
        _material_pointer_graph_evidence([second], bundle_key)
    )


def test_legacy_inventory_can_reregister_override_with_unique_assets_file() -> None:
    bundle_key = "textures.bundle"
    legacy_material = _raw_material(bundle_key)
    del legacy_material["assets_file"]
    inventory = {
        "schema_version": 2,
        "records": [{"bundle_key": bundle_key, "assets_file": "CAB-MATERIALS"}],
        "materials": [legacy_material],
    }

    assert _inventory_material_pointer_graph_evidence(
        inventory, bundle_key
    ) == _material_pointer_graph_evidence([_raw_material(bundle_key)], bundle_key)


def test_legacy_inventory_rejects_ambiguous_assets_file_identity() -> None:
    bundle_key = "textures.bundle"
    legacy_material = _raw_material(bundle_key)
    del legacy_material["assets_file"]
    inventory = {
        "schema_version": 2,
        "records": [
            {"bundle_key": bundle_key, "assets_file": "CAB-FIRST"},
            {"bundle_key": bundle_key, "assets_file": "CAB-SECOND"},
        ],
        "materials": [legacy_material],
    }

    with pytest.raises(ValueError, match="안전하게 복원할 수 없어요"):
        _inventory_material_pointer_graph_evidence(inventory, bundle_key)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("property", "_DetailMap"),
        ("file_id", 2),
        ("path_id", 43),
        ("external_assets_file", "CAB-OTHER"),
        ("scale", [0.5, 0.5]),
        ("offset", [0.0, -0.5]),
    ],
)
def test_material_pointer_graph_covers_every_texenv_field(
    field: str,
    replacement: object,
) -> None:
    bundle_key = "textures.bundle"
    original = _raw_material(bundle_key)
    changed = deepcopy(original)
    changed["texture_slots"][1][field] = replacement

    assert _material_pointer_graph_evidence([original], bundle_key) != (
        _material_pointer_graph_evidence([changed], bundle_key)
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("assets_file", "CAB-OTHER"), ("path_id", 18)],
)
def test_material_pointer_graph_distinguishes_serialized_material_identity(
    field: str,
    replacement: object,
) -> None:
    bundle_key = "textures.bundle"
    original = _raw_material(bundle_key)
    changed = deepcopy(original)
    changed[field] = replacement

    assert _material_pointer_graph_evidence([original], bundle_key) != (
        _material_pointer_graph_evidence([changed], bundle_key)
    )


def test_source_override_graph_is_reparsed_and_compared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_key = "textures.bundle"
    materials = [_raw_material(bundle_key)]
    evidence = _material_pointer_graph_evidence(materials, bundle_key)
    override = {"material_pointer_graph": evidence}
    bundle_path = tmp_path / "original.bundle"
    bundle_path.write_bytes(b"original texture payload")
    reordered = deepcopy(materials)
    reordered[0]["texture_slots"].reverse()
    monkeypatch.setattr(
        inventory_module,
        "_raw_material_pointer_graph_from_bundle",
        lambda *_args: reordered,
    )

    assert verify_source_override_material_graph(
        override, bundle_path, materials, bundle_key
    ) == evidence

    changed = deepcopy(materials)
    changed[0]["texture_slots"][0]["path_id"] = 999
    monkeypatch.setattr(
        inventory_module,
        "_raw_material_pointer_graph_from_bundle",
        lambda *_args: changed,
    )
    with pytest.raises(ValueError, match="raw Material graph"):
        verify_source_override_material_graph(
            override, bundle_path, materials, bundle_key
        )


def _dependency_fixture(tmp_path: Path) -> tuple[Path, list[dict[str, object]], Path]:
    bundle_root = tmp_path / "Windows"
    bundle_root.mkdir()
    texture_key = "assets/textures/item.bundle"
    candidate_key = "assets/models/item.bundle"
    candidate = bundle_root / candidate_key
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"material candidate")
    catalog = {
        texture_key: {"Dependencies": []},
        candidate_key: {"Dependencies": [texture_key]},
    }
    (bundle_root / "Windows.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    records = [
        {"bundle_key": texture_key, "target_id": "sample", "path_id": 7}
    ]
    return bundle_root, records, candidate


def test_material_dependency_snapshot_detects_candidate_byte_change(
    tmp_path: Path,
) -> None:
    bundle_root, records, candidate = _dependency_fixture(tmp_path)
    inventory = {
        "records": records,
        "material_dependency_snapshot": _material_dependency_snapshot(
            bundle_root, records
        ),
    }

    verify_material_dependency_snapshot(inventory, bundle_root)
    candidate.write_bytes(b"new Material consumer could be here")

    with pytest.raises(ValueError, match="후보 bundle이 변경"):
        verify_material_dependency_snapshot(inventory, bundle_root)


def test_material_dependency_snapshot_detects_catalog_change(tmp_path: Path) -> None:
    bundle_root, records, _ = _dependency_fixture(tmp_path)
    inventory = {
        "records": records,
        "material_dependency_snapshot": _material_dependency_snapshot(
            bundle_root, records
        ),
    }
    catalog_path = bundle_root / "Windows.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["unrelated.bundle"] = {"Dependencies": []}
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="Windows.json이 변경"):
        verify_material_dependency_snapshot(inventory, bundle_root)


def test_material_dependency_snapshot_requires_snapshot_and_existing_candidate(
    tmp_path: Path,
) -> None:
    bundle_root, records, candidate = _dependency_fixture(tmp_path)
    with pytest.raises(ValueError, match="extract를 다시 실행"):
        verify_material_dependency_snapshot({"records": records}, bundle_root)

    inventory = {
        "records": records,
        "material_dependency_snapshot": _material_dependency_snapshot(
            bundle_root, records
        ),
    }
    candidate.unlink()
    with pytest.raises(FileNotFoundError, match="reverse-dependency bundle"):
        verify_material_dependency_snapshot(inventory, bundle_root)


def test_material_dependency_snapshot_rejects_parent_traversal(tmp_path: Path) -> None:
    bundle_root = tmp_path / "Windows"
    bundle_root.mkdir()
    texture_key = "assets/textures/item.bundle"
    catalog = {
        "../outside.bundle": {"Dependencies": [texture_key]},
    }
    (bundle_root / "Windows.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    records = [{"bundle_key": texture_key, "target_id": "sample"}]

    with pytest.raises(ValueError, match="경로가 잘못"):
        _material_dependency_snapshot(bundle_root, records)


def _fake_material_object(
    path_id: int,
    name: str,
    assets_file_name: str,
    external_assets_files: list[str],
    slots: list[tuple[str, int, int]],
) -> SimpleNamespace:
    assets_file = SimpleNamespace(
        name=assets_file_name,
        externals=[SimpleNamespace(name=value) for value in external_assets_files],
    )
    texture_environments = []
    for property_name, file_id, texture_path_id in slots:
        texture_environments.append(
            (
                property_name,
                SimpleNamespace(
                    m_Texture=SimpleNamespace(
                        m_FileID=file_id, m_PathID=texture_path_id
                    ),
                    m_Scale=SimpleNamespace(x=1.0, y=1.0),
                    m_Offset=SimpleNamespace(x=0.0, y=0.0),
                ),
            )
        )
    material = SimpleNamespace(
        m_Name=name,
        m_SavedProperties=SimpleNamespace(m_TexEnvs=texture_environments),
    )
    return SimpleNamespace(
        type=SimpleNamespace(name="Material"),
        assets_file=assets_file,
        path_id=path_id,
        read=lambda: material,
    )


def test_auxiliary_only_shared_consumer_is_discovered_and_snapshotted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import UnityPy

    bundle_root = tmp_path / "Windows"
    bundle_root.mkdir()
    diffuse_key = "textures/a.bundle"
    auxiliary_key = "textures/b.bundle"
    direct_material_key = "models/c.bundle"
    shared_material_key = "models/d.bundle"
    for bundle_key in (direct_material_key, shared_material_key):
        path = bundle_root / bundle_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bundle_key.encode("ascii"))
    catalog = {
        direct_material_key: {"Dependencies": [diffuse_key, auxiliary_key]},
        shared_material_key: {"Dependencies": [auxiliary_key]},
    }
    catalog_path = bundle_root / "Windows.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    environments = {
        direct_material_key: SimpleNamespace(
            objects=[
                _fake_material_object(
                    301,
                    "A to B",
                    "CAB-C",
                    ["CAB-A", "CAB-B"],
                    [("_MainTex", 1, 101), ("_BumpMap", 2, 202)],
                )
            ]
        ),
        shared_material_key: SimpleNamespace(
            objects=[
                _fake_material_object(
                    302,
                    "D to B only",
                    "CAB-D",
                    ["CAB-B"],
                    [("_MainTex", 0, 404), ("_BumpMap", 1, 202)],
                )
            ]
        ),
    }

    def fake_load(path: str) -> SimpleNamespace:
        key = Path(path).relative_to(bundle_root).as_posix()
        return environments[key]

    monkeypatch.setattr(UnityPy, "load", fake_load)
    records = [
        {
            "bundle_key": diffuse_key,
            "path_id": 101,
            "target_id": "sample",
        }
    ]
    materials = _external_materials(
        SimpleNamespace(),
        bundle_root,
        records,
        [],
        {
            diffuse_key: {101: "sample_diffuse"},
            auxiliary_key: {202: "sample_normal"},
        },
        {"cab-a": {diffuse_key}, "cab-b": {auxiliary_key}},
        catalog=catalog,
    )

    assert {material["bundle_key"] for material in materials} == {
        direct_material_key,
        shared_material_key,
    }
    assert _auxiliary_pointer_identities(records, materials) == {
        auxiliary_key: {202}
    }
    shared = next(
        material
        for material in materials
        if material["bundle_key"] == shared_material_key
    )
    assert any(
        slot["texture_bundle_key"] == auxiliary_key and slot["path_id"] == 202
        for slot in shared["texture_slots"]
    )

    snapshot = _material_dependency_snapshot(
        bundle_root,
        records,
        materials,
        catalog=catalog,
        catalog_sha256=sha256_file(catalog_path),
    )
    assert snapshot["auxiliary_pointers"] == {auxiliary_key: [202]}
    assert {
        value["bundle_key"]
        for value in snapshot["reverse_dependencies"][auxiliary_key]
    } == {direct_material_key, shared_material_key}
