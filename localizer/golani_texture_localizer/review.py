from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .models import TargetSpec
from .paths import ProjectPaths


MASK_NAMES = ("old_text", "new_text", "editable", "protected", "seam_guard")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    packed = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def review_stage_sha256(record: dict[str, Any], stage_name: str) -> str:
    stages = record.get("stages")
    stage = stages.get(stage_name) if isinstance(stages, dict) else None
    if not isinstance(stage, dict):
        raise ValueError(f"작업 기록에 {stage_name} 단계가 없어요")
    return _json_sha256({"stage": stage_name, "value": stage})


def review_path(paths: ProjectPaths, target_id: str) -> Path:
    return paths.reviews / target_id / "review.json"


def approval_path(paths: ProjectPaths, target_id: str) -> Path:
    return paths.approved / f"{target_id}.approval.json"


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


def _skill_validator(paths: ProjectPaths):
    script = (
        paths.root
        / ".agents"
        / "skills"
        / "localize-spt-food-textures"
        / "scripts"
        / "review_record.py"
    )
    if not script.is_file():
        raise FileNotFoundError(f"필수 작업 기록 검사기를 찾지 못했어요: {script}")
    spec = importlib.util.spec_from_file_location("golani_review_record", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"작업 기록 검사기를 불러오지 못했어요: {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate_errors(paths: ProjectPaths, record: dict[str, Any]) -> list[str]:
    validator = _skill_validator(paths)
    errors = validator.validate_record(record, "candidate", project_root=paths.root)
    stages = record.get("stages", {})
    for stage_name in validator.STAGES[len(validator.THROUGH["candidate"]) :]:
        prefix = f"stages.{stage_name}"
        errors = [error for error in errors if not error.startswith(prefix)]
    return errors


def load_review(paths: ProjectPaths, target_id: str, *, through: str) -> tuple[Path, dict[str, Any]]:
    path = review_path(paths, target_id)
    if not path.is_file():
        raise FileNotFoundError(f"품목 작업 기록이 없어요: {path}")
    record = json.loads(path.read_text(encoding="utf-8"))
    validator = _skill_validator(paths)
    errors = (
        _candidate_errors(paths, record)
        if through == "candidate"
        else validator.validate_record(record, through, project_root=paths.root)
    )
    if errors:
        preview = "; ".join(errors[:5])
        suffix = f" 외 {len(errors) - 5}개" if len(errors) > 5 else ""
        raise ValueError(f"{target_id} {through} 작업 기록이 미완성이에요: {preview}{suffix}")
    return path, record


def _load_mask(
    paths: ProjectPaths,
    descriptor: Any,
    name: str,
    size: tuple[int, int],
) -> tuple[Path, np.ndarray]:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{name} 마스크 명세가 없어요")
    path = _project_path(paths, descriptor.get("path"), f"{name} 마스크")
    if not path.is_file():
        raise FileNotFoundError(path)
    expected_sha = descriptor.get("sha256")
    if expected_sha != sha256_file(path):
        raise ValueError(f"{name} 마스크 SHA-256이 작업 기록과 달라요")
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"}:
            raise ValueError(f"{name} 마스크는 단일 채널 1/L 이미지여야 해요")
        if image_file.size != size:
            raise ValueError(f"{name} 마스크 크기 {image_file.size}가 원본 {size}와 달라요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    unique = np.unique(values)
    if not set(int(value) for value in unique).issubset({0, 255}):
        raise ValueError(f"{name} 마스크는 0과 255만 사용해야 해요")
    return path, values == 255


def verify_candidate(
    paths: ProjectPaths,
    target: TargetSpec,
    source_path: Path,
    candidate_path: Path,
) -> dict[str, Any]:
    record_path, record = load_review(paths, target.id, through="candidate")
    if record.get("target_id") != target.id or record.get("action") != target.action:
        raise ValueError("작업 기록 대상이 profile과 달라요")
    if record.get("expected_text") != list(target.exact_text):
        raise ValueError("작업 기록 확정 문구가 현재 profile과 달라요")
    source_record = record.get("source", {})
    if source_record.get("texture") != target.texture:
        raise ValueError("작업 기록 Texture2D가 profile과 달라요")
    if target.bundle_key and source_record.get("bundle_key") != target.bundle_key:
        raise ValueError("작업 기록 bundle key가 profile과 달라요")
    source_sha = sha256_file(source_path)
    if source_record.get("sha256") != source_sha:
        raise ValueError("작업 기록이 현재 원본 SHA-256과 달라요")

    with Image.open(source_path) as source_file:
        source_mode = source_file.mode
        source_size = source_file.size
        source = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
    with Image.open(candidate_path) as candidate_file:
        candidate_mode = candidate_file.mode
        candidate_size = candidate_file.size
        candidate = np.asarray(candidate_file.convert("RGBA"), dtype=np.uint8)
    if candidate_size != source_size:
        raise ValueError(f"후보 크기 {candidate_size}가 원본 {source_size}와 달라요")
    if candidate_mode != source_mode:
        raise ValueError(f"후보 색 모드 {candidate_mode}가 원본 {source_mode}와 달라요")

    edit_data = record["stages"]["edit_plan"]["data"]
    descriptors = edit_data["masks"]
    masks: dict[str, np.ndarray] = {}
    mask_paths: dict[str, str] = {}
    mask_hashes: dict[str, str] = {}
    for name in MASK_NAMES:
        path, mask = _load_mask(paths, descriptors.get(name), name, source_size)
        masks[name] = mask
        mask_paths[name] = str(path)
        mask_hashes[name] = sha256_file(path)

    text_union = masks["old_text"] | masks["new_text"]
    if np.any(text_union & ~masks["editable"]):
        raise ValueError("old_text/new_text 마스크가 editable 밖으로 나갔어요")
    if np.any(masks["editable"] & masks["protected"]):
        raise ValueError("editable과 protected 마스크가 겹쳐요")
    if np.any(masks["seam_guard"] & ~masks["protected"]):
        raise ValueError("seam_guard가 protected에 포함되지 않았어요")

    rgb_changed = np.any(source[..., :3] != candidate[..., :3], axis=2)
    alpha_changed = source[..., 3] != candidate[..., 3]
    measurements = {
        "resized": False,
        "source_width": source_size[0],
        "source_height": source_size[1],
        "candidate_width": candidate_size[0],
        "candidate_height": candidate_size[1],
        "source_color_mode": source_mode,
        "candidate_color_mode": candidate_mode,
        "source_sha256": source_sha,
        "candidate_sha256": sha256_file(candidate_path),
        "alpha_equal": not bool(alpha_changed.any()),
        "changed_pixels": int(rgb_changed.sum()),
        "changed_outside_editable": int((rgb_changed & ~masks["editable"]).sum()),
        "changed_inside_protected": int((rgb_changed & masks["protected"]).sum()),
        "changed_inside_seam_guard": int((rgb_changed & masks["seam_guard"]).sum()),
    }
    expected = record["stages"]["candidate_validation"]["data"]
    for key, wanted in measurements.items():
        if key in expected and expected[key] != wanted:
            raise ValueError(f"후보 실측 {key}={wanted!r}가 작업 기록 {expected[key]!r}와 달라요")
    if not measurements["alpha_equal"]:
        raise ValueError("후보 알파가 원본과 달라요")
    if measurements["changed_pixels"] == 0:
        raise ValueError("현지화 후보의 RGB 변경이 없어요")
    for key in (
        "changed_outside_editable",
        "changed_inside_protected",
        "changed_inside_seam_guard",
    ):
        if measurements[key] != 0:
            raise ValueError(f"후보의 {key}가 0이 아니에요")

    candidate_sha = measurements["candidate_sha256"]
    for stage_name in ("post_ocr", "post_visual"):
        reviewed_sha = record["stages"][stage_name]["data"].get("candidate_sha256")
        if reviewed_sha != candidate_sha:
            raise ValueError(f"{stage_name}이 현재 후보 SHA-256을 검사하지 않았어요")
    return {
        **measurements,
        "review": str(record_path),
        "review_sha256": sha256_file(record_path),
        "candidate_review_sha256": _json_sha256(
            {
                "schema_version": record.get("schema_version"),
                "target_id": record.get("target_id"),
                "action": record.get("action"),
                "expected_text": record.get("expected_text"),
                "source": record.get("source"),
                "unresolved": record.get("unresolved"),
                "stages": {
                    name: record["stages"][name]
                    for name in (
                        "source_ocr",
                        "source_visual",
                        "cross_validation",
                        "translation",
                        "edit_plan",
                        "candidate_validation",
                        "post_ocr",
                        "post_visual",
                    )
                },
            }
        ),
        "masks": mask_paths,
        "mask_sha256": mask_hashes,
        "passed": True,
    }


def verify_approval(
    paths: ProjectPaths,
    target: TargetSpec,
    source_path: Path,
    approved_path: Path,
) -> dict[str, Any]:
    sidecar = approval_path(paths, target.id)
    if not sidecar.is_file():
        raise FileNotFoundError(f"승인 증거가 없어요: {sidecar}")
    approval = json.loads(sidecar.read_text(encoding="utf-8"))
    if approval.get("schema_version") != 2 or approval.get("target_id") != target.id:
        raise ValueError(f"지원하지 않는 승인 증거예요: {sidecar}")
    current = verify_candidate(paths, target, source_path, approved_path)
    for key in (
        "source_sha256",
        "candidate_sha256",
        "candidate_review_sha256",
        "mask_sha256",
    ):
        if approval.get(key) != current.get(key):
            raise ValueError(f"승인 증거의 {key}가 현재 입력과 달라요")
    return current
