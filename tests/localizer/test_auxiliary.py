from __future__ import annotations

import numpy as np
import pytest

from golani_texture_localizer.auxiliary import (
    _rnm,
    derive_linear_gloss,
    derive_packed_normal,
    project_binary_mask,
    project_master_alpha,
    projection_alignment_metrics,
    validate_same_uv_projection,
)


def test_rnm_keeps_nonflat_base_and_detail_order() -> None:
    base = np.asarray([[[0.4, -0.3, np.sqrt(0.75)]]], dtype=np.float32)
    flat = np.asarray([[[0.0, 0.0, 1.0]]], dtype=np.float32)
    detail = np.asarray([[[0.2, 0.1, np.sqrt(0.95)]]], dtype=np.float32)

    np.testing.assert_allclose(_rnm(base, flat), base, atol=1e-6)
    np.testing.assert_allclose(_rnm(flat, detail), detail, atol=1e-6)


def test_master_alpha_same_size_is_byte_exact() -> None:
    alpha = np.asarray([[0, 1, 127], [128, 254, 255]], dtype=np.uint8)

    projected = project_master_alpha(alpha, (3, 2))

    assert np.array_equal(projected, alpha)
    assert projected is not alpha


def test_master_alpha_integer_area_downsample_is_deterministic() -> None:
    alpha = np.asarray(
        [
            [0, 64, 0, 0],
            [128, 255, 0, 0],
            [0, 0, 255, 255],
            [0, 0, 255, 255],
        ],
        dtype=np.uint8,
    )

    first = project_master_alpha(alpha, (2, 2))
    second = project_master_alpha(alpha, (2, 2))

    assert np.array_equal(first, np.asarray([[112, 0], [0, 255]], dtype=np.uint8))
    assert np.array_equal(first, second)


@pytest.mark.parametrize("size", [(3, 2), (2, 1), (8, 8)])
def test_master_alpha_blocks_unsupported_size_relationships(size: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="정수 축소|종횡비"):
        project_master_alpha(np.full((4, 4), 255, dtype=np.uint8), size)


def test_uv_projection_requires_same_finite_nonzero_st() -> None:
    validate_same_uv_projection([1, 1], [0, 0], [1, 1], [0, 0])

    with pytest.raises(ValueError, match="같을 때만"):
        validate_same_uv_projection([1, 1], [0, 0], [2, 2], [0, 0])
    with pytest.raises(ValueError, match="양수"):
        validate_same_uv_projection([0, 1], [0, 0], [0, 1], [0, 0])
    with pytest.raises(ValueError, match="양수"):
        validate_same_uv_projection([-1, 1], [0, 0], [-1, 1], [0, 0])
    with pytest.raises(ValueError, match="유한"):
        validate_same_uv_projection([float("nan"), 1], [0, 0], [float("nan"), 1], [0, 0])


def test_projected_geometry_stays_inside_texel_limits() -> None:
    alpha = np.zeros((8, 8), dtype=np.uint8)
    alpha[1:7, 2:6] = 255
    alpha[1, 2] = 64
    projected = project_master_alpha(alpha, (4, 4))

    metrics = projection_alignment_metrics(alpha, projected)

    assert metrics["center_error_texels"] <= 0.5
    assert metrics["bbox_edge_error_texels"] <= 1.0
    assert metrics["rotation_error_deg"] == 0.0

    changed = projected.copy()
    changed[0, 0] = 255
    with pytest.raises(ValueError, match="결정적 area projection"):
        projection_alignment_metrics(alpha, changed)


def test_sparse_protected_pixel_survives_large_integer_downsample() -> None:
    mask = np.zeros((32, 32), dtype=bool)
    mask[7, 19] = True

    projected = project_binary_mask(mask, (1, 1))

    assert projected.tolist() == [[True]]


def test_normal_derivation_changes_only_dxt5nm_ga_inside_effect() -> None:
    base = np.full((9, 9, 4), 128, dtype=np.uint8)
    base[..., 0] = 17
    base[..., 2] = 231
    alpha = np.zeros((9, 9), dtype=np.uint8)
    alpha[3:6, 2:7] = 255

    output, effect, metrics = derive_packed_normal(
        base,
        alpha,
        height_scale_texels=1.5,
        polarity=1,
        bevel_passes=1,
    )

    assert effect.any()
    assert np.array_equal(output[..., (0, 2)], base[..., (0, 2)])
    assert np.array_equal(output[~effect], base[~effect])
    assert metrics["changed_outside_effect_mask"] == 0
    assert metrics["changed_unselected_channels"] == 0
    assert metrics["max_packed_xy_length"] <= 1.0


def test_normal_derivation_wraps_height_across_repeat_texture_edges() -> None:
    base = np.full((7, 7, 4), 128, dtype=np.uint8)
    alpha = np.zeros((7, 7), dtype=np.uint8)
    alpha[2:5, 0] = 255

    _, effect, _ = derive_packed_normal(
        base,
        alpha,
        height_scale_texels=1.0,
        polarity=1,
        bevel_passes=0,
    )

    assert effect[2:5, -1].all()


def test_normal_polarity_reverses_derived_slope() -> None:
    base = np.full((9, 9, 4), 128, dtype=np.uint8)
    alpha = np.zeros((9, 9), dtype=np.uint8)
    alpha[3:6, 3:6] = 255
    positive, effect_positive, _ = derive_packed_normal(
        base,
        alpha,
        height_scale_texels=2.0,
        polarity=1,
        bevel_passes=0,
    )
    negative, effect_negative, _ = derive_packed_normal(
        base,
        alpha,
        height_scale_texels=2.0,
        polarity=-1,
        bevel_passes=0,
    )

    effect = effect_positive & effect_negative
    positive_x = positive[..., 3].astype(np.int16) - 128
    negative_x = negative[..., 3].astype(np.int16) - 128
    assert effect.any()
    assert float((positive_x[effect] * negative_x[effect]).mean()) < 0.0


def test_normal_derivation_blocks_negative_dxt5nm_hemisphere() -> None:
    base = np.full((7, 7, 4), 128, dtype=np.uint8)
    base[..., 3] = 230
    alpha = np.zeros((7, 7), dtype=np.uint8)
    alpha[2:5, 2:5] = 255

    with pytest.raises(ValueError, match="양의 Z 반구"):
        derive_packed_normal(
            base,
            alpha,
            height_scale_texels=8.0,
            polarity=1,
            bevel_passes=0,
        )


def test_normal_derivation_blocks_effect_lost_at_mip0_quantization() -> None:
    base = np.full((5, 5, 4), 128, dtype=np.uint8)
    alpha = np.zeros((5, 5), dtype=np.uint8)
    alpha[2, 2] = 1

    with pytest.raises(ValueError, match="실제 픽셀 변경"):
        derive_packed_normal(
            base,
            alpha,
            height_scale_texels=1e-7,
            polarity=1,
            bevel_passes=0,
        )


def test_gloss_derivation_uses_continuous_alpha_and_verified_channels() -> None:
    base = np.full((1, 3, 4), 100, dtype=np.uint8)
    base[..., 1:] = np.asarray([31, 47, 255], dtype=np.uint8)
    alpha = np.asarray([[0, 128, 255]], dtype=np.uint8)

    output, effect, metrics = derive_linear_gloss(
        base,
        alpha,
        channel_deltas={"R": 40.0},
    )

    assert output[0, :, 0].tolist() == [100, 120, 140]
    assert np.array_equal(output[..., 1:], base[..., 1:])
    assert effect.tolist() == [[False, True, True]]
    assert metrics["changed_outside_effect_mask"] == 0
    assert metrics["changed_unselected_channels"] == 0


def test_gloss_derivation_supports_negative_delta_and_clipping() -> None:
    base = np.zeros((1, 2, 4), dtype=np.uint8)
    base[..., 3] = [10, 250]
    alpha = np.full((1, 2), 255, dtype=np.uint8)

    darker, _, _ = derive_linear_gloss(base, alpha, channel_deltas={"A": -40})
    brighter, _, _ = derive_linear_gloss(base, alpha, channel_deltas={"A": 40})

    assert darker[0, :, 3].tolist() == [0, 210]
    assert brighter[0, :, 3].tolist() == [50, 255]


def test_gloss_derivation_blocks_effect_lost_to_rounding_or_saturation() -> None:
    alpha = np.full((1, 1), 255, dtype=np.uint8)

    with pytest.raises(ValueError, match="실제 픽셀 변경"):
        derive_linear_gloss(
            np.full((1, 1, 4), 128, dtype=np.uint8),
            alpha,
            channel_deltas={"R": 0.1},
        )
    with pytest.raises(ValueError, match="실제 픽셀 변경"):
        derive_linear_gloss(
            np.full((1, 1, 4), 255, dtype=np.uint8),
            alpha,
            channel_deltas={"R": 20.0},
        )
