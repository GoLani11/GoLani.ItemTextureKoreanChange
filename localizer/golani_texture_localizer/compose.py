from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import PIL
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .paths import ProjectPaths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_mask(path: Path, size: tuple[int, int], label: str) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(path)
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"} or image_file.size != size:
            raise ValueError(f"{label} 마스크 규격이 원본과 달라요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(np.unique(values).tolist()).issubset({0, 255}):
        raise ValueError(f"{label} 마스크는 0/255만 사용해야 해요")
    return values == 255


def _color(value: Any, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) not in {3, 4}:
        raise ValueError(f"{label}은 RGB 또는 RGBA 배열이어야 해요")
    channels = [int(channel) for channel in value]
    if any(channel < 0 or channel > 255 for channel in channels):
        raise ValueError(f"{label} 채널은 0~255여야 해요")
    if len(channels) == 3:
        channels.append(255)
    return tuple(channels)  # type: ignore[return-value]


def _render_text_layer(
    size: tuple[int, int],
    font_path: Path,
    regions: list[dict[str, Any]],
) -> tuple[Image.Image, list[dict[str, Any]]]:
    combined = Image.new("RGBA", size, (0, 0, 0, 0))
    glyph_runs: list[dict[str, Any]] = []
    for index, region in enumerate(regions):
        text = str(region.get("text", ""))
        box = region.get("bbox")
        if not text or not isinstance(box, list) or len(box) != 4:
            raise ValueError(f"text_regions[{index}]의 text/bbox가 잘못됐어요")
        x0, y0, x1, y1 = [int(value) for value in box]
        if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > size[0] or y1 > size[1]:
            raise ValueError(f"text_regions[{index}] bbox가 원본 밖이에요")
        font_size = int(region.get("font_size", 0))
        if font_size < 1:
            raise ValueError(f"text_regions[{index}] font_size가 잘못됐어요")
        font = ImageFont.truetype(str(font_path), font_size)
        fill = _color(region.get("fill"), f"text_regions[{index}].fill")
        stroke_width = int(region.get("stroke_width", 0))
        stroke_fill = _color(
            region.get("stroke_fill", region.get("fill")),
            f"text_regions[{index}].stroke_fill",
        )
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        center = ((x0 + x1) / 2, (y0 + y1) / 2)
        anchor = str(region.get("anchor", "mm"))
        if anchor not in {"mm", "lm", "rm"}:
            raise ValueError(f"text_regions[{index}].anchor는 mm, lm 또는 rm이어야 해요")
        offset = region.get("offset", [0, 0])
        if (
            not isinstance(offset, list)
            or len(offset) != 2
            or not all(isinstance(value, (int, float)) for value in offset)
        ):
            raise ValueError(f"text_regions[{index}].offset은 [x, y] 숫자 배열이어야 해요")
        base_position = (
            (float(x0), center[1])
            if anchor == "lm"
            else (float(x1), center[1])
            if anchor == "rm"
            else center
        )
        draw_position = (
            base_position[0] + float(offset[0]),
            base_position[1] + float(offset[1]),
        )
        spacing = int(region.get("spacing", 4))
        tracking = float(region.get("tracking", 0))
        if tracking < 0:
            raise ValueError(f"text_regions[{index}].tracking은 0 이상이어야 해요")
        segments_value = region.get("segments")
        segments: list[dict[str, Any]] | None = None
        if segments_value is not None:
            if not isinstance(segments_value, list) or not segments_value:
                raise ValueError(f"text_regions[{index}].segments는 비어 있지 않은 배열이어야 해요")
            segments = []
            for segment_index, segment in enumerate(segments_value):
                if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                    raise ValueError(
                        f"text_regions[{index}].segments[{segment_index}]가 잘못됐어요"
                    )
                segment_fill = (
                    _color(
                        segment["fill"],
                        f"text_regions[{index}].segments[{segment_index}].fill",
                    )
                    if "fill" in segment
                    else fill
                )
                segment_stroke = (
                    _color(
                        segment["stroke_fill"],
                        f"text_regions[{index}].segments[{segment_index}].stroke_fill",
                    )
                    if "stroke_fill" in segment
                    else stroke_fill
                )
                segments.append(
                    {
                        "text": segment["text"],
                        "fill": segment_fill,
                        "stroke_fill": segment_stroke,
                    }
                )
            if "".join(segment["text"] for segment in segments) != text:
                raise ValueError(f"text_regions[{index}].segments 문구 합이 text와 달라요")
        arc = region.get("arc")
        rotation = float(region.get("rotation_deg", 0))
        if arc is not None:
            if segments is not None:
                raise ValueError(f"text_regions[{index}] arc와 segments를 함께 쓸 수 없어요")
            if not isinstance(arc, dict):
                raise ValueError(f"text_regions[{index}].arc가 객체가 아니에요")
            arc_center = arc.get("center")
            if not isinstance(arc_center, list) or len(arc_center) != 2:
                raise ValueError(f"text_regions[{index}].arc.center가 [x, y]가 아니에요")
            radius = float(arc.get("radius", 0))
            start_angle = float(arc.get("start_angle_deg", 0))
            end_angle = float(arc.get("end_angle_deg", 0))
            if radius <= 0 or start_angle == end_angle:
                raise ValueError(f"text_regions[{index}] arc 반지름/각도 범위가 잘못됐어요")
            direction = 1.0 if end_angle > start_angle else -1.0
            advances = [max(1.0, float(font.getlength(character))) for character in text]
            total_advance = sum(advances)
            cursor = 0.0
            for character, advance in zip(text, advances):
                fraction = (cursor + advance / 2) / total_advance
                angle = start_angle + (end_angle - start_angle) * fraction
                cursor += advance
                if character.isspace():
                    continue
                glyph_bbox = font.getbbox(
                    character,
                    stroke_width=stroke_width,
                    anchor="mm",
                )
                glyph_width = max(1, glyph_bbox[2] - glyph_bbox[0] + stroke_width * 4 + 8)
                glyph_height = max(1, glyph_bbox[3] - glyph_bbox[1] + stroke_width * 4 + 8)
                glyph = Image.new("RGBA", (glyph_width, glyph_height), (0, 0, 0, 0))
                glyph_draw = ImageDraw.Draw(glyph)
                glyph_draw.text(
                    (glyph_width / 2, glyph_height / 2),
                    character,
                    font=font,
                    fill=fill,
                    anchor="mm",
                    stroke_width=stroke_width,
                    stroke_fill=stroke_fill,
                )
                tangent_clockwise = angle + direction * 90
                glyph = glyph.rotate(
                    -tangent_clockwise,
                    resample=Image.Resampling.BICUBIC,
                    expand=True,
                )
                radians = math.radians(angle)
                glyph_center = (
                    float(arc_center[0]) + math.cos(radians) * radius,
                    float(arc_center[1]) + math.sin(radians) * radius,
                )
                layer.alpha_composite(
                    glyph,
                    (
                        round(glyph_center[0] - glyph.width / 2),
                        round(glyph_center[1] - glyph.height / 2),
                    ),
                )
        else:
            draw = ImageDraw.Draw(layer)
            if tracking or segments is not None:
                if anchor != "mm":
                    raise ValueError(
                        f"text_regions[{index}] tracking/segments는 mm anchor만 지원해요"
                    )
                if "\n" in text:
                    raise ValueError(
                        f"text_regions[{index}] tracking/segments는 한 줄 문구에만 사용할 수 있어요"
                    )
                advances = [max(0.0, float(font.getlength(character))) for character in text]
                total_width = sum(advances) + tracking * max(0, len(text) - 1)
                cursor = center[0] - total_width / 2
                styles: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]] = []
                if segments is None:
                    styles = [(fill, stroke_fill)] * len(text)
                else:
                    for segment in segments:
                        styles.extend(
                            [(segment["fill"], segment["stroke_fill"])]
                            * len(segment["text"])
                        )
                for character, advance, (character_fill, character_stroke) in zip(
                    text, advances, styles, strict=True
                ):
                    if not character.isspace():
                        draw.text(
                            (cursor + advance / 2, center[1]),
                            character,
                            font=font,
                            fill=character_fill,
                            anchor="mm",
                            stroke_width=stroke_width,
                            stroke_fill=character_stroke,
                        )
                    cursor += advance + tracking
            else:
                draw.multiline_text(
                    draw_position,
                    text,
                    font=font,
                    fill=fill,
                    anchor=anchor,
                    align=str(region.get("align", "center")),
                    spacing=spacing,
                    stroke_width=stroke_width,
                    stroke_fill=stroke_fill,
                )
            if rotation:
                layer = layer.rotate(
                    -rotation,
                    resample=Image.Resampling.BICUBIC,
                    expand=False,
                    center=center,
                )
        alpha = np.asarray(layer.getchannel("A"), dtype=np.uint8) > 0
        ys, xs = np.nonzero(alpha)
        if not len(xs):
            raise ValueError(f"text_regions[{index}]에서 글리프가 렌더되지 않았어요")
        rendered_bbox = [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)]
        if (
            rendered_bbox[0] < x0
            or rendered_bbox[1] < y0
            or rendered_bbox[2] > x1
            or rendered_bbox[3] > y1
        ):
            raise ValueError(
                f"text_regions[{index}] 글리프 {rendered_bbox}가 승인 bbox {box}를 벗어났어요"
            )
        combined.alpha_composite(layer)
        glyph_runs.append(
            {
                "region_id": str(region.get("region_id", f"text-{index + 1:03d}")),
                "text": text,
                "bbox": [x0, y0, x1, y1],
                "rendered_bbox": rendered_bbox,
                "font_size": font_size,
                "fill": list(fill),
                "stroke_width": stroke_width,
                "stroke_fill": list(stroke_fill),
                "rotation_deg": rotation,
                "anchor": anchor,
                "offset": [float(offset[0]), float(offset[1])],
                "align": str(region.get("align", "center")),
                "spacing": spacing,
                "tracking": tracking,
                "segments": segments,
                "arc": arc,
            }
        )
    return combined, glyph_runs


def _compose_legacy_font_candidate(
    paths: ProjectPaths,
    target_id: str,
    recipe_path: Path,
) -> dict[str, Any]:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if recipe.get("schema_version") != 1 or recipe.get("target_id") != target_id:
        raise ValueError("조판 recipe schema/target이 요청과 달라요")
    source_path = Path(str(recipe.get("source", "")))
    font_path = Path(str(recipe.get("font", "")))
    old_text_path = Path(str(recipe.get("old_text_mask", "")))
    seam_guard_path = Path(str(recipe.get("seam_guard_mask", "")))
    restoration_value = recipe.get("restoration_patch")
    restoration_path = Path(str(restoration_value)) if restoration_value else None
    restoration_layers = recipe.get("restoration_layers", [])
    if restoration_path is not None and restoration_layers:
        raise ValueError("restoration_patch와 restoration_layers는 동시에 사용할 수 없어요")
    if not isinstance(restoration_layers, list):
        raise ValueError("restoration_layers는 배열이어야 해요")
    required_files = [
        (source_path, recipe.get("source_sha256"), "원본"),
        (font_path, recipe.get("font_sha256"), "글꼴"),
        (old_text_path, recipe.get("old_text_mask_sha256"), "old_text 마스크"),
        (seam_guard_path, recipe.get("seam_guard_mask_sha256"), "seam_guard 마스크"),
    ]
    if restoration_path is not None:
        required_files.append(
            (restoration_path, recipe.get("restoration_patch_sha256"), "배경 복구 초안")
        )
    layer_files: list[tuple[Path, Path, dict[str, Any]]] = []
    for index, layer in enumerate(restoration_layers):
        if not isinstance(layer, dict):
            raise ValueError(f"restoration_layers[{index}]가 객체가 아니에요")
        patch_path = Path(str(layer.get("patch", "")))
        mask_path = Path(str(layer.get("mask", "")))
        required_files.extend(
            [
                (patch_path, layer.get("patch_sha256"), f"배경 복구 초안 {index}"),
                (mask_path, layer.get("mask_sha256"), f"배경 복구 마스크 {index}"),
            ]
        )
        layer_files.append((patch_path, mask_path, layer))
    for path, expected, label in required_files:
        if not path.is_file():
            raise FileNotFoundError(path)
        if _sha256(path) != expected:
            raise ValueError(f"{label} SHA-256이 recipe와 달라요")

    with Image.open(source_path) as source_file:
        source_mode = source_file.mode
        source_image = source_file.convert("RGBA")
    if source_mode != "RGBA":
        raise ValueError("결정적 조판은 RGBA 원본만 지원해요")
    size = source_image.size
    old_text = _binary_mask(old_text_path, size, "old_text")
    seam_guard = _binary_mask(seam_guard_path, size, "seam_guard")
    if not old_text.any():
        raise ValueError("old_text 마스크가 비어 있어요")
    if np.any(old_text & seam_guard):
        raise ValueError("old_text가 UV seam guard와 겹쳐 안전하게 지울 수 없어요")

    source = np.asarray(source_image, dtype=np.uint8)
    restored = source.copy()
    if restoration_path is not None:
        with Image.open(restoration_path) as patch_file:
            if patch_file.size != size:
                raise ValueError("배경 복구 초안 크기가 원본과 달라요")
            patch = np.asarray(patch_file.convert("RGBA"), dtype=np.uint8)
        restored[..., :3][old_text] = patch[..., :3][old_text]
        restoration = {
            "mode": "hash-pinned-patch-inside-old-text-only",
            "path": str(restoration_path),
            "sha256": _sha256(restoration_path),
        }
    else:
        radius = int(recipe.get("inpaint_radius", 3))
        if radius < 1:
            raise ValueError("inpaint_radius는 1 이상이어야 해요")
        hard_mask = old_text.astype(np.uint8) * 255
        for channel in range(3):
            inpainted = cv2.inpaint(source[..., channel], hard_mask, radius, cv2.INPAINT_TELEA)
            restored[..., channel][old_text] = inpainted[old_text]
        applied_layers = []
        for index, (patch_path, mask_path, layer) in enumerate(layer_files):
            layer_mask = _binary_mask(mask_path, size, f"restoration_layers[{index}]")
            if not layer_mask.any() or np.any(layer_mask & ~old_text):
                raise ValueError(
                    f"restoration_layers[{index}] 마스크는 비어 있지 않은 old_text 부분집합이어야 해요"
                )
            with Image.open(patch_path) as patch_file:
                if patch_file.size != size:
                    raise ValueError(f"restoration_layers[{index}] 초안 크기가 원본과 달라요")
                patch = np.asarray(patch_file.convert("RGBA"), dtype=np.uint8)
            restored[..., :3][layer_mask] = patch[..., :3][layer_mask]
            applied_layers.append(
                {
                    "region_id": str(layer.get("region_id", f"restoration-{index + 1:03d}")),
                    "patch": str(patch_path),
                    "patch_sha256": _sha256(patch_path),
                    "mask": str(mask_path),
                    "mask_sha256": _sha256(mask_path),
                }
            )
        restoration = {
            "mode": "telea-fallback+hash-pinned-regional-patches",
            "radius": radius,
            "layers": applied_layers,
        }

    text_regions = recipe.get("text_regions")
    if not isinstance(text_regions, list) or not text_regions:
        raise ValueError("text_regions가 비어 있어요")
    text_layer, glyph_runs = _render_text_layer(size, font_path, text_regions)
    layer = np.asarray(text_layer, dtype=np.uint8)
    new_text = layer[..., 3] > 0
    if np.any(new_text & seam_guard):
        raise ValueError("새 한글 글리프가 UV seam guard와 겹쳐요")
    editable_margin = int(recipe.get("editable_margin", 1))
    if editable_margin < 0:
        raise ValueError("editable_margin은 0 이상이어야 해요")
    editable_image = Image.fromarray(old_text.astype(np.uint8) * 255, "L")
    if editable_margin:
        editable_image = editable_image.filter(ImageFilter.MaxFilter(editable_margin * 2 + 1))
    editable = (np.asarray(editable_image, dtype=np.uint8) > 0) | new_text
    if np.any(editable & seam_guard):
        raise ValueError("editable 영역이 UV seam guard와 겹쳐요")
    protected = ~editable

    alpha = layer[..., 3:4].astype(np.float32) / 255.0
    candidate = restored.copy()
    candidate[..., :3] = np.clip(
        np.round(layer[..., :3].astype(np.float32) * alpha + restored[..., :3] * (1 - alpha)),
        0,
        255,
    ).astype(np.uint8)
    candidate[..., 3] = source[..., 3]
    changed = np.any(candidate[..., :3] != source[..., :3], axis=2)
    if not changed.any() or np.any(changed & ~editable):
        raise AssertionError("조판 결과 변경 영역이 editable과 맞지 않아요")

    output_dir = paths.drafts / target_id
    mask_dir = paths.reviews / target_id / "masks"
    output_dir.mkdir(parents=True, exist_ok=True)
    mask_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / "candidate.png"
    glyph_path = output_dir / "glyph-run.json"
    Image.fromarray(candidate, "RGBA").save(candidate_path, format="PNG", optimize=False)
    masks = {
        "old_text": old_text,
        "new_text": new_text,
        "editable": editable,
        "protected": protected,
        "seam_guard": seam_guard,
    }
    mask_reports: dict[str, dict[str, Any]] = {}
    for name, values in masks.items():
        path = mask_dir / f"{name}.png"
        Image.fromarray(values.astype(np.uint8) * 255, "L").save(path, format="PNG", optimize=False)
        mask_reports[name] = {
            "path": path.relative_to(paths.root).as_posix(),
            "sha256": _sha256(path),
            "width": size[0],
            "height": size[1],
        }
    glyph_payload = {
        "schema_version": 1,
        "target_id": target_id,
        "source_sha256": _sha256(source_path),
        "font": str(font_path),
        "font_sha256": _sha256(font_path),
        "shaping_engine": "Pillow-FreeType-basic",
        "shaping_version": PIL.__version__,
        "glyph_runs": glyph_runs,
    }
    glyph_path.write_text(
        json.dumps(glyph_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "target_id": target_id,
        "candidate": str(candidate_path),
        "candidate_sha256": _sha256(candidate_path),
        "source_sha256": _sha256(source_path),
        "width": size[0],
        "height": size[1],
        "color_mode": source_mode,
        "alpha_equal": bool(np.array_equal(candidate[..., 3], source[..., 3])),
        "changed_pixels": int(changed.sum()),
        "changed_outside_editable": int((changed & ~editable).sum()),
        "changed_inside_protected": int((changed & protected).sum()),
        "changed_inside_seam_guard": int((changed & seam_guard).sum()),
        "masks": mask_reports,
        "compositor": {
            "mode": "deterministic-local-mask-inpaint-and-font",
            "fixed_font_used": True,
            "single_pass_panels": False,
            "shaping_engine": "Pillow-FreeType-basic",
            "shaping_version": PIL.__version__,
            "font_sha256": _sha256(font_path),
            "glyph_run_sha256": _sha256(glyph_path),
            "restoration": restoration,
        },
        "recipe": str(recipe_path),
        "recipe_sha256": _sha256(recipe_path),
        "candidate_gate_eligible": False,
        "passed": True,
    }
    report_path = output_dir / "compose-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report": str(report_path), "report_sha256": _sha256(report_path)}


def compose_candidate(
    paths: ProjectPaths,
    target_id: str,
    recipe_path: Path,
) -> dict[str, Any]:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if recipe.get("target_id") != target_id:
        raise ValueError("조판 recipe target이 요청과 달라요")
    if recipe.get("schema_version") == 2:
        from .vision_compose import compose_vision_candidate

        return compose_vision_candidate(paths, target_id, recipe_path, recipe)
    if recipe.get("schema_version") == 1:
        return _compose_legacy_font_candidate(paths, target_id, recipe_path)
    raise ValueError("지원하지 않는 조판 recipe schema예요")
