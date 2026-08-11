from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .inventory import load_inventory, record_for_target
from .models import CollectionProfile, TargetSpec
from .paths import ProjectPaths


_MIN_STRUCTURE_EDGE_PIXELS = 32
_MIN_STRUCTURE_EDGE_F1 = 0.5


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _approved_path(paths: ProjectPaths, target: TargetSpec) -> Path:
    return paths.approved / f"{target.id}.png"


def stage_candidate(
    profile: CollectionProfile,
    paths: ProjectPaths,
    target_id: str,
    candidate_path: Path,
    *,
    allow_resize: bool = False,
) -> dict[str, Any]:
    inventory = load_inventory(paths.inventory)
    target = profile.target_by_id(target_id)
    if target.action != "localize":
        raise ValueError(f"{target.id}는 외국어 인쇄가 없는 보존 대상이에요")
    record = record_for_target(inventory, target)
    source_path = Path(record["source_png"])
    if not source_path.is_file() or not candidate_path.is_file():
        raise FileNotFoundError(source_path if not source_path.is_file() else candidate_path)

    with Image.open(source_path) as source_file:
        source = source_file.convert("RGBA")
    with Image.open(candidate_path) as candidate_file:
        candidate = candidate_file.convert("RGBA")
    resized = candidate.size != source.size
    if resized and not allow_resize:
        raise ValueError(f"후보 크기 {candidate.size}가 원본 {source.size}와 달라요")
    if resized:
        candidate = candidate.resize(source.size, Image.Resampling.LANCZOS)
    candidate.putalpha(source.getchannel("A"))

    destination = _approved_path(paths, target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate.save(destination)
    report = validate_image_pair(source_path, destination)
    report.update(
        {
            "target_id": target.id,
            "name_ko": target.name_ko,
            "candidate": str(candidate_path.resolve()),
            "approved": str(destination.resolve()),
            "resized": resized,
        }
    )
    report_path = paths.reports / "images" / f"{target.id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def validate_image_pair(source_path: Path, edited_path: Path) -> dict[str, Any]:
    with Image.open(source_path) as source_file:
        source = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
    with Image.open(edited_path) as edited_file:
        edited = np.asarray(edited_file.convert("RGBA"), dtype=np.uint8)
    if source.shape != edited.shape:
        raise ValueError(f"이미지 shape 불일치: {source.shape} != {edited.shape}")
    alpha_equal = bool(np.array_equal(source[..., 3], edited[..., 3]))
    rgb_changed = np.any(source[..., :3] != edited[..., :3], axis=2)
    changed_fraction = float(rgb_changed.mean())
    structure = _structure_overlap(source[..., :3], edited[..., :3])
    structure_preserved = (
        structure["structure_edge_f1"] is None
        or structure["structure_edge_f1"] >= _MIN_STRUCTURE_EDGE_F1
    )
    return {
        "source": str(source_path.resolve()),
        "source_sha256": _sha256(source_path),
        "edited_sha256": _sha256(edited_path),
        "width": int(source.shape[1]),
        "height": int(source.shape[0]),
        "alpha_equal": alpha_equal,
        "rgb_changed_fraction": round(changed_fraction, 8),
        "has_rgb_changes": bool(rgb_changed.any()),
        **structure,
        "structure_preserved": structure_preserved,
        "passed": alpha_equal and bool(rgb_changed.any()) and structure_preserved,
    }


def _adaptive_edges(rgb: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    gradient_x = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = cv2.magnitude(gradient_x, gradient_y)
    nonzero = magnitude[magnitude > 0]
    high = max(8.0, float(np.percentile(nonzero, 85))) if nonzero.size else 8.0
    return cv2.Canny(blurred, high * 0.4, high) > 0


def _structure_overlap(source_rgb: np.ndarray, edited_rgb: np.ndarray) -> dict[str, float | int | None]:
    source_edges = _adaptive_edges(source_rgb)
    edited_edges = _adaptive_edges(edited_rgb)
    source_count = int(source_edges.sum())
    edited_count = int(edited_edges.sum())
    report: dict[str, float | int | None] = {
        "source_edge_count": source_count,
        "edited_edge_count": edited_count,
        "source_edge_retention": None,
        "edited_edge_alignment": None,
        "structure_edge_f1": None,
    }
    if min(source_count, edited_count) < _MIN_STRUCTURE_EDGE_PIXELS:
        return report

    kernel = np.ones((5, 5), dtype=np.uint8)
    source_neighborhood = cv2.dilate(source_edges.astype(np.uint8), kernel) > 0
    edited_neighborhood = cv2.dilate(edited_edges.astype(np.uint8), kernel) > 0
    source_retention = float(edited_neighborhood[source_edges].mean())
    edited_alignment = float(source_neighborhood[edited_edges].mean())
    denominator = source_retention + edited_alignment
    score = 2 * source_retention * edited_alignment / denominator if denominator else 0.0
    report.update(
        {
            "source_edge_retention": round(source_retention, 8),
            "edited_edge_alignment": round(edited_alignment, 8),
            "structure_edge_f1": round(score, 8),
        }
    )
    return report


def validate_approved(profile: CollectionProfile, paths: ProjectPaths) -> dict[str, Any]:
    inventory = load_inventory(paths.inventory)
    reports = []
    missing = []
    for target in profile.targets:
        if target.action == "preserve":
            reports.append(
                {
                    "target_id": target.id,
                    "action": "preserve",
                    "passed": True,
                    "reason": "foreign text absent",
                }
            )
            continue
        edited = _approved_path(paths, target)
        if not edited.is_file():
            missing.append(target.id)
            continue
        record = record_for_target(inventory, target)
        report = validate_image_pair(Path(record["source_png"]), edited)
        report["target_id"] = target.id
        reports.append(report)
    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "target_count": len(profile.targets),
        "approved_count": sum(report.get("action") != "preserve" for report in reports),
        "preserved_count": sum(report.get("action") == "preserve" for report in reports),
        "missing": missing,
        "passed": not missing and all(report["passed"] for report in reports),
        "reports": reports,
    }
    destination = paths.reports / "approved.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def create_review_sheets(
    profile: CollectionProfile,
    paths: ProjectPaths,
    *,
    approved: bool,
    columns: int = 2,
) -> list[Path]:
    inventory = load_inventory(paths.inventory)
    cards: list[tuple[TargetSpec, Path]] = []
    for target in profile.targets:
        if approved:
            path = _approved_path(paths, target)
        else:
            path = Path(record_for_target(inventory, target)["source_png"])
        if path.is_file():
            cards.append((target, path))

    card_size = 768
    label_height = 64
    rows = 2
    per_sheet = columns * rows
    destination_dir = paths.reports / ("approved-sheets" if approved else "source-sheets")
    destination_dir.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")
    font = ImageFont.truetype(str(font_path), 24) if font_path.is_file() else ImageFont.load_default()
    results: list[Path] = []
    for page, start in enumerate(range(0, len(cards), per_sheet), 1):
        subset = cards[start : start + per_sheet]
        sheet = Image.new("RGB", (columns * card_size, rows * (card_size + label_height)), "#202124")
        draw = ImageDraw.Draw(sheet)
        for index, (target, path) in enumerate(subset):
            col = index % columns
            row = index // columns
            x = col * card_size
            y = row * (card_size + label_height)
            with Image.open(path) as image_file:
                # Texture alpha often stores material data rather than display opacity.
                # Review sheets therefore show the RGB print surface directly.
                image = image_file.convert("RGB")
                image.thumbnail((card_size, card_size), Image.Resampling.LANCZOS)
            backdrop = Image.new("RGB", (card_size, card_size), (232, 232, 232))
            backdrop.paste(image, ((card_size - image.width) // 2, (card_size - image.height) // 2))
            sheet.paste(backdrop, (x, y))
            draw.text((x + 12, y + card_size + 8), f"{target.id} · {target.name_ko}", font=font, fill="white")
        output = destination_dir / f"{page:02d}.png"
        sheet.save(output)
        results.append(output)
    return results
