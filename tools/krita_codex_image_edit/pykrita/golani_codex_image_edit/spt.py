from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from types import ModuleType
from typing import Any, Mapping, Sequence
from uuid import uuid4
import zlib

from .core import safe_stem, validate_spt_mask_contract


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
class SptFreeEditTarget:
    project_root: Path
    target_id: str
    name_ko: str
    texture: str
    bundle_key: str
    profile_path: Path
    profile_sha256: str
    review_path: Path
    review_sha256: str
    source: SptArtifact


@dataclass(frozen=True)
class SptPreparation:
    project_root: Path
    target_id: str
    name_ko: str
    review_path: Path
    review_sha256: str
    source: SptArtifact
    masks: dict[str, SptArtifact]
    analysis_errors: tuple[str, ...]
    mask_error: str | None
    recorded_generation_attempts: int
    all_panel_budgets_exhausted: bool

    @property
    def attempt_budget_exhausted(self) -> bool:
        return self.all_panel_budgets_exhausted

    @property
    def ready(self) -> bool:
        return (
            not self.analysis_errors
            and self.mask_error is None
            and not self.attempt_budget_exhausted
        )


def spt_working_view_path(
    project_root: Path,
    target_id: str,
    source: SptArtifact,
) -> Path:
    """Return a non-aliased path reserved for a display-only SPT working view."""

    digest = source.sha256.lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("SPT 원본 SHA-256 기록이 잘못됐어요")
    root = project_root.resolve()
    view_root = root / "workspace" / "krita-spt" / "view-sources"
    path = (
        view_root
        / safe_stem(target_id, "target")
        / f"{safe_stem(source.path.stem, 'source')}.{digest[:16]}.rgb-opaque"
        / "view.png"
    )
    try:
        path.relative_to(view_root)
        resolved = path.resolve()
        resolved.relative_to(view_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("SPT 작업 뷰 경로가 전용 폴더 밖을 가리켜요") from exc
    if resolved != path:
        raise ValueError("SPT 작업 뷰 경로에 심볼릭 링크나 junction을 사용할 수 없어요")
    if path.exists() and source.path.exists():
        try:
            aliases_source = path.samefile(source.path)
        except OSError as exc:
            raise ValueError("SPT 작업 뷰와 불변 원본의 파일 identity를 확인하지 못했어요") from exc
        if aliases_source:
            raise ValueError("SPT 작업 뷰가 불변 원본 파일을 직접 가리킬 수 없어요")
    return path


def build_preview_choice_record(
    spt: Mapping[str, Any],
    generation: Mapping[str, Any],
    *,
    status: str,
    created_at: str,
    request_sha256: str,
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
    alpha_semantics = required_text(spt.get("alpha_semantics"), "alpha semantics")
    if alpha_semantics != "material":
        raise ValueError("SPT 원본 alpha semantics는 material이어야 해요")
    working_view_transform = required_text(
        spt.get("working_view_transform"),
        "working view transform",
    )
    if working_view_transform != "source-rgb-force-alpha-255:v1":
        raise ValueError("SPT 불투명 RGB 작업 뷰 변환 기록이 잘못됐어요")
    working_view = spt.get("working_view")
    if not isinstance(working_view, Mapping):
        raise ValueError("SPT 불투명 RGB 작업 뷰 기록이 없어요")
    model_input = spt.get("model_input")
    if not isinstance(model_input, Mapping):
        raise ValueError("SPT imagegen 입력 해시 기록이 없어요")
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
        "schema_version": 3,
        "status": status,
        "purpose": "human-visual-selection",
        "created_at": created_at,
        "target_id": required_text(spt.get("target_id"), "target_id"),
        "panel_id": required_text(spt.get("panel_id"), "panel_id"),
        "review_sha256": required_sha256(spt.get("review_sha256"), "review"),
        "source_sha256": required_sha256(spt.get("source_sha256"), "source"),
        "alpha_semantics": alpha_semantics,
        "working_view_transform": working_view_transform,
        "working_view_path": required_text(
            working_view.get("path"),
            "working view path",
        ),
        "working_view_sha256": required_sha256(
            working_view.get("file_sha256"),
            "working view",
        ),
        "model_input": {
            "source_file_sha256": required_sha256(
                model_input.get("source_file_sha256"),
                "model source file",
            ),
            "source_pixel_sha256": required_sha256(
                model_input.get("source_pixel_sha256"),
                "model source pixels",
            ),
            "selection_mask_file_sha256": required_sha256(
                model_input.get("selection_mask_file_sha256"),
                "model selection mask file",
            ),
            "selection_mask_pixel_sha256": required_sha256(
                model_input.get("selection_mask_pixel_sha256"),
                "model selection mask pixels",
            ),
        },
        "mask_sha256": mask_sha256,
        "generated_sha256": required_sha256(artifact.get("sha256"), "generated image"),
        "request": "request.json",
        "request_sha256": required_sha256(request_sha256, "request"),
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
    status: str
    issues: tuple[str, ...] = ()
    issue_codes: tuple[str, ...] = ()
    review_sha256: str | None = None
    profile_sha256: str | None = None
    recorded_generation_attempts: int = 0
    attempt_budget_exhausted: bool = False
    artifact_pins: tuple[tuple[str, str], ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def preparation_required(self) -> bool:
        return self.status not in {"ready", "preserve"}


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
    profile, profile_sha256 = _load_profile_snapshot(root)
    summaries: list[SptTargetSummary] = []
    for target in profile["targets"]:
        target_id = str(target.get("id", "")).strip()
        name_ko = str(target.get("name_ko", target_id)).strip() or target_id
        action = target.get("action", "localize")
        review_path = root / "workspace" / "reviews" / target_id / "review.json"
        if action != "localize":
            summaries.append(
                SptTargetSummary(
                    target_id,
                    name_ko,
                    "보존 대상",
                    "preserve",
                    profile_sha256=profile_sha256,
                )
            )
            continue

        review_sha256 = _sha256(review_path) if review_path.is_file() else None
        try:
            preparation = inspect_spt_target(root, target_id, profile=profile)
        except Exception as exc:
            # Final-validation diagnostics are advisory in the free-edit picker.
            # Isolate every target-local failure so one broken analysis cannot hide
            # unrelated immutable sources from the preview-only workflow.
            recorded_attempts = _recorded_attempts_from_review(review_path)
            budget_exhausted = recorded_attempts >= MAX_GENERATION_ATTEMPTS
            issues = [str(exc)]
            issue_codes = ["record-or-source-error"]
            if budget_exhausted:
                issues.append(
                    f"기록된 생성 시도 {recorded_attempts}회가 기본 예산 "
                    f"{MAX_GENERATION_ATTEMPTS}회를 넘었어요"
                )
                issue_codes.append("attempt-budget-exhausted")
            summaries.append(
                SptTargetSummary(
                    target_id,
                    name_ko,
                    "원본·기록 확인 필요",
                    "record-or-source-error",
                    tuple(issues),
                    tuple(issue_codes),
                    review_sha256=review_sha256,
                    profile_sha256=profile_sha256,
                    recorded_generation_attempts=recorded_attempts,
                    attempt_budget_exhausted=budget_exhausted,
                )
            )
            continue

        analysis_required = bool(preparation.analysis_errors)
        masks_required = preparation.mask_error is not None
        issues = list(preparation.analysis_errors)
        issue_codes: list[str] = []
        if analysis_required:
            issue_codes.append("analysis-required")
        if preparation.mask_error is not None:
            issues.append(preparation.mask_error)
            issue_codes.append("masks-required")
        if preparation.attempt_budget_exhausted:
            issues.append(
                f"기록된 생성 시도 {preparation.recorded_generation_attempts}회가 기본 예산 "
                f"{MAX_GENERATION_ATTEMPTS}회를 넘었어요"
            )
            issue_codes.append("attempt-budget-exhausted")
        if analysis_required and masks_required:
            state = "analysis·마스크 갱신 필요"
            status = "analysis-and-masks-required"
        elif analysis_required:
            state = "analysis 갱신 필요"
            status = "analysis-required"
        elif masks_required:
            state = "마스크 갱신 필요"
            status = "masks-required"
        elif preparation.attempt_budget_exhausted:
            state = "생성 예산 승인 필요"
            status = "attempt-budget-exhausted"
        else:
            state = "생성 준비됨"
            status = "ready"
        if preparation.attempt_budget_exhausted and status != "attempt-budget-exhausted":
            state += " · 생성 예산 승인 필요"
        summaries.append(
            SptTargetSummary(
                target_id,
                name_ko,
                state,
                status,
                tuple(issues),
                tuple(issue_codes),
                review_sha256=preparation.review_sha256,
                profile_sha256=profile_sha256,
                recorded_generation_attempts=preparation.recorded_generation_attempts,
                attempt_budget_exhausted=preparation.attempt_budget_exhausted,
                artifact_pins=(
                    (preparation.source.project_path, preparation.source.sha256),
                    *(
                        (artifact.project_path, artifact.sha256)
                        for artifact in preparation.masks.values()
                    ),
                ),
            )
        )
    return tuple(summaries)


def build_spt_preparation_request(
    root: Path,
    summaries: Sequence[SptTargetSummary],
    *,
    created_at: str,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    if not created_at.strip():
        raise ValueError("SPT 전체 준비 요청 생성 시간이 없어요")
    profile_path = root / "profiles" / "food" / "collection.json"
    profile_sha256 = _sha256(profile_path)
    targets: list[dict[str, Any]] = []
    for summary in summaries:
        if summary.profile_sha256 != profile_sha256:
            raise ValueError("profile이 새로고침 뒤 바뀌었어요. 품목 목록을 다시 새로고침해 주세요")
        if summary.status == "preserve":
            continue
        review_path = root / "workspace" / "reviews" / summary.target_id / "review.json"
        current_review_sha = _sha256(review_path) if review_path.is_file() else None
        if current_review_sha != summary.review_sha256:
            raise ValueError(
                f"{summary.target_id} review.json이 새로고침 뒤 바뀌었어요. "
                "품목 목록을 다시 새로고침해 주세요"
            )
        for project_path, expected_sha in summary.artifact_pins:
            current_path = _project_relative_file(root, project_path, "scan artifact")
            if not current_path.is_file() or _sha256(current_path) != expected_sha:
                raise ValueError(
                    f"{summary.target_id} 원본 또는 마스크가 새로고침 뒤 바뀌었어요. "
                    "품목 목록을 다시 새로고침해 주세요"
                )
        if not summary.preparation_required:
            continue
        source: dict[str, Any] | None = None
        masks: dict[str, Any] = {}
        evidence_issue_codes: list[str] = []
        if review_path.is_file():
            try:
                record, record_sha = _read_json_snapshot(review_path)
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                record = {}
                record_sha = current_review_sha
            if record_sha != current_review_sha:
                raise ValueError(
                    f"{summary.target_id} review.json이 요청 기록 중 바뀌었어요. "
                    "품목 목록을 다시 새로고침해 주세요"
                )
            source_data = record.get("source")
            if isinstance(source_data, dict):
                source, artifact_issue = _preparation_artifact_pin(
                    root,
                    source_data,
                    path_field="image",
                    issue_prefix="source",
                )
                evidence_issue_codes.extend(artifact_issue)
            else:
                evidence_issue_codes.append("source-descriptor-missing")
            stages = record.get("stages")
            edit_plan = stages.get("edit_plan") if isinstance(stages, dict) else None
            edit_data = edit_plan.get("data") if isinstance(edit_plan, dict) else None
            mask_data = edit_data.get("masks") if isinstance(edit_data, dict) else None
            if isinstance(mask_data, dict):
                for name in MASK_NAMES:
                    descriptor = mask_data.get(name)
                    if isinstance(descriptor, dict):
                        pin, artifact_issue = _preparation_artifact_pin(
                            root,
                            descriptor,
                            path_field="path",
                            issue_prefix=f"mask-{name}",
                        )
                        masks[name] = pin
                        evidence_issue_codes.extend(artifact_issue)
                    else:
                        evidence_issue_codes.append(f"mask-{name}-descriptor-missing")
            else:
                evidence_issue_codes.append("mask-descriptors-missing")
        requested_work: list[str] = []
        if summary.status == "record-or-source-error":
            requested_work.append("source-and-review-audit")
        if summary.status in {
            "analysis-required",
            "analysis-and-masks-required",
            "record-or-source-error",
        }:
            requested_work.append("analysis")
        if summary.status in {
            "masks-required",
            "analysis-and-masks-required",
            "record-or-source-error",
        }:
            requested_work.append("five-masks")
        if summary.attempt_budget_exhausted:
            requested_work.append("generation-budget-review")
        if review_path.is_file() and _sha256(review_path) != current_review_sha:
            raise ValueError(
                f"{summary.target_id} review.json이 요청 기록 중 바뀌었어요. "
                "품목 목록을 다시 새로고침해 주세요"
            )
        targets.append(
            {
                "target_id": summary.target_id,
                "name_ko": summary.name_ko,
                "scan_status": summary.status,
                "issues": list(summary.issues),
                "issue_codes": list(dict.fromkeys((*summary.issue_codes, *evidence_issue_codes))),
                "requested_work": requested_work,
                "review": {
                    "path": review_path.relative_to(root).as_posix(),
                    "sha256": current_review_sha,
                },
                "source_from_review": source,
                "masks_from_review": masks,
                "generation_budget": {
                    "default_limit": MAX_GENERATION_ATTEMPTS,
                    "recorded_attempts": summary.recorded_generation_attempts,
                    "exhausted": summary.attempt_budget_exhausted,
                    "additional_attempts_approved": False,
                    "attempt_counter_reset": False,
                    "this_request_is_not_approval": True,
                },
            }
        )
    if not targets:
        raise ValueError("전체 준비 요청에 넣을 잠긴 품목이 없어요")
    if _sha256(profile_path) != profile_sha256:
        raise ValueError("profile이 요청 기록 중 바뀌었어요. 품목 목록을 다시 새로고침해 주세요")

    inventory_path = root / "workspace" / "inventory.json"
    inventory = None
    if inventory_path.is_file():
        inventory = {
            "path": inventory_path.relative_to(root).as_posix(),
            "sha256": _sha256(inventory_path),
        }
    return {
        "schema_version": 1,
        "status": "pending",
        "purpose": "spt-analysis-and-mask-preparation",
        "created_at": created_at,
        "project": {
            "profile": {
                "path": profile_path.relative_to(root).as_posix(),
                "sha256": profile_sha256,
            },
            "inventory": inventory,
        },
        "targets": targets,
        "safety": {
            "gate_override": False,
            "generation_allowed": False,
            "one_target_per_validation_unit": True,
            "revalidate_current_files_before_each_target": True,
            "additional_generation_attempts_approved": False,
            "attempt_counters_reset": False,
            "required_gate": "review_record.py --through analysis + current five-mask contract",
        },
    }


def write_spt_preparation_request(
    root: Path,
    summaries: Sequence[SptTargetSummary],
    *,
    created_at: str,
) -> Path:
    root = root.expanduser().resolve()
    record = build_spt_preparation_request(root, summaries, created_at=created_at)
    fingerprint = _preparation_request_fingerprint(record)
    record["request_fingerprint"] = fingerprint
    requests_root = (
        root
        / "workspace"
        / "krita-spt"
        / "preparation-requests"
    ).resolve()
    try:
        requests_root.relative_to(root)
    except ValueError as exc:
        raise ValueError("SPT 전체 준비 요청 경로가 프로젝트 밖을 가리켜요") from exc
    request_path = (
        requests_root
        / fingerprint[:16]
        / "request.json"
    )
    try:
        request_path.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("SPT 전체 준비 요청 파일이 프로젝트 밖을 가리켜요") from exc
    if request_path.is_file():
        existing = _read_json(request_path)
        if existing.get("request_fingerprint") != fingerprint:
            raise ValueError("SPT 전체 준비 요청 경로의 기존 기록과 fingerprint가 달라요")
        if _preparation_request_fingerprint(existing) != fingerprint:
            raise ValueError("기존 SPT 전체 준비 요청 내용이 fingerprint와 달라요")
        return request_path
    request_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = request_path.with_name(f".{request_path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(request_path)
    finally:
        if temporary.is_file():
            temporary.unlink()
    return request_path


def inspect_spt_target(
    root: Path,
    target_id: str,
    *,
    profile: dict[str, Any] | None = None,
) -> SptPreparation:
    root = root.expanduser().resolve()
    if profile is None:
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
    record, review_sha256 = _read_json_snapshot(review_path)
    if record.get("target_id") != target_id:
        raise ValueError("review.json의 target_id가 선택한 품목과 달라요")
    if record.get("action") != profile_target.get("action", "localize"):
        raise ValueError("review.json의 action이 현재 profile과 달라요")
    if record.get("expected_text") != list(profile_target.get("exact_text", [])):
        raise ValueError("review.json의 확정 문구가 현재 profile과 달라요")

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
            mask_pixels = {
                name: _read_grayscale_mask_png(
                    artifact.path,
                    source.width,
                    source.height,
                )
                for name, artifact in masks.items()
            }
            validate_spt_mask_contract(
                mask_pixels["old_text"],
                mask_pixels["new_text"],
                mask_pixels["editable"],
                mask_pixels["protected"],
                mask_pixels["seam_guard"],
                source.width,
                source.height,
            )
        except (OSError, ValueError, TypeError) as exc:
            masks = {}
            mask_error = str(exc)

    translations = stages.get("translation", {}).get("data", {}).get("regions", [])
    legacy_attempts, panel_attempts = _generation_attempts_by_panel(edit_data, translations)
    current_panel_attempts: list[int] = []
    for index, key in enumerate(_panel_keys(translations), start=1):
        panel_id = _spt_panel_id(index, *key)
        current_panel_attempts.append(
            max(
                legacy_attempts,
                panel_attempts.get(key, 0),
                _local_generation_attempts(
                    root,
                    target_id,
                    panel_id,
                    face=key[0],
                    rotation_deg=key[1],
                ),
            )
        )
    recorded_attempts = max(
        (
            legacy_attempts,
            _recorded_attempts(edit_data),
            *panel_attempts.values(),
            *current_panel_attempts,
        ),
        default=0,
    )
    all_exhausted = legacy_attempts >= MAX_GENERATION_ATTEMPTS or (
        bool(current_panel_attempts)
        and all(value >= MAX_GENERATION_ATTEMPTS for value in current_panel_attempts)
    )
    if not current_panel_attempts and recorded_attempts >= MAX_GENERATION_ATTEMPTS:
        all_exhausted = True
    return SptPreparation(
        project_root=root,
        target_id=target_id,
        name_ko=str(profile_target.get("name_ko", target_id)),
        review_path=review_path,
        review_sha256=review_sha256,
        source=source,
        masks=masks,
        analysis_errors=tuple(_validate_analysis_with_project_script(root, record)),
        mask_error=mask_error,
        recorded_generation_attempts=recorded_attempts,
        all_panel_budgets_exhausted=all_exhausted,
    )


def load_spt_free_edit_target(root: Path, target_id: str) -> SptFreeEditTarget:
    """Load only the immutable identity needed for an unvalidated SPT preview."""

    root = root.expanduser().resolve()
    profile, profile_sha256 = _load_profile_snapshot(root)
    profile_target = next(
        (item for item in profile["targets"] if item.get("id") == target_id),
        None,
    )
    if not isinstance(profile_target, dict):
        raise ValueError(f"SPT profile에 대상이 없어요: {target_id}")

    texture = profile_target.get("texture")
    bundle_key = profile_target.get("bundle_key")
    if not isinstance(texture, str) or not texture.strip():
        raise ValueError("SPT profile의 Texture2D 이름이 없어요")
    if not isinstance(bundle_key, str) or not bundle_key.strip():
        raise ValueError("SPT profile의 원본 bundle key가 없어요")

    profile_path = root / "profiles" / "food" / "collection.json"
    review_path = root / "workspace" / "reviews" / target_id / "review.json"
    if not review_path.is_file():
        raise ValueError(f"원본 기록이 없어요: {review_path}")
    record, review_sha256 = _read_json_snapshot(review_path)
    if record.get("target_id") != target_id:
        raise ValueError("review.json의 target_id가 선택한 품목과 달라요")

    source_data = record.get("source")
    if not isinstance(source_data, dict):
        raise ValueError("review.json에 원본 명세가 없어요")
    if source_data.get("bundle_key") != bundle_key:
        raise ValueError("review.json의 원본 bundle key가 profile과 달라요")
    if source_data.get("texture") != texture:
        raise ValueError("review.json의 Texture2D 이름이 profile과 달라요")
    source = _artifact_from_descriptor(root, source_data, "source", require_size=True)

    if _sha256(profile_path) != profile_sha256:
        raise ValueError("profile이 검사 중 바뀌었어요. 품목을 다시 불러와 주세요")
    if _sha256(review_path) != review_sha256:
        raise ValueError("review.json이 검사 중 바뀌었어요. 품목을 다시 불러와 주세요")
    if _sha256(source.path) != source.sha256:
        raise ValueError("source가 검사 중 바뀌었어요. 품목을 다시 불러와 주세요")

    return SptFreeEditTarget(
        project_root=root,
        target_id=target_id,
        name_ko=str(profile_target.get("name_ko", target_id)),
        texture=texture,
        bundle_key=bundle_key,
        profile_path=profile_path,
        profile_sha256=profile_sha256,
        review_path=review_path,
        review_sha256=review_sha256,
        source=source,
    )


def load_spt_target(
    root: Path,
    target_id: str,
    *,
    preparation: SptPreparation | None = None,
) -> SptTarget:
    root = root.expanduser().resolve()
    if preparation is None:
        preparation = inspect_spt_target(root, target_id)
    elif preparation.project_root != root or preparation.target_id != target_id:
        raise ValueError("선택한 품목과 SPT 준비 기록이 서로 달라요")
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
    record, review_sha256 = _read_json_snapshot(preparation.review_path)
    if review_sha256 != preparation.review_sha256:
        raise ValueError("review.json이 검사 중 바뀌었어요. 품목 목록을 다시 불러와 주세요")
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

    grouped: dict[tuple[str, float], list[SptRegion]] = {}
    for region in parsed_regions:
        grouped.setdefault((region.face, region.rotation_deg), []).append(region)
    panels: list[SptPanel] = []
    legacy_attempts, panel_attempts = _generation_attempts_by_panel(edit_data, translations)
    for index, ((face, rotation), regions) in enumerate(grouped.items(), start=1):
        panel_id = _spt_panel_id(index, face, rotation)
        panels.append(
            SptPanel(
                panel_id=panel_id,
                label=f"{face} · {rotation:g}° · {len(regions)}개 문구",
                face=face,
                rotation_deg=rotation,
                regions=tuple(regions),
                recorded_attempts=max(
                    legacy_attempts,
                    panel_attempts.get((face, rotation), 0),
                ),
            )
        )

    return SptTarget(
        project_root=root,
        target_id=target_id,
        name_ko=preparation.name_ko,
        texture=str(record.get("source", {}).get("texture", "")),
        review_path=preparation.review_path,
        review_sha256=preparation.review_sha256,
        source=preparation.source,
        masks=preparation.masks,
        panels=tuple(panels),
    )


def current_generation_attempts(target: SptTarget, panel: SptPanel) -> int:
    return max(
        panel.recorded_attempts,
        _local_generation_attempts(
            target.project_root,
            target.target_id,
            panel.panel_id,
            face=panel.face,
            rotation_deg=panel.rotation_deg,
        ),
    )


def _local_generation_attempts(
    project_root: Path,
    target_id: str,
    panel_id: str,
    *,
    face: str,
    rotation_deg: float,
) -> int:
    jobs_root = (
        project_root
        / "workspace"
        / "krita-spt"
        / target_id
    )
    if not jobs_root.is_dir():
        return 0
    local_attempts = 0
    highest_recorded_attempt = 0
    for request_path in jobs_root.glob("*/*/request.json"):
        try:
            value = _read_json(request_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        spt = value.get("spt")
        if not isinstance(spt, dict):
            continue
        if spt.get("target_id") not in {None, target_id}:
            continue
        transform = spt.get("panel_transform")
        recorded_rotation = (
            transform.get("source_rotation_deg") if isinstance(transform, dict) else None
        )
        same_stable_panel = (
            spt.get("face") == face
            and isinstance(recorded_rotation, (int, float))
            and not isinstance(recorded_rotation, bool)
            and float(recorded_rotation) == float(rotation_deg)
        )
        if spt.get("panel_id") != panel_id and not same_stable_panel:
            continue
        generated = value.get("generation")
        has_generated_artifact = (
            isinstance(generated, dict)
            and isinstance(generated.get("artifact"), dict)
            and isinstance(generated["artifact"].get("sha256"), str)
        ) or (request_path.parent / "generated.png").is_file()
        if has_generated_artifact:
            local_attempts += 1
            recorded_attempt = spt.get("generation_attempt")
            if (
                isinstance(recorded_attempt, int)
                and not isinstance(recorded_attempt, bool)
                and recorded_attempt > 0
            ):
                highest_recorded_attempt = max(
                    highest_recorded_attempt,
                    recorded_attempt,
                )
    return max(local_attempts, highest_recorded_attempt)


def first_available_spt_panel(target: SptTarget) -> SptPanel | None:
    return next(
        (
            panel
            for panel in target.panels
            if current_generation_attempts(target, panel) < MAX_GENERATION_ATTEMPTS
        ),
        None,
    )


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

Image 1 is a temporary, deskewed working panel whose RGB is byte-derived from the immutable source mip 0 and whose display-only alpha is forced to 255.
The immutable source alpha is material data pinned separately and must be restored byte-for-byte downstream; never treat the working-view alpha as output texture data.
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
    return _load_profile_snapshot(root)[0]


def _load_profile_snapshot(root: Path) -> tuple[dict[str, Any], str]:
    if not is_spt_project_root(root):
        raise ValueError("SPT 음식 텍스처 저장소 루트를 선택해 주세요")
    value, sha256 = _read_json_snapshot(root / "profiles" / "food" / "collection.json")
    if not isinstance(value, dict) or not isinstance(value.get("targets"), list):
        raise ValueError("profiles/food/collection.json 형식이 올바르지 않아요")
    return value, sha256


def _preparation_request_fingerprint(record: Mapping[str, Any]) -> str:
    identity = {
        key: value
        for key, value in record.items()
        if key not in {"created_at", "request_fingerprint"}
    }
    packed = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(packed).hexdigest()


def _project_relative_file(root: Path, project_path: Any, label: str) -> Path:
    if not isinstance(project_path, str) or not project_path.strip():
        raise ValueError(f"{label}: 프로젝트 상대 경로가 없어요")
    relative = Path(project_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label}: 프로젝트 밖 경로는 사용할 수 없어요")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}: 프로젝트 밖 경로는 사용할 수 없어요") from exc
    return path


def _read_grayscale_mask_png(path: Path, width: int, height: int) -> bytes:
    payload = path.read_bytes()
    if len(payload) > 32 * 1024 * 1024 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"SPT 마스크가 유효한 PNG가 아니에요: {path}")
    offset = 8
    ihdr: bytes | None = None
    compressed = bytearray()
    saw_end = False
    while offset + 12 <= len(payload):
        length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise ValueError(f"SPT 마스크 PNG chunk가 잘렸어요: {path}")
        chunk = payload[offset + 8 : offset + 8 + length]
        recorded_crc = int.from_bytes(payload[offset + 8 + length : end], "big")
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != recorded_crc:
            raise ValueError(f"SPT 마스크 PNG CRC가 올바르지 않아요: {path}")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise ValueError(f"SPT 마스크 PNG IHDR가 올바르지 않아요: {path}")
            ihdr = chunk
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        offset = end
    if ihdr is None or not compressed or not saw_end:
        raise ValueError(f"SPT 마스크 PNG 구조가 불완전해요: {path}")
    actual_width = int.from_bytes(ihdr[0:4], "big")
    actual_height = int.from_bytes(ihdr[4:8], "big")
    bit_depth, color_type, compression, filtering, interlace = ihdr[8:13]
    if (actual_width, actual_height) != (width, height):
        raise ValueError(f"SPT 마스크 실제 크기가 review.json과 달라요: {path}")
    if color_type != 0 or bit_depth not in {1, 2, 4, 8}:
        raise ValueError(f"SPT 마스크는 단일 채널 grayscale PNG여야 해요: {path}")
    if compression != 0 or filtering != 0 or interlace != 0:
        raise ValueError(f"SPT 마스크 PNG 압축·필터·interlace 형식을 지원하지 않아요: {path}")
    row_bytes = (width * bit_depth + 7) // 8
    expected = (row_bytes + 1) * height
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(compressed), expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unconsumed_tail:
        raise ValueError(f"SPT 마스크 PNG 픽셀 payload 크기가 올바르지 않아요: {path}")

    rows: list[bytearray] = []
    cursor = 0
    previous = bytearray(row_bytes)
    for _ in range(height):
        filter_type = raw[cursor]
        encoded = raw[cursor + 1 : cursor + 1 + row_bytes]
        cursor += row_bytes + 1
        decoded = bytearray(row_bytes)
        for index, value in enumerate(encoded):
            left = decoded[index - 1] if index else 0
            up = previous[index]
            upper_left = previous[index - 1] if index else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                base = left + up - upper_left
                distances = (abs(base - left), abs(base - up), abs(base - upper_left))
                predictor = (left, up, upper_left)[distances.index(min(distances))]
            else:
                raise ValueError(f"SPT 마스크 PNG row filter가 올바르지 않아요: {path}")
            decoded[index] = (value + predictor) & 0xFF
        rows.append(decoded)
        previous = decoded

    pixels = bytearray(width * height)
    max_sample = (1 << bit_depth) - 1
    for y, row in enumerate(rows):
        for x in range(width):
            bit_offset = x * bit_depth
            byte_value = row[bit_offset // 8]
            shift = 8 - bit_depth - (bit_offset % 8)
            sample = (byte_value >> shift) & max_sample
            pixels[y * width + x] = round(sample * 255 / max_sample)
    if any(value not in {0, 255} for value in pixels):
        raise ValueError(f"SPT 마스크는 0과 255만 사용해야 해요: {path}")
    return bytes(pixels)


def _preparation_artifact_pin(
    root: Path,
    descriptor: Mapping[str, Any],
    *,
    path_field: str,
    issue_prefix: str,
) -> tuple[dict[str, Any], list[str]]:
    project_path = descriptor.get(path_field)
    recorded_sha = descriptor.get("sha256")
    pin = {
        "path": project_path,
        "recorded_sha256": recorded_sha,
        "current_sha256": None,
        "fresh": False,
        "width": descriptor.get("width"),
        "height": descriptor.get("height"),
    }
    issues: list[str] = []
    if not isinstance(project_path, str) or not project_path.strip():
        issues.append(f"{issue_prefix}-path-missing")
        return pin, issues
    relative = Path(project_path)
    if relative.is_absolute() or ".." in relative.parts:
        issues.append(f"{issue_prefix}-path-invalid")
        return pin, issues
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        issues.append(f"{issue_prefix}-path-invalid")
        return pin, issues
    if not path.is_file():
        issues.append(f"{issue_prefix}-file-missing")
        return pin, issues
    current_sha = _sha256(path)
    pin["current_sha256"] = current_sha
    if not isinstance(recorded_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", recorded_sha) is None:
        issues.append(f"{issue_prefix}-recorded-sha-invalid")
        return pin, issues
    pin["fresh"] = current_sha.lower() == recorded_sha.lower()
    if not pin["fresh"]:
        issues.append(f"{issue_prefix}-sha-mismatch")
    return pin, issues


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


def _generation_attempts_by_panel(
    edit_data: dict[str, Any],
    translations: Any,
) -> tuple[int, dict[tuple[str, float], int]]:
    top_level = edit_data.get("generation_attempts")
    legacy_attempts = (
        top_level
        if isinstance(top_level, int) and not isinstance(top_level, bool) and top_level > 0
        else 0
    )
    region_attempts: dict[str, int] = {}
    compositor = edit_data.get("compositor")
    if isinstance(compositor, dict):
        for region in compositor.get("regions", []):
            if not isinstance(region, dict):
                continue
            region_id = region.get("region_id")
            attempts = region.get("generation_attempts")
            if (
                isinstance(region_id, str)
                and region_id
                and isinstance(attempts, int)
                and not isinstance(attempts, bool)
                and attempts >= 0
            ):
                region_attempts[region_id] = max(region_attempts.get(region_id, 0), attempts)
    result: dict[tuple[str, float], int] = {}
    if isinstance(translations, list):
        for region in translations:
            if not isinstance(region, dict):
                continue
            region_id = region.get("region_id")
            face = region.get("face")
            rotation = region.get("rotation_deg")
            if (
                not isinstance(region_id, str)
                or not isinstance(face, str)
                or not isinstance(rotation, (int, float))
                or isinstance(rotation, bool)
            ):
                continue
            key = (face, float(rotation))
            result[key] = max(result.get(key, 0), region_attempts.get(region_id, 0))
    return legacy_attempts, result


def _panel_keys(translations: Any) -> tuple[tuple[str, float], ...]:
    keys: list[tuple[str, float]] = []
    if not isinstance(translations, list):
        return ()
    for region in translations:
        if not isinstance(region, dict):
            continue
        face = region.get("face")
        rotation = region.get("rotation_deg")
        if (
            not isinstance(face, str)
            or not isinstance(rotation, (int, float))
            or isinstance(rotation, bool)
        ):
            continue
        key = (face, float(rotation))
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def _spt_panel_id(index: int, face: str, rotation: float) -> str:
    digest = hashlib.sha256(f"{face}\0{rotation}".encode("utf-8")).hexdigest()[:8]
    return f"panel-{index:02d}-{digest}"


def _recorded_attempts_from_review(review_path: Path) -> int:
    if not review_path.is_file():
        return 0
    try:
        record = _read_json(review_path)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return 0
    stages = record.get("stages")
    edit_plan = stages.get("edit_plan") if isinstance(stages, dict) else None
    edit_data = edit_plan.get("data") if isinstance(edit_plan, dict) else None
    return _recorded_attempts(edit_data) if isinstance(edit_data, dict) else 0


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_snapshot(path)[0]


def _read_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 객체가 아니에요: {path}")
    return value, hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
