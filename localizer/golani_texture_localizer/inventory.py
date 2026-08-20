from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
from typing import Any

from .models import CollectionProfile, ProfileError, TargetSpec
from .names import safe_bundle_name, texture_family, texture_role
from .paths import ProjectPaths


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _material_identity(material: Any) -> tuple[str, str, int]:
    if not isinstance(material, dict):
        raise ValueError("Material identity 항목이 객체가 아니에요")
    bundle_key = material.get("bundle_key")
    assets_file = material.get("assets_file")
    path_id = material.get("path_id")
    if (
        not isinstance(bundle_key, str)
        or not bundle_key
        or not isinstance(assets_file, str)
        or not assets_file
        or not isinstance(path_id, int)
        or isinstance(path_id, bool)
        or path_id == 0
    ):
        raise ValueError(
            "Material identity는 bundle_key/assets_file/0이 아닌 path_id가 필요해요. "
            "extract를 다시 실행해 주세요"
        )
    return bundle_key, assets_file, path_id


_MATERIAL_POINTER_GRAPH_SIGNATURE = "unity-material-texenv-pptr:v1"
_MATERIAL_DEPENDENCY_SNAPSHOT_SCHEMA = 3
_MAIN_TEXTURE_PROPERTIES = {"_MainTex", "_BaseMap", "_BaseColorMap"}
_AUXILIARY_TEXTURE_PROPERTIES = {
    "_BumpMap",
    "_NormalMap",
    "_SpecMap",
    "_GlossMap",
    "_MetallicGlossMap",
}


def _json_sha256(value: Any) -> str:
    packed = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _canonical_float_pair(value: Any, label: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label}는 숫자 두 개 배열이어야 해요")
    result: list[float] = []
    for item in value:
        if (
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
        ):
            raise ValueError(f"{label}에 유한하지 않은 값이 있어요")
        number = float(item)
        result.append(0.0 if number == 0.0 else number)
    return result


def _canonical_material_pointer_graph(
    materials: Any,
    bundle_key: str,
) -> list[dict[str, Any]]:
    """Material의 raw Texture PPtr/ST만 순서 독립적인 형태로 고정해요."""

    if not isinstance(materials, list):
        raise ValueError("inventory Material graph가 배열이 아니에요")
    result: list[dict[str, Any]] = []
    for material in materials:
        if not isinstance(material, dict) or material.get("bundle_key") != bundle_key:
            continue
        path_id = material.get("path_id")
        name = material.get("material")
        assets_file = material.get("assets_file")
        slots = material.get("texture_slots")
        if (
            not isinstance(path_id, int)
            or isinstance(path_id, bool)
            or not isinstance(assets_file, str)
            or not assets_file
            or not isinstance(slots, list)
        ):
            raise ValueError(f"{bundle_key} Material raw graph 항목이 잘못됐어요")
        diagnostic_name = name if isinstance(name, str) and name else str(path_id)
        canonical_slots: list[dict[str, Any]] = []
        for slot in slots:
            if not isinstance(slot, dict):
                raise ValueError(f"{bundle_key} Material texture slot이 객체가 아니에요")
            property_name = slot.get("property")
            file_id = slot.get("file_id")
            texture_path_id = slot.get("path_id")
            external_assets_file = slot.get("external_assets_file")
            if (
                not isinstance(property_name, str)
                or not property_name
                or not isinstance(file_id, int)
                or isinstance(file_id, bool)
                or not isinstance(texture_path_id, int)
                or isinstance(texture_path_id, bool)
                or (
                    external_assets_file is not None
                    and (
                        not isinstance(external_assets_file, str)
                        or not external_assets_file
                    )
                )
            ):
                raise ValueError(
                    f"{bundle_key}::{diagnostic_name} Material raw Texture PPtr가 잘못됐어요"
                )
            canonical_slots.append(
                {
                    "property": property_name,
                    "file_id": file_id,
                    "path_id": texture_path_id,
                    "scale": _canonical_float_pair(
                        slot.get("scale"),
                        f"{bundle_key}::{diagnostic_name}::{property_name} scale",
                    ),
                    "offset": _canonical_float_pair(
                        slot.get("offset"),
                        f"{bundle_key}::{diagnostic_name}::{property_name} offset",
                    ),
                    "external_assets_file": external_assets_file,
                }
            )
        canonical_slots.sort(
            key=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        result.append(
            {
                "assets_file": assets_file,
                "path_id": path_id,
                "texture_slots": canonical_slots,
            }
        )
    result.sort(
        key=lambda value: json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return result


def _material_pointer_graph_evidence(
    materials: Any,
    bundle_key: str,
) -> dict[str, Any]:
    canonical = _canonical_material_pointer_graph(materials, bundle_key)
    payload = {
        "signature": _MATERIAL_POINTER_GRAPH_SIGNATURE,
        "materials": canonical,
    }
    return {
        "signature": _MATERIAL_POINTER_GRAPH_SIGNATURE,
        "sha256": _json_sha256(payload),
        "material_count": len(canonical),
        "texture_slot_count": sum(
            len(material["texture_slots"]) for material in canonical
        ),
    }


def _inventory_material_pointer_graph_evidence(
    inventory: Any,
    bundle_key: str,
) -> dict[str, Any]:
    """v1/v2 inventory는 같은 serialized file이 유일할 때만 owner를 복원해요."""

    if not isinstance(inventory, dict):
        raise ValueError("inventory가 객체가 아니에요")
    materials = inventory.get("materials")
    if inventory.get("schema_version") not in {1, 2}:
        return _material_pointer_graph_evidence(materials, bundle_key)
    records = inventory.get("records")
    if not isinstance(materials, list) or not isinstance(records, list):
        raise ValueError("legacy inventory Material graph 입력이 잘못됐어요")
    assets_files = {
        record.get("assets_file")
        for record in records
        if isinstance(record, dict)
        and record.get("bundle_key") == bundle_key
        and isinstance(record.get("assets_file"), str)
        and record.get("assets_file")
    }
    normalized: list[Any] = []
    for material in materials:
        if (
            isinstance(material, dict)
            and material.get("bundle_key") == bundle_key
            and material.get("assets_file") is None
        ):
            if len(assets_files) != 1:
                raise ValueError(
                    f"{bundle_key} legacy inventory의 Material assets-file을 "
                    "안전하게 복원할 수 없어요"
                )
            material = {**material, "assets_file": next(iter(assets_files))}
        normalized.append(material)
    return _material_pointer_graph_evidence(normalized, bundle_key)


def _raw_material_pointer_graph_from_environment(
    environment: Any,
    bundle_key: str,
) -> list[dict[str, Any]]:
    materials: list[dict[str, Any]] = []
    for obj in environment.objects:
        if obj.type.name != "Material":
            continue
        material = obj.read()
        slots: list[dict[str, Any]] = []
        externals = getattr(obj.assets_file, "externals", [])
        for property_name, environment_value in material.m_SavedProperties.m_TexEnvs:
            pointer = environment_value.m_Texture
            file_id = int(pointer.m_FileID)
            external_assets_file = None
            if 0 < file_id <= len(externals):
                external_assets_file = str(externals[file_id - 1].name)
            slots.append(
                {
                    "property": str(property_name),
                    "file_id": file_id,
                    "path_id": int(pointer.m_PathID),
                    "scale": [
                        float(environment_value.m_Scale.x),
                        float(environment_value.m_Scale.y),
                    ],
                    "offset": [
                        float(environment_value.m_Offset.x),
                        float(environment_value.m_Offset.y),
                    ],
                    "external_assets_file": external_assets_file,
                }
            )
        materials.append(
            {
                "bundle_key": bundle_key,
                "assets_file": str(obj.assets_file.name),
                "path_id": int(obj.path_id),
                "material": str(material.m_Name),
                "texture_slots": slots,
            }
        )
    return materials


def _raw_material_pointer_graph_from_bundle(
    bundle_path: Path,
    bundle_key: str,
) -> list[dict[str, Any]]:
    try:
        import UnityPy
    except ImportError as exc:
        raise RuntimeError("Material graph 검증에는 UnityPy 1.25.0이 필요해요") from exc
    environment = UnityPy.load(bundle_path.read_bytes())
    return _raw_material_pointer_graph_from_environment(environment, bundle_key)


def _validate_recorded_override_material_graph(
    override: Any,
    materials: Any,
    bundle_key: str,
) -> dict[str, Any]:
    if not isinstance(override, dict):
        raise ValueError(f"source override 항목이 객체가 아니에요: {bundle_key}")
    recorded = override.get("material_pointer_graph")
    if not isinstance(recorded, dict):
        raise ValueError(
            f"{bundle_key} source override에 Material graph 증거가 없어요. "
            "source-override를 다시 등록해 주세요"
        )
    expected = _material_pointer_graph_evidence(materials, bundle_key)
    if recorded != expected:
        raise ValueError(
            f"{bundle_key} source override Material graph가 현재 inventory와 달라요. "
            "inventory를 다시 추출하고 source-override를 다시 등록해 주세요"
        )
    return expected


def verify_source_override_material_graph(
    override: Any,
    bundle_path: Path,
    materials: Any,
    bundle_key: str,
) -> dict[str, Any]:
    expected = _validate_recorded_override_material_graph(
        override, materials, bundle_key
    )
    actual = _material_pointer_graph_evidence(
        _raw_material_pointer_graph_from_bundle(bundle_path, bundle_key),
        bundle_key,
    )
    if actual != expected:
        raise ValueError(
            f"{bundle_key} source override bundle의 raw Material graph가 "
            "등록 증거와 달라요"
        )
    return actual


def _load_windows_catalog(bundle_root: Path) -> tuple[dict[str, Any], str]:
    catalog_path = bundle_root / "Windows.json"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Material 역연결용 게임 카탈로그가 없어요: {catalog_path}")
    try:
        raw_catalog = catalog_path.read_bytes()
        catalog = json.loads(raw_catalog.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"게임 카탈로그가 UTF-8 JSON이 아니에요: {catalog_path}") from exc
    if not isinstance(catalog, dict):
        raise ValueError(f"게임 카탈로그 루트가 객체가 아니에요: {catalog_path}")
    return catalog, hashlib.sha256(raw_catalog).hexdigest()


def _reverse_dependency_candidate_keys(
    catalog: dict[str, Any],
    texture_bundle_key: str,
) -> list[str]:
    return sorted(
        key
        for key, metadata in catalog.items()
        if isinstance(key, str)
        and key
        and isinstance(metadata, dict)
        and isinstance(metadata.get("Dependencies", []), list)
        and texture_bundle_key in metadata.get("Dependencies", [])
        and key != texture_bundle_key
    )


def _auxiliary_pointer_identities(
    records: Any,
    materials: Any,
) -> dict[str, set[int]]:
    """target Diffuse 소비 Material에서 직접 도달한 N/G PPtr만 반환해요."""

    if not isinstance(records, list) or not isinstance(materials, list):
        raise ValueError("inventory Material/Texture graph가 배열이 아니에요")
    targets = {
        (str(record["bundle_key"]), int(record["path_id"]))
        for record in records
        if isinstance(record, dict)
        and record.get("target_id")
        and isinstance(record.get("bundle_key"), str)
        and record.get("bundle_key")
        and isinstance(record.get("path_id"), int)
        and not isinstance(record.get("path_id"), bool)
    }
    result: dict[str, set[int]] = {}
    for material in materials:
        if not isinstance(material, dict):
            continue
        slots = material.get("texture_slots")
        if not isinstance(slots, list) or not any(
            isinstance(slot, dict)
            and slot.get("property") in _MAIN_TEXTURE_PROPERTIES
            and (
                str(slot.get("texture_bundle_key")),
                int(slot.get("path_id", 0)),
            )
            in targets
            for slot in slots
        ):
            continue
        for slot in slots:
            if not isinstance(slot, dict) or slot.get(
                "property"
            ) not in _AUXILIARY_TEXTURE_PROPERTIES:
                continue
            bundle_key = slot.get("texture_bundle_key")
            path_id = slot.get("path_id")
            if (
                isinstance(bundle_key, str)
                and bundle_key
                and isinstance(path_id, int)
                and not isinstance(path_id, bool)
                and path_id
            ):
                result.setdefault(bundle_key, set()).add(path_id)
    return result


def _material_dependency_snapshot(
    bundle_root: Path,
    records: Any,
    materials: Any | None = None,
    *,
    catalog: dict[str, Any] | None = None,
    catalog_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(records, list):
        raise ValueError("inventory records가 배열이 아니에요")
    if catalog is None or catalog_sha256 is None:
        catalog, catalog_sha256 = _load_windows_catalog(bundle_root)
    target_bundle_keys = {
        str(record["bundle_key"])
        for record in records
        if isinstance(record, dict)
        and record.get("target_id")
        and isinstance(record.get("bundle_key"), str)
        and record.get("bundle_key")
    }
    auxiliary_pointers = _auxiliary_pointer_identities(
        records, materials if materials is not None else []
    )
    observed_texture_bundle_keys = sorted(
        target_bundle_keys | set(auxiliary_pointers)
    )
    root = bundle_root.expanduser().resolve()
    hashes: dict[str, str] = {}

    def candidate_descriptors(dependency_bundle_key: str) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        for candidate_key in _reverse_dependency_candidate_keys(
            catalog, dependency_bundle_key
        ):
            relative = Path(candidate_key)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(
                    f"Windows.json reverse-dependency 경로가 잘못됐어요: {candidate_key}"
                )
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"Windows.json reverse-dependency가 bundle root 밖이에요: {candidate_key}"
                ) from exc
            if not candidate.is_file():
                raise FileNotFoundError(
                    f"Material reverse-dependency bundle이 없어요: {candidate}"
                )
            checksum = hashes.get(candidate_key)
            if checksum is None:
                checksum = sha256_file(candidate)
                hashes[candidate_key] = checksum
            candidates.append({"bundle_key": candidate_key, "sha256": checksum})
        return candidates

    reverse_dependencies = {
        texture_bundle_key: candidate_descriptors(texture_bundle_key)
        for texture_bundle_key in observed_texture_bundle_keys
    }
    renderer_dependencies = {
        material_bundle_key: candidate_descriptors(material_bundle_key)
        for material_bundle_key in sorted(
            {
                identity[0]
                for identity in _material_targets(
                    records, materials if materials is not None else []
                )
            }
        )
    }
    return {
        "schema_version": _MATERIAL_DEPENDENCY_SNAPSHOT_SCHEMA,
        "catalog": {"path": "Windows.json", "sha256": catalog_sha256},
        "auxiliary_pointers": {
            bundle_key: sorted(path_ids)
            for bundle_key, path_ids in sorted(auxiliary_pointers.items())
        },
        "reverse_dependencies": reverse_dependencies,
        "renderer_dependencies": renderer_dependencies,
    }


def verify_material_dependency_snapshot(
    inventory: Any,
    bundle_root: Path,
) -> dict[str, Any]:
    if not isinstance(inventory, dict):
        raise ValueError("inventory가 객체가 아니에요")
    recorded = inventory.get("material_dependency_snapshot")
    if not isinstance(recorded, dict):
        raise ValueError(
            "inventory에 Material reverse-dependency 스냅샷이 없어요. "
            "extract를 다시 실행해 주세요"
        )
    current = _material_dependency_snapshot(
        bundle_root,
        inventory.get("records"),
        inventory.get("materials"),
    )
    if recorded != current:
        recorded_catalog = recorded.get("catalog")
        if (
            not isinstance(recorded_catalog, dict)
            or recorded_catalog.get("sha256") != current["catalog"]["sha256"]
        ):
            raise ValueError(
                "inventory 이후 Windows.json이 변경됐어요. extract를 다시 실행해 주세요"
            )
        raise ValueError(
            "inventory 이후 Material reverse-dependency 후보 bundle이 변경됐어요. "
            "extract를 다시 실행해 주세요"
        )
    return current


def _target_for(profile: CollectionProfile, key: str, texture_name: str) -> TargetSpec | None:
    matches = [
        target
        for target in profile.targets
        if target.texture == texture_name and (target.bundle_key is None or target.bundle_key == key)
    ]
    if len(matches) > 1:
        raise ProfileError(f"대상 텍스처 매칭이 모호해요: {key}::{texture_name}")
    return matches[0] if matches else None


def _load_source_overrides(paths: ProjectPaths) -> dict[str, dict[str, Any]]:
    if not paths.source_override_manifest.is_file():
        return {}
    data = json.loads(paths.source_override_manifest.read_text(encoding="utf-8"))
    bundles = data.get("bundles")
    if data.get("schema_version") != 1 or not isinstance(bundles, dict):
        raise ValueError(f"지원하지 않는 source override manifest예요: {paths.source_override_manifest}")
    return bundles


def _apply_source_overrides(
    records: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]],
    paths: ProjectPaths,
    materials: list[dict[str, Any]] | None = None,
) -> None:
    """재스캔 뒤에도 검증된 원본 오버라이드를 무결성 검사와 함께 복원한다."""

    for bundle_key, override in overrides.items():
        if not isinstance(override, dict):
            raise ValueError(f"source override 항목이 객체가 아니에요: {bundle_key}")
        target_id = override.get("target_id")
        bundle_value = override.get("path")
        bundle_sha256 = override.get("sha256")
        if not all(isinstance(value, str) and value for value in (target_id, bundle_value, bundle_sha256)):
            raise ValueError(f"source override 필수 필드가 누락됐어요: {bundle_key}")

        matches = [
            record
            for record in records
            if record.get("bundle_key") == bundle_key and record.get("target_id") == target_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"source override 대상은 정확히 하나여야 해요: {bundle_key}::{target_id} "
                f"(현재 {len(matches)}개)"
            )

        bundle_path = Path(bundle_value).expanduser().resolve()
        if not bundle_path.is_file():
            raise FileNotFoundError(f"source override bundle을 찾지 못했어요: {bundle_path}")
        if sha256_file(bundle_path) != bundle_sha256:
            raise ValueError(f"source override bundle 해시가 달라졌어요: {bundle_path}")
        current_materials = materials if materials is not None else []
        if not isinstance(override.get("material_pointer_graph"), dict):
            expected_graph = _material_pointer_graph_evidence(
                current_materials, bundle_key
            )
            actual_graph = _material_pointer_graph_evidence(
                _raw_material_pointer_graph_from_bundle(bundle_path, bundle_key),
                bundle_key,
            )
            if actual_graph != expected_graph:
                raise ValueError(
                    f"{bundle_key} 기존 source override의 raw Material graph가 "
                    "현재 inventory와 달라요. source-override를 다시 등록해 주세요"
                )
            override["material_pointer_graph"] = actual_graph
        verify_source_override_material_graph(
            override,
            bundle_path,
            current_materials,
            bundle_key,
        )

        source_png = paths.source_overrides / f"{target_id}.png"
        if not source_png.is_file():
            raise FileNotFoundError(f"source override PNG를 찾지 못했어요: {source_png}")
        source_sha256 = sha256_file(source_png)
        expected_source_sha256 = override.get("source_png_sha256")
        if expected_source_sha256 is not None and expected_source_sha256 != source_sha256:
            raise ValueError(f"source override PNG 해시가 달라졌어요: {source_png}")

        override["source_png_sha256"] = source_sha256
        record = matches[0]
        record["source_png"] = str(source_png.resolve())
        record["source_origin"] = "verified_override"
        record["source_sha256"] = source_sha256


def _slot(
    property_name: str,
    environment_value: Any,
    local_textures: dict[int, str],
    local_bundle_key: str,
    *,
    assets_file: Any | None = None,
    textures_by_bundle: dict[str, dict[int, str]] | None = None,
    bundle_keys_by_assets_file: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    pointer = environment_value.m_Texture
    file_id = int(pointer.m_FileID)
    path_id = int(pointer.m_PathID)
    texture = local_textures.get(path_id) if file_id == 0 else None
    texture_bundle_key = local_bundle_key if texture else None
    external_assets_file = None
    if file_id and assets_file is not None:
        externals = getattr(assets_file, "externals", [])
        if 0 < file_id <= len(externals):
            external_assets_file = str(externals[file_id - 1].name)
            bundle_keys = (bundle_keys_by_assets_file or {}).get(
                external_assets_file.casefold(), set()
            )
            matches = [
                (bundle_key, (textures_by_bundle or {}).get(bundle_key, {}).get(path_id))
                for bundle_key in sorted(bundle_keys)
            ]
            matches = [(bundle_key, name) for bundle_key, name in matches if name]
            if len(matches) == 1:
                texture_bundle_key, texture = matches[0]
    return {
        "property": str(property_name),
        "file_id": file_id,
        "path_id": path_id,
        "texture": texture,
        "texture_bundle_key": texture_bundle_key,
        "external_assets_file": external_assets_file,
        "scale": [float(environment_value.m_Scale.x), float(environment_value.m_Scale.y)],
        "offset": [float(environment_value.m_Offset.x), float(environment_value.m_Offset.y)],
    }


def _material_targets(
    records: list[dict[str, Any]],
    materials: list[dict[str, Any]],
) -> dict[tuple[str, str, int], list[str]]:
    """실제 주 텍스처 슬롯을 기준으로 Material과 현지화 target을 연결해요."""

    target_by_texture = {
        (record["bundle_key"], int(record["path_id"])): str(record["target_id"])
        for record in records
        if record.get("target_id")
    }
    result: dict[tuple[str, str, int], list[str]] = {}
    for material in materials:
        target_ids = {
            target_by_texture[(slot["texture_bundle_key"], int(slot["path_id"]))]
            for slot in material.get("texture_slots", [])
            if slot.get("property") in _MAIN_TEXTURE_PROPERTIES
            and (slot.get("texture_bundle_key"), int(slot.get("path_id", 0))) in target_by_texture
        }
        if target_ids:
            identity = _material_identity(material)
            if identity in result:
                raise ValueError(f"Material identity가 중복됐어요: {identity}")
            result[identity] = sorted(target_ids)
    return result


def _mesh_pointer(renderer: Any) -> Any | None:
    pointer = getattr(renderer, "m_Mesh", None)
    if pointer is not None and int(pointer.m_PathID):
        return pointer
    game_object_pointer = getattr(renderer, "m_GameObject", None)
    if game_object_pointer is None or not int(game_object_pointer.m_PathID):
        return None
    game_object = game_object_pointer.read()
    for component in game_object.m_Component:
        candidate = component.component
        try:
            if candidate.deref().type.name == "MeshFilter":
                mesh_filter = candidate.read()
                if int(mesh_filter.m_Mesh.m_PathID):
                    return mesh_filter.m_Mesh
        except (FileNotFoundError, KeyError, ValueError):
            continue
    return None


def _material_pointer_identity(
    pointer: Any,
    owner_assets_file: Any,
) -> tuple[str, int] | None:
    if not int(pointer.m_PathID):
        return None
    if int(pointer.m_FileID) == 0:
        return str(owner_assets_file.name), int(pointer.m_PathID)
    try:
        resolved = pointer.deref()
    except (FileNotFoundError, KeyError, ValueError):
        return None
    if resolved.type.name != "Material":
        return None
    return str(resolved.assets_file.name), int(resolved.path_id)


def _renderer_meshes(
    bundle_root: Path,
    records: list[dict[str, Any]],
    materials: list[dict[str, Any]],
    paths: ProjectPaths,
    *,
    extract: bool,
) -> list[dict[str, Any]]:
    """target을 소비하는 Renderer와 실제 UV mesh를 찾아 검증 자료로 저장해요."""

    import numpy as np
    import UnityPy
    from UnityPy.helpers.MeshHelper import MeshHandler

    target_materials = _material_targets(records, materials)
    material_texture_dependencies: dict[str, set[str]] = {}
    for material in materials:
        identity = _material_identity(material)
        if identity not in target_materials:
            continue
        for slot in material.get("texture_slots", []):
            if slot.get("property") in _MAIN_TEXTURE_PROPERTIES and slot.get("texture_bundle_key"):
                material_texture_dependencies.setdefault(
                    material["bundle_key"], set()
                ).add(
                    str(slot["texture_bundle_key"])
                )

    catalog_path = bundle_root / "Windows.json"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Renderer/UV 의존성용 게임 카탈로그가 없어요: {catalog_path}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_bundle: dict[str, dict[tuple[str, int], list[str]]] = {}
    material_bundles_by_renderer: dict[str, set[str]] = {}
    for (
        material_bundle_key,
        material_assets_file,
        material_path_id,
    ), target_ids in target_materials.items():
        renderer_bundle_keys = {
            material_bundle_key,
            *_reverse_dependency_candidate_keys(catalog, material_bundle_key),
        }
        for renderer_bundle_key in renderer_bundle_keys:
            local_materials = by_bundle.setdefault(renderer_bundle_key, {})
            local_identity = (material_assets_file, material_path_id)
            existing_targets = local_materials.get(local_identity)
            if existing_targets is not None and existing_targets != target_ids:
                raise ValueError(
                    "Renderer Material assets-file/path identity가 여러 target으로 "
                    f"충돌해요: {renderer_bundle_key}::{local_identity}"
                )
            local_materials[local_identity] = target_ids
            material_bundles_by_renderer.setdefault(renderer_bundle_key, set()).add(
                material_bundle_key
            )

    def dependency_paths(bundle_key: str) -> list[Path]:
        metadata = catalog.get(bundle_key)
        dependencies = metadata.get("Dependencies", []) if isinstance(metadata, dict) else []
        bundle_parts = Path(bundle_key).parts
        product_token = ""
        if "usable_items" in bundle_parts:
            index = bundle_parts.index("usable_items")
            if index + 1 < len(bundle_parts):
                product_token = Path(bundle_parts[index + 1]).stem.removesuffix("_container")
        preferred = [
            value
            for value in dependencies
            if value.endswith("client_assets.bundle")
            and "/textures/" not in value
            and (not product_token or f"/{product_token}/" in value)
        ]
        if not preferred:
            preferred = [
                value
                for value in dependencies
                if value.endswith("client_assets.bundle") and "/textures/" not in value
            ]
        result = []
        for dependency in preferred:
            candidate = (bundle_root / Path(dependency)).resolve()
            if candidate.is_file():
                result.append(candidate)
        return result

    renderers: list[dict[str, Any]] = []
    resolved_targets: set[str] = set()
    mesh_files: dict[tuple[str, str, int], tuple[Path, dict[str, Any]]] = {}
    for bundle_key, local_materials in sorted(by_bundle.items()):
        bundle_path = (bundle_root / Path(bundle_key)).resolve()
        if not bundle_path.is_file():
            raise FileNotFoundError(f"Renderer/UV 추출용 bundle이 없어요: {bundle_path}")
        primary_environment = UnityPy.load(str(bundle_path))
        primary_asset_names = {str(obj.assets_file.name) for obj in primary_environment.objects}
        dependency_bundle_keys = set(material_bundles_by_renderer[bundle_key])
        for material_bundle_key in material_bundles_by_renderer[bundle_key]:
            dependency_bundle_keys.update(
                material_texture_dependencies.get(material_bundle_key, set())
            )
        texture_paths = [
            (bundle_root / Path(value)).resolve()
            for value in sorted(dependency_bundle_keys)
            if value != bundle_key and (bundle_root / Path(value)).is_file()
        ]
        base_environment = (
            UnityPy.load(str(bundle_path), *(str(value) for value in texture_paths))
            if texture_paths
            else primary_environment
        )

        def pointer_targets(pointer: Any, owner_assets_file: Any) -> list[str]:
            identity = _material_pointer_identity(pointer, owner_assets_file)
            if identity is None:
                return []
            return local_materials.get(identity, [])

        needs_external_mesh = False
        for candidate in base_environment.objects:
            if candidate.type.name not in {"MeshRenderer", "SkinnedMeshRenderer"}:
                continue
            if str(candidate.assets_file.name) not in primary_asset_names:
                continue
            candidate_renderer = candidate.read()
            uses_target = any(
                pointer_targets(pointer, candidate.assets_file)
                for pointer in candidate_renderer.m_Materials
            )
            mesh_candidate = _mesh_pointer(candidate_renderer) if uses_target else None
            if mesh_candidate is not None and int(mesh_candidate.m_FileID) != 0:
                needs_external_mesh = True
                break
        dependencies = dependency_paths(bundle_key) if needs_external_mesh else []
        environment = (
            UnityPy.load(
                str(bundle_path),
                *(str(value) for value in [*texture_paths, *dependencies]),
            )
            if dependencies
            else base_environment
        )
        for obj in environment.objects:
            if obj.type.name not in {"MeshRenderer", "SkinnedMeshRenderer"}:
                continue
            if str(obj.assets_file.name) not in primary_asset_names:
                continue
            renderer = obj.read()
            material_slots = []
            for index, pointer in enumerate(renderer.m_Materials):
                path_id = int(pointer.m_PathID)
                material_identity = _material_pointer_identity(pointer, obj.assets_file)
                target_ids = (
                    local_materials.get(material_identity, [])
                    if material_identity is not None
                    else []
                )
                material_slots.append(
                    {
                        "index": index,
                        "file_id": int(pointer.m_FileID),
                        "path_id": path_id,
                        "assets_file": (
                            material_identity[0]
                            if material_identity is not None
                            else None
                        ),
                        "target_ids": target_ids,
                    }
                )
            renderer_targets = sorted(
                {target_id for slot in material_slots for target_id in slot["target_ids"]}
            )
            if not renderer_targets:
                continue

            pointer = _mesh_pointer(renderer)
            if pointer is None:
                continue
            try:
                mesh_obj = pointer.deref()
                mesh = pointer.read()
            except (FileNotFoundError, KeyError, ValueError):
                continue
            if mesh_obj.type.name != "Mesh":
                continue

            mesh_assets_file = str(mesh_obj.assets_file.name)
            mesh_key = (bundle_key, mesh_assets_file, int(mesh_obj.path_id))
            cached = mesh_files.get(mesh_key)
            if cached is None:
                handler = MeshHandler(mesh)
                handler.process()
                vertices = np.asarray(handler.m_Vertices, dtype=np.float32)
                uv0 = (
                    np.asarray(handler.m_UV0, dtype=np.float32)
                    if handler.m_UV0 is not None
                    else np.empty((0, 2), dtype=np.float32)
                )
                normals = (
                    np.asarray(handler.m_Normals, dtype=np.float32)
                    if handler.m_Normals is not None
                    else np.empty((0, 3), dtype=np.float32)
                )
                tangents = (
                    np.asarray(handler.m_Tangents, dtype=np.float32)
                    if handler.m_Tangents is not None
                    else np.empty((0, 4), dtype=np.float32)
                )
                triangles = [np.asarray(value, dtype=np.int32) for value in handler.get_triangles()]
                if vertices.ndim != 2 or vertices.shape[1] < 3 or len(vertices) == 0:
                    continue
                if uv0.ndim != 2 or len(uv0) != len(vertices) or uv0.shape[1] < 2:
                    continue
                uv0 = uv0[:, :2]
                if not triangles or any(value.ndim != 2 or value.shape[1] != 3 for value in triangles):
                    continue
                uv_bounds = {
                    "min": [float(value) for value in uv0.min(axis=0)],
                    "max": [float(value) for value in uv0.max(axis=0)],
                }
                mesh_path = (
                    paths.meshes
                    / safe_bundle_name(bundle_key)
                    / f"{safe_bundle_name(mesh_assets_file)}@{mesh_obj.path_id}.npz"
                )
                if extract:
                    mesh_path.parent.mkdir(parents=True, exist_ok=True)
                    arrays: dict[str, Any] = {
                        "vertices": vertices[:, :3],
                        "uv0": uv0,
                        "normals": normals,
                        "tangents": tangents,
                    }
                    arrays.update({f"triangles_{index}": value for index, value in enumerate(triangles)})
                    np.savez_compressed(mesh_path, **arrays)
                mesh_metadata = {
                    "mesh_assets_file": mesh_assets_file,
                    "mesh_path_id": int(mesh_obj.path_id),
                    "mesh": str(mesh.m_Name),
                    "vertex_count": int(len(vertices)),
                    "submesh_count": len(triangles),
                    "triangle_count": int(sum(len(value) for value in triangles)),
                    "uv0_bounds": uv_bounds,
                }
                cached = (mesh_path, mesh_metadata)
                mesh_files[mesh_key] = cached

            mesh_path, mesh_metadata = cached
            target_submeshes: dict[str, list[int]] = {target_id: [] for target_id in renderer_targets}
            if material_slots:
                for submesh_index in range(int(mesh_metadata["submesh_count"])):
                    slot = material_slots[min(submesh_index, len(material_slots) - 1)]
                    for target_id in slot["target_ids"]:
                        target_submeshes.setdefault(target_id, []).append(submesh_index)
            target_submeshes = {
                target_id: submeshes
                for target_id, submeshes in target_submeshes.items()
                if submeshes
            }
            if not target_submeshes:
                continue
            renderer_targets = sorted(target_submeshes)
            game_object = getattr(renderer, "m_GameObject", None)
            game_object_name = None
            if game_object is not None and int(game_object.m_PathID):
                game_object_name = str(game_object.read().m_Name)
            resolved_targets.update(renderer_targets)
            renderers.append(
                {
                    "bundle_key": bundle_key,
                    "bundle_sha256": sha256_file(bundle_path),
                    "assets_file": str(obj.assets_file.name),
                    "path_id": int(obj.path_id),
                    "renderer_type": obj.type.name,
                    "game_object": game_object_name,
                    "target_ids": renderer_targets,
                    "target_submeshes": target_submeshes,
                    "material_slots": material_slots,
                    "mesh_file": str(mesh_path.resolve()) if extract else None,
                    **mesh_metadata,
                }
            )

    expected_targets = {str(record["target_id"]) for record in records if record.get("target_id")}
    missing_targets = sorted(expected_targets - resolved_targets)
    if missing_targets:
        raise ValueError(f"실제 Renderer/UV Mesh 연결을 찾지 못한 target이 있어요: {missing_targets}")
    return renderers


def _external_materials(
    profile: CollectionProfile,
    bundle_root: Path,
    records: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    textures_by_bundle: dict[str, dict[int, str]],
    bundle_keys_by_assets_file: dict[str, set[str]],
    *,
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """게임 카탈로그의 역의존성을 따라 별도 모델 번들의 Material PPtr를 해석해요."""

    import UnityPy

    if catalog is None:
        catalog, _ = _load_windows_catalog(bundle_root)
    target_by_bundle: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("target_id"):
            target_by_bundle.setdefault(record["bundle_key"], []).append(record)
    known = {_material_identity(value) for value in existing}
    discovered: list[dict[str, Any]] = []
    candidate_cache: dict[str, list[dict[str, Any]]] = {}

    def candidate_materials(material_bundle_key: str) -> list[dict[str, Any]]:
        cached = candidate_cache.get(material_bundle_key)
        if cached is not None:
            return cached
        material_path = bundle_root / Path(material_bundle_key)
        if not material_path.is_file():
            raise FileNotFoundError(
                f"Material reverse-dependency bundle이 없어요: {material_path}"
            )
        try:
            environment = UnityPy.load(str(material_path))
        except Exception as exc:
            raise RuntimeError(
                f"Material 의존 bundle을 읽지 못했어요: {material_bundle_key}"
            ) from exc
        local_by_assets_file: dict[str, dict[int, str]] = {}
        for obj in environment.objects:
            if obj.type.name == "Texture2D":
                local_by_assets_file.setdefault(
                    str(obj.assets_file.name).casefold(), {}
                )[int(obj.path_id)] = str(obj.read().m_Name)
        bundle_sha256 = sha256_file(material_path)
        parsed: list[dict[str, Any]] = []
        for obj in environment.objects:
            if obj.type.name != "Material":
                continue
            material = obj.read()
            assets_file_name = str(obj.assets_file.name)
            origin_bundle_keys = bundle_keys_by_assets_file.get(
                assets_file_name.casefold(), set()
            )
            if len(origin_bundle_keys) == 1:
                origin_bundle_key = next(iter(origin_bundle_keys))
                local_textures = textures_by_bundle.get(origin_bundle_key, {})
            else:
                origin_bundle_key = material_bundle_key
                local_textures = local_by_assets_file.get(
                    assets_file_name.casefold(), {}
                )
            slots = [
                _slot(
                    property_name,
                    environment_value,
                    local_textures,
                    origin_bundle_key,
                    assets_file=obj.assets_file,
                    textures_by_bundle=textures_by_bundle,
                    bundle_keys_by_assets_file=bundle_keys_by_assets_file,
                )
                for property_name, environment_value in material.m_SavedProperties.m_TexEnvs
            ]
            parsed.append(
                {
                    "bundle_key": material_bundle_key,
                    "bundle_sha256": bundle_sha256,
                    "assets_file": assets_file_name,
                    "path_id": int(obj.path_id),
                    "material": str(material.m_Name),
                    "texture_slots": slots,
                }
            )
        candidate_cache[material_bundle_key] = parsed
        return parsed

    def consumes(
        material: dict[str, Any],
        texture_bundle_key: str,
        path_ids: set[int],
        *,
        properties: set[str] | None = None,
    ) -> bool:
        return any(
            slot.get("texture_bundle_key") == texture_bundle_key
            and int(slot.get("path_id", 0)) in path_ids
            and (properties is None or slot.get("property") in properties)
            for slot in material.get("texture_slots", [])
            if isinstance(slot, dict)
        )

    for texture_bundle_key, target_records in target_by_bundle.items():
        candidates = _reverse_dependency_candidate_keys(catalog, texture_bundle_key)
        target_path_ids = {int(record["path_id"]) for record in target_records}
        for material_bundle_key in candidates:
            for material in candidate_materials(material_bundle_key):
                identity = _material_identity(material)
                if identity in known or not consumes(
                    material,
                    texture_bundle_key,
                    target_path_ids,
                    properties=_MAIN_TEXTURE_PROPERTIES,
                ):
                    continue
                known.add(identity)
                discovered.append(material)
        resolved_target_ids = {
            int(slot.get("path_id", 0))
            for material in [*existing, *discovered]
            for slot in material.get("texture_slots", [])
            if isinstance(slot, dict)
            and slot.get("property") in _MAIN_TEXTURE_PROPERTIES
            and slot.get("texture_bundle_key") == texture_bundle_key
        }
        unresolved = target_path_ids - resolved_target_ids
        if unresolved:
            names = sorted(
                record["target_id"]
                for record in target_records
                if int(record["path_id"]) in unresolved
            )
            raise ValueError(f"실제 Material 연결을 찾지 못한 target이 있어요: {names}")

    auxiliary_pointers = _auxiliary_pointer_identities(
        records, [*existing, *discovered]
    )
    for texture_bundle_key, path_ids in sorted(auxiliary_pointers.items()):
        for material_bundle_key in _reverse_dependency_candidate_keys(
            catalog, texture_bundle_key
        ):
            for material in candidate_materials(material_bundle_key):
                identity = _material_identity(material)
                if identity in known or not consumes(
                    material, texture_bundle_key, path_ids
                ):
                    continue
                known.add(identity)
                discovered.append(material)
    return discovered


def scan_collection(
    profile: CollectionProfile,
    bundle_root: Path,
    paths: ProjectPaths,
    *,
    extract: bool,
) -> dict[str, Any]:
    try:
        import UnityPy
    except ImportError as exc:
        raise RuntimeError("UnityPy 1.25.0이 필요해요. pyproject.toml 의존성을 설치해 주세요") from exc

    overrides = _load_source_overrides(paths)
    records: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    missing: list[str] = []
    loaded_bundles: list[tuple[Any, Path, Any, str, dict[int, str]]] = []
    textures_by_bundle: dict[str, dict[int, str]] = {}
    bundle_keys_by_assets_file: dict[str, set[str]] = {}
    for bundle in profile.bundles:
        bundle_path = (bundle_root / Path(bundle.key)).resolve()
        if not bundle_path.is_file():
            missing.append(bundle.key)
            continue
        environment = UnityPy.load(str(bundle_path))
        bundle_sha256 = sha256_file(bundle_path)
        local_textures: dict[int, str] = {}
        local_texture_owners: dict[int, str] = {}
        for obj in environment.objects:
            bundle_keys_by_assets_file.setdefault(
                str(obj.assets_file.name).casefold(), set()
            ).add(bundle.key)
            if obj.type.name == "Texture2D":
                path_id = int(obj.path_id)
                assets_file_name = str(obj.assets_file.name)
                previous_owner = local_texture_owners.get(path_id)
                if previous_owner is not None and previous_owner != assets_file_name:
                    raise ValueError(
                        f"{bundle.key} Texture2D path_id {path_id}가 서로 다른 "
                        f"serialized assets file({previous_owner}, {assets_file_name})에서 "
                        "충돌해요. 이 bundle은 자동 처리하지 않아요"
                    )
                local_texture_owners[path_id] = assets_file_name
                local_textures[path_id] = str(obj.read().m_Name)
        textures_by_bundle[bundle.key] = local_textures
        loaded_bundles.append(
            (bundle, bundle_path, environment, bundle_sha256, local_textures)
        )

    for bundle, bundle_path, environment, bundle_sha256, local_textures in loaded_bundles:
        for obj in environment.objects:
            if obj.type.name != "Texture2D":
                continue
            texture = obj.read()
            name = str(texture.m_Name)
            target = _target_for(profile, bundle.key, name)
            role = texture_role(name)
            ignored = role == "diffuse" and any(
                token in name.lower() for token in profile.ignored_diffuse_tokens
            )
            source_png = paths.source / safe_bundle_name(bundle.key) / f"{name}.png"
            if extract:
                source_png.parent.mkdir(parents=True, exist_ok=True)
                texture.image.save(source_png)
            stream = getattr(texture, "m_StreamData", None)
            settings = texture.m_TextureSettings
            records.append(
                {
                    "bundle_key": bundle.key,
                    "bundle_label": bundle.label,
                    "bundle_sha256": bundle_sha256,
                    "path_id": int(obj.path_id),
                    "assets_file": str(obj.assets_file.name),
                    "texture": name,
                    "family": texture_family(name),
                    "role": role,
                    "width": int(texture.m_Width),
                    "height": int(texture.m_Height),
                    "format": int(texture.m_TextureFormat),
                    "mip_count": int(texture.m_MipCount),
                    "filter_mode": int(settings.m_FilterMode),
                    "aniso": int(settings.m_Aniso),
                    "mip_bias": float(settings.m_MipBias),
                    "wrap_u": int(settings.m_WrapU),
                    "wrap_v": int(settings.m_WrapV),
                    "wrap_w": int(settings.m_WrapW),
                    "stream_path": str(getattr(stream, "path", "")),
                    "stream_offset": int(getattr(stream, "offset", 0)),
                    "stream_size": int(getattr(stream, "size", 0)),
                    "source_png": str(source_png.resolve()) if extract else None,
                    "target_id": target.id if target else None,
                    "ignored": ignored,
                }
            )
        for obj in environment.objects:
            if obj.type.name != "Material":
                continue
            material = obj.read()
            slots = []
            for property_name, environment_value in material.m_SavedProperties.m_TexEnvs:
                slots.append(
                    _slot(
                        property_name,
                        environment_value,
                        local_textures,
                        bundle.key,
                        assets_file=obj.assets_file,
                        textures_by_bundle=textures_by_bundle,
                        bundle_keys_by_assets_file=bundle_keys_by_assets_file,
                    )
                )
            materials.append(
                {
                    "bundle_key": bundle.key,
                    "bundle_sha256": bundle_sha256,
                    "assets_file": str(obj.assets_file.name),
                    "path_id": int(obj.path_id),
                    "material": str(material.m_Name),
                    "texture_slots": slots,
                }
            )
    material_dependency_snapshot = None
    if not missing:
        catalog, catalog_sha256 = _load_windows_catalog(bundle_root)
        materials.extend(
            _external_materials(
                profile,
                bundle_root,
                records,
                materials,
                textures_by_bundle,
                bundle_keys_by_assets_file,
                catalog=catalog,
            )
        )
        material_dependency_snapshot = _material_dependency_snapshot(
            bundle_root,
            records,
            materials,
            catalog=catalog,
            catalog_sha256=catalog_sha256,
        )
    _apply_source_overrides(records, overrides, paths, materials)
    renderers = (
        _renderer_meshes(bundle_root, records, materials, paths, extract=extract)
        if not missing
        else []
    )
    payload = {
        "schema_version": 3,
        "collection": profile.id,
        "profile": str(profile.path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_root": str(bundle_root),
        "missing_bundles": missing,
        "records": records,
        "materials": materials,
        "renderers": renderers,
        "material_dependency_snapshot": material_dependency_snapshot,
        "source_bundle_overrides": overrides,
    }
    paths.inventory.parent.mkdir(parents=True, exist_ok=True)
    paths.inventory.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if overrides:
        paths.source_override_manifest.write_text(
            json.dumps({"schema_version": 1, "bundles": overrides}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return payload


def load_inventory(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") not in {1, 2, 3} or not isinstance(data.get("records"), list):
        raise ValueError(f"지원하지 않는 inventory예요: {path}")
    data.setdefault("materials", [])
    data.setdefault("renderers", [])
    return data


def record_for_target(inventory: dict[str, Any], target: TargetSpec) -> dict[str, Any]:
    matches = [record for record in inventory["records"] if record.get("target_id") == target.id]
    if len(matches) != 1:
        raise ValueError(f"{target.id}의 원본 텍스처는 정확히 하나여야 해요. 현재 {len(matches)}개예요")
    return matches[0]


def register_source_override(
    profile: CollectionProfile,
    paths: ProjectPaths,
    target_id: str,
    image_path: Path,
    bundle_path: Path,
) -> dict[str, Any]:
    """기존 작업의 검증된 원본을 라이브 설치본 대신 기준으로 등록한다."""

    try:
        import UnityPy
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("source override 검증에는 UnityPy, NumPy와 Pillow가 필요해요") from exc

    inventory = load_inventory(paths.inventory)
    target = profile.target_by_id(target_id)
    record = record_for_target(inventory, target)
    image_path = image_path.expanduser().resolve()
    bundle_path = bundle_path.expanduser().resolve()
    for candidate in (image_path, bundle_path):
        if not candidate.is_file():
            raise FileNotFoundError(candidate)

    environment = UnityPy.load(bundle_path.read_bytes())
    try:
        inventory_material_graph = _inventory_material_pointer_graph_evidence(
            inventory, record["bundle_key"]
        )
    except ValueError as exc:
        raise ValueError(
            "inventory에 canonical Material graph 입력이 없어요. "
            "extract를 다시 실행한 뒤 source-override를 등록해 주세요"
        ) from exc
    override_material_graph = _material_pointer_graph_evidence(
        _raw_material_pointer_graph_from_environment(
            environment, record["bundle_key"]
        ),
        record["bundle_key"],
    )
    if override_material_graph != inventory_material_graph:
        raise ValueError(
            "override bundle의 raw Material graph가 inventory와 달라요. "
            "같은 Material PPtr/ST를 가진 원본 bundle을 등록해 주세요"
        )
    matches = []
    for obj in environment.objects:
        if obj.type.name != "Texture2D":
            continue
        texture = obj.read()
        if texture.m_Name == target.texture:
            matches.append(texture)
    if len(matches) != 1:
        raise ValueError(
            f"override bundle에서 {target.texture!r} Texture2D를 하나 찾아야 해요. 현재 {len(matches)}개예요"
        )
    texture = matches[0]
    with Image.open(image_path) as source_file:
        supplied = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
    extracted = np.asarray(texture.image.convert("RGBA"), dtype=np.uint8)
    if supplied.shape != extracted.shape or not np.array_equal(supplied, extracted):
        raise ValueError("override PNG가 override bundle의 대상 Texture2D와 픽셀 단위로 같지 않아요")

    destination = paths.source_overrides / f"{target.id}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(image_path, destination)
    record["source_png"] = str(destination.resolve())
    record["source_origin"] = "verified_override"
    record["source_sha256"] = sha256_file(destination)
    source_png_sha256 = sha256_file(destination)
    inventory.setdefault("source_bundle_overrides", {})[record["bundle_key"]] = {
        "path": str(bundle_path),
        "sha256": sha256_file(bundle_path),
        "target_id": target.id,
        "source_png_sha256": source_png_sha256,
        "material_pointer_graph": override_material_graph,
    }
    paths.inventory.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "bundles": inventory["source_bundle_overrides"],
    }
    paths.source_override_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "target_id": target.id,
        "source_png": str(destination),
        "source_png_sha256": source_png_sha256,
        "source_bundle": str(bundle_path),
        "source_bundle_sha256": sha256_file(bundle_path),
    }
