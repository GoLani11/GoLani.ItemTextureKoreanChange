from __future__ import annotations

import numpy as np
from PIL import Image

from golani_texture_localizer.bundles import _next_mip


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
