from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .inventory import _resolve_portable_path
from .paths import discover_project_root, game_bundle_root
from .profile import load_profile


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="한 품목의 원본 추출 → 편집 → 검증 → 테스트 패키지")
    cli.add_argument("--project-root", type=Path)
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="사용 가능한 품목 ID를 확인해요")
    prepare = commands.add_parser("prepare", help="한 품목의 원본과 편집 작업 폴더를 만들어요")
    prepare.add_argument("target")
    prepare.add_argument("--spt-root", default=os.environ.get("SPT_DIR", "D:/SPT"))
    prepare.add_argument("--output", type=Path)
    for name, help_text in [("compose", "컬러 글자 레이어를 원본 위에 합성해요"),
                            ("derive", "같은 글자 알파에서 보조맵 효과를 만들어요")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("job", type=Path)
        command.add_argument("recipe", type=Path)
    check = commands.add_parser("validate", help="크기·알파·변경 영역을 검사하고 비교 이미지를 만들어요")
    check.add_argument("job", type=Path)
    pack = commands.add_parser("package", help="서버 모드를 빌드하고 게임 확인용 패키지를 만들어요")
    pack.add_argument("job", type=Path)
    pack.add_argument("--dotnet", default="dotnet")
    promote = commands.add_parser("release", help="시각·게임 확인 기록이 있는 패키지를 배포본으로 복사해요")
    promote.add_argument("package", type=Path)
    promote.add_argument("--review", type=Path, required=True)
    promote.add_argument("--output", type=Path, required=True)
    return cli


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        root = args.project_root.resolve() if args.project_root else discover_project_root()
        workspace = (root / "workspace").resolve()
        for name in ("job", "output"):
            value = getattr(args, name, None)
            if value is not None:
                value.resolve().relative_to(workspace)
        if args.command in {"list", "prepare"}:
            profile = load_profile(root / "profiles" / "food" / "collection.json")
        if args.command == "list":
            result = [{"id": t.id, "name": t.name_ko, "action": t.action} for t in profile.targets]
        elif args.command == "prepare":
            from .jobs import prepare
            output = args.output or workspace / "items" / args.target
            result = prepare(profile, args.target, game_bundle_root(_resolve_portable_path(args.spt_root)), output)
        elif args.command in {"compose", "derive"}:
            from .editing import compose, derive
            result = (compose if args.command == "compose" else derive)(args.job, args.recipe)
        elif args.command == "validate":
            from .validation import validate
            result = validate(args.job)
        elif args.command == "package":
            from .packaging import package
            result = package(args.job, root, dotnet=args.dotnet)
            result = {k: v for k, v in result.items() if k not in {"files", "bundles", "inputs"}}
        else:
            from .packaging import release
            result = release(args.package, args.review, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("passed") is False else 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"작업을 완료하지 못했어요: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
