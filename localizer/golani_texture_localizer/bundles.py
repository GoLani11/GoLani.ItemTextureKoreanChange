from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np
from PIL import Image

from .images import validate_approved
from .inventory import load_inventory, record_for_target
from .models import CollectionProfile
from .names import safe_bundle_name
from .paths import ProjectPaths
from .review import approval_path, sha256_file, verify_approval
from .unityfs import (
    bytes_equal_outside_ranges,
    find_directory_entry,
    layout_signature,
    merge_ranges,
    parse_unityfs_layout,
    patch_uncompressed_logical_range,
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_texture(environment: Any, name: str):
    matches = []
    for obj in environment.objects:
        if obj.type.name != "Texture2D":
            continue
        texture = obj.read()
        if texture.m_Name == name:
            matches.append((obj, texture))
    if len(matches) != 1:
        raise ValueError(f"Texture2D {name!r}는 하나여야 해요. 현재 {len(matches)}개예요")
    return matches[0]


def _object_hashes(environment: Any) -> dict[str, str]:
    return {
        f"{obj.assets_file.name}:{obj.path_id}:{obj.type.name}": _sha256_bytes(obj.get_raw_data())
        for obj in environment.objects
    }


def _texture_metadata(texture: Any) -> tuple[Any, ...]:
    stream = texture.m_StreamData
    return (
        texture.m_Name,
        texture.m_Width,
        texture.m_Height,
        int(texture.m_TextureFormat),
        texture.m_MipCount,
        texture.m_CompleteImageSize,
        stream.path,
        stream.offset,
        stream.size,
    )


def _srgb_to_linear(values: np.ndarray) -> np.ndarray:
    values = values.astype(np.float32) / 255.0
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0.0, 1.0)
    encoded = np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1 / 2.4) - 0.055)
    return np.clip(np.round(encoded * 255), 0, 255).astype(np.uint8)


def _resize_float(values: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    return cv2.resize(values, size, interpolation=cv2.INTER_AREA)


def _next_mip(image: Image.Image, role: str) -> Image.Image:
    size = (max(1, image.width // 2), max(1, image.height // 2))
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    if role == "diffuse":
        rgb = _linear_to_srgb(_resize_float(_srgb_to_linear(rgba[..., :3]), size))
        alpha = np.clip(np.round(_resize_float(rgba[..., 3].astype(np.float32), size)), 0, 255).astype(np.uint8)
        return Image.fromarray(np.dstack((rgb, alpha)), "RGBA")
    if role == "normal":
        x = rgba[..., 3].astype(np.float32) / 127.5 - 1.0
        y = rgba[..., 1].astype(np.float32) / 127.5 - 1.0
        z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
        vector = _resize_float(np.dstack((x, y, z)), size)
        length = np.linalg.norm(vector, axis=2, keepdims=True)
        vector /= np.maximum(length, 1e-8)
        other = np.clip(np.round(_resize_float(rgba[..., [0, 2]].astype(np.float32), size)), 0, 255).astype(np.uint8)
        output = np.empty((size[1], size[0], 4), dtype=np.uint8)
        output[..., 0] = other[..., 0]
        output[..., 1] = np.clip(np.round((vector[..., 1] + 1.0) * 127.5), 0, 255).astype(np.uint8)
        output[..., 2] = other[..., 1]
        output[..., 3] = np.clip(np.round((vector[..., 0] + 1.0) * 127.5), 0, 255).astype(np.uint8)
        return Image.fromarray(output, "RGBA")
    if role == "gloss":
        values = np.clip(np.round(_resize_float(rgba.astype(np.float32), size)), 0, 255).astype(np.uint8)
        return Image.fromarray(values, "RGBA")
    raise ValueError(f"지원하지 않는 텍스처 역할이에요: {role}")


def _encode_mip_chain(obj: Any, texture: Any, image: Image.Image, role: str) -> bytes:
    from UnityPy.export import Texture2DConverter

    parts: list[bytes] = []
    level = image
    for index in range(texture.m_MipCount):
        encoded, actual_format = Texture2DConverter.image_to_texture2d(
            level,
            texture.m_TextureFormat,
            obj.platform,
            texture.m_PlatformBlob,
        )
        if actual_format != texture.m_TextureFormat:
            raise ValueError(f"mip {index} 포맷이 {actual_format}으로 바뀌었어요")
        parts.append(encoded)
        if index + 1 < texture.m_MipCount:
            level = _next_mip(level, role)
    return b"".join(parts)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        if temporary.read_bytes() != value:
            raise AssertionError("임시 bundle 쓰기 검증에 실패했어요")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verified_source_bundle(
    bundle_key: str,
    bundle_root: Path,
    override: Mapping[str, Any] | None,
    records: list[dict[str, Any]],
) -> Path:
    if override is not None:
        source_value = override.get("path")
        expected_sha256 = override.get("sha256")
        if not all(isinstance(value, str) and value for value in (source_value, expected_sha256)):
            raise ValueError(f"source override 필수 필드가 누락됐어요: {bundle_key}")
        source = Path(source_value).expanduser().resolve()
        fallback = (bundle_root / Path(bundle_key)).expanduser().resolve()
        if (
            (not source.is_file() or _sha256_file(source) != expected_sha256)
            and fallback.is_file()
            and _sha256_file(fallback) == expected_sha256
        ):
            source = fallback
    else:
        source = (bundle_root / Path(bundle_key)).expanduser().resolve()
        hashes = {
            record.get("bundle_sha256")
            for record in records
            if record.get("bundle_key") == bundle_key and record.get("bundle_sha256")
        }
        if len(hashes) != 1:
            raise ValueError(f"inventory의 원본 bundle 해시가 하나가 아니에요: {bundle_key}")
        expected_sha256 = hashes.pop()

    if not source.is_file():
        raise FileNotFoundError(f"원본 bundle을 찾지 못했어요: {source}")
    if _sha256_file(source) != expected_sha256:
        raise ValueError(f"inventory 이후 원본 bundle이 변경됐어요: {source}")
    return source


def patch_bundle_exact(
    source_bundle: Path,
    output_bundle: Path,
    replacements: Mapping[str, Path],
    *,
    roundtrip_dir: Path | None = None,
    max_mae: float = 6.0,
    roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    import UnityPy

    if not replacements:
        raise ValueError("교체할 Texture2D가 없어요")
    if not math.isfinite(max_mae) or max_mae <= 0:
        raise ValueError("max_mae는 양의 유한값이어야 해요")
    source_bundle = source_bundle.expanduser().resolve()
    output_bundle = output_bundle.expanduser().resolve()
    if source_bundle == output_bundle:
        raise ValueError("원본 bundle과 출력 bundle은 달라야 해요")
    original_bytes = source_bundle.read_bytes()
    environment = UnityPy.load(original_bytes)
    bundle = environment.file
    original_layout = parse_unityfs_layout(original_bytes, bundle)
    before_objects = _object_hashes(environment)
    rebuilt_bytes = original_bytes
    physical_ranges: list[tuple[int, int]] = []
    expected: dict[str, dict[str, Any]] = {}

    for texture_name, image_path in sorted(replacements.items()):
        obj, texture = _find_texture(environment, texture_name)
        metadata = _texture_metadata(texture)
        with Image.open(image_path) as image_file:
            image = image_file.convert("RGBA")
        if image.size != (texture.m_Width, texture.m_Height):
            raise ValueError(
                f"{texture_name} 크기 {image.size} != 원본 {(texture.m_Width, texture.m_Height)}"
            )
        stream = texture.m_StreamData
        if not stream.path or stream.offset < 0 or stream.size <= 0:
            raise ValueError(f"{texture_name}에 유효한 streamed image data가 없어요")
        role = (roles or {}).get(texture_name)
        if role not in {"diffuse", "normal", "gloss"}:
            raise ValueError(f"{texture_name}의 diffuse/normal/gloss 역할이 명시되지 않았어요")
        payload = _encode_mip_chain(obj, texture, image, role)
        if len(payload) != stream.size:
            raise ValueError(f"{texture_name} payload {len(payload)} != stream size {stream.size}")
        resource_name = stream.path.rsplit("/", 1)[-1]
        if resource_name not in bundle.files:
            raise ValueError(f"bundle 안에 stream resource가 없어요: {resource_name}")
        resource_entry = find_directory_entry(original_layout, resource_name)
        if stream.offset + stream.size > resource_entry.size:
            raise ValueError(f"{texture_name} stream 범위가 resource entry 밖이에요")
        rebuilt_bytes, patches = patch_uncompressed_logical_range(
            rebuilt_bytes,
            original_layout,
            resource_entry.offset + stream.offset,
            payload,
        )
        physical_ranges.extend((patch.start, patch.end) for patch in patches)
        expected[texture_name] = {
            "metadata": metadata,
            "image": image,
            "image_path": image_path,
            "payload_size": len(payload),
            "resource": resource_name,
            "role": role,
        }

    merged_ranges = merge_ranges(physical_ranges)
    if not bytes_equal_outside_ranges(original_bytes, rebuilt_bytes, merged_ranges):
        raise AssertionError("대상 stream payload 밖의 bundle bytes가 달라졌어요")

    rebuilt = UnityPy.load(rebuilt_bytes)
    if before_objects != _object_hashes(rebuilt):
        raise AssertionError("serialized object가 변경됐어요")
    rebuilt_layout = parse_unityfs_layout(rebuilt_bytes, rebuilt.file)
    if layout_signature(original_layout) != layout_signature(rebuilt_layout):
        raise AssertionError("UnityFS layout이 변경됐어요")

    texture_reports = []
    for texture_name, info in expected.items():
        _, texture = _find_texture(rebuilt, texture_name)
        if _texture_metadata(texture) != info["metadata"]:
            raise AssertionError(f"{texture_name} metadata가 변경됐어요")
        roundtrip = texture.image.convert("RGBA")
        intended = np.asarray(info["image"], dtype=np.int16)
        actual = np.asarray(roundtrip, dtype=np.int16)
        if intended.shape != actual.shape:
            raise AssertionError(f"{texture_name} 왕복 이미지 shape가 달라요")
        channel_mae = np.abs(intended - actual).mean(axis=(0, 1))
        absolute = np.abs(intended - actual)
        p95 = np.percentile(absolute, 95, axis=(0, 1))
        p99 = np.percentile(absolute, 99, axis=(0, 1))
        maximum = absolute.max(axis=(0, 1))
        localized_p99_limit = max(32.0, max_mae * 8.0)
        if np.any(channel_mae > max_mae):
            raise AssertionError(
                f"{texture_name} 왕복 MAE {channel_mae.tolist()}가 제한 {max_mae}를 넘었어요"
            )
        if np.any(p99 > localized_p99_limit):
            raise AssertionError(
                f"{texture_name} 왕복 p99 {p99.tolist()}가 제한 {localized_p99_limit}를 넘었어요"
            )
        roundtrip_path = None
        if roundtrip_dir is not None:
            roundtrip_path = roundtrip_dir / f"{texture_name}.png"
            roundtrip_path.parent.mkdir(parents=True, exist_ok=True)
            roundtrip.save(roundtrip_path)
        texture_reports.append(
            {
                "texture": texture_name,
                "source_image": str(info["image_path"]),
                "payload_size": info["payload_size"],
                "resource": info["resource"],
                "channel_mae": [round(float(value), 6) for value in channel_mae],
                "channel_p95": [round(float(value), 6) for value in p95],
                "channel_p99": [round(float(value), 6) for value in p99],
                "channel_max": [int(value) for value in maximum],
                "mip_filter": {
                    "diffuse": "linear-light-area",
                    "normal": "vector-area-renormalized",
                    "gloss": "linear-area",
                }[info["role"]],
                "roundtrip": str(roundtrip_path) if roundtrip_path else None,
            }
        )

    _atomic_write(output_bundle, rebuilt_bytes)
    if output_bundle.read_bytes() != rebuilt_bytes:
        raise AssertionError("공개된 bundle bytes가 검증본과 달라요")
    return {
        "source_bundle": str(source_bundle),
        "source_sha256": _sha256_bytes(original_bytes),
        "output_bundle": str(output_bundle),
        "output_sha256": _sha256_bytes(rebuilt_bytes),
        "bundle_size": len(rebuilt_bytes),
        "physical_patch_ranges": merged_ranges,
        "bytes_equal_outside_payloads": True,
        "layout_equal": True,
        "serialized_objects_equal": True,
        "textures": texture_reports,
        "passed": True,
    }


def repack_collection(
    profile: CollectionProfile,
    paths: ProjectPaths,
    bundle_root: Path,
    *,
    max_mae: float = 6.0,
) -> dict[str, Any]:
    image_report = validate_approved(profile, paths)
    if not image_report["passed"]:
        failed = [report["target_id"] for report in image_report["reports"] if not report["passed"]]
        raise ValueError(
            f"승인 이미지 게이트가 실패해 재패킹을 중단해요: "
            f"missing={image_report['missing']}, failed={failed}"
        )
    inventory = load_inventory(paths.inventory)
    groups: dict[str, dict[str, Path]] = {}
    roles: dict[str, dict[str, str]] = {}
    for target in profile.targets:
        approved = paths.approved / f"{target.id}.png"
        if target.action != "localize" or not approved.is_file():
            continue
        record = record_for_target(inventory, target)
        groups.setdefault(record["bundle_key"], {})[record["texture"]] = approved
        roles.setdefault(record["bundle_key"], {})[record["texture"]] = "diffuse"

    if not paths.derived_manifest.is_file():
        raise ValueError("현재 승인 SHA에 묶인 D/N/G derived manifest가 없어요")
    derived = json.loads(paths.derived_manifest.read_text(encoding="utf-8"))
    if derived.get("schema_version") != 2:
        raise ValueError("예전 derived manifest는 재사용할 수 없어요. derive를 다시 실행해 주세요")
    expected_targets = {target.id for target in profile.targets if target.action == "localize"}
    validated = {
        value.get("target_id"): value
        for value in derived.get("validated_targets", [])
        if isinstance(value, dict)
    }
    missing_material_validation = expected_targets - set(validated)
    if missing_material_validation:
        raise ValueError(
            f"재질 게이트를 통과하지 않은 대상이 있어요: {sorted(missing_material_validation)}"
        )
    for target_id, value in validated.items():
        target = profile.target_by_id(target_id)
        approved = paths.approved / f"{target.id}.png"
        if value.get("approved_sha256") != sha256_file(approved):
            raise ValueError(f"{target.id} 재질 검증 뒤 승인본이 변경됐어요")
        if value.get("approval_sha256") != sha256_file(approval_path(paths, target.id)):
            raise ValueError(f"{target.id} 재질 검증 뒤 승인 증거가 변경됐어요")
    output_targets = {output.get("target_id") for output in derived.get("outputs", [])}
    unknown = output_targets - expected_targets
    if unknown:
        raise ValueError(f"derived manifest에 profile 밖 대상이 있어요: {sorted(unknown)}")
    for output in derived.get("outputs", []):
        target = profile.target_by_id(output["target_id"])
        approved = paths.approved / f"{target.id}.png"
        source_record = record_for_target(inventory, target)
        verify_approval(paths, target, Path(source_record["source_png"]), approved)
        source = Path(output["derived_png"])
        source_map = Path(output["source_png"])
        checks = {
            "approved_sha256": sha256_file(approved),
            "approval_sha256": sha256_file(approval_path(paths, target.id)),
            "source_sha256": sha256_file(source_map),
            "derived_sha256": sha256_file(source),
        }
        for field, current in checks.items():
            if output.get(field) != current:
                raise ValueError(
                    f"{target.id}::{output['texture']} derived {field}가 현재 입력과 달라요"
                )
        groups.setdefault(output["bundle_key"], {})[output["texture"]] = source
        roles.setdefault(output["bundle_key"], {})[output["texture"]] = output["role"]

    reports = []
    overrides = inventory.get("source_bundle_overrides", {})
    for bundle_key, replacements in groups.items():
        source = _verified_source_bundle(
            bundle_key,
            bundle_root,
            overrides.get(bundle_key),
            inventory["records"],
        )
        output = paths.bundles / Path(bundle_key)
        roundtrip = paths.reports / "roundtrip" / safe_bundle_name(bundle_key)
        report = patch_bundle_exact(
            source,
            output,
            replacements,
            roundtrip_dir=roundtrip,
            max_mae=max_mae,
            roles=roles[bundle_key],
        )
        report["bundle_key"] = bundle_key
        report_path = paths.reports / "bundles" / f"{safe_bundle_name(bundle_key)}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)

    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "partial": False,
        "bundle_count": len(reports),
        "texture_count": sum(len(report["textures"]) for report in reports),
        "passed": all(report["passed"] for report in reports),
        "bundles": reports,
    }
    summary = paths.reports / "repack.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
