from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from golani_texture_localizer.images import stage_candidate, validate_image_pair
from golani_texture_localizer.models import TargetSpec
from golani_texture_localizer.paths import ProjectPaths


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


def test_validate_image_pair_rejects_color_mode_change(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    changed_path = tmp_path / "changed.png"
    Image.new("RGBA", (4, 4), (0, 0, 0, 255)).save(source_path)
    Image.new("RGB", (4, 4), (1, 0, 0)).save(changed_path)

    report = validate_image_pair(source_path, changed_path)

    assert report["color_mode_equal"] is False
    assert report["passed"] is False


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


def test_stage_candidate_does_not_replace_approved_image_when_review_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    paths.workspace.mkdir()
    source_path = tmp_path / "source.png"
    candidate_path = tmp_path / "candidate.png"
    approved_path = paths.approved / "sample.png"
    source = np.zeros((64, 64, 4), dtype=np.uint8)
    source[..., 3] = 255
    source[8:24, 8:24, :3] = 255
    candidate = np.zeros_like(source)
    candidate[..., 3] = 255
    candidate[40:56, 40:56, :3] = 255
    previous = source.copy()
    previous[0, 0, 0] = 1
    _save(source_path, source)
    _save(candidate_path, candidate)
    approved_path.parent.mkdir(parents=True)
    _save(approved_path, previous)
    previous_bytes = approved_path.read_bytes()
    target = TargetSpec(
        id="sample",
        texture="sample_D",
        name_ko="샘플",
        category="food",
        action="localize",
        bundle_key="sample.bundle",
        exact_text=("샘플",),
        notes="",
    )
    profile = SimpleNamespace(target_by_id=lambda target_id: target)
    inventory = {"schema_version": 1, "records": [{"target_id": "sample", "source_png": str(source_path)}]}
    monkeypatch.setattr("golani_texture_localizer.images.load_inventory", lambda path: inventory)

    with pytest.raises(ValueError, match="품질 게이트"):
        stage_candidate(profile, paths, "sample", candidate_path)

    assert approved_path.read_bytes() == previous_bytes
