from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .paths import ProjectPaths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _intersection_over_smaller(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / max(1, min(left_area, right_area))


def _visual_regions(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    candidates = []
    for detection in detections:
        bbox = detection.get("bbox")
        confidence = float(detection.get("confidence", 0))
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or bbox[2] - bbox[0] < 4
            or bbox[3] - bbox[1] < 4
            or confidence < 0.45
            or (detection.get("script") == "other" and confidence < 0.75)
        ):
            continue
        candidates.append(detection)
    candidates.sort(
        key=lambda value: (
            (value["bbox"][2] - value["bbox"][0])
            * (value["bbox"][3] - value["bbox"][1]),
            float(value.get("confidence", 0)),
        ),
        reverse=True,
    )
    for detection in candidates:
        bbox = detection["bbox"]
        confidence = float(detection.get("confidence", 0))
        if any(
            _intersection_over_smaller(bbox, current["bbox"]) >= 0.78
            and float(current["confidence"]) >= confidence * 0.7
            for current in selected
        ):
            continue
        selected.append(
            {
                "bbox": [int(value) for value in bbox],
                "rotation_deg": int(detection.get("rotation_deg", 0)),
                "ocr_region_id": str(detection.get("region_id", "")),
                "confidence": confidence,
            }
        )
    selected.sort(key=lambda value: (value["bbox"][1], value["bbox"][0]))
    for index, region in enumerate(selected, 1):
        region["visual_region_id"] = f"visual-{index:03d}"
    return selected


def create_visual_transcription_sheet(
    paths: ProjectPaths,
    target_id: str,
    source_path: Path,
    ocr_report_path: Path | None = None,
) -> dict[str, Any]:
    regions: list[dict[str, Any]] = []
    if ocr_report_path is not None:
        report = json.loads(ocr_report_path.read_text(encoding="utf-8"))
        if report.get("image_sha256") != _sha256(source_path):
            raise ValueError(f"{target_id} OCR 보고서가 현재 원본과 달라요")
        if report.get("status") != "completed" or report.get("errors"):
            raise ValueError(f"{target_id} OCR 보고서가 오류 없이 완료된 상태가 아니에요")
        regions = _visual_regions(report.get("detections", []))

    with Image.open(source_path) as source_file:
        source = source_file.convert("RGBA")
    whole_image_fallback = not regions
    if whole_image_fallback:
        regions = [
            {
                "bbox": [0, 0, source.width, source.height],
                "rotation_deg": 0,
                "ocr_region_id": "",
                "confidence": 0.0,
                "visual_region_id": "visual-001",
            }
        ]
    review_dir = paths.reviews / target_id
    crop_dir = review_dir / "visual-crops-v2"
    crop_dir.mkdir(parents=True, exist_ok=True)
    cards: list[tuple[dict[str, Any], Image.Image]] = []
    for region in regions:
        x0, y0, x1, y1 = region["bbox"]
        margin = max(4, round(min(source.size) / 128))
        crop = source.crop(
            (
                max(0, x0 - margin),
                max(0, y0 - margin),
                min(source.width, x1 + margin),
                min(source.height, y1 + margin),
            )
        )
        rotation = int(region["rotation_deg"])
        if rotation:
            crop = crop.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
        scale = min(6.0, max(1.0, 320 / max(1, crop.width), 96 / max(1, crop.height)))
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.Resampling.NEAREST,
        )
        crop_path = crop_dir / f"{region['visual_region_id']}.png"
        crop.save(crop_path, format="PNG", optimize=False)
        region["crop"] = str(crop_path)
        region["crop_sha256"] = _sha256(crop_path)
        cards.append((region, crop))

    cell_width = max(360, max(crop.width for _, crop in cards) + 24)
    cell_height = max(140, max(crop.height for _, crop in cards) + 46)
    columns = 3
    rows = math.ceil(len(cards) / columns)
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "#202124")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, (region, crop) in enumerate(cards):
        column = index % columns
        row = index // columns
        left = column * cell_width
        top = row * cell_height
        label = (
            f"{region['visual_region_id']}  bbox={region['bbox']}  "
            f"rotation={region['rotation_deg']}"
        )
        draw.text((left + 8, top + 7), label, fill="white", font=font)
        sheet.paste(crop.convert("RGB"), (left + 8, top + 30))

    sheet_path = review_dir / "visual-source-crops.png"
    index_path = review_dir / "visual-source-index.json"
    sheet.save(sheet_path, format="PNG", optimize=False)
    index = {
        "schema_version": 1,
        "target_id": target_id,
        "source": str(source_path),
        "source_sha256": _sha256(source_path),
        "ocr_region_source": str(ocr_report_path) if ocr_report_path is not None else None,
        "ocr_region_source_sha256": (
            _sha256(ocr_report_path) if ocr_report_path is not None else None
        ),
        "ocr_text_hidden_from_sheet": True,
        "vision_first": ocr_report_path is None,
        "whole_image_fallback": whole_image_fallback,
        "requires_whole_image_visual_check_for_missed_regions": True,
        "regions": regions,
        "sheet": str(sheet_path),
        "sheet_sha256": _sha256(sheet_path),
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**index, "index": str(index_path), "index_sha256": _sha256(index_path)}
