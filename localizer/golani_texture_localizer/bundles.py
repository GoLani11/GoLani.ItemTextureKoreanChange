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


def _encode_mip_chain(obj: Any, texture: Any, image: Image.Image) -> bytes:
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
            level = level.resize(
                (max(1, level.width // 2), max(1, level.height // 2)),
                Image.Resampling.BICUBIC,
            )
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
        payload = _encode_mip_chain(obj, texture, image)
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
        if np.any(channel_mae > max_mae):
            raise AssertionError(
                f"{texture_name} 왕복 MAE {channel_mae.tolist()}가 제한 {max_mae}를 넘었어요"
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
    allow_partial: bool = False,
    max_mae: float = 6.0,
) -> dict[str, Any]:
    image_report = validate_approved(profile, paths)
    if image_report["missing"] and not allow_partial:
        raise ValueError(f"승인되지 않은 대상이 있어요: {image_report['missing']}")
    inventory = load_inventory(paths.inventory)
    groups: dict[str, dict[str, Path]] = {}
    for target in profile.targets:
        approved = paths.approved / f"{target.id}.png"
        if target.action != "localize" or not approved.is_file():
            continue
        record = record_for_target(inventory, target)
        groups.setdefault(record["bundle_key"], {})[record["texture"]] = approved

    if paths.derived_manifest.is_file():
        derived = json.loads(paths.derived_manifest.read_text(encoding="utf-8"))
        for output in derived.get("outputs", []):
            source = Path(output["derived_png"])
            if source.is_file():
                groups.setdefault(output["bundle_key"], {})[output["texture"]] = source

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
        )
        report["bundle_key"] = bundle_key
        report_path = paths.reports / "bundles" / f"{safe_bundle_name(bundle_key)}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)

    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "partial": bool(image_report["missing"]),
        "bundle_count": len(reports),
        "texture_count": sum(len(report["textures"]) for report in reports),
        "passed": all(report["passed"] for report in reports),
        "bundles": reports,
    }
    summary = paths.reports / "repack.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
