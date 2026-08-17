from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import cv2
import numpy as np
from PIL import Image

from .paths import ProjectPaths


_DIRECTIONS = {
    "left-to-right",
    "right-to-left",
    "top-to-bottom",
    "bottom-to-top",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_file(paths: ProjectPaths, descriptor: Any, label: str) -> tuple[Path, dict[str, str]]:
    if not isinstance(descriptor, Mapping):
        raise ValueError(f"{label} 파일 명세가 없어요")
    value = descriptor.get("path")
    expected = descriptor.get("sha256")
    if not isinstance(value, str) or not value or not isinstance(expected, str):
        raise ValueError(f"{label} path/SHA-256 명세가 잘못됐어요")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} 경로는 프로젝트 상대 경로여야 해요")
    path = (paths.root / relative).resolve()
    try:
        path.relative_to(paths.root)
    except ValueError as exc:
        raise ValueError(f"{label} 경로가 프로젝트 밖을 가리켜요") from exc
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(f"{label} SHA-256이 recipe와 달라요")
    return path, {"path": relative.as_posix(), "sha256": actual}


def _rgba_artifact(
    paths: ProjectPaths,
    descriptor: Any,
    label: str,
    *,
    size: tuple[int, int] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    path, report = _project_file(paths, descriptor, label)
    with Image.open(path) as image_file:
        if image_file.mode != "RGBA":
            raise ValueError(f"{label}은 RGBA PNG여야 해요")
        if size is not None and image_file.size != size:
            raise ValueError(f"{label} 크기가 원본과 달라요")
        values = np.asarray(image_file, dtype=np.uint8)
        width, height = image_file.size
    return values, {**report, "width": width, "height": height, "color_mode": "RGBA"}


def _image_artifact(
    paths: ProjectPaths,
    descriptor: Any,
    label: str,
) -> dict[str, Any]:
    path, report = _project_file(paths, descriptor, label)
    try:
        with Image.open(path) as image_file:
            if image_file.format != "PNG" or image_file.mode not in {"RGB", "RGBA"}:
                raise ValueError(f"{label}은 lossless RGB/RGBA PNG여야 해요")
            width, height = image_file.size
            mode = image_file.mode
    except OSError as exc:
        raise ValueError(f"{label}을 이미지로 열 수 없어요") from exc
    return {**report, "width": width, "height": height, "color_mode": mode}


def _panel_ocr_artifact(
    paths: ProjectPaths,
    descriptor: Any,
    label: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    path, report = _project_file(paths, descriptor, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}은 유효한 JSON이어야 해요") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}은 JSON 객체여야 해요")
    if value.get("recognition_contract") != "approved-regions+nfc-literal-v1":
        raise ValueError(f"{label}의 OCR 완전일치 계약이 현재 기준과 달라요")
    return value, report


def _mask_artifact(
    paths: ProjectPaths,
    descriptor: Any,
    label: str,
    size: tuple[int, int],
) -> tuple[np.ndarray, dict[str, Any]]:
    path, report = _project_file(paths, descriptor, label)
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"} or image_file.size != size:
            raise ValueError(f"{label}은 원본 크기의 단일 채널 마스크여야 해요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(np.unique(values).tolist()).issubset({0, 255}):
        raise ValueError(f"{label} 마스크는 0/255만 사용해야 해요")
    return values == 255, {
        **report,
        "width": size[0],
        "height": size[1],
        "color_mode": "L",
    }


def _save_rgba(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(values, "RGBA").save(path, format="PNG", optimize=False)


def _relative_report(paths: ProjectPaths, path: Path, **extra: Any) -> dict[str, Any]:
    return {
        "path": path.relative_to(paths.root).as_posix(),
        "sha256": _sha256(path),
        **extra,
    }


def _background_fingerprint(
    source_sha256: str,
    old_text_sha256: str,
    background: Mapping[str, Any],
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "old_text_sha256": old_text_sha256,
        "background": background,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_cached_background(
    path: Path,
    report_path: Path,
    fingerprint: str,
    size: tuple[int, int],
) -> np.ndarray | None:
    if not path.is_file() or not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            report.get("fingerprint") != fingerprint
            or report.get("sha256") != _sha256(path)
        ):
            return None
        with Image.open(path) as image_file:
            if image_file.mode != "RGBA" or image_file.size != size:
                return None
            return np.asarray(image_file, dtype=np.uint8)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _restore_background(
    paths: ProjectPaths,
    source: np.ndarray,
    source_sha256: str,
    old_text: np.ndarray,
    old_text_report: Mapping[str, Any],
    background: Any,
    output_dir: Path,
) -> tuple[np.ndarray, dict[str, Any]]:
    if not isinstance(background, Mapping):
        raise ValueError("background 명세가 없어요")
    method = background.get("method")
    if method not in {"telea", "hash-pinned-patch"}:
        raise ValueError("background.method는 telea 또는 hash-pinned-patch여야 해요")

    size = (source.shape[1], source.shape[0])
    normalized: dict[str, Any] = {"method": method}
    full_patch: np.ndarray | None = None
    if method == "telea":
        radius = int(background.get("inpaint_radius", 3))
        if radius < 1:
            raise ValueError("background.inpaint_radius는 1 이상이어야 해요")
        normalized["inpaint_radius"] = radius
    else:
        full_patch, patch_report = _rgba_artifact(
            paths,
            background.get("patch"),
            "background.patch",
            size=size,
        )
        normalized["patch"] = patch_report

    patches_value = background.get("patches", [])
    if not isinstance(patches_value, list):
        raise ValueError("background.patches는 배열이어야 해요")
    regional_patches: list[tuple[np.ndarray, np.ndarray, dict[str, Any]]] = []
    normalized_patches: list[dict[str, Any]] = []
    for index, value in enumerate(patches_value):
        if not isinstance(value, Mapping):
            raise ValueError(f"background.patches[{index}]가 객체가 아니에요")
        patch, patch_report = _rgba_artifact(
            paths,
            value.get("patch"),
            f"background.patches[{index}].patch",
            size=size,
        )
        mask, mask_report = _mask_artifact(
            paths,
            value.get("mask"),
            f"background.patches[{index}].mask",
            size,
        )
        if not mask.any() or np.any(mask & ~old_text):
            raise ValueError(
                f"background.patches[{index}].mask는 비어 있지 않은 old_text 부분집합이어야 해요"
            )
        signature = value.get("generator_signature")
        if not isinstance(signature, str) or not signature:
            raise ValueError(
                f"background.patches[{index}].generator_signature가 비어 있어요"
            )
        record = {
            "region_id": str(value.get("region_id", f"background-{index + 1:03d}")),
            "generator_signature": signature,
            "patch": patch_report,
            "mask": mask_report,
        }
        regional_patches.append((patch, mask, record))
        normalized_patches.append(record)
    normalized["patches"] = normalized_patches

    fingerprint = _background_fingerprint(
        source_sha256,
        str(old_text_report["sha256"]),
        normalized,
    )
    cache_path = output_dir / "clean-background.png"
    cache_report_path = output_dir / "clean-background-report.json"
    cached = _load_cached_background(cache_path, cache_report_path, fingerprint, size)
    if cached is not None:
        return cached, {
            **normalized,
            "fingerprint": fingerprint,
            "cache_reused": True,
            "clean_background": _relative_report(
                paths,
                cache_path,
                width=size[0],
                height=size[1],
                color_mode="RGBA",
            ),
            "cache_report": _relative_report(paths, cache_report_path),
        }

    restored = source.copy()
    if method == "telea":
        hard_mask = old_text.astype(np.uint8) * 255
        radius = int(normalized["inpaint_radius"])
        for channel in range(3):
            inpainted = cv2.inpaint(source[..., channel], hard_mask, radius, cv2.INPAINT_TELEA)
            restored[..., channel][old_text] = inpainted[old_text]
    else:
        assert full_patch is not None
        restored[..., :3][old_text] = full_patch[..., :3][old_text]

    for patch, mask, _ in regional_patches:
        restored[..., :3][mask] = patch[..., :3][mask]
    restored[..., 3] = source[..., 3]
    _save_rgba(cache_path, restored)
    cache_report = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "sha256": _sha256(cache_path),
        "source_sha256": source_sha256,
        "old_text_sha256": old_text_report["sha256"],
        "background": normalized,
    }
    cache_report_path.write_text(
        json.dumps(cache_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return restored, {
        **normalized,
        "fingerprint": fingerprint,
        "cache_reused": False,
        "clean_background": _relative_report(
            paths,
            cache_path,
            width=size[0],
            height=size[1],
            color_mode="RGBA",
        ),
        "cache_report": _relative_report(paths, cache_report_path),
    }


def _bbox(value: Any, size: tuple[int, int], label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        raise ValueError(f"{label}은 정수 bbox여야 해요")
    x0, y0, x1, y1 = value
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > size[0] or y1 > size[1]:
        raise ValueError(f"{label}이 원본 밖이에요")
    return list(value)


def _panel_transform(
    value: Any,
    rotation_deg: float,
    bbox: list[int],
    size: tuple[int, int],
    label: str,
) -> dict[str, Any]:
    if value is None and math.isclose(rotation_deg % 360.0, 0.0, abs_tol=1e-6):
        return {
            "coordinate_space": "source-mip0",
            "crop_bbox": bbox,
            "padding_px": 0,
            "source_rotation_deg": rotation_deg,
            "deskew_rotation_deg": 0.0,
            "inverse_rotation_deg": 0.0,
            "selected_lettering_restored_to_source": True,
            "source_texture_resampled": False,
            "final_texture_resampled": False,
        }
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}이 없어요")
    crop_bbox = _bbox(value.get("crop_bbox"), size, f"{label}.crop_bbox")
    if not (
        crop_bbox[0] <= bbox[0]
        and crop_bbox[1] <= bbox[1]
        and crop_bbox[2] >= bbox[2]
        and crop_bbox[3] >= bbox[3]
    ):
        raise ValueError(f"{label}.crop_bbox가 번역 bbox를 포함하지 않아요")
    padding = value.get("padding_px")
    if not isinstance(padding, int) or isinstance(padding, bool) or padding < 0:
        raise ValueError(f"{label}.padding_px는 0 이상의 정수여야 해요")
    source_rotation = value.get("source_rotation_deg")
    deskew = value.get("deskew_rotation_deg")
    inverse = value.get("inverse_rotation_deg")
    if not all(
        isinstance(number, (int, float)) and not isinstance(number, bool)
        for number in (source_rotation, deskew, inverse)
    ):
        raise ValueError(f"{label} 회전 기록이 숫자가 아니에요")
    if not math.isclose(float(source_rotation), rotation_deg, abs_tol=1e-6):
        raise ValueError(f"{label}.source_rotation_deg가 영역 회전과 달라요")
    if not math.isclose(float(deskew), rotation_deg, abs_tol=1e-6):
        raise ValueError(f"{label}.deskew_rotation_deg가 원문 역회전과 달라요")
    if not math.isclose(float(inverse), -rotation_deg, abs_tol=1e-6):
        raise ValueError(f"{label}.inverse_rotation_deg가 정확한 역변환이 아니에요")
    for field in (
        "selected_lettering_restored_to_source",
        "source_texture_resampled",
        "final_texture_resampled",
    ):
        expected = field == "selected_lettering_restored_to_source"
        if value.get(field) is not expected:
            raise ValueError(f"{label}.{field}는 {str(expected).lower()}여야 해요")
    if value.get("coordinate_space") != "source-mip0":
        raise ValueError(f"{label}.coordinate_space는 source-mip0여야 해요")
    return {
        "coordinate_space": "source-mip0",
        "crop_bbox": crop_bbox,
        "padding_px": padding,
        "source_rotation_deg": float(source_rotation),
        "deskew_rotation_deg": float(deskew),
        "inverse_rotation_deg": float(inverse),
        "selected_lettering_restored_to_source": True,
        "source_texture_resampled": False,
        "final_texture_resampled": False,
    }


def compose_vision_candidate(
    paths: ProjectPaths,
    target_id: str,
    recipe_path: Path,
    recipe: Mapping[str, Any],
) -> dict[str, Any]:
    if recipe.get("schema_version") != 2 or recipe.get("target_id") != target_id:
        raise ValueError("비전 패널 recipe schema/target이 요청과 달라요")
    if recipe.get("mode") != "vision-panel-localization":
        raise ValueError("recipe.mode는 vision-panel-localization이어야 해요")

    source_path, source_report = _project_file(paths, recipe.get("source"), "source")
    with Image.open(source_path) as source_file:
        if source_file.mode != "RGBA":
            raise ValueError("원본은 RGBA PNG여야 해요")
        size = source_file.size
        source = np.asarray(source_file, dtype=np.uint8)
    source_report.update({"width": size[0], "height": size[1], "color_mode": "RGBA"})

    masks_value = recipe.get("masks")
    if not isinstance(masks_value, Mapping):
        raise ValueError("masks 명세가 없어요")
    masks: dict[str, np.ndarray] = {}
    mask_reports: dict[str, dict[str, Any]] = {}
    for name in ("old_text", "new_text", "editable", "protected", "seam_guard"):
        masks[name], mask_reports[name] = _mask_artifact(
            paths,
            masks_value.get(name),
            f"masks.{name}",
            size,
        )
    if not masks["old_text"].any() or not masks["new_text"].any():
        raise ValueError("old_text와 new_text 마스크는 비어 있으면 안 돼요")
    if np.any((masks["old_text"] | masks["new_text"]) & ~masks["editable"]):
        raise ValueError("old_text와 new_text는 editable의 부분집합이어야 해요")
    if np.any(masks["editable"] & masks["protected"]):
        raise ValueError("editable과 protected가 겹쳐요")
    if np.any(masks["seam_guard"] & ~masks["protected"]):
        raise ValueError("seam_guard는 protected의 부분집합이어야 해요")

    output_dir = paths.drafts / target_id
    output_dir.mkdir(parents=True, exist_ok=True)
    restored, background_report = _restore_background(
        paths,
        source,
        source_report["sha256"],
        masks["old_text"],
        mask_reports["old_text"],
        recipe.get("background"),
        output_dir,
    )

    panels = recipe.get("panels")
    if not isinstance(panels, list) or not panels:
        raise ValueError("panels가 비어 있어요")
    candidate = restored.copy()
    lettering_union = np.zeros(masks["new_text"].shape, dtype=bool)
    region_ids: set[str] = set()
    compositor_regions: list[dict[str, Any]] = []
    lettering_runs: list[dict[str, Any]] = []
    panel_reports: list[dict[str, Any]] = []
    for panel_index, panel in enumerate(panels):
        if not isinstance(panel, Mapping):
            raise ValueError(f"panels[{panel_index}]가 객체가 아니에요")
        panel_id = str(panel.get("panel_id", ""))
        model_signature = panel.get("model_signature")
        attempts = panel.get("generation_attempts")
        if not panel_id or not isinstance(model_signature, str) or not model_signature:
            raise ValueError(f"panels[{panel_index}] ID/모델 서명이 비어 있어요")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise ValueError(f"panels[{panel_index}].generation_attempts가 잘못됐어요")
        if panel.get("single_generation_panel") is not True:
            raise ValueError(f"panels[{panel_index}].single_generation_panel은 true여야 해요")
        style_report = _image_artifact(
            paths,
            panel.get("source_style_reference"),
            f"panels[{panel_index}].source_style_reference",
        )
        generated_report = _image_artifact(
            paths,
            panel.get("generated_panel"),
            f"panels[{panel_index}].generated_panel",
        )
        panel_ocr_data, ocr_report = _panel_ocr_artifact(
            paths,
            panel.get("panel_ocr"),
            f"panels[{panel_index}].panel_ocr",
        )
        if panel_ocr_data.get("image_sha256") != generated_report["sha256"]:
            raise ValueError(f"panels[{panel_index}].panel_ocr가 현재 생성 패널과 묶이지 않았어요")
        if panel.get("ocr_exact_match") is not True:
            raise ValueError(f"panels[{panel_index}].ocr_exact_match는 true여야 해요")
        regions = panel.get("regions")
        if not isinstance(regions, list) or not regions:
            raise ValueError(f"panels[{panel_index}].regions가 비어 있어요")
        ocr_regions = {
            str(value.get("region_id")): value
            for value in panel_ocr_data.get("regions", [])
            if isinstance(value, Mapping)
        }
        panel_region_ids: list[str] = []
        for region_index, region in enumerate(regions):
            label = f"panels[{panel_index}].regions[{region_index}]"
            if not isinstance(region, Mapping):
                raise ValueError(f"{label}가 객체가 아니에요")
            region_id = str(region.get("region_id", ""))
            exact_text = region.get("exact_text")
            if not region_id or region_id in region_ids:
                raise ValueError(f"{label}.region_id가 비어 있거나 중복됐어요")
            if not isinstance(exact_text, str) or not exact_text:
                raise ValueError(f"{label}.exact_text가 비어 있어요")
            bbox = _bbox(region.get("bbox"), size, f"{label}.bbox")
            rotation = region.get("rotation_deg", 0)
            if not isinstance(rotation, (int, float)) or isinstance(rotation, bool):
                raise ValueError(f"{label}.rotation_deg가 숫자가 아니에요")
            direction = region.get("direction")
            if direction not in _DIRECTIONS:
                raise ValueError(f"{label}.direction이 잘못됐어요")
            occurrences = region.get("occurrences", 1)
            if not isinstance(occurrences, int) or isinstance(occurrences, bool) or occurrences < 1:
                raise ValueError(f"{label}.occurrences는 1 이상의 정수여야 해요")
            if region.get("ocr_exact_match") is not True:
                raise ValueError(f"{label}.ocr_exact_match는 true여야 해요")
            ocr_region = ocr_regions.get(region_id)
            if (
                ocr_region is None
                or ocr_region.get("expected_text") != exact_text
                or ocr_region.get("matched") is not True
                or ocr_region.get("match_mode") != "nfc-literal"
            ):
                raise ValueError(f"{label}이 생성 패널 OCR 완전일치를 통과하지 않았어요")
            transform = _panel_transform(
                region.get("panel_transform"),
                float(rotation),
                bbox,
                size,
                f"{label}.panel_transform",
            )
            lettering, lettering_report = _rgba_artifact(
                paths,
                region.get("selected_lettering"),
                f"{label}.selected_lettering",
                size=size,
            )
            lettering_mask, lettering_mask_report = _mask_artifact(
                paths,
                region.get("lettering_mask"),
                f"{label}.lettering_mask",
                size,
            )
            visible = lettering[..., 3] > 0
            if not np.array_equal(visible, lettering_mask):
                raise ValueError(f"{label} 레터링 알파와 lettering_mask가 정확히 일치해야 해요")
            if np.any(lettering_mask & ~masks["new_text"]):
                raise ValueError(f"{label}.lettering_mask가 new_text 밖을 변경해요")
            if np.any(lettering_mask & lettering_union):
                raise ValueError(f"{label}.lettering_mask가 다른 레터링 영역과 겹쳐요")
            alpha = lettering[..., 3:4].astype(np.float32) / 255.0
            blended = np.clip(
                np.round(
                    lettering[..., :3].astype(np.float32) * alpha
                    + candidate[..., :3].astype(np.float32) * (1.0 - alpha)
                ),
                0,
                255,
            ).astype(np.uint8)
            candidate[..., :3][lettering_mask] = blended[lettering_mask]
            lettering_union |= lettering_mask
            region_ids.add(region_id)
            panel_region_ids.append(region_id)
            compositor_regions.append(
                {
                    "panel_id": panel_id,
                    "region_id": region_id,
                    "exact_text": exact_text,
                    "occurrences": occurrences,
                    "bbox": bbox,
                    "rotation_deg": float(rotation),
                    "direction": direction,
                    "model_signature": model_signature,
                    "generation_attempts": attempts,
                    "ocr_exact_match": True,
                    "panel_ocr": ocr_report,
                    "panel_transform": transform,
                    "source_typography": region.get("source_typography"),
                    "result_typography": region.get("result_typography"),
                    "typography_checks": region.get("typography_checks"),
                    "source_style_reference": style_report,
                    "generated_panel": generated_report,
                    "selected_lettering": lettering_report,
                    "lettering_mask": lettering_mask_report,
                }
            )
            lettering_runs.append(
                {
                    "panel_id": panel_id,
                    "region_id": region_id,
                    "text": exact_text,
                    "occurrences": occurrences,
                    "bbox": bbox,
                    "rotation_deg": float(rotation),
                    "direction": direction,
                    "model_signature": model_signature,
                    "selected_lettering_sha256": lettering_report["sha256"],
                    "lettering_mask_sha256": lettering_mask_report["sha256"],
                }
            )
        panel_reports.append(
            {
                "panel_id": panel_id,
                "model_signature": model_signature,
                "generation_attempts": attempts,
                "single_generation_panel": True,
                "ocr_exact_match": True,
                "panel_ocr": ocr_report,
                "source_style_reference": style_report,
                "generated_panel": generated_report,
                "region_ids": panel_region_ids,
            }
        )
    if not np.array_equal(lettering_union, masks["new_text"]):
        raise ValueError("모든 lettering_mask의 합이 new_text 마스크와 정확히 같아야 해요")

    candidate[..., 3] = source[..., 3]
    candidate[..., :3][~masks["editable"]] = source[..., :3][~masks["editable"]]
    changed = np.any(candidate[..., :3] != source[..., :3], axis=2)
    if not changed.any():
        raise ValueError("후보 이미지에서 변경된 픽셀이 없어요")
    if np.any(changed & ~masks["editable"]):
        raise AssertionError("editable 밖 픽셀이 변경됐어요")

    candidate_path = output_dir / "candidate.png"
    lettering_path = output_dir / "lettering-run.json"
    _save_rgba(candidate_path, candidate)
    lettering_payload = {
        "schema_version": 2,
        "target_id": target_id,
        "source_sha256": source_report["sha256"],
        "lettering_runs": lettering_runs,
    }
    lettering_path.write_text(
        json.dumps(lettering_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    report = {
        "schema_version": 2,
        "target_id": target_id,
        "candidate": candidate_path.relative_to(paths.root).as_posix(),
        "candidate_sha256": _sha256(candidate_path),
        "source": source_report,
        "source_sha256": source_report["sha256"],
        "width": size[0],
        "height": size[1],
        "color_mode": "RGBA",
        "alpha_equal": bool(np.array_equal(candidate[..., 3], source[..., 3])),
        "changed_pixels": int(changed.sum()),
        "changed_outside_editable": int((changed & ~masks["editable"]).sum()),
        "changed_inside_protected": int((changed & masks["protected"]).sum()),
        "changed_inside_seam_guard": int((changed & masks["seam_guard"]).sum()),
        "masks": mask_reports,
        "compositor": {
            "mode": "vision-panel-localization",
            "fixed_font_used": False,
            "single_pass_panels": True,
            "background": background_report,
            "panels": panel_reports,
            "regions": compositor_regions,
            "lettering_run": _relative_report(paths, lettering_path),
        },
        "recipe": recipe_path.relative_to(paths.root).as_posix(),
        "recipe_sha256": _sha256(recipe_path),
        "candidate_gate_eligible": True,
        "passed": True,
    }
    report_path = output_dir / "compose-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**report, "report": str(report_path), "report_sha256": _sha256(report_path)}
