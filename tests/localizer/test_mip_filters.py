from __future__ import annotations

import numpy as np
from PIL import Image

from golani_texture_localizer.bundles import (
    _mip_chain,
    _next_mip,
    _pad_uv_outside,
    _roundtrip_limits,
)


def test_diffuse_mip_uses_linear_light_not_gamma_average() -> None:
    values = np.zeros((2, 2, 4), dtype=np.uint8)
    values[..., 3] = 255
    values[0, 0, :3] = 255

    mip = np.asarray(_next_mip(Image.fromarray(values, "RGBA"), "diffuse"))

    assert mip.shape == (1, 1, 4)
    assert 136 <= int(mip[0, 0, 0]) <= 138
    assert int(mip[0, 0, 3]) == 255


def test_normal_mip_renormalizes_dxt5nm_xy() -> None:
    values = np.full((2, 2, 4), 255, dtype=np.uint8)
    values[..., 1] = 128
    values[..., 3] = 128

    mip = np.asarray(_next_mip(Image.fromarray(values, "RGBA"), "normal"))
    x = float(mip[0, 0, 3]) / 127.5 - 1.0
    y = float(mip[0, 0, 1]) / 127.5 - 1.0

    assert abs(x) < 0.01
    assert abs(y) < 0.01


def test_unknown_mip_role_is_blocked() -> None:
    import pytest

    with pytest.raises(ValueError, match="지원하지 않는"):
        _next_mip(Image.new("RGBA", (2, 2)), "unknown")


def test_mip_chain_contains_every_requested_level() -> None:
    image = Image.new("RGBA", (8, 4), (10, 20, 30, 255))

    levels = _mip_chain(image, "diffuse", 4)

    assert [level.size for level in levels] == [(8, 4), (4, 2), (2, 1), (1, 1)]


def test_mip_chain_rejects_empty_chain() -> None:
    import pytest

    with pytest.raises(ValueError, match="1 이상"):
        _mip_chain(Image.new("RGBA", (2, 2)), "diffuse", 0)


def test_uv_padding_copies_nearest_island_texel_before_mip() -> None:
    values = np.zeros((2, 4, 4), dtype=np.uint8)
    values[..., 2] = 255
    values[..., 3] = 255
    values[:, 0, :3] = (255, 0, 0)
    coverage_values = np.zeros((2, 4), dtype=np.uint8)
    coverage_values[:, 0] = 255
    coverage = Image.fromarray(coverage_values, "L")

    levels = _mip_chain(Image.fromarray(values, "RGBA"), "diffuse", 2, coverage=coverage)
    mip = np.asarray(levels[1])

    assert np.all(mip[..., 0] == 255)
    assert np.all(mip[..., 1:3] == 0)


def test_uv_padding_keeps_top_level_byte_exact() -> None:
    values = np.arange(4 * 4 * 4, dtype=np.uint8).reshape(4, 4, 4)
    coverage = Image.new("L", (4, 4), 0)
    coverage.putpixel((0, 0), 255)

    levels = _mip_chain(Image.fromarray(values, "RGBA"), "gloss", 2, coverage=coverage)

    assert np.array_equal(np.asarray(levels[0]), values)


def test_uv_padding_rejects_empty_coverage() -> None:
    import pytest

    with pytest.raises(ValueError, match="비어"):
        _pad_uv_outside(Image.new("RGBA", (2, 2)), np.zeros((2, 2), dtype=bool))


def test_diffuse_roundtrip_limits_account_for_small_bc_blocks() -> None:
    assert _roundtrip_limits("diffuse", 512, 512, 6.0) == (6.0, 64.0, 128.0)
    assert _roundtrip_limits("diffuse", 32, 32, 6.0) == (12.0, 80.0, 128.0)
    assert _roundtrip_limits("diffuse", 16, 16, 6.0) == (16.0, 80.0, 128.0)
    assert _roundtrip_limits("diffuse", 4, 4, 6.0) == (24.0, 80.0, 128.0)
    assert _roundtrip_limits("diffuse", 2, 2, 6.0) == (16.0, 80.0, 128.0)


def test_normal_and_gloss_keep_strict_mae_at_small_mips() -> None:
    assert _roundtrip_limits("normal", 4, 4, 6.0)[0] == 6.0
    assert _roundtrip_limits("gloss", 4, 4, 6.0)[0] == 6.0
