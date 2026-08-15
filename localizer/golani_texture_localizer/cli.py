from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

from .bundles import repack_collection
from .candidate import audit_candidate
from .compose import compose_candidate
from .deployment import create_release, deploy_release
from .images import create_review_sheets, stage_candidate, validate_approved
from .inventory import load_inventory, register_source_override, scan_collection
from .legacy import create_legacy_layout_sheet
from .masking import create_old_text_mask
from .materials import derive_approved_materials
from .ocr import OcrSession, ocr_doctor, reusable_ocr_report, run_ocr, setup_ocr_models
from .paths import ProjectPaths, discover_project_root, game_bundle_root
from .profile import load_profile
from .uv import generate_uv_review
from .visual import create_visual_transcription_sheet


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="golani-localize")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--spt-root", type=Path, default=Path(os.environ.get("SPT_DIR", "D:/SPT")))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inventory", help="번들을 읽되 PNG는 만들지 않고 Texture2D 목록만 기록해요")
    subparsers.add_parser("extract", help="전체 대상 번들의 Texture2D를 workspace/source에 추출해요")

    stage = subparsers.add_parser("stage", help="시안을 원본 규격으로 검사해 승인 폴더에 넣어요")
    stage.add_argument("target_id")
    stage.add_argument("image", type=Path)

    ocr = subparsers.add_parser("ocr", help="PaddleOCR와 EasyOCR로 품목 문자를 교차 판독해요")
    ocr_subparsers = ocr.add_subparsers(dest="ocr_command", required=True)
    ocr_subparsers.add_parser("doctor", help="OCR 패키지와 모델 준비 상태를 확인해요")
    ocr_subparsers.add_parser("setup", help="공식 OCR 모델을 내려받아 로컬 캐시에 준비해요")
    ocr_run = ocr_subparsers.add_parser("run", help="원본 또는 후보 이미지를 실제 판독해요")
    ocr_run.add_argument("target_id")
    ocr_run.add_argument("--phase", choices=("source", "candidate"), required=True)
    ocr_run.add_argument("--image", type=Path)
    ocr_run.add_argument("--output", type=Path)
    ocr_batch = ocr_subparsers.add_parser(
        "batch", help="모델을 한 번만 올려 여러 품목을 연속 판독해요"
    )
    ocr_batch.add_argument("--phase", choices=("source", "candidate"), required=True)
    ocr_batch.add_argument("--target", action="append", dest="target_ids")
    ocr_batch.add_argument(
        "--reference-approved",
        action="store_true",
        help="과거 승인본을 조판 참고용으로 판독하며 실제 후보 보고서와 분리해요",
    )
    ocr_batch.add_argument(
        "--force",
        action="store_true",
        help="입력 SHA와 OCR 엔진 서명이 같은 완료 보고서도 다시 판독해요",
    )

    source_override = subparsers.add_parser(
        "source-override",
        help="이미 수정된 라이브 번들 대신 검증된 원본 PNG·번들을 기준으로 등록해요",
    )
    source_override.add_argument("target_id")
    source_override.add_argument("image", type=Path)
    source_override.add_argument("bundle", type=Path)

    sheets = subparsers.add_parser("review-sheets", help="원본 또는 승인본 검수 시트를 만들어요")
    sheets.add_argument("--approved", action="store_true")

    uv_review = subparsers.add_parser(
        "uv-review", help="실제 Renderer Mesh에서 절취선·UV 경계 보호 마스크를 만들어요"
    )
    uv_review.add_argument("--target", action="append", dest="target_ids")
    uv_review.add_argument("--padding", type=int, default=4)

    visual_sheets = subparsers.add_parser(
        "visual-sheets", help="OCR 문자열을 숨긴 독립 시각 판독용 확대 시트를 만들어요"
    )
    visual_sheets.add_argument("--target", action="append", dest="target_ids")

    compose = subparsers.add_parser(
        "compose", help="해시 고정 마스크·글꼴 recipe로 한글 후보를 결정적으로 조판해요"
    )
    compose.add_argument("target_id")
    compose.add_argument("recipe", type=Path)

    mask = subparsers.add_parser(
        "mask",
        help="좌표·색 조건과 실제 UV seam으로 원문 글자 마스크를 결정적으로 만들어요",
    )
    mask.add_argument("target_id")
    mask.add_argument("recipe", type=Path)

    candidate_check = subparsers.add_parser(
        "candidate-check",
        help="조판·마스크·후보 OCR을 해시로 묶고 시각 비교 시트를 만들어요",
    )
    candidate_check.add_argument("target_id")

    legacy_sheets = subparsers.add_parser(
        "legacy-layout-sheets",
        help="과거 한글본은 픽셀 재사용 없이 번역·배치 참고 시트로만 만들어요",
    )
    legacy_sheets.add_argument("--target", action="append", dest="target_ids")

    subparsers.add_parser("validate", help="승인된 전체 이미지의 크기·알파·변경 여부를 검사해요")
    derive = subparsers.add_parser(
        "derive", help="승인된 diffuse를 기준으로 normal·gloss 인쇄 위치를 이식해요"
    )
    derive.add_argument("--target", action="append", dest="target_ids")
    repack = subparsers.add_parser("repack", help="승인된 이미지만 원본 UnityFS payload에 패치해요")
    repack.add_argument("--max-mae", type=float, default=6.0)
    repack.add_argument("--target", action="append", dest="target_ids")
    subparsers.add_parser("release", help="모든 렌더·밉·번들 검증을 통과한 해시 고정본을 만들어요")
    deploy = subparsers.add_parser("deploy", help="검증된 release만 설치 계획 또는 실제 설치로 적용해요")
    deploy.add_argument("--release", default="latest")
    deploy.add_argument("--execute", action="store_true")
    subparsers.add_parser("status", help="전체 대상과 승인 진행률을 보여줘요")
    return parser


def _context(args: argparse.Namespace):
    root = (args.project_root or discover_project_root()).expanduser().resolve()
    profile_path = (args.profile or (root / "profiles" / "food" / "collection.json")).resolve()
    profile = load_profile(profile_path)
    paths = ProjectPaths.create(root, args.workspace)
    return profile, paths


def _candidate_ocr_regions(paths: ProjectPaths, target_id: str) -> list[dict] | None:
    path = paths.reviews / target_id / "review.json"
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    translation = record.get("stages", {}).get("translation", {})
    regions = translation.get("data", {}).get("regions", [])
    if translation.get("status") != "pass" or not isinstance(regions, list):
        return None
    selected = [
        region
        for region in regions
        if isinstance(region, dict) and region.get("ocr_required") is True
    ]
    return selected or None


def _print(value) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    profile, paths = _context(args)

    if args.command in {"inventory", "extract"}:
        bundle_root = game_bundle_root(args.spt_root)
        result = scan_collection(profile, bundle_root, paths, extract=args.command == "extract")
        _print(
            {
                "inventory": str(paths.inventory),
                "bundles": len(profile.bundles),
                "textures": len(result["records"]),
                "targets": sum(record.get("target_id") is not None for record in result["records"]),
                "missing_bundles": result["missing_bundles"],
            }
        )
        return

    if args.command == "stage":
        _print(stage_candidate(profile, paths, args.target_id, args.image.resolve()))
        return

    if args.command == "ocr":
        if args.ocr_command == "doctor":
            result = ocr_doctor(paths.root)
            _print(result)
            if not result["offline_ready"]:
                raise SystemExit(2)
            return
        if args.ocr_command == "setup":
            _print(setup_ocr_models(paths.root))
            return
        if args.ocr_command == "batch":
            inventory = load_inventory(paths.inventory)
            selected = (
                [profile.target_by_id(target_id) for target_id in args.target_ids]
                if args.target_ids
                else [target for target in profile.targets if target.action == "localize"]
            )
            session = OcrSession(paths.root, phase=args.phase)
            reports = []
            from .inventory import record_for_target

            for target in selected:
                if args.reference_approved:
                    if args.phase != "candidate":
                        raise ValueError("--reference-approved는 candidate phase에서만 사용할 수 있어요")
                    image = paths.approved / f"{target.id}.png"
                    report_name = "legacy-approved-ocr.json"
                elif args.phase == "source":
                    image = Path(record_for_target(inventory, target)["source_png"])
                    report_name = "source-ocr.json"
                else:
                    image = paths.drafts / target.id / "candidate.png"
                    report_name = "candidate-ocr.json"
                if not image.is_file():
                    reports.append(
                        {"target_id": target.id, "passed": False, "reason": f"이미지가 없어요: {image}"}
                    )
                    continue
                output = paths.reviews / target.id / report_name
                regions = (
                    _candidate_ocr_regions(paths, target.id)
                    if args.phase == "candidate"
                    else None
                )
                cached = (
                    None
                    if args.force
                    else reusable_ocr_report(session, image, output, regions=regions)
                )
                result = cached or session.run(image, output, regions=regions)
                reports.append(
                    {
                        "target_id": target.id,
                        "passed": not result["errors"],
                        "report": str(output),
                        "reused": cached is not None,
                        "image_sha256": result["image_sha256"],
                        "detections": len(result["detections"]),
                        "conflicting_regions": sum(
                            bool(detection.get("conflicting_readings"))
                            for detection in result["detections"]
                        ),
                        "errors": len(result["errors"]),
                    }
                )
            _print(
                {
                    "phase": args.phase,
                    "target_count": len(selected),
                    "passed": all(report["passed"] for report in reports),
                    "reports": reports,
                    "requires_independent_visual_review": True,
                }
            )
            if not all(report["passed"] for report in reports):
                raise SystemExit(2)
            return
        target = profile.target_by_id(args.target_id)
        if args.image:
            image = args.image.expanduser().resolve()
        elif args.phase == "source":
            inventory = load_inventory(paths.inventory)
            from .inventory import record_for_target

            image = Path(record_for_target(inventory, target)["source_png"])
        else:
            image = paths.drafts / target.id / "candidate.png"
        output = (
            args.output.expanduser().resolve()
            if args.output
            else paths.reviews / target.id / f"{args.phase}-ocr.json"
        )
        regions = (
            _candidate_ocr_regions(paths, target.id)
            if args.phase == "candidate"
            else None
        )
        result = run_ocr(paths.root, image, output, phase=args.phase, regions=regions)
        _print(
            {
                "target_id": target.id,
                "phase": args.phase,
                "report": str(output),
                "image_sha256": result["image_sha256"],
                "detections": len(result["detections"]),
                "conflicting_regions": sum(
                    bool(detection.get("conflicting_readings"))
                    for detection in result["detections"]
                ),
                "errors": len(result["errors"]),
                "requires_independent_visual_review": True,
            }
        )
        return

    if args.command == "source-override":
        _print(
            register_source_override(
                profile,
                paths,
                args.target_id,
                args.image,
                args.bundle,
            )
        )
        return

    if args.command == "review-sheets":
        outputs = create_review_sheets(profile, paths, approved=args.approved)
        _print({"sheets": [str(path) for path in outputs]})
        return

    if args.command == "uv-review":
        inventory = load_inventory(paths.inventory)
        target_ids = args.target_ids or [target.id for target in profile.targets]
        reports = [
            generate_uv_review(paths, inventory, target_id, padding=args.padding)
            for target_id in target_ids
        ]
        _print(
            {
                "target_count": len(reports),
                "passed": all(report["passed"] for report in reports),
                "reports": reports,
            }
        )
        return

    if args.command == "visual-sheets":
        inventory = load_inventory(paths.inventory)
        from .inventory import record_for_target

        selected = (
            [profile.target_by_id(target_id) for target_id in args.target_ids]
            if args.target_ids
            else [target for target in profile.targets if target.action == "localize"]
        )
        reports = []
        for target in selected:
            source = Path(record_for_target(inventory, target)["source_png"])
            reports.append(
                create_visual_transcription_sheet(
                    paths,
                    target.id,
                    source,
                    paths.reviews / target.id / "source-ocr.json",
                )
            )
        _print({"target_count": len(reports), "passed": True, "reports": reports})
        return

    if args.command == "compose":
        _print(compose_candidate(paths, args.target_id, args.recipe.expanduser().resolve()))
        return

    if args.command == "mask":
        _print(create_old_text_mask(paths, args.target_id, args.recipe.expanduser().resolve()))
        return

    if args.command == "candidate-check":
        target = profile.target_by_id(args.target_id)
        result = audit_candidate(target, paths)
        _print(result)
        if not result["passed"]:
            raise SystemExit(2)
        return

    if args.command == "legacy-layout-sheets":
        inventory = load_inventory(paths.inventory)
        from .inventory import record_for_target

        selected = (
            [profile.target_by_id(target_id) for target_id in args.target_ids]
            if args.target_ids
            else [target for target in profile.targets if target.action == "localize"]
        )
        reports = []
        missing = []
        for target in selected:
            ocr_report = paths.reviews / target.id / "legacy-approved-ocr.json"
            legacy = paths.approved / f"{target.id}.png"
            if not ocr_report.is_file() or not legacy.is_file():
                missing.append(target.id)
                continue
            source = Path(record_for_target(inventory, target)["source_png"])
            reports.append(
                create_legacy_layout_sheet(
                    paths,
                    target.id,
                    source,
                    legacy,
                    ocr_report,
                )
            )
        _print(
            {
                "target_count": len(selected),
                "created": len(reports),
                "missing": missing,
                "passed": not missing,
                "reports": reports,
            }
        )
        if missing:
            raise SystemExit(2)
        return

    if args.command == "validate":
        result = validate_approved(profile, paths)
        _print(result)
        if not result["passed"]:
            raise SystemExit(2)
        return

    if args.command == "derive":
        if args.target_ids:
            for target_id in args.target_ids:
                profile.target_by_id(target_id)
        _print(derive_approved_materials(profile, paths, target_ids=args.target_ids))
        return

    if args.command == "repack":
        if args.target_ids:
            for target_id in args.target_ids:
                profile.target_by_id(target_id)
        bundle_root = game_bundle_root(args.spt_root)
        _print(
            repack_collection(
                profile,
                paths,
                bundle_root,
                max_mae=args.max_mae,
                target_ids=args.target_ids,
            )
        )
        return

    if args.command == "release":
        _print(create_release(profile, paths))
        return

    if args.command == "deploy":
        _print(
            deploy_release(
                paths,
                args.spt_root,
                release_id=args.release,
                execute=args.execute,
            )
        )
        return

    if args.command == "status":
        inventory = load_inventory(paths.inventory)
        localized = {target.id for target in profile.targets if target.action == "localize"}
        approved = {
            path.name.removesuffix(".approval.json")
            for path in paths.approved.glob("*.approval.json")
            if (paths.approved / path.name.removesuffix(".approval.json")).with_suffix(".png").is_file()
        } if paths.approved.is_dir() else set()
        stage_counts: dict[str, Counter[str]] = {}
        for target in profile.targets:
            review_file = paths.reviews / target.id / "review.json"
            if not review_file.is_file():
                continue
            review = json.loads(review_file.read_text(encoding="utf-8"))
            for name, stage in review.get("stages", {}).items():
                if isinstance(stage, dict):
                    stage_counts.setdefault(name, Counter())[str(stage.get("status", "missing"))] += 1
        _print(
            {
                "collection": profile.id,
                "bundle_count": len(profile.bundles),
                "inventory_texture_count": len(inventory["records"]),
                "target_count": len(profile.targets),
                "localization_count": sum(target.action == "localize" for target in profile.targets),
                "preserved_count": sum(target.action == "preserve" for target in profile.targets),
                "approved_count": len(approved & localized),
                "source_ocr_reports": len(list(paths.reviews.glob("*/source-ocr.json"))),
                "independent_visual_sheets": len(
                    list(paths.reviews.glob("*/visual-source-index.json"))
                ),
                "uv_mesh_reviews": len(list(paths.reviews.glob("*/uv-report.json"))),
                "legacy_layout_references": len(
                    list(paths.reviews.glob("*/legacy-layout-index.json"))
                ),
                "review_stage_status": {
                    name: dict(sorted(counts.items()))
                    for name, counts in sorted(stage_counts.items())
                },
                "pending": [
                    target.id
                    for target in profile.targets
                    if target.action == "localize" and target.id not in approved
                ],
            }
        )
        return

    raise AssertionError(args.command)
