from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest
from PIL import Image


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
    mask_pixels = {
        "old_text": bytes([255, 0, 0, 0] + [0] * 12),
        "new_text": bytes([0, 255, 0, 0] + [0] * 12),
        "editable": bytes([255, 255, 0, 0] + [0] * 12),
        "protected": bytes([0, 0, 255, 255] + [255] * 12),
        "seam_guard": bytes([0, 0, 255, 0] + [0] * 12),
    }
    for name in spt.MASK_NAMES:
        path = root / f"workspace/reviews/can/masks/{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.frombytes("L", (4, 4), mask_pixels[name]).save(path)
        data = path.read_bytes()
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


def _update_mask_descriptor(root: Path, name: str) -> None:
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    path = root / f"workspace/reviews/can/masks/{name}.png"
    review["stages"]["edit_plan"]["data"]["masks"][name]["sha256"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")


def _preview_spt_identity() -> dict[str, object]:
    return {
        "target_id": "can",
        "panel_id": "front",
        "review_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "alpha_semantics": "material",
        "working_view_transform": "source-rgb-force-alpha-255:v1",
        "working_view": {
            "path": "workspace/krita-spt/view-sources/can/view.png",
            "file_sha256": "e" * 64,
        },
        "model_input": {
            "source_file_sha256": "1" * 64,
            "source_pixel_sha256": "2" * 64,
            "selection_mask_file_sha256": "3" * 64,
            "selection_mask_pixel_sha256": "4" * 64,
        },
        "mask_sha256": {
            name: f"{index + 1:x}" * 64
            for index, name in enumerate(spt.MASK_NAMES)
        },
    }


def test_working_view_path_rejects_symlink_or_hardlink_to_source(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "workspace/source/item.png"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"pinned source")
    artifact = spt.SptArtifact(
        path=source_path,
        project_path="workspace/source/item.png",
        sha256="a" * 64,
        width=1,
        height=1,
    )
    working_view = spt.spt_working_view_path(tmp_path, "item", artifact)
    working_view.parent.mkdir(parents=True)

    try:
        working_view.symlink_to(source_path)
    except OSError:
        pytest.skip("이 파일시스템은 symlink 회귀 검사를 지원하지 않아요")
    with pytest.raises(ValueError, match="심볼릭 링크|전용 폴더"):
        spt.spt_working_view_path(tmp_path, "item", artifact)
    working_view.unlink()

    try:
        working_view.hardlink_to(source_path)
    except OSError:
        pytest.skip("이 파일시스템은 hardlink 회귀 검사를 지원하지 않아요")
    with pytest.raises(ValueError, match="불변 원본 파일"):
        spt.spt_working_view_path(tmp_path, "item", artifact)


def test_preview_choice_record_has_no_plugin_ocr_state() -> None:
    spt_identity = _preview_spt_identity()
    record = spt.build_preview_choice_record(
        spt_identity,
        {"artifact": {"sha256": "d" * 64}},
        status="selected-for-validation",
        created_at="2026-08-20T00:00:00+00:00",
        request_sha256="f" * 64,
    )

    assert record["schema_version"] == 3
    assert record["purpose"] == "human-visual-selection"
    assert record["next_gate"] == "external-project-validation"
    assert record["candidate_approved"] is False
    assert "ocr" not in json.dumps(record).lower()
    assert record["mask_sha256"] == spt_identity["mask_sha256"]
    assert record["alpha_semantics"] == "material"
    assert record["working_view_transform"] == "source-rgb-force-alpha-255:v1"
    assert record["working_view_sha256"] == "e" * 64
    assert record["model_input"] == spt_identity["model_input"]
    assert record["request_sha256"] == "f" * 64


def test_preview_choice_record_supports_discard_without_next_gate() -> None:
    record = spt.build_preview_choice_record(
        _preview_spt_identity(),
        {"artifact": {"sha256": "d" * 64}},
        status="discarded",
        created_at="2026-08-20T00:00:00+00:00",
        request_sha256="f" * 64,
    )

    assert record["next_gate"] == "none"
    assert record["candidate_approved"] is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target_id", "", "target_id"),
        ("review_sha256", "not-a-sha", "review"),
        ("alpha_semantics", "opacity", "alpha semantics"),
        ("working_view_transform", "legacy", "작업 뷰 변환"),
        ("working_view", None, "작업 뷰 기록"),
        ("model_input", None, "imagegen 입력"),
        ("mask_sha256", {"editable": "c" * 64}, "5종 mask"),
    ],
)
def test_preview_choice_record_rejects_unpinned_identity(
    field: str,
    value: object,
    message: str,
) -> None:
    spt_data = _preview_spt_identity()
    spt_data[field] = value

    with pytest.raises(ValueError, match=message):
        spt.build_preview_choice_record(
            spt_data,
            {"artifact": {"sha256": "d" * 64}},
            status="selected-for-validation",
            created_at="2026-08-20T00:00:00+00:00",
            request_sha256="f" * 64,
        )


def test_preview_choice_record_rejects_legacy_ocr_status() -> None:
    with pytest.raises(ValueError, match="지원하지 않는"):
        spt.build_preview_choice_record(
            {},
            {"artifact": {"sha256": "d" * 64}},
            status="selected-for-panel-ocr",
            created_at="2026-08-20T00:00:00+00:00",
            request_sha256="f" * 64,
        )


def test_preview_choice_record_rejects_unpinned_request() -> None:
    with pytest.raises(ValueError, match="request"):
        spt.build_preview_choice_record(
            _preview_spt_identity(),
            {"artifact": {"sha256": "d" * 64}},
            status="selected-for-validation",
            created_at="2026-08-20T00:00:00+00:00",
            request_sha256="not-a-sha",
        )


def test_scan_shows_current_gate_and_hash_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summaries = spt.scan_spt_targets(root)

    assert [(item.target_id, item.state, item.status) for item in summaries] == [
        ("can", "생성 준비됨", "ready"),
        ("keep", "보존 대상", "preserve"),
    ]


def test_scan_reports_analysis_and_mask_problems_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["stages"]["edit_plan"]["data"] = {}
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        spt,
        "_validate_analysis_with_project_script",
        lambda root, record: ["translation: pending"],
    )

    summary = spt.scan_spt_targets(root)[0]

    assert summary.status == "analysis-and-masks-required"
    assert summary.state == "analysis·마스크 갱신 필요"
    assert summary.preparation_required
    assert summary.issues[0] == "translation: pending"
    assert "5종 편집 마스크" in summary.issues[1]


def test_scan_checks_current_mask_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    summary = spt.scan_spt_targets(root)[0]

    assert summary.status == "masks-required"
    assert summary.state == "마스크 갱신 필요"
    assert any("현재 파일 SHA" in issue for issue in summary.issues)


@pytest.mark.parametrize("failure", ["rgb", "size", "contract"])
def test_scan_checks_actual_mask_png_and_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root = _project(tmp_path)
    path = root / "workspace/reviews/can/masks/old_text.png"
    if failure == "rgb":
        Image.new("RGB", (4, 4), (255, 255, 255)).save(path)
    elif failure == "size":
        Image.new("L", (2, 2), 255).save(path)
    else:
        Image.new("L", (4, 4), 0).save(path)
        editable = root / "workspace/reviews/can/masks/editable.png"
        Image.new("L", (4, 4), 0).save(editable)
        _update_mask_descriptor(root, "editable")
    _update_mask_descriptor(root, "old_text")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    summary = spt.scan_spt_targets(root)[0]

    assert summary.status == "masks-required"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("action", "preserve", "action"),
        ("exact_text", ["새 문구"], "확정 문구"),
    ],
)
def test_scan_rejects_review_identity_stale_against_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    root = _project(tmp_path)
    if field == "action":
        review_path = root / "workspace/reviews/can/review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["action"] = value
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    else:
        profile_path = root / "profiles/food/collection.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["targets"][0][field] = value
        profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    summary = spt.scan_spt_targets(root)[0]

    assert summary.status == "record-or-source-error"
    assert any(message in issue for issue in summary.issues)


def test_preparation_request_pins_all_blocked_reviews_without_bypassing_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summaries = spt.scan_spt_targets(root)

    record = spt.build_spt_preparation_request(
        root,
        summaries,
        created_at="2026-08-20T00:00:00+00:00",
    )

    assert record["status"] == "pending"
    assert record["purpose"] == "spt-analysis-and-mask-preparation"
    assert [item["target_id"] for item in record["targets"]] == ["can"]
    assert record["targets"][0]["requested_work"] == ["five-masks"]
    assert record["targets"][0]["review"]["sha256"] == summaries[0].review_sha256
    assert record["targets"][0]["source_from_review"]["current_sha256"] == hashlib.sha256(
        b"source"
    ).hexdigest()
    editable = record["targets"][0]["masks_from_review"]["editable"]
    assert editable["recorded_sha256"] != editable["current_sha256"]
    assert editable["fresh"] is False
    assert "mask-editable-sha-mismatch" in record["targets"][0]["issue_codes"]
    assert record["safety"]["gate_override"] is False
    assert record["safety"]["generation_allowed"] is False
    assert record["safety"]["one_target_per_validation_unit"] is True
    assert record["safety"]["additional_generation_attempts_approved"] is False
    assert record["safety"]["attempt_counters_reset"] is False

    first = spt.write_spt_preparation_request(
        root,
        summaries,
        created_at="2026-08-20T00:00:00+00:00",
    )
    second = spt.write_spt_preparation_request(
        root,
        summaries,
        created_at="2026-08-20T00:01:00+00:00",
    )
    assert first == second
    written = json.loads(first.read_text(encoding="utf-8"))
    assert len(written["request_fingerprint"]) == 64


def test_preparation_request_ignores_existing_review_for_preserve_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")
    preserve_review = root / "workspace/reviews/keep/review.json"
    preserve_review.parent.mkdir(parents=True)
    preserve_review.write_text('{"target_id":"keep"}', encoding="utf-8")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    record = spt.build_spt_preparation_request(
        root,
        spt.scan_spt_targets(root),
        created_at="2026-08-20T00:00:00+00:00",
    )

    assert [item["target_id"] for item in record["targets"]] == ["can"]


def test_scan_and_request_keep_exhausted_generation_budget_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["stages"]["edit_plan"]["data"]["generation_attempts"] = 4
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    summary = spt.scan_spt_targets(root)[0]

    assert summary.status == "attempt-budget-exhausted"
    assert summary.recorded_generation_attempts == 4
    assert summary.attempt_budget_exhausted is True
    assert "attempt-budget-exhausted" in summary.issue_codes
    preparation = spt.inspect_spt_target(root, "can")
    assert preparation.ready is False

    record = spt.build_spt_preparation_request(
        root,
        (summary,),
        created_at="2026-08-20T00:00:00+00:00",
    )
    budget = record["targets"][0]["generation_budget"]
    assert budget == {
        "default_limit": spt.MAX_GENERATION_ATTEMPTS,
        "recorded_attempts": 4,
        "exhausted": True,
        "additional_attempts_approved": False,
        "attempt_counter_reset": False,
        "this_request_is_not_approval": True,
    }
    assert "generation-budget-review" in record["targets"][0]["requested_work"]


def test_preparation_request_pins_current_source_sha_and_mismatch_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    source_path = root / "workspace/source/can.png"
    source_path.write_bytes(b"new-source")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summary = spt.scan_spt_targets(root)[0]

    record = spt.build_spt_preparation_request(
        root,
        (summary,),
        created_at="2026-08-20T00:00:00+00:00",
    )

    target = record["targets"][0]
    source = target["source_from_review"]
    assert source["recorded_sha256"] == hashlib.sha256(b"source").hexdigest()
    assert source["current_sha256"] == hashlib.sha256(b"new-source").hexdigest()
    assert source["fresh"] is False
    assert "source-sha-mismatch" in target["issue_codes"]


def test_preparation_request_rejects_review_changed_after_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summaries = spt.scan_spt_targets(root)
    review_path = root / "workspace/reviews/can/review.json"
    review_path.write_text(review_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="새로고침 뒤 바뀌었어요"):
        spt.build_spt_preparation_request(
            root,
            summaries,
            created_at="2026-08-20T00:00:00+00:00",
        )


def test_preparation_request_rejects_ready_artifact_changed_after_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summaries = spt.scan_spt_targets(root)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")

    with pytest.raises(ValueError, match="원본 또는 마스크가 새로고침 뒤 바뀌었어요"):
        spt.build_spt_preparation_request(
            root,
            summaries,
            created_at="2026-08-20T00:00:00+00:00",
        )


def test_preparation_request_rejects_existing_payload_with_forged_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summaries = spt.scan_spt_targets(root)
    request_path = spt.write_spt_preparation_request(
        root,
        summaries,
        created_at="2026-08-20T00:00:00+00:00",
    )
    record = json.loads(request_path.read_text(encoding="utf-8"))
    record["safety"]["generation_allowed"] = True
    request_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="내용이 fingerprint와 달라요"):
        spt.write_spt_preparation_request(
            root,
            summaries,
            created_at="2026-08-20T00:01:00+00:00",
        )


def test_preparation_request_rejects_destination_symlink_outside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "workspace/reviews/can/masks/editable.png").write_bytes(b"changed")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    summaries = spt.scan_spt_targets(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    request_parent = root / "workspace/krita-spt"
    request_parent.mkdir(parents=True)
    (request_parent / "preparation-requests").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="프로젝트 밖"):
        spt.write_spt_preparation_request(
            root,
            summaries,
            created_at="2026-08-20T00:00:00+00:00",
        )


def test_generation_budget_is_attributed_to_connected_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["stages"]["edit_plan"]["data"]["compositor"] = {
        "regions": [
            {"region_id": "front", "generation_attempts": 2},
            {"region_id": "side", "generation_attempts": 0},
        ]
    }
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    summary = spt.scan_spt_targets(root)[0]
    target = spt.load_spt_target(root, "can")

    assert summary.status == "ready"
    assert [panel.recorded_attempts for panel in target.panels] == [2, 0]
    assert spt.first_available_spt_panel(target) == target.panels[1]


def test_legacy_top_level_generation_budget_still_locks_every_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["stages"]["edit_plan"]["data"]["generation_attempts"] = 2
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    summary = spt.scan_spt_targets(root)[0]
    target = spt.load_spt_target(root, "can")

    assert summary.status == "attempt-budget-exhausted"
    assert [panel.recorded_attempts for panel in target.panels] == [2, 2]


def test_scan_counts_local_generation_jobs_when_every_panel_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    target = spt.load_spt_target(root, "can")
    for panel in target.panels:
        for attempt in range(2):
            request_path = (
                root
                / "workspace/krita-spt/can"
                / panel.panel_id
                / f"job-{attempt}"
                / "request.json"
            )
            request_path.parent.mkdir(parents=True)
            request_path.write_text(
                json.dumps(
                    {
                        "spt": {
                            "target_id": target.target_id,
                            "review_sha256": target.review_sha256,
                            "panel_id": panel.panel_id,
                            "face": panel.face,
                            "generation_attempt": attempt + 1,
                            "panel_transform": {
                                "source_rotation_deg": panel.rotation_deg,
                            },
                        },
                        "generation": {"artifact": {"sha256": "a" * 64}},
                    }
                ),
                encoding="utf-8",
            )

    summary = spt.scan_spt_targets(root)[0]
    preparation = spt.inspect_spt_target(root, "can")

    assert summary.status == "attempt-budget-exhausted"
    assert summary.recorded_generation_attempts == 2
    assert preparation.ready is False


def test_local_generation_attempt_survives_unrelated_review_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    target = spt.load_spt_target(root, "can")
    panel = target.panels[0]
    request_path = (
        root
        / "workspace/krita-spt/can"
        / panel.panel_id
        / "job-0/request.json"
    )
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps(
            {
                "spt": {
                    "target_id": "can",
                    "review_sha256": target.review_sha256,
                    "panel_id": panel.panel_id,
                    "face": panel.face,
                    "generation_attempt": 1,
                    "panel_transform": {
                        "source_rotation_deg": panel.rotation_deg,
                    },
                },
                "generation": {"artifact": {"sha256": "a" * 64}},
            }
        ),
        encoding="utf-8",
    )
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["unrelated_note"] = "review hash changed"
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    refreshed = spt.load_spt_target(root, "can")

    assert spt.current_generation_attempts(refreshed, refreshed.panels[0]) == 1


def test_review_attempt_and_matching_local_job_are_not_double_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    review_path = root / "workspace/reviews/can/review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["stages"]["edit_plan"]["data"]["compositor"] = {
        "regions": [{"region_id": "front", "generation_attempts": 1}]
    }
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])
    target = spt.load_spt_target(root, "can")
    panel = target.panels[0]
    request_path = (
        root
        / "workspace/krita-spt/can"
        / panel.panel_id
        / "job-0/request.json"
    )
    request_path.parent.mkdir(parents=True)
    request_path.write_text(
        json.dumps(
            {
                "spt": {
                    "target_id": "can",
                    "panel_id": panel.panel_id,
                    "face": panel.face,
                    "generation_attempt": 1,
                    "panel_transform": {
                        "source_rotation_deg": panel.rotation_deg,
                    },
                },
                "generation": {"artifact": {"sha256": "a" * 64}},
            }
        ),
        encoding="utf-8",
    )

    assert spt.current_generation_attempts(target, panel) == 1


def test_load_rejects_review_changed_between_inspection_and_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    review_path = (root / "workspace/reviews/can/review.json").resolve()
    original = spt._read_json_snapshot
    changed = False

    def read_and_change(path: Path) -> tuple[dict[str, object], str]:
        nonlocal changed
        result = original(path)
        if path.resolve() == review_path and not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(spt, "_read_json_snapshot", read_and_change)
    monkeypatch.setattr(spt, "_validate_analysis_with_project_script", lambda root, record: [])

    with pytest.raises(ValueError, match="검사 중 바뀌었어요"):
        spt.load_spt_target(root, "can")


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
    assert "unvalidated visual preview only" in prompt
    assert "ocr" not in prompt.lower()
    assert "num_last_images_to_include=2" in prompt
    assert "display-only alpha is forced to 255" in prompt
    assert "source alpha is material data" in prompt


def test_docker_selection_ui_has_no_plugin_ocr_wording() -> None:
    source = (
        ROOT
        / "tools/krita_codex_image_edit/pykrita/golani_codex_image_edit/docker.py"
    ).read_text(encoding="utf-8")

    assert 'QPushButton("이 결과 선택")' in source
    assert 'QPushButton("결과 제외")' in source
    assert 'QPushButton("SPT RGB 작업 뷰·추천 선택 불러오기")' in source
    assert '_record_spt_preview_choice("selected-for-validation")' in source
    assert '_record_spt_preview_choice("discarded")' in source
    assert 'QPushButton("전체 준비 요청 기록")' in source
    assert "spt_targets_ready = pyqtSignal(object)" in source
    assert "self._shutting_down = False" in source
    assert "if self._shutting_down:" in source
    assert "self._shutting_down = True" in source
    assert "session.shutdown()" in source
    assert "[SPT 검증 전 미리보기]" in source
    assert "_open_spt_working_view(" in source
    assert "_verify_spt_working_view(" in source
    assert "source-rgb-force-alpha-255:v1" in source
    assert '"pixel_sha256": model_source_pixel_sha256' in source
    assert '"pre_transform_pixel_sha256": sha256_bytes(source_bgra)' in source
    assert "request_sha256=_sha256_file(request_path)" in source
    assert "candidate_directory.rename(published_directory)" in source
    assert "기존 SPT 작업 뷰 디렉터리가 불완전해 덮어쓰지 않았어요" in source
    assert "time.sleep" not in source
    assert "os.link(temporary, view_path)" not in source
    assert "_open_or_activate_document(preparation.source.path)" not in source
    assert "_open_or_activate_document(current.source.path)" not in source
    assert "ocr" not in source.lower()


def test_docker_clears_previous_panel_context_before_switching_panel() -> None:
    source = (
        ROOT
        / "tools/krita_codex_image_edit/pykrita/golani_codex_image_edit/docker.py"
    ).read_text(encoding="utf-8")
    start = source.index("    def _spt_panel_changed(self) -> None:")
    end = source.index("    def _open_spt_panel(", start)
    handler = source[start:end]

    assert handler.index("self._spt_panel = None") < handler.index(
        "self._open_spt_panel(self._spt_target, panel)"
    )
    assert handler.index("self._spt_allowed_mask = None") < handler.index(
        "self._open_spt_panel(self._spt_target, panel)"
    )


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
