from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from .inventory import load_inventory, record_for_target
from .models import CollectionProfile
from .names import safe_bundle_name
from .paths import ProjectPaths


NORMAL_RELIEF_THRESHOLD = 0.012
GLOSS_DELTA_THRESHOLD = 3 / 255.0


def _unit(width: int, height: int) -> float:
    return min(width, height) / 512.0


def _rgb(path: Path, size: tuple[int, int]) -> np.ndarray:
    with Image.open(path) as image_file:
        return np.asarray(image_file.convert("RGB").resize(size, Image.Resampling.LANCZOS))


def _design_mask(rgb_u8: np.ndarray, unit: float) -> np.ndarray:
    lab = cv2.cvtColor(rgb_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
    lightness, channel_a, channel_b = lab[..., 0], lab[..., 1], lab[..., 2]
    high_small = np.abs(lightness - cv2.GaussianBlur(lightness, (0, 0), max(1.0, 1.2 * unit)))
    high_medium = np.abs(lightness - cv2.GaussianBlur(lightness, (0, 0), max(1.0, 3.0 * unit)))
    chroma = np.abs(channel_a - cv2.GaussianBlur(channel_a, (0, 0), max(1.0, 2.0 * unit)))
    chroma += np.abs(channel_b - cv2.GaussianBlur(channel_b, (0, 0), max(1.0, 2.0 * unit)))
    score = np.maximum.reduce([high_small, high_medium, 0.5 * chroma])
    median = np.median(score)
    mad = np.median(np.abs(score - median)) + 1e-6
    mask = (score > median + 3.0 * mad).astype(np.uint8)
    kernel_size = max(1, int(round(1.5 * unit)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), np.uint8))

    count, components, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    total = mask.size
    filtered = np.zeros_like(mask)
    for index in range(1, count):
        ratio = stats[index, cv2.CC_STAT_AREA] / total
        if 3e-6 <= ratio <= 0.30:
            filtered[components == index] = 1
    dilation = max(1, int(round(1.5 * unit)))
    filtered = cv2.dilate(filtered, np.ones((dilation, dilation), np.uint8))
    return np.clip(
        cv2.GaussianBlur(filtered.astype(np.float32), (0, 0), max(1.0, 1.5 * unit)),
        0,
        1,
    )


def transplant_normal(original_map: Path, original_diffuse: Path, localized_diffuse: Path):
    with Image.open(original_map) as image_file:
        original = np.asarray(image_file.convert("RGBA"))
    height, width = original.shape[:2]
    unit = _unit(width, height)
    x = original[..., 3].astype(np.float32) / 255 * 2 - 1
    y = original[..., 1].astype(np.float32) / 255 * 2 - 1

    sigma = max(1.0, 3.0 * unit)
    x_base = cv2.GaussianBlur(x, (0, 0), sigma)
    y_base = cv2.GaussianBlur(y, (0, 0), sigma)
    x_detail, y_detail = x - x_base, y - y_base
    relief = np.sqrt(x_detail * x_detail + y_detail * y_detail)
    old_mask = _design_mask(_rgb(original_diffuse, (width, height)), unit)
    new_mask = _design_mask(_rgb(localized_diffuse, (width, height)), unit)
    protect = (relief > np.percentile(relief, 90)).astype(np.float32)
    remove = old_mask * (1 - protect)
    selected = (old_mask > 0.5) & (protect < 0.5)
    strength = float(np.median(relief[selected])) if selected.sum() > 50 else 0.0

    x = x_base + x_detail * (1 - remove)
    y = y_base + y_detail * (1 - remove)
    added = False
    if strength > NORMAL_RELIEF_THRESHOLD:
        hard_mask = (new_mask > 0.5).astype(np.uint8)
        signed_distance = cv2.distanceTransform(hard_mask, cv2.DIST_L2, 5)
        signed_distance -= cv2.distanceTransform(1 - hard_mask, cv2.DIST_L2, 5)
        gradient_x = cv2.Sobel(signed_distance, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(signed_distance, cv2.CV_32F, 0, 1, ksize=3)
        norm = np.sqrt(gradient_x * gradient_x + gradient_y * gradient_y) + 1e-6
        x += strength * (gradient_x / norm) * new_mask
        y += strength * (gradient_y / norm) * new_mask
        added = True

    length = np.sqrt(x * x + y * y)
    scale = np.minimum(0.98 / np.maximum(length, 1e-6), 1.0)
    x, y = x * scale, y * scale
    output = original.copy()
    output[..., 0] = 255
    output[..., 1] = np.clip((y * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)
    output[..., 3] = np.clip((x * 0.5 + 0.5) * 255, 0, 255).astype(np.uint8)
    return Image.fromarray(output, "RGBA"), {"strength": round(strength, 6), "added": added}


def transplant_gloss(original_map: Path, original_diffuse: Path, localized_diffuse: Path):
    with Image.open(original_map) as image_file:
        original = np.asarray(image_file.convert("RGBA"))
    height, width = original.shape[:2]
    unit = _unit(width, height)
    values = original[..., 0].astype(np.float32) / 255.0
    old_mask = _design_mask(_rgb(original_diffuse, (width, height)), unit)
    new_mask = _design_mask(_rgb(localized_diffuse, (width, height)), unit)
    hard_mask = (old_mask > 0.5).astype(np.uint8)
    erosion = max(1, int(round(2 * unit)))
    dilation = max(2, int(round(5 * unit)))
    core = cv2.erode(hard_mask, np.ones((erosion, erosion), np.uint8))
    ring = cv2.dilate(hard_mask, np.ones((dilation, dilation), np.uint8))
    ring -= cv2.dilate(hard_mask, np.ones((erosion, erosion), np.uint8))
    design_value = np.median(values[core > 0]) if (core > 0).sum() > 50 else 0.0
    background_value = np.median(values[ring > 0]) if (ring > 0).sum() > 50 else 0.0
    delta = float(design_value - background_value)

    inpaint_mask = (old_mask > 0.3).astype(np.uint8) * 255
    cleaned = cv2.inpaint(
        (values * 255).astype(np.uint8),
        inpaint_mask,
        max(1, int(round(3 * unit))),
        cv2.INPAINT_TELEA,
    ).astype(np.float32) / 255.0
    added = False
    if abs(delta) > GLOSS_DELTA_THRESHOLD:
        soft = cv2.GaussianBlur(new_mask, (0, 0), max(1.0, 1.2 * unit))
        cleaned = np.clip(cleaned + delta * soft, 0, 1)
        added = True
    output_value = (cleaned * 255).astype(np.uint8)
    output = np.dstack([output_value, output_value, output_value, np.full_like(output_value, 255)])
    return Image.fromarray(output, "RGBA"), {"delta": round(delta, 6), "added": added}


def derive_approved_materials(profile: CollectionProfile, paths: ProjectPaths) -> dict[str, Any]:
    inventory = load_inventory(paths.inventory)
    outputs: list[dict[str, Any]] = []
    for target in profile.targets:
        localized = paths.approved / f"{target.id}.png"
        if target.action != "localize" or not localized.is_file():
            continue
        diffuse_record = record_for_target(inventory, target)
        original_diffuse = Path(diffuse_record["source_png"])
        companions = [
            record
            for record in inventory["records"]
            if record["family"] == diffuse_record["family"] and record["role"] in {"normal", "gloss"}
        ]
        for record in companions:
            original_map = Path(record["source_png"])
            destination = paths.derived / safe_bundle_name(record["bundle_key"]) / f"{record['texture']}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if record["role"] == "normal":
                image, metrics = transplant_normal(original_map, original_diffuse, localized)
            else:
                image, metrics = transplant_gloss(original_map, original_diffuse, localized)
            image.save(destination)
            outputs.append(
                {
                    "target_id": target.id,
                    "bundle_key": record["bundle_key"],
                    "texture": record["texture"],
                    "role": record["role"],
                    "source_png": str(original_map),
                    "derived_png": str(destination),
                    "metrics": metrics,
                }
            )
    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "derived_count": len(outputs),
        "outputs": outputs,
    }
    paths.derived_manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.derived_manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload
