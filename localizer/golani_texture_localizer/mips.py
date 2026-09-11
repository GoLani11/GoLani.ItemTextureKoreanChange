from __future__ import annotations

import numpy as np
from PIL import Image
from .auxiliary import pack_dxt5nm_xy

_ROUNDTRIP_P99_FLOOR = 64.0
_ROUNDTRIP_MAX_FLOOR = 128.0


def apply_mip_delta(original: Image.Image, baseline: Image.Image, edited: Image.Image, role: str) -> Image.Image:
    """Transfer only the filtered edit onto the game's authored mip.

    Rebuilding a touched block entirely from mip0 replaces the game's original mip
    filtering even at its unedited pixels. The original mip remains the base here.
    """
    source, before, after = [np.asarray(image.convert("RGBA")) for image in (original, baseline, edited)]
    changed = np.any(before != after, axis=2)
    result = source.copy()
    if not changed.any():
        return original.copy()
    if role == "diffuse":
        value = _srgb_to_linear(source[..., :3]) + _srgb_to_linear(after[..., :3]) - _srgb_to_linear(before[..., :3])
        result[changed, :3] = _linear_to_srgb(value)[changed]
    elif role == "normal":
        def vectors(array):
            x = array[..., 3].astype(np.float32) / 127.5 - 1
            y = array[..., 1].astype(np.float32) / 127.5 - 1
            return np.dstack((x, y, np.sqrt(np.clip(1 - x*x - y*y, 0, 1))))
        value = vectors(source) + vectors(after) - vectors(before)
        value /= np.maximum(np.linalg.norm(value, axis=2, keepdims=True), 1e-8)
        x, y, _ = pack_dxt5nm_xy(value)
        result[changed, 3], result[changed, 1] = x[changed], y[changed]
    elif role == "gloss":
        value = np.clip(source.astype(np.int16) + after.astype(np.int16) - before.astype(np.int16), 0, 255).astype(np.uint8)
        result[changed] = value[changed]
    else:
        raise ValueError(f"지원하지 않는 텍스처 역할이에요: {role}")
    return Image.fromarray(result)

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
        packed_x, packed_y, _ = pack_dxt5nm_xy(vector)
        output[..., 1] = packed_y
        output[..., 2] = other[..., 1]
        output[..., 3] = packed_x
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
