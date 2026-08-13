from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
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
) -> dict[str, Any]:
    pointer = environment_value.m_Texture
    file_id = int(pointer.m_FileID)
    path_id = int(pointer.m_PathID)
    texture = local_textures.get(path_id) if file_id == 0 else None
    return {
        "property": str(property_name),
        "file_id": file_id,
        "path_id": path_id,
        "texture": texture,
        "texture_bundle_key": local_bundle_key if texture else None,
        "scale": [float(environment_value.m_Scale.x), float(environment_value.m_Scale.y)],
        "offset": [float(environment_value.m_Offset.x), float(environment_value.m_Offset.y)],
    }


def _external_materials(
    profile: CollectionProfile,
    bundle_root: Path,
    records: list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """게임 카탈로그의 역의존성을 따라 별도 모델 번들의 Material PPtr를 해석해요."""

    import UnityPy

    catalog_path = bundle_root / "Windows.json"
    if not catalog_path.is_file():
        raise FileNotFoundError(f"Material 역연결용 게임 카탈로그가 없어요: {catalog_path}")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    target_by_bundle: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if record.get("target_id"):
            target_by_bundle.setdefault(record["bundle_key"], []).append(record)
    known = {(value["bundle_key"], int(value["path_id"])) for value in existing}
    discovered: list[dict[str, Any]] = []
    for texture_bundle_key, target_records in target_by_bundle.items():
        candidates = sorted(
            key
            for key, metadata in catalog.items()
            if isinstance(metadata, dict)
            and texture_bundle_key in metadata.get("Dependencies", [])
            and key != texture_bundle_key
        )
        texture_path = bundle_root / Path(texture_bundle_key)
        all_path_ids = {
            int(record["path_id"]): record
            for record in records
            if record["bundle_key"] == texture_bundle_key
        }
        target_path_ids = {int(record["path_id"]) for record in target_records}
        resolved_target_ids: set[int] = set()
        for material_bundle_key in candidates:
            material_path = bundle_root / Path(material_bundle_key)
            if not material_path.is_file():
                continue
            try:
                environment = UnityPy.load(str(material_path), str(texture_path))
            except Exception as exc:
                raise RuntimeError(f"Material 의존 bundle을 읽지 못했어요: {material_bundle_key}") from exc
            for obj in environment.objects:
                if obj.type.name != "Material":
                    continue
                material = obj.read()
                slots: list[dict[str, Any]] = []
                consumes_target = False
                for property_name, environment_value in material.m_SavedProperties.m_TexEnvs:
                    pointer = environment_value.m_Texture
                    file_id = int(pointer.m_FileID)
                    path_id = int(pointer.m_PathID)
                    texture = None
                    resolved_bundle = None
                    if path_id:
                        try:
                            resolved = pointer.deref()
                            if resolved.type.name == "Texture2D":
                                texture = str(pointer.read().m_Name)
                                record = all_path_ids.get(int(resolved.path_id))
                                if record is not None:
                                    resolved_bundle = record["bundle_key"]
                                    if (
                                        int(resolved.path_id) in target_path_ids
                                        and property_name in {"_MainTex", "_BaseMap", "_BaseColorMap"}
                                    ):
                                        consumes_target = True
                                        resolved_target_ids.add(int(resolved.path_id))
                        except (FileNotFoundError, KeyError, ValueError):
                            pass
                    slots.append(
                        {
                            "property": str(property_name),
                            "file_id": file_id,
                            "path_id": path_id,
                            "texture": texture,
                            "texture_bundle_key": resolved_bundle,
                            "scale": [
                                float(environment_value.m_Scale.x),
                                float(environment_value.m_Scale.y),
                            ],
                            "offset": [
                                float(environment_value.m_Offset.x),
                                float(environment_value.m_Offset.y),
                            ],
                        }
                    )
                identity = (material_bundle_key, int(obj.path_id))
                if not consumes_target or identity in known:
                    continue
                known.add(identity)
                discovered.append(
                    {
                        "bundle_key": material_bundle_key,
                        "bundle_sha256": sha256_file(material_path),
                        "path_id": int(obj.path_id),
                        "material": str(material.m_Name),
                        "texture_slots": slots,
                    }
                )
        local_target_ids = {
            int(slot["path_id"])
            for material in existing
            if material["bundle_key"] == texture_bundle_key
            for slot in material["texture_slots"]
            if slot.get("property") in {"_MainTex", "_BaseMap", "_BaseColorMap"}
            and slot.get("texture_bundle_key") == texture_bundle_key
        }
        unresolved = target_path_ids - resolved_target_ids - local_target_ids
        if unresolved:
            names = sorted(
                record["target_id"]
                for record in target_records
                if int(record["path_id"]) in unresolved
            )
            raise ValueError(f"실제 Material 연결을 찾지 못한 target이 있어요: {names}")
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
    for bundle in profile.bundles:
        bundle_path = (bundle_root / Path(bundle.key)).resolve()
        if not bundle_path.is_file():
            missing.append(bundle.key)
            continue
        environment = UnityPy.load(str(bundle_path))
        bundle_sha256 = sha256_file(bundle_path)
        local_textures: dict[int, str] = {}
        for obj in environment.objects:
            if obj.type.name == "Texture2D":
                local_textures[int(obj.path_id)] = str(obj.read().m_Name)
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
                slots.append(_slot(property_name, environment_value, local_textures, bundle.key))
            materials.append(
                {
                    "bundle_key": bundle.key,
                    "bundle_sha256": bundle_sha256,
                    "path_id": int(obj.path_id),
                    "material": str(material.m_Name),
                    "texture_slots": slots,
                }
            )
    if not missing:
        materials.extend(_external_materials(profile, bundle_root, records, materials))
    _apply_source_overrides(records, overrides, paths)
    payload = {
        "schema_version": 2,
        "collection": profile.id,
        "profile": str(profile.path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_root": str(bundle_root),
        "missing_bundles": missing,
        "records": records,
        "materials": materials,
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
    if data.get("schema_version") not in {1, 2} or not isinstance(data.get("records"), list):
        raise ValueError(f"지원하지 않는 inventory예요: {path}")
    if data.get("schema_version") == 1:
        data.setdefault("materials", [])
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
