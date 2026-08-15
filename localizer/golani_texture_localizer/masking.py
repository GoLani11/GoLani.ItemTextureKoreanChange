from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .paths import ProjectPaths
from .review import sha256_file


def _read_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} 경로가 비어 있어요")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _binary_mask(path: Path, size: tuple[int, int], label: str) -> np.ndarray:
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"} or image_file.size != size:
            raise ValueError(f"{label} 마스크 규격이 원본과 달라요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(np.unique(values).tolist()).issubset({0, 255}):
        raise ValueError(f"{label} 마스크는 0/255만 사용해야 해요")
    return values == 255


def _shape_mask(size: tuple[int, int], shape: dict[str, Any], index: int) -> np.ndarray:
    image = Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    kind = shape.get("kind")
    if kind == "rectangle":
        bbox = shape.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"regions[{index}].bbox가 [x0,y0,x1,y1]이 아니에요")
        x0, y0, x1, y1 = [int(value) for value in bbox]
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > size[0] or y1 > size[1]:
            raise ValueError(f"regions[{index}].bbox가 원본 밖이에요")
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    elif kind == "polygon":
        points = shape.get("points")
        if not isinstance(points, list) or len(points) < 3:
            raise ValueError(f"regions[{index}].points가 다각형이 아니에요")
        normalized = [(int(point[0]), int(point[1])) for point in points]
        if any(x < 0 or y < 0 or x >= size[0] or y >= size[1] for x, y in normalized):
            raise ValueError(f"regions[{index}].points가 원본 밖이에요")
        draw.polygon(normalized, fill=255)
    elif kind == "annulus_sector":
        center = shape.get("center")
        if not isinstance(center, list) or len(center) != 2:
            raise ValueError(f"regions[{index}].center가 [x,y]가 아니에요")
        center_x, center_y = float(center[0]), float(center[1])
        inner = float(shape.get("inner_radius", 0))
        outer = float(shape.get("outer_radius", 0))
        start = float(shape.get("start_angle_deg", 0)) % 360
        end = float(shape.get("end_angle_deg", 0)) % 360
        if inner < 0 or outer <= inner or outer > math.hypot(*size) or start == end:
            raise ValueError(f"regions[{index}] 원호 반지름/각도가 잘못됐어요")
        ys, xs = np.indices((size[1], size[0]), dtype=np.float32)
        distance = np.hypot(xs - center_x, ys - center_y)
        angle = np.degrees(np.arctan2(ys - center_y, xs - center_x)) % 360
        if start <= end:
            angle_selected = (angle >= start) & (angle <= end)
        else:
            angle_selected = (angle >= start) | (angle <= end)
        return (distance >= inner) & (distance <= outer) & angle_selected
    elif kind == "text":
        bbox = shape.get("bbox")
        text = shape.get("text")
        font_value = shape.get("font")
        font_size = int(shape.get("font_size", 0))
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"regions[{index}].bbox가 [x0,y0,x1,y1]이 아니에요")
        x0, y0, x1, y1 = [int(value) for value in bbox]
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > size[0] or y1 > size[1]:
            raise ValueError(f"regions[{index}].bbox가 원본 밖이에요")
        if not isinstance(text, str) or not text or not isinstance(font_value, str) or font_size < 1:
            raise ValueError(f"regions[{index}] text/font/font_size가 잘못됐어요")
        font_path = Path(font_value).expanduser().resolve()
        if not font_path.is_file():
            raise FileNotFoundError(font_path)
        if sha256_file(font_path) != shape.get("font_sha256"):
            raise ValueError(f"regions[{index}] 글꼴 SHA-256이 달라요")
        stroke_width = int(shape.get("stroke_width", 0))
        spacing = int(shape.get("spacing", 4))
        if stroke_width < 0 or spacing < 0:
            raise ValueError(f"regions[{index}] stroke_width/spacing이 잘못됐어요")
        font = ImageFont.truetype(str(font_path), font_size)
        center = ((x0 + x1) / 2, (y0 + y1) / 2)
        draw.multiline_text(
            center,
            text,
            font=font,
            fill=255,
            anchor="mm",
            align=str(shape.get("align", "center")),
            spacing=spacing,
            stroke_width=stroke_width,
            stroke_fill=255,
        )
        rotation = float(shape.get("rotation_deg", 0))
        if rotation:
            image = image.rotate(
                -rotation,
                resample=Image.Resampling.BICUBIC,
                expand=False,
                center=center,
            )
        values = np.asarray(image, dtype=np.uint8) > 0
        ys, xs = np.nonzero(values)
        if not len(xs):
            raise ValueError(f"regions[{index}] text 글리프가 렌더되지 않았어요")
        rendered_bbox = [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]
        if (
            rendered_bbox[0] < x0
            or rendered_bbox[1] < y0
            or rendered_bbox[2] > x1
            or rendered_bbox[3] > y1
        ):
            raise ValueError(
                f"regions[{index}] text 글리프 {rendered_bbox}가 승인 bbox {bbox}를 벗어났어요"
            )
        return values
    else:
        raise ValueError(
            f"regions[{index}].kind는 rectangle, polygon, annulus_sector 또는 text여야 해요"
        )
    return np.asarray(image, dtype=np.uint8) == 255


def _color_selection(rgb: np.ndarray, condition: Any, index: int) -> np.ndarray:
    if condition is None:
        return np.ones(rgb.shape[:2], dtype=bool)
    if not isinstance(condition, dict):
        raise ValueError(f"regions[{index}].condition이 객체가 아니에요")
    selected = np.ones(rgb.shape[:2], dtype=bool)
    if "rgb_targets" in condition:
        targets = condition["rgb_targets"]
        tolerance = float(condition.get("rgb_tolerance", 0))
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(value, list) or len(value) != 3 for value in targets)
            or tolerance < 0
        ):
            raise ValueError(f"regions[{index}] RGB 조건이 잘못됐어요")
        values = rgb.astype(np.float32)
        distances = [
            np.linalg.norm(values - np.asarray(target, dtype=np.float32), axis=2)
            for target in targets
        ]
        selected &= np.minimum.reduce(distances) <= tolerance
    if "rgb_reference" in condition:
        reference = condition["rgb_reference"]
        minimum = condition.get("rgb_distance_min")
        maximum = condition.get("rgb_distance_max")
        if (
            not isinstance(reference, list)
            or len(reference) != 3
            or any(not isinstance(value, (int, float)) for value in reference)
            or any(float(value) < 0 or float(value) > 255 for value in reference)
            or (minimum is None and maximum is None)
            or (minimum is not None and float(minimum) < 0)
            or (maximum is not None and float(maximum) < 0)
        ):
            raise ValueError(f"regions[{index}] RGB 기준 거리 조건이 잘못됐어요")
        distance = np.linalg.norm(
            rgb.astype(np.float32) - np.asarray(reference, dtype=np.float32),
            axis=2,
        )
        if minimum is not None:
            selected &= distance >= float(minimum)
        if maximum is not None:
            selected &= distance <= float(maximum)
    luminance = (
        rgb[..., 0].astype(np.float32) * 0.2126
        + rgb[..., 1].astype(np.float32) * 0.7152
        + rgb[..., 2].astype(np.float32) * 0.0722
    )
    if "luminance_min" in condition:
        selected &= luminance >= float(condition["luminance_min"])
    if "luminance_max" in condition:
        selected &= luminance <= float(condition["luminance_max"])
    if not any(
        key in condition
        for key in ("rgb_targets", "rgb_reference", "luminance_min", "luminance_max")
    ):
        raise ValueError(f"regions[{index}]에 지원하는 색 조건이 없어요")
    return selected


def create_old_text_mask(
    paths: ProjectPaths,
    target_id: str,
    recipe_path: Path,
) -> dict[str, Any]:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if recipe.get("schema_version") != 1 or recipe.get("target_id") != target_id:
        raise ValueError("마스크 recipe schema/target이 요청과 달라요")
    source_path = _read_path(recipe.get("source"), "원본")
    seam_path = _read_path(recipe.get("seam_guard_mask"), "seam guard")
    for path, expected, label in (
        (source_path, recipe.get("source_sha256"), "원본"),
        (seam_path, recipe.get("seam_guard_mask_sha256"), "seam guard"),
    ):
        if sha256_file(path) != expected:
            raise ValueError(f"{label} SHA-256이 recipe와 달라요")
    with Image.open(source_path) as source_file:
        if source_file.mode != "RGBA":
            raise ValueError("마스크 생성 원본은 RGBA여야 해요")
        size = source_file.size
        rgb = np.asarray(source_file.convert("RGB"), dtype=np.uint8)
    seam = _binary_mask(seam_path, size, "seam guard")
    regions = recipe.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("regions가 비어 있어요")
    combined = np.zeros((size[1], size[0]), dtype=bool)
    region_reports = []
    selection_sources: dict[Path, np.ndarray] = {source_path: rgb}
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            raise ValueError(f"regions[{index}]가 객체가 아니에요")
        shape = _shape_mask(size, region, index)
        selection_value = region.get("selection_source")
        selection_path = source_path
        if selection_value is not None:
            selection_path = _read_path(selection_value, f"regions[{index}] 선택 원본")
            if sha256_file(selection_path) != region.get("selection_source_sha256"):
                raise ValueError(f"regions[{index}] 선택 원본 SHA-256이 달라요")
            if selection_path not in selection_sources:
                with Image.open(selection_path) as selection_file:
                    if selection_file.size != size:
                        raise ValueError(f"regions[{index}] 선택 원본 규격이 원본과 달라요")
                    selection_sources[selection_path] = np.asarray(
                        selection_file.convert("RGB"), dtype=np.uint8
                    )
        selected = shape & _color_selection(
            selection_sources[selection_path], region.get("condition"), index
        )
        dilation = int(region.get("dilate", 0))
        if dilation < 0:
            raise ValueError(f"regions[{index}].dilate는 0 이상이어야 해요")
        if dilation:
            selected = np.asarray(
                Image.fromarray(selected.astype(np.uint8) * 255, "L").filter(
                    ImageFilter.MaxFilter(dilation * 2 + 1)
                ),
                dtype=np.uint8,
            ) == 255
            selected &= shape
        exclude_seam_guard = region.get("exclude_seam_guard", False)
        if not isinstance(exclude_seam_guard, bool):
            raise ValueError(f"regions[{index}].exclude_seam_guard가 bool이 아니에요")
        if exclude_seam_guard:
            selected &= ~seam
        if not selected.any():
            raise ValueError(f"regions[{index}] 색 조건에 선택된 픽셀이 없어요")
        if np.any(selected & seam):
            raise ValueError(f"regions[{index}] 원문 마스크가 UV seam guard와 겹쳐요")
        combined |= selected
        region_reports.append(
            {
                "region_id": str(region.get("region_id", f"mask-{index + 1:03d}")),
                "selected_pixels": int(selected.sum()),
                "excluded_seam_guard": exclude_seam_guard,
                "selection_source": str(selection_path),
                "selection_source_sha256": sha256_file(selection_path),
            }
        )
    output_stem = recipe.get("output_stem", "old-text-mask")
    if not isinstance(output_stem, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", output_stem):
        raise ValueError("output_stem은 영문 소문자·숫자·하이픈만 사용해야 해요")
    output = paths.reviews / target_id / f"{output_stem}.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(combined.astype(np.uint8) * 255, "L").save(
        output, format="PNG", optimize=False
    )
    report = {
        "schema_version": 1,
        "target_id": target_id,
        "source": str(source_path),
        "source_sha256": sha256_file(source_path),
        "seam_guard_mask": str(seam_path),
        "seam_guard_mask_sha256": sha256_file(seam_path),
        "old_text_mask": str(output),
        "old_text_mask_sha256": sha256_file(output),
        "width": size[0],
        "height": size[1],
        "selected_pixels": int(combined.sum()),
        "changed_inside_seam_guard": int((combined & seam).sum()),
        "regions": region_reports,
        "recipe": str(recipe_path),
        "recipe_sha256": sha256_file(recipe_path),
        "passed": True,
    }
    report_path = output.with_name(f"{output_stem}-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report": str(report_path), "report_sha256": sha256_file(report_path)}
