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
    missing: list[str] = []
    for bundle in profile.bundles:
        bundle_path = (bundle_root / Path(bundle.key)).resolve()
        if not bundle_path.is_file():
            missing.append(bundle.key)
            continue
        environment = UnityPy.load(str(bundle_path))
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
                    "bundle_sha256": sha256_file(bundle_path),
                    "path_id": int(obj.path_id),
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
    _apply_source_overrides(records, overrides, paths)
    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "profile": str(profile.path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_root": str(bundle_root),
        "missing_bundles": missing,
        "records": records,
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
    if data.get("schema_version") != 1 or not isinstance(data.get("records"), list):
        raise ValueError(f"지원하지 않는 inventory예요: {path}")
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
