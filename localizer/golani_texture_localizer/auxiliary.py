from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np


PROJECTION_SIGNATURE = "continuous-alpha-same-st-integer-area:v1"
NORMAL_SIGNATURE = "dxt5nm-rnm-height-from-master-alpha:v1"
GLOSS_SIGNATURE = "linear-gloss-delta-from-master-alpha:v1"
MIN_DXT5NM_Z = 0.05


def validate_same_uv_projection(
    diffuse_scale: Sequence[float],
    diffuse_offset: Sequence[float],
    auxiliary_scale: Sequence[float],
    auxiliary_offset: Sequence[float],
) -> None:
    values = (
        tuple(float(value) for value in diffuse_scale),
        tuple(float(value) for value in diffuse_offset),
        tuple(float(value) for value in auxiliary_scale),
        tuple(float(value) for value in auxiliary_offset),
    )
    if any(len(value) != 2 for value in values):
        raise ValueError("Diffuse/보조맵 UV ST는 숫자 두 개 배열이어야 해요")
    if not all(math.isfinite(item) for value in values for item in value):
        raise ValueError("Diffuse/보조맵 UV ST에 유한하지 않은 값이 있어요")
    if values[0][0] <= 0.0 or values[0][1] <= 0.0:
        raise ValueError("v1 파생은 양수인 Diffuse UV scale만 지원해요")
    if values[0] != values[2] or values[1] != values[3]:
        raise ValueError(
            "v1 파생은 같은 Material의 Diffuse와 보조맵 UV scale/offset이 같을 때만 지원해요"
        )


def project_master_alpha(
    alpha: np.ndarray,
    target_size: tuple[int, int],
) -> np.ndarray:
    """같은 UV ST의 연속 알파를 2^n 정수 block-area 평균으로 재래스터화해요."""

    if alpha.ndim != 2 or alpha.dtype != np.uint8:
        raise ValueError("master alpha는 uint8 단일 채널이어야 해요")
    if not alpha.any():
        raise ValueError("master alpha가 비어 있어요")
    target_width, target_height = target_size
    if target_width < 1 or target_height < 1:
        raise ValueError("보조맵 크기는 1 이상이어야 해요")
    source_height, source_width = alpha.shape
    if source_width == target_width and source_height == target_height:
        return alpha.copy()
    if source_width % target_width or source_height % target_height:
        raise ValueError("master alpha와 보조맵은 정수 축소 관계여야 해요")
    factor_x = source_width // target_width
    factor_y = source_height // target_height
    if factor_x != factor_y or factor_x < 1 or factor_x & (factor_x - 1):
        raise ValueError("master alpha 축소는 같은 종횡비의 2^n 배율이어야 해요")
    area = factor_x * factor_y
    sums = alpha.astype(np.uint64).reshape(
        target_height,
        factor_y,
        target_width,
        factor_x,
    ).sum(axis=(1, 3))
    # NumPy/OpenCV 버전에 영향을 받지 않는 정수 round-half-up이에요.
    return ((sums + area // 2) // area).astype(np.uint8)


def project_binary_mask(
    mask: np.ndarray,
    target_size: tuple[int, int],
) -> np.ndarray:
    """보호/seam 마스크는 희소 픽셀도 잃지 않도록 block 합집합으로 축소해요."""

    if mask.ndim != 2 or mask.dtype != np.bool_:
        raise ValueError("보호 마스크는 bool 단일 채널이어야 해요")
    target_width, target_height = target_size
    if target_width < 1 or target_height < 1:
        raise ValueError("보조맵 크기는 1 이상이어야 해요")
    source_height, source_width = mask.shape
    if source_width == target_width and source_height == target_height:
        return mask.copy()
    if source_width % target_width or source_height % target_height:
        raise ValueError("보호 마스크와 보조맵은 정수 축소 관계여야 해요")
    factor_x = source_width // target_width
    factor_y = source_height // target_height
    if factor_x != factor_y or factor_x < 1 or factor_x & (factor_x - 1):
        raise ValueError("보호 마스크 축소는 같은 종횡비의 2^n 배율이어야 해요")
    return mask.reshape(
        target_height,
        factor_y,
        target_width,
        factor_x,
    ).any(axis=(1, 3))


def projection_alignment_metrics(
    source_alpha: np.ndarray,
    projected_alpha: np.ndarray,
) -> dict[str, float | int]:
    if (
        source_alpha.ndim != 2
        or projected_alpha.ndim != 2
        or source_alpha.dtype != np.uint8
        or projected_alpha.dtype != np.uint8
        or not source_alpha.any()
        or not projected_alpha.any()
    ):
        raise ValueError("정렬 측정 알파는 비어 있지 않은 uint8 단일 채널이어야 해요")
    expected_projection = project_master_alpha(
        source_alpha,
        (projected_alpha.shape[1], projected_alpha.shape[0]),
    )
    if not np.array_equal(projected_alpha, expected_projection):
        raise ValueError("정렬 측정 알파가 결정적 area projection 결과와 달라요")

    def geometry(values: np.ndarray) -> tuple[float, float, tuple[int, int, int, int]]:
        height, width = values.shape
        weights = values.astype(np.float64)
        total = float(weights.sum())
        ys, xs = np.indices(values.shape, dtype=np.float64)
        center_x = float(((xs + 0.5) * weights).sum() / total / width)
        center_y = float(((ys + 0.5) * weights).sum() / total / height)
        rows, columns = np.nonzero(values)
        return center_x, center_y, (
            int(columns.min()),
            int(rows.min()),
            int(columns.max()) + 1,
            int(rows.max()) + 1,
        )

    source_height, source_width = source_alpha.shape
    target_height, target_width = projected_alpha.shape
    source_x, source_y, source_bbox = geometry(source_alpha)
    target_x, target_y, target_bbox = geometry(projected_alpha)
    center_error = max(
        abs(source_x * target_width - target_x * target_width),
        abs(source_y * target_height - target_y * target_height),
    )
    expected_bbox = (
        source_bbox[0] / source_width * target_width,
        source_bbox[1] / source_height * target_height,
        source_bbox[2] / source_width * target_width,
        source_bbox[3] / source_height * target_height,
    )
    bbox_error = max(
        abs(float(actual) - expected)
        for actual, expected in zip(target_bbox, expected_bbox, strict=True)
    )
    return {
        "center_error_texels": float(center_error),
        "bbox_edge_error_texels": float(bbox_error),
        "rotation_error_deg": 0.0,
        "source_coverage_sum": int(source_alpha.astype(np.uint64).sum()),
        "projected_coverage_sum": int(projected_alpha.astype(np.uint64).sum()),
    }


def _binomial_blur(values: np.ndarray, passes: int) -> np.ndarray:
    result = values.astype(np.float32, copy=True)
    weights = np.asarray([1.0, 4.0, 6.0, 4.0, 1.0], dtype=np.float32) / 16.0
    for _ in range(passes):
        horizontal = np.pad(result, ((0, 0), (2, 2)), mode="wrap")
        result = sum(
            weights[index] * horizontal[:, index : index + values.shape[1]]
            for index in range(5)
        )
        vertical = np.pad(result, ((2, 2), (0, 0)), mode="wrap")
        result = sum(
            weights[index] * vertical[index : index + values.shape[0], :]
            for index in range(5)
        )
    return result


def _decode_dxt5nm(values: np.ndarray) -> np.ndarray:
    x = values[..., 3].astype(np.float32) / 127.5 - 1.0
    y = values[..., 1].astype(np.float32) / 127.5 - 1.0
    xy_length = np.sqrt(x * x + y * y)
    scale = np.maximum(1.0, xy_length)
    x /= scale
    y /= scale
    z = np.sqrt(np.maximum(0.0, 1.0 - x * x - y * y))
    return np.dstack((x, y, z))


def _rnm(base: np.ndarray, detail: np.ndarray) -> np.ndarray:
    tangent = base + np.asarray([0.0, 0.0, 1.0], dtype=np.float32)
    mapped_detail = detail * np.asarray([-1.0, -1.0, 1.0], dtype=np.float32)
    dot = np.sum(tangent * mapped_detail, axis=2, keepdims=True)
    combined = tangent * dot / np.maximum(tangent[..., 2:3], 1e-8) - mapped_detail
    combined /= np.maximum(np.linalg.norm(combined, axis=2, keepdims=True), 1e-8)
    return combined


def pack_dxt5nm_xy(vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    if vectors.ndim != 3 or vectors.shape[2] < 2:
        raise ValueError("DXT5nm packing 입력은 XY 벡터 이미지여야 해요")
    def encode(values: np.ndarray) -> np.ndarray:
        return np.floor(np.clip((values + 1.0) * 127.5, 0.0, 255.0) + 0.5).astype(
            np.uint8
        )

    packed_x = encode(vectors[..., 0])
    packed_y = encode(vectors[..., 1])
    decoded_x = packed_x.astype(np.float32) / 127.5 - 1.0
    decoded_y = packed_y.astype(np.float32) / 127.5 - 1.0
    length = np.sqrt(decoded_x * decoded_x + decoded_y * decoded_y)
    outside = length > 1.0
    if outside.any():
        safe_radius = np.float32(0.99)
        corrected_x = decoded_x.copy()
        corrected_y = decoded_y.copy()
        corrected_x[outside] = decoded_x[outside] / length[outside] * safe_radius
        corrected_y[outside] = decoded_y[outside] / length[outside] * safe_radius
        packed_x[outside] = encode(corrected_x[outside])
        packed_y[outside] = encode(corrected_y[outside])
        decoded_x = packed_x.astype(np.float32) / 127.5 - 1.0
        decoded_y = packed_y.astype(np.float32) / 127.5 - 1.0
        length = np.sqrt(decoded_x * decoded_x + decoded_y * decoded_y)
    return packed_x, packed_y, float(length.max(initial=0.0))


def derive_packed_normal(
    neutral_base: np.ndarray,
    projected_alpha: np.ndarray,
    *,
    height_scale_texels: float,
    polarity: int,
    bevel_passes: int,
) -> tuple[np.ndarray, np.ndarray, dict[str, float | int | str | list[str]]]:
    if neutral_base.ndim != 3 or neutral_base.shape[2] != 4 or neutral_base.dtype != np.uint8:
        raise ValueError("Normal 중립 base는 uint8 RGBA여야 해요")
    if projected_alpha.shape != neutral_base.shape[:2] or projected_alpha.dtype != np.uint8:
        raise ValueError("Normal master alpha 크기/형식이 중립 base와 달라요")
    if (
        not isinstance(height_scale_texels, (int, float))
        or isinstance(height_scale_texels, bool)
        or not math.isfinite(float(height_scale_texels))
        or not 0.0 < float(height_scale_texels) <= 8.0
    ):
        raise ValueError("Normal height_scale_texels는 0 초과 8 이하 유한수여야 해요")
    if polarity not in {-1, 1}:
        raise ValueError("Normal polarity는 -1 또는 1이어야 해요")
    if (
        not isinstance(bevel_passes, int)
        or isinstance(bevel_passes, bool)
        or not 0 <= bevel_passes <= 8
    ):
        raise ValueError("Normal bevel_passes는 0~8 정수여야 해요")

    height = _binomial_blur(projected_alpha.astype(np.float32) / 255.0, bevel_passes)
    height *= float(height_scale_texels) * polarity
    horizontal = np.pad(height, ((0, 0), (1, 1)), mode="wrap")
    vertical = np.pad(height, ((1, 1), (0, 0)), mode="wrap")
    gradient_x = (horizontal[:, 2:] - horizontal[:, :-2]) * 0.5
    gradient_y = (vertical[2:, :] - vertical[:-2, :]) * 0.5
    effect_mask = (np.abs(gradient_x) > 1e-8) | (np.abs(gradient_y) > 1e-8)

    output = neutral_base.copy()
    if effect_mask.any():
        detail = np.dstack((-gradient_x, gradient_y, np.ones_like(gradient_x)))
        detail /= np.maximum(np.linalg.norm(detail, axis=2, keepdims=True), 1e-8)
        combined = _rnm(_decode_dxt5nm(neutral_base), detail)
        min_combined_z = float(combined[..., 2][effect_mask].min())
        if min_combined_z < MIN_DXT5NM_Z:
            raise ValueError(
                "파생 Normal이 DXT5nm 양의 Z 반구 안전 한계 아래로 내려갔어요"
            )
        packed_x, packed_y, max_xy_length = pack_dxt5nm_xy(combined)
        output[..., 3][effect_mask] = packed_x[effect_mask]
        output[..., 1][effect_mask] = packed_y[effect_mask]
    else:
        max_xy_length = 0.0
        min_combined_z = 1.0

    changed = np.any(output != neutral_base, axis=2)
    if projected_alpha.any() and not changed.any():
        raise ValueError("파생 Normal 효과가 mip0 양자화 뒤 실제 픽셀 변경을 만들지 못했어요")
    return output, effect_mask, {
        "algorithm_signature": NORMAL_SIGNATURE,
        "selected_channels": ["G", "A"],
        "effect_pixels": int(effect_mask.sum()),
        "changed_pixels_from_neutral_base": int(changed.sum()),
        "changed_outside_effect_mask": int((changed & ~effect_mask).sum()),
        "changed_unselected_channels": int(
            (output[..., (0, 2)] != neutral_base[..., (0, 2)]).sum()
        ),
        "max_packed_xy_length": max_xy_length,
        "min_combined_z": min_combined_z,
    }


def derive_linear_gloss(
    neutral_base: np.ndarray,
    projected_alpha: np.ndarray,
    *,
    channel_deltas: Mapping[str, float],
) -> tuple[np.ndarray, np.ndarray, dict[str, float | int | str | list[str] | dict[str, float]]]:
    if neutral_base.ndim != 3 or neutral_base.shape[2] != 4 or neutral_base.dtype != np.uint8:
        raise ValueError("Gloss 중립 base는 uint8 RGBA여야 해요")
    if projected_alpha.shape != neutral_base.shape[:2] or projected_alpha.dtype != np.uint8:
        raise ValueError("Gloss master alpha 크기/형식이 중립 base와 달라요")
    if not isinstance(channel_deltas, Mapping) or not channel_deltas:
        raise ValueError("Gloss channel_deltas가 비어 있어요")
    indices = {"R": 0, "G": 1, "B": 2, "A": 3}
    normalized: dict[str, float] = {}
    for channel, delta in channel_deltas.items():
        if channel not in indices:
            raise ValueError("Gloss channel_deltas는 RGBA 채널만 사용할 수 있어요")
        if (
            not isinstance(delta, (int, float))
            or isinstance(delta, bool)
            or not math.isfinite(float(delta))
            or not -255.0 <= float(delta) <= 255.0
        ):
            raise ValueError("Gloss delta는 -255~255 유한수여야 해요")
        normalized[channel] = float(delta)
    if not any(delta != 0.0 for delta in normalized.values()):
        raise ValueError("Gloss delta가 모두 0이면 파생 효과가 없어요")

    output = neutral_base.copy()
    alpha = projected_alpha.astype(np.float32) / 255.0
    effect_mask = projected_alpha > 0
    for channel, delta in normalized.items():
        index = indices[channel]
        values = neutral_base[..., index].astype(np.float32) + delta * alpha
        encoded = np.floor(np.clip(values, 0.0, 255.0) + 0.5).astype(np.uint8)
        output[..., index][effect_mask] = encoded[effect_mask]

    changed = np.any(output != neutral_base, axis=2)
    if projected_alpha.any() and not changed.any():
        raise ValueError("파생 Gloss 효과가 mip0 양자화/포화 뒤 실제 픽셀 변경을 만들지 못했어요")
    unselected = tuple(index for channel, index in indices.items() if channel not in normalized)
    return output, effect_mask, {
        "algorithm_signature": GLOSS_SIGNATURE,
        "selected_channels": list(normalized),
        "channel_deltas": normalized,
        "effect_pixels": int(effect_mask.sum()),
        "changed_pixels_from_neutral_base": int(changed.sum()),
        "changed_outside_effect_mask": int((changed & ~effect_mask).sum()),
        "changed_unselected_channels": int(
            (output[..., unselected] != neutral_base[..., unselected]).sum()
        )
        if unselected
        else 0,
    }
