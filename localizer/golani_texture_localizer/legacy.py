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


def _coverage(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / max(1, min(left_area, right_area))


def _korean_regions(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        detection
        for detection in detections
        if "korean" in str(detection.get("script", ""))
        and float(detection.get("confidence", 0)) >= 0.75
        and str(detection.get("text", "")).strip()
    ]
    candidates.sort(
        key=lambda value: (
            (value["bbox"][2] - value["bbox"][0])
            * (value["bbox"][3] - value["bbox"][1]),
            len(str(value["text"])),
            float(value["confidence"]),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    for detection in candidates:
        if any(
            _coverage(detection["bbox"], current["bbox"]) >= 0.8
            and str(detection["text"]) in str(current["text_ko_suggestion"])
            for current in selected
        ):
            continue
        selected.append(
            {
                "region_id": str(detection["region_id"]),
                "text_ko_suggestion": str(detection["text"]),
                "bbox": [int(value) for value in detection["bbox"]],
                "rotation_deg": int(detection.get("rotation_deg", 0)),
                "confidence": float(detection["confidence"]),
                "reference_only": True,
            }
        )
    selected.sort(key=lambda value: (value["bbox"][1], value["bbox"][0]))
    return selected


def create_legacy_layout_sheet(
    paths: ProjectPaths,
    target_id: str,
    source_path: Path,
    legacy_path: Path,
    ocr_report_path: Path,
) -> dict[str, Any]:
    report = json.loads(ocr_report_path.read_text(encoding="utf-8"))
    if report.get("status") != "completed" or report.get("errors"):
        raise ValueError(f"{target_id} 과거 승인본 참고 OCR이 완료되지 않았어요")
    if report.get("image_sha256") != _sha256(legacy_path):
        raise ValueError(f"{target_id} 과거 승인본 OCR 입력 SHA가 달라요")
    with Image.open(source_path) as source_file, Image.open(legacy_path) as legacy_file:
        source = source_file.convert("RGB")
        legacy = legacy_file.convert("RGB")
    if source.size != legacy.size:
        raise ValueError(f"{target_id} 과거 승인본 크기가 원본과 달라 배치 참고에 쓸 수 없어요")
    regions = _korean_regions(report.get("detections", []))
    if not regions:
        raise ValueError(f"{target_id} 과거 승인본에서 신뢰할 한글 배치 제안을 찾지 못했어요")

    review_dir = paths.reviews / target_id
    crop_dir = review_dir / "legacy-layout-crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    cards: list[tuple[dict[str, Any], Image.Image]] = []
    for index, region in enumerate(regions, 1):
        x0, y0, x1, y1 = region["bbox"]
        margin = max(4, round(min(source.size) / 128))
        crop_box = (
            max(0, x0 - margin),
            max(0, y0 - margin),
            min(source.width, x1 + margin),
            min(source.height, y1 + margin),
        )
        source_crop = source.crop(crop_box)
        legacy_crop = legacy.crop(crop_box)
        rotation = int(region["rotation_deg"])
        if rotation:
            source_crop = source_crop.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
            legacy_crop = legacy_crop.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
        scale = min(6.0, max(1.0, 280 / max(1, source_crop.width)))
        resized = (
            max(1, round(source_crop.width * scale)),
            max(1, round(source_crop.height * scale)),
        )
        source_crop = source_crop.resize(resized, Image.Resampling.NEAREST)
        legacy_crop = legacy_crop.resize(resized, Image.Resampling.NEAREST)
        card = Image.new("RGB", (resized[0] * 2 + 8, resized[1]), "#202124")
        card.paste(source_crop, (0, 0))
        card.paste(legacy_crop, (resized[0] + 8, 0))
        crop_path = crop_dir / f"layout-{index:03d}.png"
        card.save(crop_path, format="PNG", optimize=False)
        region["comparison_crop"] = str(crop_path)
        region["comparison_crop_sha256"] = _sha256(crop_path)
        cards.append((region, card))

    cell_width = max(640, max(card.width for _, card in cards) + 24)
    cell_height = max(150, max(card.height for _, card in cards) + 58)
    columns = 2
    rows = math.ceil(len(cards) / columns)
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "#161719")
    draw = ImageDraw.Draw(sheet)
    font_path = Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")
    font = ImageFont.truetype(str(font_path), 20) if font_path.is_file() else ImageFont.load_default()
    for index, (region, card) in enumerate(cards):
        left = (index % columns) * cell_width
        top = (index // columns) * cell_height
        draw.text(
            (left + 8, top + 7),
            f"{region['text_ko_suggestion']} · bbox={region['bbox']} · r={region['rotation_deg']}",
            fill="white",
            font=font,
        )
        draw.text((left + 8, top + 33), "원본  |  과거 한글 참고본", fill="#b7bcc4")
        sheet.paste(card, (left + 8, top + 54))

    sheet_path = review_dir / "legacy-layout-sheet.png"
    index_path = review_dir / "legacy-layout-index.json"
    sheet.save(sheet_path, format="PNG", optimize=False)
    payload = {
        "schema_version": 1,
        "target_id": target_id,
        "source": str(source_path),
        "source_sha256": _sha256(source_path),
        "legacy_reference": str(legacy_path),
        "legacy_reference_sha256": _sha256(legacy_path),
        "ocr_report": str(ocr_report_path),
        "ocr_report_sha256": _sha256(ocr_report_path),
        "warning": "과거 승인본은 최종 픽셀로 재사용하지 않고 번역·배치 제안으로만 검토해요",
        "regions": regions,
        "sheet": str(sheet_path),
        "sheet_sha256": _sha256(sheet_path),
    }
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**payload, "index": str(index_path), "index_sha256": _sha256(index_path)}
