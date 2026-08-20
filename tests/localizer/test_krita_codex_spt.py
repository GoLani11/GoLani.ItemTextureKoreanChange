from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYKRITA = ROOT / "tools/krita_codex_image_edit/pykrita"
if str(PYKRITA) not in sys.path:
    sys.path.insert(0, str(PYKRITA))

from golani_codex_image_edit import spt  # noqa: E402


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    skill = root / ".agents/skills/localize-spt-food-textures/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# test", encoding="utf-8")
    profile = {
        "targets": [
            {
                "id": "can",
                "name_ko": "시험 캔",
                "texture": "can_diff",
                "bundle_key": "assets/can.bundle",
                "exact_text": ["시험"],
            },
            {
                "id": "keep",
                "name_ko": "보존",
                "texture": "keep_diff",
                "bundle_key": "assets/keep.bundle",
                "action": "preserve",
                "exact_text": ["외국어 인쇄 없음"],
            },
        ]
    }
    profile_path = root / "profiles/food/collection.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    source_path = root / "workspace/source/can.png"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"source")
    masks = {}
    for name in spt.MASK_NAMES:
        path = root / f"workspace/reviews/can/masks/{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = name.encode("ascii")
        path.write_bytes(data)
        masks[name] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "width": 4,
            "height": 4,
        }
    typography = {
        "style_class": "display",
        "stroke_character": "heavy",
        "glyph_proportions": "wide",
        "alignment": "centered",
        "spacing": "tight",
        "effects": "outline",
        "surface_finish": "worn",
    }
    review = {
        "schema_version": 1,
        "target_id": "can",
        "action": "localize",
        "expected_text": ["시험"],
        "source": {
            "bundle_key": "assets/can.bundle",
            "texture": "can_diff",
            "image": source_path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(b"source").hexdigest(),
            "width": 4,
            "height": 4,
        },
        "stages": {
            "source_visual": {
                "status": "pass",
                "data": {
                    "vision_first": True,
                    "ocr_fallback_required": False,
                    "regions": [
                        {
                            "region_id": "front",
                            "needs_ocr_fallback": False,
                            "typography": typography,
                        },
                        {
                            "region_id": "side",
                            "needs_ocr_fallback": False,
                            "typography": typography,
                        },
                    ]
                },
            },
            "translation": {
                "status": "pass",
                "data": {
                    "regions": [
                        {
                            "region_id": "front",
                            "source_text": "TEST",
                            "final_text_ko": "시험",
                            "bbox": [0, 0, 2, 2],
                            "rotation_deg": 0,
                            "direction": "left-to-right",
                            "face": "front label",
                            "occurrences": 1,
                        },
                        {
                            "region_id": "side",
                            "source_text": "SIDE",
                            "final_text_ko": "옆면",
                            "bbox": [2, 0, 4, 2],
                            "rotation_deg": 90,
                            "direction": "top-to-bottom",
                            "face": "side label",
                            "occurrences": 1,
                        },
                    ]
                },
            },
            "edit_plan": {"status": "pending", "data": {"masks": masks}},
        },
    }
    review_path = root / "workspace/reviews/can/review.json"
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return root


def test_scan_shows_preflight_readiness(tmp_path: Path) -> None:
    root = _project(tmp_path)
    summaries = spt.scan_spt_targets(root)

    assert [(item.target_id, item.state) for item in summaries] == [
        ("can", "형식 준비됨 · 공식 게이트·SHA 검사 대기"),
        ("keep", "보존 대상"),
    ]


def test_inspection_keeps_safe_source_and_masks_for_blocked_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(
        spt,
        "_validate_analysis_with_project_script",
        lambda root, record: ["source_visual.data.vision_first: true여야 해요"],
    )

    preparation = spt.inspect_spt_target(root, "can")

    assert not preparation.ready
    assert preparation.source.path.name == "can.png"
    assert set(preparation.masks) == set(spt.MASK_NAMES)
    assert preparation.mask_error is None
    assert preparation.analysis_errors == (
        "source_visual.data.vision_first: true여야 해요",
    )


def test_load_target_binds_profile_hashes_panels_and_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    target = spt.load_spt_target(root, "can")

    assert target.target_id == "can"
    assert len(target.panels) == 2
    assert target.panels[1].rotation_deg == 90
    prompt = spt.build_spt_prompt(target, target.panels[0], "낡은 인쇄 유지", 512, 512)
    assert prompt.startswith("$imagegen\nUse case: text-localization")
    assert '"TEST" -> "시험"' in prompt
    assert "pre-OCR visual preview only" in prompt
    assert "num_last_images_to_include=2" in prompt


def test_load_target_rejects_official_analysis_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(
        spt,
        "_validate_analysis_with_project_script",
        lambda root, record: ["translation: block"],
    )

    with pytest.raises(ValueError, match="analysis 게이트가 막혔어요"):
        spt.load_spt_target(root, "can")
