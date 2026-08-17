from __future__ import annotations

import json
import math
from pathlib import Path
import unicodedata
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .bundles import (
    _coverage_values,
    _mip_chain,
    _pad_uv_outside,
    _resize_coverage,
    _verified_uv_coverage,
)
from .inventory import load_inventory, record_for_target
from .models import TargetSpec
from .ocr import _region_plan_sha256
from .paths import ProjectPaths
from .review import load_review, sha256_file


def _project_path(paths: ProjectPaths, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} 경로가 비어 있어요")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label}는 프로젝트 상대 경로여야 해요")
    resolved = (paths.root / relative).resolve()
    try:
        resolved.relative_to(paths.root)
    except ValueError as exc:
        raise ValueError(f"{label}가 프로젝트 밖을 가리켜요") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label}이 없어요: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label}이 JSON 객체가 아니에요")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize_text(value: Any) -> str:
    return "".join(
        character.casefold()
        for character in unicodedata.normalize("NFC", str(value))
        if character.isalnum()
    )


def _mask(paths: ProjectPaths, descriptor: Any, size: tuple[int, int], label: str) -> np.ndarray:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{label} 마스크 명세가 없어요")
    path = _project_path(paths, descriptor.get("path"), f"{label} 마스크")
    if not path.is_file() or descriptor.get("sha256") != sha256_file(path):
        raise ValueError(f"{label} 마스크가 없거나 SHA가 달라요")
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"} or image_file.size != size:
            raise ValueError(f"{label} 마스크 규격이 후보와 달라요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(np.unique(values).tolist()).issubset({0, 255}):
        raise ValueError(f"{label} 마스크는 0/255만 사용해야 해요")
    return values == 255


def _bbox_overlap(mask: np.ndarray, bbox: Any) -> float:
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(value, int) for value in bbox)
    ):
        return 0.0
    height, width = mask.shape
    x0, y0, x1, y1 = bbox
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(mask[y0:y1, x0:x1].mean())


def _ocr_requirement_counts(
    translations: list[dict[str, Any]],
    glyph_ocr_capable: dict[str, bool],
) -> tuple[dict[str, int], dict[str, int]]:
    expected_counts: dict[str, int] = {}
    explicit_occurrences: dict[str, int] = {}
    explicit_required: dict[str, int] = {}
    for region in translations:
        text = str(region.get("final_text_ko", ""))
        occurrences = int(region.get("occurrences", 0))
        expected_counts[text] = expected_counts.get(text, 0) + occurrences
        required = region.get("ocr_required")
        if isinstance(required, bool):
            explicit_occurrences[text] = explicit_occurrences.get(text, 0) + occurrences
            if required:
                explicit_required[text] = explicit_required.get(text, 0) + occurrences
    required_counts = {}
    for text, expected in expected_counts.items():
        unspecified = expected - explicit_occurrences.get(text, 0)
        required_counts[text] = explicit_required.get(text, 0) + (
            unspecified if glyph_ocr_capable.get(text, False) else 0
        )
    return expected_counts, required_counts


def _comparison_sheet(
    source: np.ndarray,
    candidate: np.ndarray,
    editable: np.ndarray,
    output: Path,
    target_id: str,
) -> None:
    height, width = source.shape[:2]
    difference = np.any(source[..., :3] != candidate[..., :3], axis=2)
    heatmap = source[..., :3].copy()
    heatmap[difference] = np.array([255, 35, 35], dtype=np.uint8)
    heatmap[editable & ~difference] = (
        heatmap[editable & ~difference].astype(np.uint16) // 2
        + np.array([0, 120, 120], dtype=np.uint16)
    ).clip(0, 255).astype(np.uint8)
    panels = [
        Image.fromarray(source[..., :3], "RGB"),
        Image.fromarray(candidate[..., :3], "RGB"),
        Image.fromarray(heatmap, "RGB"),
    ]
    labels = ("원본", "후보", "빨강=변경, 청록=편집 허용")
    label_height = 48
    sheet = Image.new("RGB", (width * 3, height + label_height), "#202124")
    draw = ImageDraw.Draw(sheet)
    font_path = Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")
    font = ImageFont.truetype(str(font_path), 20) if font_path.is_file() else ImageFont.load_default()
    for index, (panel, label) in enumerate(zip(panels, labels, strict=True)):
        sheet.paste(panel, (index * width, label_height))
        draw.text((index * width + 12, 12), label, font=font, fill="white")
    draw.text((sheet.width - 12, 12), target_id, font=font, fill="#9aa0a6", anchor="ra")
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, format="PNG", optimize=False)


def _region_comparison_sheet(
    source: np.ndarray,
    candidate: np.ndarray,
    regions: list[dict[str, Any]],
    output: Path,
) -> None:
    source_image = Image.fromarray(source[..., :3], "RGB")
    candidate_image = Image.fromarray(candidate[..., :3], "RGB")
    rows: list[tuple[str, Image.Image, Image.Image]] = []
    for index, region in enumerate(regions):
        bbox = region.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, int) for value in bbox)
        ):
            continue
        x0, y0, x1, y1 = bbox
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > source_image.width or y1 > source_image.height:
            continue
        source_crop = source_image.crop((x0, y0, x1, y1))
        candidate_crop = candidate_image.crop((x0, y0, x1, y1))
        rotation = float(region.get("rotation_deg", 0))
        if rotation:
            source_crop = source_crop.rotate(
                rotation,
                expand=True,
                resample=Image.Resampling.BICUBIC,
            )
            candidate_crop = candidate_crop.rotate(
                rotation,
                expand=True,
                resample=Image.Resampling.BICUBIC,
            )
        scale = max(1, min(5, math.ceil(144 / max(1, candidate_crop.height))))
        if scale > 1:
            size = (candidate_crop.width * scale, candidate_crop.height * scale)
            source_crop = source_crop.resize(size, Image.Resampling.LANCZOS)
            candidate_crop = candidate_crop.resize(size, Image.Resampling.LANCZOS)
        rows.append(
            (
                str(region.get("region_id", f"region-{index + 1:03d}")),
                source_crop,
                candidate_crop,
            )
        )
    if not rows:
        raise ValueError("후보 영역 비교 시트에 넣을 번역 bbox가 없어요")
    label_height = 26
    gap = 12
    width = max(left.width + gap + right.width for _, left, right in rows)
    height = sum(max(left.height, right.height) + label_height + gap for _, left, right in rows)
    sheet = Image.new("RGB", (width, height), "#202124")
    draw = ImageDraw.Draw(sheet)
    y = 0
    for label, left, right in rows:
        draw.text((6, y + 5), f"{label} | source / candidate", fill="#f1f3f4")
        row_y = y + label_height
        sheet.paste(left, (0, row_y))
        sheet.paste(right, (left.width + gap, row_y))
        y += max(left.height, right.height) + label_height + gap
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, format="PNG", optimize=False)


def _mip_seam_metrics(
    source: np.ndarray,
    candidate: np.ndarray,
    seam_guard: np.ndarray,
    mip_count: int,
    coverage: Image.Image | None = None,
) -> dict[str, Any]:
    source_levels = _mip_chain(
        Image.fromarray(source, "RGBA"), "diffuse", mip_count, coverage=coverage
    )
    candidate_levels = _mip_chain(
        Image.fromarray(candidate, "RGBA"), "diffuse", mip_count, coverage=coverage
    )
    guard_image = Image.fromarray(seam_guard.astype(np.uint8) * 255, "L")
    coverage_values = (
        _coverage_values(coverage, (source.shape[1], source.shape[0]))
        if coverage is not None
        else None
    )
    reports = []
    total = 0
    padding_mismatch = 0
    for level, (source_image, candidate_image) in enumerate(
        zip(source_levels, candidate_levels, strict=True)
    ):
        if level:
            guard_image = guard_image.resize(source_image.size, Image.Resampling.BOX)
            if coverage_values is not None:
                coverage_values = _resize_coverage(coverage_values, source_image.size)
        guard = np.asarray(guard_image, dtype=np.uint8) > 0
        source_values = np.asarray(source_image, dtype=np.uint8)
        candidate_values = np.asarray(candidate_image, dtype=np.uint8)
        changed = np.any(source_values[..., :3] != candidate_values[..., :3], axis=2)
        overlap = int((changed & guard).sum())
        total += overlap
        level_padding_mismatch = 0
        if level and coverage_values is not None:
            padded = np.asarray(
                _pad_uv_outside(candidate_image, coverage_values), dtype=np.uint8
            )
            level_padding_mismatch = int(
                (np.any(candidate_values != padded, axis=2) & ~coverage_values).sum()
            )
            padding_mismatch += level_padding_mismatch
        reports.append(
            {
                "level": level,
                "width": source_image.width,
                "height": source_image.height,
                "changed_pixels": int(changed.sum()),
                "changed_inside_seam_guard": overlap,
                "uv_padding_mismatch_pixels": level_padding_mismatch,
            }
        )
    return {
        "checked_mips": [report["level"] for report in reports],
        "missing_mips": 0,
        "mip_seam_changed_pixels": total,
        "mip_seam_changes_are_diagnostic": True,
        "uv_padding_mismatch_pixels": padding_mismatch,
        "uv_padding_verified": coverage is not None and padding_mismatch == 0,
        "mips": reports,
    }


def audit_candidate(
    target: TargetSpec,
    paths: ProjectPaths,
) -> dict[str, Any]:
    if target.action != "localize":
        raise ValueError(f"{target.id}는 원본 보존 대상이에요")
    _, review = load_review(paths, target.id, through="analysis")
    if review.get("expected_text") != list(target.exact_text):
        raise ValueError(f"{target.id} 작업 기록이 현재 profile 확정 문구와 달라요")
    target_dir = paths.reviews / target.id
    draft_dir = paths.drafts / target.id
    compose_path = draft_dir / "compose-report.json"
    lettering_path = draft_dir / "lettering-run.json"
    candidate_path = draft_dir / "candidate.png"
    ocr_path = target_dir / "candidate-ocr.json"
    compose = _read_json(compose_path, "조판 보고서")
    lettering = _read_json(lettering_path, "레터링 실행 기록")
    ocr = _read_json(ocr_path, "후보 OCR 보고서")
    if (
        compose.get("target_id") != target.id
        or lettering.get("schema_version") != 2
        or lettering.get("target_id") != target.id
        or lettering.get("source_sha256") != compose.get("source_sha256")
    ):
        raise ValueError("조판 보고서 target이 현재 품목과 달라요")
    compositor = compose.get("compositor", {})
    if (
        compose.get("schema_version") != 2
        or compositor.get("mode") != "vision-panel-localization"
        or compositor.get("fixed_font_used") is not False
        or compositor.get("single_pass_panels") is not True
        or compose.get("candidate_gate_eligible") is not True
    ):
        raise ValueError("후보가 최신 비전 패널 합성 계약으로 만들어지지 않았어요")
    lettering_descriptor = compositor.get("lettering_run")
    if not isinstance(lettering_descriptor, dict):
        raise ValueError("조판 보고서에 레터링 실행 기록 명세가 없어요")
    recorded_lettering_path = _project_path(
        paths,
        lettering_descriptor.get("path"),
        "레터링 실행 기록",
    )
    if (
        recorded_lettering_path != lettering_path.resolve()
        or lettering_descriptor.get("sha256") != sha256_file(lettering_path)
    ):
        raise ValueError("레터링 실행 기록이 조판 보고서 뒤 변경됐어요")
    if not candidate_path.is_file() or compose.get("candidate_sha256") != sha256_file(candidate_path):
        raise ValueError("후보 이미지가 없거나 조판 뒤 변경됐어요")
    source_path = _project_path(paths, review["source"]["image"], "원본")
    if review["source"].get("sha256") != sha256_file(source_path):
        raise ValueError("작업 기록의 원본 SHA가 현재 파일과 달라요")
    if compose.get("source_sha256") != review["source"].get("sha256"):
        raise ValueError("조판 후보가 현재 작업 기록의 원본에서 만들어지지 않았어요")
    candidate_sha = sha256_file(candidate_path)
    if (
        ocr.get("schema_version") != 1
        or ocr.get("phase") != "candidate"
        or ocr.get("status") != "completed"
        or ocr.get("image_sha256") != candidate_sha
        or ocr.get("recognition_contract") != "approved-regions+nfc-literal-v1"
        or ocr.get("scope") != "approved-regions"
        or ocr.get("errors") != []
    ):
        raise ValueError("후보 OCR이 현재 후보에서 오류 없이 완료되지 않았어요")

    with Image.open(source_path) as source_file:
        source_mode = source_file.mode
        source_size = source_file.size
        source = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
    with Image.open(candidate_path) as candidate_file:
        candidate_mode = candidate_file.mode
        candidate_size = candidate_file.size
        candidate = np.asarray(candidate_file.convert("RGBA"), dtype=np.uint8)
    if candidate_size != source_size or candidate_mode != source_mode:
        raise ValueError("후보의 크기·색 모드가 원본과 달라요")
    masks = {
        name: _mask(paths, compose.get("masks", {}).get(name), source_size, name)
        for name in ("old_text", "new_text", "editable", "protected", "seam_guard")
    }
    changed = np.any(source[..., :3] != candidate[..., :3], axis=2)
    alpha_equal = bool(np.array_equal(source[..., 3], candidate[..., 3]))
    inventory = load_inventory(paths.inventory)
    mip_count = int(record_for_target(inventory, target)["mip_count"])
    coverage_path = _verified_uv_coverage(paths, inventory, target.id)
    with Image.open(coverage_path) as coverage_file:
        coverage = coverage_file.convert("L")
    mip_metrics = _mip_seam_metrics(
        source,
        candidate,
        masks["seam_guard"],
        mip_count,
        coverage,
    )
    candidate_data = {
        "resized": False,
        "alpha_equal": alpha_equal,
        "source_sha256": sha256_file(source_path),
        "candidate_sha256": candidate_sha,
        "source_width": source_size[0],
        "source_height": source_size[1],
        "candidate_width": candidate_size[0],
        "candidate_height": candidate_size[1],
        "source_color_mode": source_mode,
        "candidate_color_mode": candidate_mode,
        "changed_pixels": int(changed.sum()),
        "changed_outside_editable": int((changed & ~masks["editable"]).sum()),
        "changed_inside_protected": int((changed & masks["protected"]).sum()),
        "changed_inside_seam_guard": int((changed & masks["seam_guard"]).sum()),
        **mip_metrics,
    }
    translations = review["stages"]["translation"]["data"].get("regions", [])
    lettering_counts: dict[str, int] = {}
    for run in lettering.get("lettering_runs", []):
        text = str(run.get("text", ""))
        occurrences = int(run.get("occurrences", 0))
        lettering_counts[text] = lettering_counts.get(text, 0) + occurrences
    expected_counts, _ = _ocr_requirement_counts(translations, {})
    ocr_required_counts = dict(expected_counts)
    required_translation_regions = [
        region
        for region in translations
        if isinstance(region, dict)
    ]
    expected_region_plan_sha256 = (
        _region_plan_sha256(required_translation_regions)
        if required_translation_regions
        else None
    )
    if ocr.get("region_plan_sha256") != expected_region_plan_sha256:
        raise ValueError("후보 OCR의 방향별 영역 계획이 현재 번역 기록과 달라요")
    ocr_required = {text: count > 0 for text, count in ocr_required_counts.items()}
    recipe_matched = lettering_counts == expected_counts
    detections = [value for value in ocr.get("detections", []) if isinstance(value, dict)]
    region_results = {
        str(value.get("region_id")): value
        for value in ocr.get("region_ocr", [])
        if isinstance(value, dict)
    }
    region_ocr_complete = bool(required_translation_regions) and all(
        str(region.get("region_id")) in region_results
        for region in required_translation_regions
    )
    ocr_counts = {text: 0 for text in expected_counts}
    if region_ocr_complete:
        for region in required_translation_regions:
            result = region_results[str(region["region_id"])]
            if (
                result.get("matched") is True
                and result.get("match_mode") == "nfc-literal"
                and result.get("expected_text") == region.get("final_text_ko")
            ):
                text = str(region.get("final_text_ko", ""))
                ocr_counts[text] += int(region.get("occurrences", 0))
    ocr_expected_matched = all(
        ocr_counts[text] >= count for text, count in ocr_required_counts.items()
    ) and region_ocr_complete
    expected_with_latin = [
        _normalize_text(text)
        for text in expected_counts
        if any("a" <= character.casefold() <= "z" for character in text)
    ]
    preserved_tokens = {
        _normalize_text(value)
        for value in review["stages"]["translation"]["data"].get(
            "preserved_tokens", []
        )
        if _normalize_text(value)
    }
    foreign_detections = []
    ambiguous_foreign_detections = []
    allowed_foreign_detections = []
    for value in detections:
        if not any(token in str(value.get("script", "")) for token in ("latin", "cyrillic")):
            continue
        editable_overlap = _bbox_overlap(masks["editable"], value.get("bbox"))
        if editable_overlap <= 0.05:
            continue
        normalized = _normalize_text(value.get("text", ""))
        entry = {
            "region_id": value.get("region_id"),
            "text": value.get("text"),
            "script": value.get("script"),
            "bbox": value.get("bbox"),
            "editable_overlap": round(editable_overlap, 6),
            "old_text_overlap": round(_bbox_overlap(masks["old_text"], value.get("bbox")), 6),
            "new_text_overlap": round(_bbox_overlap(masks["new_text"], value.get("bbox")), 6),
        }
        if normalized in preserved_tokens or (
            normalized
            and any(
                normalized in expected or expected in normalized
                for expected in expected_with_latin
            )
        ):
            allowed_foreign_detections.append(entry)
        elif entry["new_text_overlap"] > 0.05 or entry["old_text_overlap"] > 0.05:
            ambiguous_foreign_detections.append(entry)
        else:
            foreign_detections.append(entry)
    duplicate = any(
        lettering_counts.get(text, 0) > count for text, count in expected_counts.items()
    )
    post_ocr_data = {
        "candidate_sha256": candidate_sha,
        "engine_signature": json.dumps(
            ocr.get("engine_signature", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "forbidden_foreign_detected": bool(foreign_detections),
        "expected_text_matched": recipe_matched and ocr_expected_matched,
        "duplicate_text_detected": duplicate,
        "recipe_expected_text_matched": recipe_matched,
        "ocr_expected_text_matched": ocr_expected_matched,
        "ocr_required": ocr_required,
        "ocr_required_counts": ocr_required_counts,
        "match_mode": "nfc-literal",
        "expected_counts": expected_counts,
        "lettering_counts": lettering_counts,
        "ocr_counts": ocr_counts,
        "oriented_region_ocr_complete": region_ocr_complete,
        "oriented_region_ocr": [
            region_results[str(region["region_id"])]
            for region in required_translation_regions
            if str(region.get("region_id")) in region_results
        ],
        "foreign_detections_in_editable": foreign_detections,
        "ambiguous_foreign_detections": ambiguous_foreign_detections,
        "allowed_foreign_detections": allowed_foreign_detections,
        "requires_visual_resolution": bool(ambiguous_foreign_detections),
        "ocr_report": ocr_path.relative_to(paths.root).as_posix(),
        "ocr_report_sha256": sha256_file(ocr_path),
    }
    edit_plan_data = {
        "masks": compose["masks"],
        "compositor": compose["compositor"],
        "compose_report": compose_path.relative_to(paths.root).as_posix(),
        "compose_report_sha256": sha256_file(compose_path),
    }
    visual_template = {
        "candidate_sha256": candidate_sha,
        "translation_matched": None,
        "text_orientation_matched": None,
        "artwork_orientation_matched": None,
        "color_preserved": None,
        "sharpness_passed": None,
        "seams_preserved": None,
        "ocr_ambiguities_resolved": None,
        "requires_codex_visual_review": True,
    }
    comparison_path = target_dir / "candidate-comparison.png"
    _comparison_sheet(source, candidate, masks["editable"], comparison_path, target.id)
    region_comparison_path = target_dir / "candidate-region-comparison.png"
    _region_comparison_sheet(source, candidate, translations, region_comparison_path)
    outputs = {
        "edit_plan": target_dir / "edit-plan-data.json",
        "candidate_validation": target_dir / "candidate-validation-data.json",
        "post_ocr": target_dir / "post-ocr-data.json",
        "post_visual_template": target_dir / "post-visual-template.json",
    }
    for key, output in outputs.items():
        value = {
            "edit_plan": edit_plan_data,
            "candidate_validation": candidate_data,
            "post_ocr": post_ocr_data,
            "post_visual_template": visual_template,
        }[key]
        _write_json(output, value)
    passed = (
        alpha_equal
        and candidate_data["changed_pixels"] > 0
        and candidate_data["changed_outside_editable"] == 0
        and candidate_data["changed_inside_protected"] == 0
        and candidate_data["changed_inside_seam_guard"] == 0
        and candidate_data["uv_padding_verified"] is True
        and post_ocr_data["forbidden_foreign_detected"] is False
        and post_ocr_data["expected_text_matched"] is True
        and post_ocr_data["duplicate_text_detected"] is False
    )
    return {
        "schema_version": 1,
        "target_id": target.id,
        "candidate_sha256": candidate_sha,
        "passed": passed,
        "requires_codex_visual_review": True,
        "comparison_sheet": str(comparison_path),
        "region_comparison_sheet": str(region_comparison_path),
        "reports": {key: str(value) for key, value in outputs.items()},
        "candidate_validation": candidate_data,
        "post_ocr": post_ocr_data,
    }
