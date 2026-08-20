from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import ModuleType
from typing import Any, Mapping


MASK_NAMES = ("old_text", "new_text", "editable", "protected", "seam_guard")
MAX_GENERATION_ATTEMPTS = 2


@dataclass(frozen=True)
class SptArtifact:
    path: Path
    project_path: str
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class SptRegion:
    region_id: str
    source_text: str
    final_text_ko: str
    bbox: tuple[int, int, int, int]
    rotation_deg: float
    direction: str
    face: str
    occurrences: int
    typography: dict[str, Any]


@dataclass(frozen=True)
class SptPanel:
    panel_id: str
    label: str
    face: str
    rotation_deg: float
    regions: tuple[SptRegion, ...]
    recorded_attempts: int


@dataclass(frozen=True)
class SptTarget:
    project_root: Path
    target_id: str
    name_ko: str
    texture: str
    review_path: Path
    review_sha256: str
    source: SptArtifact
    masks: dict[str, SptArtifact]
    panels: tuple[SptPanel, ...]


@dataclass(frozen=True)
class SptPreparation:
    project_root: Path
    target_id: str
    name_ko: str
    review_path: Path
    source: SptArtifact
    masks: dict[str, SptArtifact]
    analysis_errors: tuple[str, ...]
    mask_error: str | None

    @property
    def ready(self) -> bool:
        return not self.analysis_errors and self.mask_error is None


def build_preview_choice_record(
    spt: Mapping[str, Any],
    generation: Mapping[str, Any],
    *,
    status: str,
    created_at: str,
) -> dict[str, Any]:
    def required_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"SPT {label} 기록이 없어요")
        return value.strip()

    def required_sha256(value: Any, label: str) -> str:
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None
        ):
            raise ValueError(f"SPT {label} SHA-256 기록이 잘못됐어요")
        return value.lower()

    if status not in {"selected-for-validation", "discarded"}:
        raise ValueError("지원하지 않는 SPT 미리보기 선택 상태예요")
    artifact = generation.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("SPT 생성 이미지 SHA 기록이 없어요")
    mask_values = spt.get("mask_sha256")
    if not isinstance(mask_values, Mapping) or set(mask_values) != set(MASK_NAMES):
        raise ValueError("SPT 5종 mask SHA-256 기록이 완전하지 않아요")
    mask_sha256 = {
        name: required_sha256(mask_values.get(name), f"{name} mask")
        for name in MASK_NAMES
    }
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("SPT 미리보기 선택 시간이 없어요")
    return {
        "schema_version": 2,
        "status": status,
        "purpose": "human-visual-selection",
        "created_at": created_at,
        "target_id": required_text(spt.get("target_id"), "target_id"),
        "panel_id": required_text(spt.get("panel_id"), "panel_id"),
        "review_sha256": required_sha256(spt.get("review_sha256"), "review"),
        "source_sha256": required_sha256(spt.get("source_sha256"), "source"),
        "mask_sha256": mask_sha256,
        "generated_sha256": required_sha256(artifact.get("sha256"), "generated image"),
        "request": "request.json",
        "next_gate": (
            "external-project-validation" if status == "selected-for-validation" else "none"
        ),
        "candidate_approved": False,
    }


@dataclass(frozen=True)
class SptTargetSummary:
    target_id: str
    name_ko: str
    state: str


def is_spt_project_root(root: Path) -> bool:
    return (
        (root / "profiles" / "food" / "collection.json").is_file()
        and (
            root
            / ".agents"
            / "skills"
            / "localize-spt-food-textures"
            / "SKILL.md"
        ).is_file()
    )


def scan_spt_targets(root: Path) -> tuple[SptTargetSummary, ...]:
    root = root.expanduser().resolve()
    profile = _load_profile(root)
    summaries: list[SptTargetSummary] = []
    for target in profile["targets"]:
        target_id = str(target.get("id", "")).strip()
        name_ko = str(target.get("name_ko", target_id)).strip() or target_id
        action = target.get("action", "localize")
        review_path = root / "workspace" / "reviews" / target_id / "review.json"
        if action != "localize":
            state = "보존 대상"
        elif not review_path.is_file():
            state = "분석 기록 없음"
        else:
            try:
                record = _read_json(review_path)
                if record.get("target_id") != target_id:
                    state = "원본 또는 기록 오류"
                elif _analysis_preflight_gaps(record):
                    state = "analysis 갱신 필요 · 원본 확인 가능"
                elif not _has_all_mask_descriptors(record):
                    state = "마스크 준비 필요 · 원본 확인 가능"
                else:
                    state = "형식 준비됨 · 공식 게이트·SHA 검사 대기"
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                state = "원본 또는 기록 오류"
        summaries.append(SptTargetSummary(target_id, name_ko, state))
    return tuple(summaries)


def inspect_spt_target(root: Path, target_id: str) -> SptPreparation:
    root = root.expanduser().resolve()
    profile = _load_profile(root)
    profile_target = next(
        (item for item in profile["targets"] if item.get("id") == target_id),
        None,
    )
    if not isinstance(profile_target, dict):
        raise ValueError(f"SPT profile에 대상이 없어요: {target_id}")
    if profile_target.get("action", "localize") != "localize":
        raise ValueError("이 품목은 현지화가 아니라 원본 보존 대상으로 지정되어 있어요")

    review_path = root / "workspace" / "reviews" / target_id / "review.json"
    if not review_path.is_file():
        raise ValueError(f"분석 기록이 없어요: {review_path}")
    record = _read_json(review_path)
    if record.get("target_id") != target_id:
        raise ValueError("review.json의 target_id가 선택한 품목과 달라요")

    source_data = record.get("source")
    if not isinstance(source_data, dict):
        raise ValueError("review.json에 원본 명세가 없어요")
    if source_data.get("bundle_key") != profile_target.get("bundle_key"):
        raise ValueError("review.json의 원본 bundle key가 profile과 달라요")
    if source_data.get("texture") != profile_target.get("texture"):
        raise ValueError("review.json의 Texture2D 이름이 profile과 달라요")
    source = _artifact_from_descriptor(root, source_data, "source", require_size=True)

    stages = record.get("stages", {})
    edit_data = stages.get("edit_plan", {}).get("data", {})
    mask_data = edit_data.get("masks") if isinstance(edit_data, dict) else None
    masks: dict[str, SptArtifact] = {}
    mask_error: str | None = None
    if not isinstance(mask_data, dict):
        mask_error = "5종 편집 마스크가 아직 준비되지 않았어요"
    else:
        try:
            masks = {
                name: _artifact_from_descriptor(
                    root,
                    mask_data.get(name),
                    f"edit_plan.masks.{name}",
                    require_size=True,
                )
                for name in MASK_NAMES
            }
            for name, artifact in masks.items():
                if (artifact.width, artifact.height) != (source.width, source.height):
                    raise ValueError(f"{name} 마스크 크기가 원본과 달라요")
        except (OSError, ValueError, TypeError) as exc:
            masks = {}
            mask_error = str(exc)

    return SptPreparation(
        project_root=root,
        target_id=target_id,
        name_ko=str(profile_target.get("name_ko", target_id)),
        review_path=review_path,
        source=source,
        masks=masks,
        analysis_errors=tuple(_validate_analysis_with_project_script(root, record)),
        mask_error=mask_error,
    )


def load_spt_target(root: Path, target_id: str) -> SptTarget:
    preparation = inspect_spt_target(root, target_id)
    if preparation.analysis_errors:
        preview = "; ".join(preparation.analysis_errors[:3])
        remaining = len(preparation.analysis_errors) - 3
        suffix = f" 외 {remaining}건" if remaining > 0 else ""
        raise ValueError(f"SPT analysis 게이트가 막혔어요: {preview}{suffix}")
    if preparation.mask_error is not None:
        raise ValueError(
            "분석은 통과했지만 편집 마스크를 사용할 수 없어요: "
            f"{preparation.mask_error}"
        )

    root = preparation.project_root
    record = _read_json(preparation.review_path)
    stages = record.get("stages", {})
    edit_data = stages.get("edit_plan", {}).get("data", {})

    visual_regions = {
        item.get("region_id"): item
        for item in stages.get("source_visual", {}).get("data", {}).get("regions", [])
        if isinstance(item, dict) and isinstance(item.get("region_id"), str)
    }
    translations = stages.get("translation", {}).get("data", {}).get("regions", [])
    if not isinstance(translations, list) or not translations:
        raise ValueError("통과한 번역 영역이 없어요")
    parsed_regions: list[SptRegion] = []
    for index, item in enumerate(translations):
        if not isinstance(item, dict):
            raise ValueError(f"번역 영역 {index + 1}의 형식이 올바르지 않아요")
        region_id = str(item.get("region_id", "")).strip()
        visual = visual_regions.get(region_id)
        if not isinstance(visual, dict) or not isinstance(visual.get("typography"), dict):
            raise ValueError(f"{region_id}: 원본 typography 기록이 없어요")
        bbox = item.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in bbox)
        ):
            raise ValueError(f"{region_id}: bbox가 올바르지 않아요")
        parsed_regions.append(
            SptRegion(
                region_id=region_id,
                source_text=str(item.get("source_text", "")),
                final_text_ko=str(item.get("final_text_ko", "")),
                bbox=(bbox[0], bbox[1], bbox[2], bbox[3]),
                rotation_deg=float(item.get("rotation_deg", 0)),
                direction=str(item.get("direction", "")),
                face=str(item.get("face", "미지정 면")),
                occurrences=int(item.get("occurrences", 1)),
                typography=dict(visual["typography"]),
            )
        )

    recorded_attempts = _recorded_attempts(edit_data)
    grouped: dict[tuple[str, float], list[SptRegion]] = {}
    for region in parsed_regions:
        grouped.setdefault((region.face, region.rotation_deg), []).append(region)
    panels: list[SptPanel] = []
    for index, ((face, rotation), regions) in enumerate(grouped.items(), start=1):
        digest = hashlib.sha256(f"{face}\0{rotation}".encode("utf-8")).hexdigest()[:8]
        panel_id = f"panel-{index:02d}-{digest}"
        panels.append(
            SptPanel(
                panel_id=panel_id,
                label=f"{face} · {rotation:g}° · {len(regions)}개 문구",
                face=face,
                rotation_deg=rotation,
                regions=tuple(regions),
                recorded_attempts=recorded_attempts,
            )
        )

    return SptTarget(
        project_root=root,
        target_id=target_id,
        name_ko=preparation.name_ko,
        texture=str(record.get("source", {}).get("texture", "")),
        review_path=preparation.review_path,
        review_sha256=_sha256(preparation.review_path),
        source=preparation.source,
        masks=preparation.masks,
        panels=tuple(panels),
    )


def current_generation_attempts(target: SptTarget, panel: SptPanel) -> int:
    attempts = panel.recorded_attempts
    jobs_root = (
        target.project_root
        / "workspace"
        / "krita-spt"
        / target.target_id
        / panel.panel_id
    )
    if not jobs_root.is_dir():
        return attempts
    local_attempts = 0
    for request_path in jobs_root.glob("*/request.json"):
        try:
            value = _read_json(request_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        spt = value.get("spt")
        if not isinstance(spt, dict):
            continue
        generated = value.get("generation")
        has_generated_artifact = (
            isinstance(generated, dict)
            and isinstance(generated.get("artifact"), dict)
            and isinstance(generated["artifact"].get("sha256"), str)
        ) or (request_path.parent / "generated.png").is_file()
        if (
            spt.get("review_sha256") == target.review_sha256
            and spt.get("panel_id") == panel.panel_id
            and has_generated_artifact
        ):
            local_attempts += 1
    return attempts + local_attempts


def build_spt_prompt(
    target: SptTarget,
    panel: SptPanel,
    additional_instruction: str,
    width: int,
    height: int,
) -> str:
    if width < 1 or height < 1:
        raise ValueError("SPT 작업 패널 크기가 올바르지 않아요")
    lines: list[str] = []
    for region in panel.regions:
        typography = region.typography
        lines.append(
            "- "
            f"{region.region_id}: {json.dumps(region.source_text, ensure_ascii=False)} -> "
            f"{json.dumps(region.final_text_ko, ensure_ascii=False)}; "
            f"occurrences={region.occurrences}; direction={region.direction}; "
            f"font={typography.get('style_class')}; stroke={typography.get('stroke_character')}; "
            f"proportions={typography.get('glyph_proportions')}; "
            f"alignment={typography.get('alignment')}; spacing={typography.get('spacing')}; "
            f"effects={typography.get('effects')}; surface={typography.get('surface_finish')}"
        )
    extra = additional_instruction.strip()
    if len(extra) > 2000:
        raise ValueError("SPT 추가 지시는 2000자 이하여야 해요")
    extra_block = extra if extra else "No additional change beyond the exact localization spec."
    return f"""$imagegen
Use case: text-localization
Asset type: SPT food or drink Texture2D connected-label-face preview
Target: {target.target_id} / {target.name_ko}
Panel: {panel.face}

Image 1 is a temporary, deskewed working panel derived from the immutable source mip 0.
Image 2 is the same-size edit guide. White and gray are editable; black is protected.
Replace only the listed source lettering with the exact Korean text in one image edit call:
{chr(10).join(lines)}

Additional visual correction for this attempt:
{extra_block}

Use the built-in image_gen tool exactly once with num_last_images_to_include=2.
Never pass referenced_image_paths; use only the two images already attached to this turn.
Return exactly one raster image with the same crop, aspect ratio ({width}:{height}), alignment, and orientation as Image 1.
Preserve every non-text pixel, small legal/ingredient/nutrition/address/barcode/certification/date label, artwork, logo shape, label geometry, material, wear, folds, shadows, and UV edge.
Match font character, stroke, proportions, ink size, baseline, spacing, outline, shadow, layering, and print wear. Render every Korean string verbatim with no extra, missing, or duplicated characters.
Do not draw the guide, selection tint, labels, explanations, borders, or watermarks. Do not regenerate the full texture.
Do not use an API/SDK fallback, OPENAI_API_KEY, shell command, or local image-generation script.
This is an unvalidated visual preview only. Do not modify project files and do not claim candidate approval."""


def _load_profile(root: Path) -> dict[str, Any]:
    if not is_spt_project_root(root):
        raise ValueError("SPT 음식 텍스처 저장소 루트를 선택해 주세요")
    value = _read_json(root / "profiles" / "food" / "collection.json")
    if not isinstance(value, dict) or not isinstance(value.get("targets"), list):
        raise ValueError("profiles/food/collection.json 형식이 올바르지 않아요")
    return value


def _artifact_from_descriptor(
    root: Path,
    descriptor: Any,
    label: str,
    *,
    require_size: bool,
) -> SptArtifact:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{label}: 파일 명세가 없어요")
    project_path = descriptor.get("image") if label == "source" else descriptor.get("path")
    if not isinstance(project_path, str) or not project_path.strip():
        raise ValueError(f"{label}: 프로젝트 상대 경로가 없어요")
    path = (root / project_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}: 프로젝트 밖 경로는 사용할 수 없어요") from exc
    if not path.is_file():
        raise ValueError(f"{label}: 파일이 없어요: {path}")
    expected_sha = descriptor.get("sha256")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
        raise ValueError(f"{label}: SHA-256 명세가 올바르지 않아요")
    actual_sha = _sha256(path)
    if actual_sha.lower() != expected_sha.lower():
        raise ValueError(f"{label}: 현재 파일 SHA가 review.json과 달라요")
    width = descriptor.get("width")
    height = descriptor.get("height")
    if require_size and (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width < 1
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height < 1
    ):
        raise ValueError(f"{label}: 원본 크기 명세가 올바르지 않아요")
    return SptArtifact(path, project_path, actual_sha, int(width), int(height))


def _validate_analysis_with_project_script(root: Path, record: dict[str, Any]) -> list[str]:
    script = (
        root
        / ".agents"
        / "skills"
        / "localize-spt-food-textures"
        / "scripts"
        / "review_record.py"
    )
    if not script.is_file():
        return ["공식 review_record.py 검사기가 없어요"]
    module = _load_module(script)
    validator = getattr(module, "validate_record", None)
    if not callable(validator):
        return ["공식 analysis 검사 함수를 찾지 못했어요"]
    result = validator(record, "analysis", project_root=root)
    return list(result) if isinstance(result, list) else ["analysis 검사 결과 형식이 잘못됐어요"]


def _has_all_mask_descriptors(record: dict[str, Any]) -> bool:
    stages = record.get("stages")
    if not isinstance(stages, dict):
        return False
    edit_plan = stages.get("edit_plan")
    if not isinstance(edit_plan, dict):
        return False
    data = edit_plan.get("data")
    if not isinstance(data, dict):
        return False
    masks = data.get("masks")
    return isinstance(masks, dict) and all(isinstance(masks.get(name), dict) for name in MASK_NAMES)


def _analysis_preflight_gaps(record: dict[str, Any]) -> bool:
    stages = record.get("stages")
    if not isinstance(stages, dict):
        return True
    visual = stages.get("source_visual")
    translation = stages.get("translation")
    if not isinstance(visual, dict) or visual.get("status") != "pass":
        return True
    if not isinstance(translation, dict) or translation.get("status") != "pass":
        return True
    visual_data = visual.get("data")
    if not isinstance(visual_data, dict):
        return True
    if visual_data.get("vision_first") is not True:
        return True
    if not isinstance(visual_data.get("ocr_fallback_required"), bool):
        return True
    regions = visual_data.get("regions")
    if not isinstance(regions, list) or not regions:
        return True
    return any(
        not isinstance(region, dict)
        or not isinstance(region.get("needs_ocr_fallback"), bool)
        or not isinstance(region.get("typography"), dict)
        for region in regions
    )


def _load_module(path: Path) -> ModuleType:
    name = f"golani_spt_review_record_{hashlib.sha256(str(path).encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("공식 review_record.py를 불러오지 못했어요")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded_attempts(edit_data: dict[str, Any]) -> int:
    values: list[int] = []
    top_level = edit_data.get("generation_attempts")
    if isinstance(top_level, int) and not isinstance(top_level, bool) and top_level > 0:
        values.append(top_level)
    compositor = edit_data.get("compositor")
    if isinstance(compositor, dict):
        for region in compositor.get("regions", []):
            if not isinstance(region, dict):
                continue
            value = region.get("generation_attempts")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values.append(value)
    return max(values, default=0)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 아니에요: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
