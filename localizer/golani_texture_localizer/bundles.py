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
from .inventory import _material_targets, load_inventory, record_for_target
from .models import CollectionProfile
from .names import safe_bundle_name
from .paths import ProjectPaths
from .review import (
    approval_path,
    load_review,
    review_stage_sha256,
    sha256_file,
    verify_approval,
)
from .unityfs import (
    bytes_equal_outside_ranges,
    find_directory_entry,
    layout_signature,
    merge_ranges,
    parse_unityfs_layout,
    patch_uncompressed_logical_range,
)


# BC 계열의 4x4 블록이 하위 mip 전체를 차지할 때 정상 no-op 왕복에서도
# 경계 p99가 48을 넘을 수 있어요. 실제 BC7 원본 왕복으로 교정한 하한이에요.
_ROUNDTRIP_P99_FLOOR = 64.0
_ROUNDTRIP_MAX_FLOOR = 128.0


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


def _roundtrip_limits(
    role: str,
    width: int,
    height: int,
    max_mae: float,
) -> tuple[float, float, float]:
    """역할·밉 크기별 정상 블록 압축 왕복 한계를 돌려줘요."""

    minimum = min(width, height)
    mae_limit = max_mae
    p99_limit = max(_ROUNDTRIP_P99_FLOOR, max_mae * 8.0)
    if role == "diffuse":
        # 4x4는 BC7 한 블록이 mip 전체라 UV padding 뒤의 유효한 색 분포에서도
        # Mayo 원본 no-op 왕복 최대 channel MAE 20.875가 측정됐어요.
        if minimum == 4:
            mae_limit = max(mae_limit, 24.0)
        elif minimum <= 2:
            mae_limit = max(mae_limit, 16.0)
        elif minimum <= 16:
            # 16x16은 BC7 블록이 16개뿐이라 강한 고대비 패키지 디자인에서
            # 무편집 원본도 channel MAE 10.56, 검증 후보는 13.05가 측정됐어요.
            # p99/max 한계는 그대로 유지해 국소적인 큰 파손은 계속 차단해요.
            mae_limit = max(mae_limit, 16.0)
        elif minimum <= 32:
            mae_limit = max(mae_limit, 12.0)
        elif minimum <= 64:
            mae_limit = max(mae_limit, 8.0)
        if minimum <= 32:
            p99_limit = max(p99_limit, 80.0)
    elif role == "gloss" and minimum == 8:
        # Aquamari 원본 Gloss의 정상 BC 왕복에서 8x8 mip channel MAE가
        # 7.125였어요. p99/max 한계는 유지한 채 MAE만 원본 교정값으로 올려요.
        mae_limit = max(mae_limit, 8.0)
    return mae_limit, p99_limit, _ROUNDTRIP_MAX_FLOOR


def _coverage_values(coverage: Image.Image, size: tuple[int, int]) -> np.ndarray:
    if coverage.mode not in {"1", "L"}:
        raise ValueError("UV coverage는 단일 채널 마스크여야 해요")
    values = np.asarray(coverage.convert("L"), dtype=np.uint8)
    if not set(int(value) for value in np.unique(values)).issubset({0, 255}):
        raise ValueError("UV coverage는 0/255만 사용해야 해요")
    if coverage.size != size:
        source_width, source_height = coverage.size
        target_width, target_height = size
        if (
            source_width % target_width != 0
            or source_height % target_height != 0
            or source_width // target_width != source_height // target_height
        ):
            raise ValueError(
                "UV coverage와 텍스처 해상도는 같은 종횡비의 정수 축소 관계여야 해요"
            )
        factor = source_width // target_width
        if factor < 1 or factor & (factor - 1):
            raise ValueError("UV coverage 축소 배율은 2의 거듭제곱이어야 해요")
        # 보조맵이 diffuse보다 작을 때 UV island가 걸친 픽셀을 잃지 않도록
        # 각 블록의 합집합(max pooling)을 사용해 보수적으로 축소해요.
        values = values.reshape(
            target_height,
            factor,
            target_width,
            factor,
        ).max(axis=(1, 3))
    result = values == 255
    if not result.any():
        raise ValueError("UV coverage가 비어 있어요")
    return result


def _pad_uv_outside(image: Image.Image, coverage: np.ndarray) -> Image.Image:
    """UV island 밖을 가장 가까운 island texel로 채워 하위 mip의 atlas bleed를 막아요."""

    import cv2

    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    if coverage.shape != rgba.shape[:2]:
        raise ValueError("UV coverage 크기가 mip과 달라요")
    if not coverage.any():
        raise ValueError("UV coverage가 비어 있어요")
    if coverage.all():
        return Image.fromarray(rgba.copy(), "RGBA")

    # distanceTransformWithLabels의 PIXEL label은 각 0 픽셀(coverage)에 대응해요.
    outside = np.where(coverage, 0, 255).astype(np.uint8)
    _, labels = cv2.distanceTransformWithLabels(
        outside,
        cv2.DIST_L2,
        5,
        labelType=cv2.DIST_LABEL_PIXEL,
    )
    covered_flat = np.flatnonzero(coverage)
    covered_labels = labels.reshape(-1)[covered_flat]
    maximum_label = int(labels.max())
    nearest = np.full(maximum_label + 1, -1, dtype=np.int64)
    nearest[covered_labels] = covered_flat
    source_indices = nearest[labels]
    if np.any(source_indices < 0):
        raise AssertionError("UV coverage의 최근접 texel을 찾지 못했어요")
    output = rgba.copy().reshape(-1, 4)
    outside_flat = ~coverage.reshape(-1)
    output[outside_flat] = rgba.reshape(-1, 4)[source_indices.reshape(-1)[outside_flat]]
    return Image.fromarray(output.reshape(rgba.shape), "RGBA")


def _resize_coverage(coverage: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    resized = cv2.resize(
        coverage.astype(np.uint8) * 255,
        size,
        interpolation=cv2.INTER_AREA,
    )
    result = resized > 0
    if not result.any():
        raise AssertionError("하위 mip에서 UV coverage가 사라졌어요")
    return result


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


def _mip_chain(
    image: Image.Image,
    role: str,
    count: int,
    *,
    coverage: Image.Image | None = None,
) -> list[Image.Image]:
    if count < 1:
        raise ValueError("mip 수는 1 이상이어야 해요")
    levels = [image.convert("RGBA")]
    coverage_values = _coverage_values(coverage, image.size) if coverage is not None else None
    while len(levels) < count:
        current = levels[-1]
        if coverage_values is not None:
            current = _pad_uv_outside(current, coverage_values)
        next_level = _next_mip(current, role)
        if coverage_values is not None:
            coverage_values = _resize_coverage(coverage_values, next_level.size)
            next_level = _pad_uv_outside(next_level, coverage_values)
        levels.append(next_level)
    return levels


def _encode_mip_parts(
    obj: Any,
    texture: Any,
    image: Image.Image,
    role: str,
    coverage: Image.Image | None = None,
) -> tuple[list[bytes], list[Image.Image]]:
    from UnityPy.export import Texture2DConverter

    parts: list[bytes] = []
    levels = _mip_chain(image, role, texture.m_MipCount, coverage=coverage)
    for index, level in enumerate(levels):
        encoded, actual_format = Texture2DConverter.image_to_texture2d(
            level,
            texture.m_TextureFormat,
            obj.platform,
            texture.m_PlatformBlob,
        )
        if actual_format != texture.m_TextureFormat:
            raise ValueError(f"mip {index} 포맷이 {actual_format}으로 바뀌었어요")
        parts.append(encoded)
    return parts, levels


def _encode_mip_chain(
    obj: Any,
    texture: Any,
    image: Image.Image,
    role: str,
    coverage: Image.Image | None = None,
) -> bytes:
    parts, _ = _encode_mip_parts(obj, texture, image, role, coverage)
    return b"".join(parts)


def _decode_mip_parts(
    obj: Any,
    texture: Any,
    payload: bytes,
    part_lengths: list[int],
) -> list[Image.Image]:
    from UnityPy.export import Texture2DConverter

    if len(part_lengths) != texture.m_MipCount or sum(part_lengths) != len(payload):
        raise ValueError("mip payload 길이 명세가 실제 데이터와 달라요")
    levels: list[Image.Image] = []
    width, height = texture.m_Width, texture.m_Height
    offset = 0
    version = getattr(obj, "version", (0, 0, 0, 0))
    for index, length in enumerate(part_lengths):
        part = payload[offset : offset + length]
        offset += length
        try:
            level = Texture2DConverter.parse_image_data(
                part,
                width,
                height,
                texture.m_TextureFormat,
                version,
                obj.platform,
                texture.m_PlatformBlob,
                flip=True,
            ).convert("RGBA")
        except Exception as exc:
            raise ValueError(f"mip {index} 디코딩에 실패했어요: {exc}") from exc
        levels.append(level)
        width, height = max(1, width // 2), max(1, height // 2)
    return levels


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


def _verified_uv_coverage(
    paths: ProjectPaths,
    inventory: dict[str, Any],
    target_id: str,
) -> Path:
    records = [record for record in inventory["records"] if record.get("target_id") == target_id]
    if len(records) != 1:
        raise ValueError(f"{target_id} 원본 record는 정확히 하나여야 해요")
    source = Path(records[0]["source_png"])
    report_path = paths.reviews / target_id / "uv-report.json"
    if not report_path.is_file():
        raise FileNotFoundError(f"{target_id} UV 검토 보고서가 없어요: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("schema_version") != 1
        or report.get("target_id") != target_id
        or report.get("passed") is not True
    ):
        raise ValueError(f"{target_id} UV 검토 보고서가 통과 상태가 아니에요")
    if not source.is_file() or report.get("source_sha256") != _sha256_file(source):
        raise ValueError(f"{target_id} UV 검토 뒤 원본이 변경됐어요")
    for mesh_value, expected_sha256 in report.get("mesh_sha256", {}).items():
        mesh = Path(mesh_value)
        if not mesh.is_file() or _sha256_file(mesh) != expected_sha256:
            raise ValueError(f"{target_id} UV 검토 뒤 mesh가 변경됐어요: {mesh}")
    coverage = Path(str(report.get("coverage", "")))
    if not coverage.is_file() or report.get("coverage_sha256") != _sha256_file(coverage):
        raise ValueError(f"{target_id} UV coverage가 없거나 SHA가 달라요")
    with Image.open(coverage) as coverage_file:
        expected_size = (int(records[0]["width"]), int(records[0]["height"]))
        _coverage_values(coverage_file, expected_size)
    return coverage


def _combined_uv_coverage(
    paths: list[Path],
    size: tuple[int, int],
) -> tuple[Image.Image, list[dict[str, str]]]:
    if not paths:
        raise ValueError("Texture2D의 UV coverage가 명시되지 않았어요")
    combined = np.zeros((size[1], size[0]), dtype=bool)
    sources = []
    for path in dict.fromkeys(value.expanduser().resolve() for value in paths):
        with Image.open(path) as coverage_file:
            values = _coverage_values(coverage_file, size)
        combined |= values
        sources.append({"path": str(path), "sha256": _sha256_file(path)})
    return Image.fromarray(combined.astype(np.uint8) * 255, "L"), sources


def patch_bundle_exact(
    source_bundle: Path,
    output_bundle: Path,
    replacements: Mapping[str, Path],
    *,
    roundtrip_dir: Path | None = None,
    max_mae: float = 6.0,
    roles: Mapping[str, str] | None = None,
    coverage_masks: Mapping[str, list[Path]] | None = None,
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
        coverage, coverage_sources = _combined_uv_coverage(
            list((coverage_masks or {}).get(texture_name, [])),
            image.size,
        )
        mip_parts, intended_mips = _encode_mip_parts(
            obj,
            texture,
            image,
            role,
            coverage,
        )
        payload = b"".join(mip_parts)
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
            "mips": intended_mips,
            "mip_part_lengths": [len(part) for part in mip_parts],
            "image_path": image_path,
            "payload_size": len(payload),
            "resource": resource_name,
            "role": role,
            "coverage_sources": coverage_sources,
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
        rebuilt_obj, texture = _find_texture(rebuilt, texture_name)
        if _texture_metadata(texture) != info["metadata"]:
            raise AssertionError(f"{texture_name} metadata가 변경됐어요")
        roundtrip_mips = _decode_mip_parts(
            rebuilt_obj,
            texture,
            bytes(texture.get_image_data()),
            info["mip_part_lengths"],
        )
        if len(roundtrip_mips) != len(info["mips"]):
            raise AssertionError(f"{texture_name} 왕복 mip 수가 달라요")
        mip_reports = []
        for index, (intended_image, roundtrip) in enumerate(
            zip(info["mips"], roundtrip_mips, strict=True)
        ):
            intended = np.asarray(intended_image, dtype=np.int16)
            actual = np.asarray(roundtrip, dtype=np.int16)
            if intended.shape != actual.shape:
                raise AssertionError(f"{texture_name} mip {index} 왕복 이미지 shape가 달라요")
            absolute = np.abs(intended - actual)
            channel_mae = absolute.mean(axis=(0, 1))
            p95 = np.percentile(absolute, 95, axis=(0, 1))
            p99 = np.percentile(absolute, 99, axis=(0, 1))
            maximum = absolute.max(axis=(0, 1))
            mae_limit, localized_p99_limit, maximum_limit = _roundtrip_limits(
                info["role"],
                intended_image.width,
                intended_image.height,
                max_mae,
            )
            if np.any(channel_mae > mae_limit):
                raise AssertionError(
                    f"{texture_name} mip {index} 왕복 MAE {channel_mae.tolist()}가 "
                    f"제한 {mae_limit}를 넘었어요"
                )
            if np.any(p99 > localized_p99_limit):
                raise AssertionError(
                    f"{texture_name} mip {index} 왕복 p99 {p99.tolist()}가 "
                    f"제한 {localized_p99_limit}를 넘었어요"
                )
            if np.any(maximum > maximum_limit):
                raise AssertionError(
                    f"{texture_name} mip {index} 왕복 최대 오차 {maximum.tolist()}가 "
                    f"제한 {maximum_limit}를 넘었어요"
                )
            roundtrip_path = None
            if roundtrip_dir is not None:
                suffix = "" if index == 0 else f".mip-{index:02d}"
                roundtrip_path = roundtrip_dir / f"{texture_name}{suffix}.png"
                roundtrip_path.parent.mkdir(parents=True, exist_ok=True)
                roundtrip.save(roundtrip_path)
            mip_reports.append(
                {
                    "level": index,
                    "width": intended_image.width,
                    "height": intended_image.height,
                    "payload_size": info["mip_part_lengths"][index],
                    "channel_mae": [round(float(value), 6) for value in channel_mae],
                    "channel_p95": [round(float(value), 6) for value in p95],
                    "channel_p99": [round(float(value), 6) for value in p99],
                    "channel_max": [int(value) for value in maximum],
                    "limits": {
                        "channel_mae": mae_limit,
                        "channel_p99": localized_p99_limit,
                        "channel_max": maximum_limit,
                    },
                    "roundtrip": str(roundtrip_path) if roundtrip_path else None,
                }
            )
        top = mip_reports[0]
        texture_reports.append(
            {
                "texture": texture_name,
                "source_image": str(info["image_path"]),
                "payload_size": info["payload_size"],
                "resource": info["resource"],
                "channel_mae": top["channel_mae"],
                "channel_p95": top["channel_p95"],
                "channel_p99": top["channel_p99"],
                "channel_max": top["channel_max"],
                "mip_filter": {
                    "diffuse": "uv-nearest-pad+linear-light-area",
                    "normal": "uv-nearest-pad+vector-area-renormalized",
                    "gloss": "uv-nearest-pad+linear-area",
                }[info["role"]],
                "uv_coverage": info["coverage_sources"],
                "uv_padding_verified": True,
                "roundtrip": top["roundtrip"],
                "checked_mips": [report["level"] for report in mip_reports],
                "missing_mips": 0,
                "mips": mip_reports,
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


def _prune_stale_preserved_auxiliary_outputs(
    paths: ProjectPaths,
    inventory: dict[str, Any],
    auxiliary_plans: Mapping[tuple[Any, int, Any], dict[str, Any]],
    selected_target_ids: set[str],
    current_bundle_keys: set[str],
) -> list[dict[str, Any]]:
    """현재 보존 정책과 충돌하는 예전 N/G 출력만 증명 가능한 경우 제거해요."""

    material_targets = _material_targets(
        inventory.get("records", []), inventory.get("materials", [])
    )
    candidates: dict[str, set[str]] = {}
    for (bundle_key, path_id, role), plan in auxiliary_plans.items():
        if (
            not isinstance(bundle_key, str)
            or bundle_key in current_bundle_keys
            or role not in {"normal", "gloss"}
            or plan.get("policy") != "preserve"
        ):
            continue
        consumer_identities = {
            (str(material["bundle_key"]), int(material["path_id"]))
            for material in inventory.get("materials", [])
            if any(
                slot.get("texture_bundle_key") == bundle_key
                and int(slot.get("path_id", 0)) == path_id
                for slot in material.get("texture_slots", [])
            )
        }
        if not consumer_identities or any(
            identity not in material_targets for identity in consumer_identities
        ):
            continue
        consumer_targets = {
            target_id
            for identity in consumer_identities
            for target_id in material_targets[identity]
        }
        if not consumer_targets or not consumer_targets.issubset(selected_target_ids):
            continue
        records = [
            record
            for record in inventory.get("records", [])
            if record.get("bundle_key") == bundle_key
            and int(record.get("path_id", 0)) == path_id
            and record.get("role") == role
        ]
        if len(records) == 1:
            candidates.setdefault(bundle_key, set()).add(str(records[0]["texture"]))

    pruned: list[dict[str, Any]] = []
    roundtrip_root = (paths.reports / "roundtrip").resolve()
    for bundle_key, texture_names in sorted(candidates.items()):
        output = (paths.bundles / Path(bundle_key)).resolve()
        report_path = (
            paths.reports / "bundles" / f"{safe_bundle_name(bundle_key)}.json"
        ).resolve()
        if not output.is_file() or not report_path.is_file():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        reported_output = Path(str(report.get("output_bundle", ""))).resolve()
        reported_textures = {
            str(value.get("texture"))
            for value in report.get("textures", [])
            if isinstance(value, dict) and value.get("texture")
        }
        if (
            reported_output != output
            or not reported_textures
            or not reported_textures.issubset(texture_names)
            or report.get("output_sha256") != _sha256_file(output)
        ):
            continue
        roundtrip_paths: set[Path] = set()
        for texture_report in report.get("textures", []):
            if not isinstance(texture_report, dict):
                continue
            values = [texture_report.get("roundtrip")]
            values.extend(
                mip.get("roundtrip")
                for mip in texture_report.get("mips", [])
                if isinstance(mip, dict)
            )
            for value in values:
                if not isinstance(value, str) or not value:
                    continue
                candidate = Path(value).resolve()
                if candidate.is_file() and roundtrip_root in candidate.parents:
                    roundtrip_paths.add(candidate)
        output_sha256 = _sha256_file(output)
        output.unlink()
        report_path.unlink()
        for candidate in roundtrip_paths:
            candidate.unlink()
        pruned.append(
            {
                "bundle_key": bundle_key,
                "output_sha256": output_sha256,
                "textures": sorted(reported_textures),
                "reason": "current-material-policy-preserve",
            }
        )
    return pruned


def repack_collection(
    profile: CollectionProfile,
    paths: ProjectPaths,
    bundle_root: Path,
    *,
    max_mae: float = 6.0,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    image_report = validate_approved(profile, paths, target_ids=target_ids)
    if not image_report["passed"]:
        failed = [report["target_id"] for report in image_report["reports"] if not report["passed"]]
        raise ValueError(
            f"승인 이미지 게이트가 실패해 재패킹을 중단해요: "
            f"missing={image_report['missing']}, failed={failed}"
        )
    inventory = load_inventory(paths.inventory)
    groups: dict[str, dict[str, Path]] = {}
    roles: dict[str, dict[str, str]] = {}
    coverage_masks: dict[str, dict[str, list[Path]]] = {}
    for target in profile.targets:
        if target_ids is not None and target.id not in target_ids:
            continue
        approved = paths.approved / f"{target.id}.png"
        if target.action != "localize" or not approved.is_file():
            continue
        record = record_for_target(inventory, target)
        groups.setdefault(record["bundle_key"], {})[record["texture"]] = approved
        roles.setdefault(record["bundle_key"], {})[record["texture"]] = "diffuse"
        coverage_masks.setdefault(record["bundle_key"], {}).setdefault(
            record["texture"], []
        ).append(_verified_uv_coverage(paths, inventory, target.id))

    if not paths.derived_manifest.is_file():
        raise ValueError("현재 승인 SHA에 묶인 D/N/G derived manifest가 없어요")
    derived = json.loads(paths.derived_manifest.read_text(encoding="utf-8"))
    if derived.get("schema_version") != 2:
        raise ValueError("예전 derived manifest는 재사용할 수 없어요. derive를 다시 실행해 주세요")
    expected_targets = {
        target.id
        for target in profile.targets
        if target.action == "localize" and (target_ids is None or target.id in target_ids)
    }
    validated = {
        value.get("target_id"): value
        for value in derived.get("validated_targets", [])
        if isinstance(value, dict)
    }
    profile_target_ids = {target.id for target in profile.targets}
    unknown_validated = set(validated) - profile_target_ids
    if unknown_validated:
        raise ValueError(
            f"derived manifest에 profile 밖 검증 대상이 있어요: {sorted(unknown_validated)}"
        )
    missing_material_validation = expected_targets - set(validated)
    if missing_material_validation:
        raise ValueError(
            f"재질 게이트를 통과하지 않은 대상이 있어요: {sorted(missing_material_validation)}"
        )
    material_review_hashes: dict[str, str] = {}
    for target_id in sorted(expected_targets):
        value = validated[target_id]
        target = profile.target_by_id(target_id)
        approved = paths.approved / f"{target.id}.png"
        if value.get("approved_sha256") != sha256_file(approved):
            raise ValueError(f"{target.id} 재질 검증 뒤 승인본이 변경됐어요")
        if value.get("approval_sha256") != sha256_file(approval_path(paths, target.id)):
            raise ValueError(f"{target.id} 재질 검증 뒤 승인 증거가 변경됐어요")
        _, current_review = load_review(paths, target.id, through="material")
        current_material_hash = review_stage_sha256(
            current_review, "material_validation"
        )
        if value.get("material_review_sha256") != current_material_hash:
            raise ValueError(f"{target.id} 파생 뒤 재질 검증 단계가 변경됐어요")
        material_review_hashes[target_id] = current_material_hash
    auxiliary_plans = {
        (value.get("texture_bundle_key"), int(value.get("path_id", 0)), value.get("role")): value
        for value in derived.get("auxiliary_plans", [])
        if isinstance(value, dict)
    }
    manifest_outputs = [
        output for output in derived.get("outputs", []) if isinstance(output, dict)
    ]
    output_targets = {output.get("target_id") for output in manifest_outputs}
    unknown = output_targets - profile_target_ids
    if unknown:
        raise ValueError(f"derived manifest에 profile 밖 대상이 있어요: {sorted(unknown)}")
    selected_outputs = [
        output for output in manifest_outputs if output.get("target_id") in expected_targets
    ]
    for output in selected_outputs:
        target = profile.target_by_id(output["target_id"])
        approved = paths.approved / f"{target.id}.png"
        source_record = record_for_target(inventory, target)
        verify_approval(paths, target, Path(source_record["source_png"]), approved)
        source = Path(output["derived_png"])
        source_map = Path(output["source_png"])
        checks = {
            "approved_sha256": sha256_file(approved),
            "approval_sha256": sha256_file(approval_path(paths, target.id)),
            "material_review_sha256": material_review_hashes[target.id],
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
        plan = auxiliary_plans.get(
            (output["bundle_key"], int(output["path_id"]), output["role"])
        )
        if not isinstance(plan, dict) or not plan.get("target_ids"):
            raise ValueError(
                f"{output['texture']} 보조맵의 실제 target/UV coverage 계획이 없어요"
            )
        destination_coverages = coverage_masks.setdefault(output["bundle_key"], {}).setdefault(
            output["texture"], []
        )
        for target_id in plan["target_ids"]:
            destination_coverages.append(
                _verified_uv_coverage(paths, inventory, str(target_id))
            )

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
            coverage_masks=coverage_masks[bundle_key],
        )
        report["bundle_key"] = bundle_key
        report_path = paths.reports / "bundles" / f"{safe_bundle_name(bundle_key)}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)

    pruned_stale_outputs = _prune_stale_preserved_auxiliary_outputs(
        paths,
        inventory,
        auxiliary_plans,
        expected_targets,
        set(groups),
    )

    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "partial": target_ids is not None,
        "target_ids": sorted(target_ids) if target_ids is not None else None,
        "bundle_count": len(reports),
        "texture_count": sum(len(report["textures"]) for report in reports),
        "passed": all(report["passed"] for report in reports),
        "bundles": reports,
        "pruned_stale_outputs": pruned_stale_outputs,
    }
    suffix = "" if target_ids is None else "." + "+".join(sorted(target_ids))
    summary = paths.reports / f"repack{suffix}.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
