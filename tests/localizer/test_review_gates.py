from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from golani_texture_localizer.models import TargetSpec
from golani_texture_localizer.paths import ProjectPaths
from golani_texture_localizer.review import verify_candidate


def _target() -> TargetSpec:
    return TargetSpec(
        id="sample",
        texture="sample_D",
        name_ko="샘플",
        category="food",
        action="localize",
        bundle_key="sample.bundle",
        exact_text=("샘플",),
        notes="",
    )


def _save_rgba(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(values, "RGBA").save(path)


def test_candidate_without_review_record_is_blocked(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    values = np.zeros((4, 4, 4), dtype=np.uint8)
    source = root / "source.png"
    candidate = root / "candidate.png"
    _save_rgba(source, values)
    _save_rgba(candidate, values)

    with pytest.raises(FileNotFoundError, match="작업 기록"):
        verify_candidate(paths, _target(), source, candidate)


def test_candidate_mask_relationships_are_measured_not_trusted(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source_values = np.zeros((4, 4, 4), dtype=np.uint8)
    source_values[..., 3] = 255
    candidate_values = source_values.copy()
    candidate_values[0, 0, 0] = 255
    source = root / "source.png"
    candidate = root / "candidate.png"
    _save_rgba(source, source_values)
    _save_rgba(candidate, candidate_values)
    review = {
        "target_id": "sample",
        "action": "localize",
        "source": {"texture": "sample_D", "bundle_key": "sample.bundle", "sha256": "0" * 64},
    }
    monkeypatch.setattr(
        "golani_texture_localizer.review.load_review", lambda *args, **kwargs: (root / "review.json", review)
    )

    with pytest.raises(ValueError, match="원본 SHA-256"):
        verify_candidate(paths, _target(), source, candidate)
