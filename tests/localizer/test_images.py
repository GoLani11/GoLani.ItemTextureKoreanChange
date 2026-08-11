from pathlib import Path

import numpy as np
from PIL import Image

from golani_texture_localizer.images import validate_image_pair


def _save(path: Path, value: np.ndarray) -> None:
    Image.fromarray(value, "RGBA").save(path)


def test_validate_image_pair_requires_rgb_change_and_equal_alpha(tmp_path: Path) -> None:
    source = np.zeros((8, 8, 4), dtype=np.uint8)
    source[..., 3] = 255
    changed = source.copy()
    changed[2:4, 2:4, 0] = 100
    source_path = tmp_path / "source.png"
    changed_path = tmp_path / "changed.png"
    _save(source_path, source)
    _save(changed_path, changed)

    report = validate_image_pair(source_path, changed_path)

    assert report["passed"] is True
    assert report["alpha_equal"] is True
    assert report["rgb_changed_fraction"] == 0.0625
    assert report["structure_preserved"] is True


def test_validate_image_pair_rejects_alpha_change(tmp_path: Path) -> None:
    source = np.zeros((4, 4, 4), dtype=np.uint8)
    source[..., 3] = 255
    changed = source.copy()
    changed[0, 0] = [255, 0, 0, 0]
    source_path = tmp_path / "source.png"
    changed_path = tmp_path / "changed.png"
    _save(source_path, source)
    _save(changed_path, changed)

    assert validate_image_pair(source_path, changed_path)["passed"] is False


def test_validate_image_pair_rejects_displaced_structure(tmp_path: Path) -> None:
    source = np.zeros((64, 64, 4), dtype=np.uint8)
    source[..., 3] = 255
    source[8:24, 8:24, :3] = 255
    changed = np.zeros_like(source)
    changed[..., 3] = 255
    changed[40:56, 40:56, :3] = 255
    source_path = tmp_path / "source.png"
    changed_path = tmp_path / "changed.png"
    _save(source_path, source)
    _save(changed_path, changed)

    report = validate_image_pair(source_path, changed_path)

    assert report["structure_edge_f1"] == 0.0
    assert report["structure_preserved"] is False
    assert report["passed"] is False
