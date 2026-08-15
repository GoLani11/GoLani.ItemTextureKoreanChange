from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from golani_texture_localizer.masking import _shape_mask, create_old_text_mask
from golani_texture_localizer.paths import ProjectPaths


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mask_selects_only_matching_color_inside_bbox(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    values = np.zeros((8, 8, 4), dtype=np.uint8)
    values[..., 3] = 255
    values[2:5, 3:6, :3] = 250
    source = root / "source.png"
    seam = root / "seam.png"
    Image.fromarray(values, "RGBA").save(source)
    Image.fromarray(np.zeros((8, 8), dtype=np.uint8), "L").save(seam)
    recipe = root / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "sample",
                "output_stem": "material-old-text-mask",
                "source": str(source),
                "source_sha256": _sha(source),
                "seam_guard_mask": str(seam),
                "seam_guard_mask_sha256": _sha(seam),
                "regions": [
                    {
                        "region_id": "white-text",
                        "kind": "rectangle",
                        "bbox": [1, 1, 7, 7],
                        "condition": {"luminance_min": 240},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = create_old_text_mask(paths, "sample", recipe)

    mask = np.asarray(Image.open(report["old_text_mask"]).convert("L")) == 255
    assert Path(report["old_text_mask"]).name == "material-old-text-mask.png"
    assert Path(report["report"]).name == "material-old-text-mask-report.json"
    assert int(mask.sum()) == 9
    assert mask[2:5, 3:6].all()


def test_mask_blocks_pixels_on_actual_seam_guard(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    seam = root / "seam.png"
    Image.new("RGBA", (4, 4), "white").save(source)
    seam_values = np.zeros((4, 4), dtype=np.uint8)
    seam_values[1, 1] = 255
    Image.fromarray(seam_values, "L").save(seam)
    recipe = root / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "sample",
                "source": str(source),
                "source_sha256": _sha(source),
                "seam_guard_mask": str(seam),
                "seam_guard_mask_sha256": _sha(seam),
                "regions": [{"kind": "rectangle", "bbox": [0, 0, 3, 3]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="seam guard"):
        create_old_text_mask(paths, "sample", recipe)


def test_annulus_sector_selects_only_requested_arc() -> None:
    mask = _shape_mask(
        (21, 21),
        {
            "kind": "annulus_sector",
            "center": [10, 10],
            "inner_radius": 5,
            "outer_radius": 8,
            "start_angle_deg": 200,
            "end_angle_deg": 340,
        },
        0,
    )

    assert mask[4, 10]
    assert not mask[16, 10]
    assert not mask[10, 10]
