from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .bundles import repack_collection
from .images import create_review_sheets, stage_candidate, validate_approved
from .inventory import load_inventory, register_source_override, scan_collection
from .materials import derive_approved_materials
from .paths import ProjectPaths, discover_project_root, game_bundle_root
from .profile import load_profile


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
    stage.add_argument("--allow-resize", action="store_true")

    source_override = subparsers.add_parser(
        "source-override",
        help="이미 수정된 라이브 번들 대신 검증된 원본 PNG·번들을 기준으로 등록해요",
    )
    source_override.add_argument("target_id")
    source_override.add_argument("image", type=Path)
    source_override.add_argument("bundle", type=Path)

    sheets = subparsers.add_parser("review-sheets", help="원본 또는 승인본 검수 시트를 만들어요")
    sheets.add_argument("--approved", action="store_true")

    subparsers.add_parser("validate", help="승인된 전체 이미지의 크기·알파·변경 여부를 검사해요")
    subparsers.add_parser("derive", help="승인된 diffuse를 기준으로 normal·gloss 인쇄 위치를 이식해요")
    repack = subparsers.add_parser(
        "repack",
        help="승인 이미지를 UnityFS payload에 패치하고 CAB 충돌을 방지해요",
    )
    repack.add_argument("--allow-partial", action="store_true")
    repack.add_argument("--max-mae", type=float, default=6.0)
    subparsers.add_parser("status", help="전체 대상과 승인 진행률을 보여줘요")
    return parser


def _context(args: argparse.Namespace):
    root = (args.project_root or discover_project_root()).expanduser().resolve()
    profile_path = (args.profile or (root / "profiles" / "food" / "collection.json")).resolve()
    profile = load_profile(profile_path)
    paths = ProjectPaths.create(root, args.workspace)
    return profile, paths


def _print(value) -> None:
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
        _print(stage_candidate(profile, paths, args.target_id, args.image.resolve(), allow_resize=args.allow_resize))
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

    if args.command == "validate":
        _print(validate_approved(profile, paths))
        return

    if args.command == "derive":
        _print(derive_approved_materials(profile, paths))
        return

    if args.command == "repack":
        bundle_root = game_bundle_root(args.spt_root)
        _print(
            repack_collection(
                profile,
                paths,
                bundle_root,
                allow_partial=args.allow_partial,
                max_mae=args.max_mae,
            )
        )
        return

    if args.command == "status":
        inventory = load_inventory(paths.inventory)
        approved = {path.stem for path in paths.approved.glob("*.png")} if paths.approved.is_dir() else set()
        _print(
            {
                "collection": profile.id,
                "bundle_count": len(profile.bundles),
                "inventory_texture_count": len(inventory["records"]),
                "target_count": len(profile.targets),
                "localization_count": sum(target.action == "localize" for target in profile.targets),
                "preserved_count": sum(target.action == "preserve" for target in profile.targets),
                "approved_count": len(
                    approved & {target.id for target in profile.targets if target.action == "localize"}
                ),
                "pending": [
                    target.id
                    for target in profile.targets
                    if target.action == "localize" and target.id not in approved
                ],
            }
        )
        return

    raise AssertionError(args.command)
