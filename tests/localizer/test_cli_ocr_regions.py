from __future__ import annotations

import json
from pathlib import Path

from golani_texture_localizer.cli import _candidate_ocr_regions, _source_ocr_regions
from golani_texture_localizer.paths import ProjectPaths


def test_source_ocr_selects_only_vision_approved_fallback_regions(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    review = paths.reviews / "sample" / "review.json"
    review.parent.mkdir(parents=True)
    review.write_text(
        json.dumps(
            {
                "stages": {
                    "source_visual": {
                        "status": "pass",
                        "data": {
                            "regions": [
                                {"region_id": "clear", "needs_ocr_fallback": False},
                                {"region_id": "unclear", "needs_ocr_fallback": True},
                            ]
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert _source_ocr_regions(paths, "sample") == [
        {"region_id": "unclear", "needs_ocr_fallback": True}
    ]


def test_candidate_ocr_selects_every_translation_region(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    review = paths.reviews / "sample" / "review.json"
    review.parent.mkdir(parents=True)
    regions = [
        {"region_id": "brand", "ocr_required": True},
        {"region_id": "small-label", "ocr_required": False},
    ]
    review.write_text(
        json.dumps(
            {
                "stages": {
                    "translation": {"status": "pass", "data": {"regions": regions}}
                }
            }
        ),
        encoding="utf-8",
    )

    assert _candidate_ocr_regions(paths, "sample") == regions
