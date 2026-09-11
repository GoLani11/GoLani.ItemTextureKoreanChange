"""Deterministic texture checks, independent of OCR and visual approval."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .auxiliary import project_binary_mask
from .bindings import check_projection
from .files import descriptor, local_path, verified_file, write_json
from .jobs import load_job


def rgba(path: Path, size: tuple[int, int] | None = None) -> np.ndarray:
    with Image.open(path) as image:
        if image.mode != "RGBA" or (size is not None and image.size != size):
            raise ValueError(f"원본 크기의 RGBA PNG가 필요해요: {path}")
        return np.array(image)


def mask(path: Path, size: tuple[int, int]) -> np.ndarray:
    with Image.open(path) as image:
        if image.mode != "L" or image.size != size:
            raise ValueError(f"원본 크기의 L 마스크가 필요해요: {path}")
        values = np.array(image)
    if not np.isin(values, [0, 255]).all():
        raise ValueError(f"마스크는 0/255만 사용할 수 있어요: {path}")
    return values != 0


def compare_pixels(source: np.ndarray, candidate: np.ndarray, editable: np.ndarray,
                   preserved_channels: list[int], seam: np.ndarray, shared: bool = False) -> dict:
    if candidate.shape != source.shape or editable.shape != source.shape[:2] or seam.shape != editable.shape:
        raise ValueError("원본·편집본·마스크 크기가 달라요")
    changed = np.any(source != candidate, axis=2)
    outside = int(np.count_nonzero(changed & ~editable))
    channels = {str(c): int(np.count_nonzero(source[..., c] != candidate[..., c])) for c in preserved_channels}
    seam_changes = int(np.count_nonzero(changed & seam))
    return {"changed_pixels": int(changed.sum()), "editable_pixels": int(editable.sum()),
            "outside_editable_changed_pixels": outside, "preserved_channel_changes": channels,
            "seam_changed_pixels": seam_changes, "shared_map_changed": bool(shared and changed.any()),
            "passed": outside == 0 and seam_changes == 0 and not any(channels.values())
                      and not (shared and changed.any())}


def validate(job_path: Path, *, write: bool = True) -> dict:
    root, job, snapshot = load_job(job_path)
    verified_file(root, snapshot["inventory"])
    for value in snapshot["bundles"].values():
        verified_file(root, value)
    seam_path = verified_file(root, snapshot["uv_seam"])
    verified_file(root, snapshot["uv_coverage"])
    with Image.open(seam_path) as image:
        seam_source = np.array(image.convert("L")) != 0
    checks = []
    inputs = {"job": descriptor(root, job_path), "source": job["source"]}
    for entry in snapshot["maps"]:
        size = (entry["width"], entry["height"])
        source = rgba(verified_file(root, entry["source"]), size)
        candidate_path = local_path(root, entry["candidate"])
        editable_path = local_path(root, entry["editable"])
        candidate, editable = rgba(candidate_path, size), mask(editable_path, size)
        changed = np.any(candidate != source, axis=2)
        if entry["role"] != "diffuse" and changed.any():
            check_projection(entry)
        seam = project_binary_mask(seam_source, size) if changed.any() else np.zeros(source.shape[:2], bool)
        result = compare_pixels(source, candidate, editable, entry["preserved_channels"], seam, entry["shared"])
        checks.append({"id": entry["id"], "texture": entry["texture"], "role": entry["role"], **result})
        inputs[entry["id"]] = {"candidate": descriptor(root, candidate_path), "editable": descriptor(root, editable_path)}
        if write:
            changed = np.any(candidate != source, axis=2)
            highlight = source.copy()
            highlight[changed, :3] = (255, 48, 48)
            sheet = Image.new("RGB", (size[0] * 3, size[1] + 24), "#202020")
            draw = ImageDraw.Draw(sheet)
            for i, (label, array) in enumerate(zip(["SOURCE", "CANDIDATE", "CHANGED"], [source, candidate, highlight])):
                draw.text((i * size[0] + 8, 6), label, fill="white")
                sheet.paste(Image.fromarray(array).convert("RGB"), (i * size[0], 24))
            path = root / "reports" / f"{entry['id']}-comparison.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            sheet.save(path)
    report = {"schema_version": 1, "target_id": snapshot["target_id"], "inputs": inputs,
              "passed": all(c["passed"] for c in checks),
              "changed_pixels": sum(c["changed_pixels"] for c in checks), "maps": checks,
              "visual_reviewed": False, "runtime_tested": False}
    if write:
        write_json(root / "reports" / "validation.json", report)
    return report
