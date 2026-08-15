from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache import ResultCache
from .config import PROJECT_ROOT, load_config, resolve_project_path
from .engines import (
    create_configured_engines,
    paddle_model_directory_ready,
    package_version,
    package_version_any,
)
from .manifest import discover_sources
from .pipeline import build_plan, scan_sources
from .reporting import CANDIDATE_TIERS, effective_tier, load_results, write_reports
from .scoring import file_sha256, safe_join, sanitize_component


DEFAULT_INPUT = PROJECT_ROOT / "work" / "1_raw"
DEFAULT_MANIFEST = PROJECT_ROOT / "tools" / "map.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "work" / "ocr_selection"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _project_path(value: str | Path) -> Path:
    resolved = resolve_project_path(value, PROJECT_ROOT)
    if resolved is None:
        raise ValueError("빈 경로입니다")
    return resolved


def _source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--input",
        action="append",
        dest="inputs",
        metavar="PATH",
        help="이미지 파일/폴더. 여러 번 지정 가능 (기본: work/1_raw)",
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        help="기존 tools/map.json 또는 범용 JSON/JSONL manifest",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="기본 tools/map.json을 사용하지 않고 입력 폴더를 직접 탐색",
    )
    parser.add_argument(
        "--include-unmanifested",
        action="store_true",
        help="manifest에 없는 입력 폴더 이미지도 포함",
    )
    parser.add_argument("--config", metavar="PATH", help="기본 설정을 덮어쓸 JSON")


def _output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        metavar="PATH",
        help="캐시/실행 결과 루트 (기본: work/ocr_selection)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocr_select",
        description="영어·러시아어가 있는 Tarkov 텍스처만 선별하는 재개 가능한 OCR 도구",
    )
    parser.add_argument("--version", action="version", version="ocr_select 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="패키지와 로컬 모델 준비 상태 확인")
    doctor.add_argument("--config", metavar="PATH")
    doctor.add_argument("--json", action="store_true", dest="as_json")

    plan = subparsers.add_parser(
        "plan", help="이미지를 디코딩하지 않고 manifest/파일명 기준 예정 수량 확인"
    )
    _source_options(plan)
    plan.add_argument("--json", action="store_true", dest="as_json")

    scan = subparsers.add_parser("scan", help="OCR 선별 실행 (명시 확인 필요)")
    _source_options(scan)
    _output_option(scan)
    scan.add_argument("--execute", action="store_true", help="실제 OCR 실행을 명시적으로 허용")
    scan.add_argument("--allow-model-download", action="store_true")
    scan.add_argument(
        "--allow-missing",
        action="store_true",
        help="manifest 누락 입력이 있어도 상세 기록을 남기고 계속",
    )
    scan.add_argument("--force", action="store_true", help="정상 캐시도 무시하고 다시 OCR")
    scan.add_argument("--limit", type=int, help="고유 컬러 텍스처 최대 처리 수")
    scan.add_argument("--run-id", help="지정하지 않으면 UTC 시각으로 생성")

    status = subparsers.add_parser("status", help="마지막 실행과 캐시 상태 표시")
    _output_option(status)
    status.add_argument("--json", action="store_true", dest="as_json")

    queue = subparsers.add_parser("queue", help="덱스/사람이 검수할 후보 큐 출력")
    _output_option(queue)
    queue.add_argument("--run", help="run 디렉터리 또는 run id (기본: latest)")
    queue.add_argument(
        "--tier",
        action="append",
        dest="tiers",
        choices=sorted(CANDIDATE_TIERS | {"rejected", "error", "skipped"}),
        help="여러 번 지정 가능 (기본: confirmed/probable/needs_review)",
    )
    queue.add_argument("--limit", type=int)
    queue.add_argument("--json", action="store_true", dest="as_json")

    review = subparsers.add_parser("review", help="후보에 최종 검수 결정을 기록")
    _output_option(review)
    review.add_argument("--asset-id", required=True)
    review.add_argument(
        "--decision",
        required=True,
        choices=["confirmed", "probable", "needs_review", "rejected", "clear"],
    )
    review.add_argument("--note", default="")
    review.add_argument("--reviewer", default="dex")

    report = subparsers.add_parser("report", help="캐시된 실행 결과와 검수로 보고서 재생성")
    _output_option(report)
    report.add_argument("--run", help="run 디렉터리 또는 run id (기본: latest)")

    materialize = subparsers.add_parser(
        "materialize", help="선택된 원본만 maps/items 카탈로그로 복사"
    )
    _output_option(materialize)
    materialize.add_argument("--run", help="run 디렉터리 또는 run id (기본: latest)")
    materialize.add_argument(
        "--destination", default=str(PROJECT_ROOT / "catalog"), metavar="PATH"
    )
    materialize.add_argument(
        "--tier",
        action="append",
        dest="tiers",
        choices=sorted(CANDIDATE_TIERS | {"rejected"}),
        help="기본: confirmed/probable/needs_review",
    )
    materialize.add_argument("--execute", action="store_true", help="실제 파일 복사를 허용")
    materialize.add_argument(
        "--overwrite", action="store_true", help="이미 존재하는 카탈로그 파일 덮어쓰기"
    )
    return parser


def _resolved_sources(args: argparse.Namespace, config: Mapping[str, Any]):
    inputs = [_project_path(value) for value in (args.inputs or [DEFAULT_INPUT])]
    if args.no_manifest:
        manifest = None
    elif args.manifest:
        manifest = _project_path(args.manifest)
    else:
        manifest = DEFAULT_MANIFEST if DEFAULT_MANIFEST.is_file() else None
    include_unmanifested = bool(
        args.include_unmanifested or config["input"].get("include_unmanifested", False)
    )
    discovery = discover_sources(
        inputs,
        manifest,
        include_unmanifested=include_unmanifested,
        extensions=config["input"]["extensions"],
        project_root=PROJECT_ROOT,
    )
    return inputs, manifest, discovery


def _doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    package_names = ["paddleocr", "easyocr", "torch", "Pillow", "numpy"]
    packages = {name: package_version(name) for name in package_names}
    packages["paddlepaddle"] = package_version_any("paddlepaddle", "paddlepaddle-gpu")
    enabled_engines = [
        (label, engine)
        for label, engine in config["engines"].items()
        if isinstance(engine, Mapping) and engine.get("enabled", True)
    ]
    model_state: dict[str, dict[str, Any]] = {}
    required_packages = {"Pillow", "numpy"}
    expected_versions: dict[str, str] = {}
    version_matches: dict[str, bool] = {}
    for label, engine in enabled_engines:
        name = str(engine.get("name"))
        expected = engine.get("package_version")
        if expected:
            expected_versions[name] = str(expected)
            version_matches[name] = packages.get(name) == str(expected)
        if name == "paddleocr":
            required_packages.update({"paddleocr", "paddlepaddle"})
            default_root = Path.home() / ".paddlex" / "official_models"
            for kind in ("detector", "recognizer"):
                configured = _project_path(engine[f"{kind}_dir"])
                cached = default_root / str(engine[f"{kind}_model"])
                selected = configured if paddle_model_directory_ready(configured) else cached
                model_state[f"{label}_{name}_{kind}"] = {
                    "path": str(selected),
                    "present": paddle_model_directory_ready(selected),
                }
        elif name == "easyocr":
            required_packages.update({"easyocr", "torch"})
            model_dir = _project_path(engine["model_dir"])
            for kind in ("detector", "recognizer"):
                path = model_dir / str(engine[f"{kind}_file"])
                model_state[f"{label}_{name}_{kind}"] = {
                    "path": str(path),
                    "present": path.is_file() and path.stat().st_size > 0,
                }
    python_recommended = sys.version_info[:2] in {(3, 11), (3, 12)}
    package_ready = all(packages.get(name) for name in required_packages)
    versions_ready = all(version_matches.values())
    model_ready = all(value["present"] for value in model_state.values())
    report = {
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "recommended_3_11_or_3_12": python_recommended,
        },
        "packages": packages,
        "required_packages": sorted(required_packages),
        "expected_versions": expected_versions,
        "package_version_matches": version_matches,
        "models": model_state,
        "offline_scan_ready": package_ready and versions_ready and model_ready,
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Python {report['python']['version']} ({report['python']['executable']})")
        print(f"  권장 버전(3.11/3.12): {'예' if python_recommended else '아니오'}")
        print("패키지:")
        for name, version in packages.items():
            expected = expected_versions.get(name)
            suffix = ""
            if expected:
                suffix = f" (요구: {expected}, {'일치' if version_matches[name] else '불일치'})"
            print(f"  {name}: {version or '없음'}{suffix}")
        print("로컬 모델:")
        for name, state in model_state.items():
            print(f"  {name}: {'준비됨' if state['present'] else '없음'} - {state['path']}")
        print(f"오프라인 scan 준비: {'완료' if report['offline_scan_ready'] else '미완료'}")
    return 0 if report["offline_scan_ready"] else 1


def _plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    inputs, manifest, discovery = _resolved_sources(args, config)
    plan = build_plan(discovery, config)
    payload = plan.to_dict()
    payload["inputs"] = [str(value) for value in inputs]
    payload["manifest"] = str(manifest) if manifest else None
    payload["missing"] = discovery.missing
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"입력 참조: {plan.total_sources}")
        print(f"컬러 후보: {plan.color_candidates}")
        print(f"비컬러 스킵 예정: {plan.skipped_non_color}")
        print(f"누락 입력: {plan.missing_sources}")
        print(f"asset_type: {json.dumps(plan.asset_types, ensure_ascii=False, sort_keys=True)}")
        print("이 명령은 이미지 디코딩·OCR·파일 생성을 하지 않았습니다.")
    return 0 if not discovery.missing else 2


def _new_run_id(value: str | None, now: datetime) -> str:
    if value:
        cleaned = sanitize_component(value, stable_id=value, max_length=80)
        if cleaned != value:
            raise ValueError(f"안전하지 않은 run id입니다: {value}")
        return value
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def _scan_preflight(config: Mapping[str, Any], allow_model_download: bool) -> list[str]:
    errors: list[str] = []
    for distribution in ("Pillow", "numpy"):
        if not package_version(distribution):
            errors.append(f"필수 패키지 없음: {distribution}")
    for label, engine in config["engines"].items():
        if not isinstance(engine, Mapping) or not engine.get("enabled", True):
            continue
        name = str(engine.get("name"))
        if name == "paddleocr":
            installed = package_version("paddleocr")
            if not installed:
                errors.append(f"{label}: paddleocr 패키지 없음")
            expected = str(engine.get("package_version", ""))
            if installed and expected and installed != expected:
                errors.append(
                    f"{label}: paddleocr 버전 불일치 (설치 {installed}, 요구 {expected})"
                )
            if not package_version_any("paddlepaddle", "paddlepaddle-gpu"):
                errors.append(f"{label}: PaddlePaddle 추론 엔진 없음")
            if not allow_model_download:
                default_root = Path.home() / ".paddlex" / "official_models"
                for kind in ("detector", "recognizer"):
                    configured = _project_path(engine[f"{kind}_dir"])
                    cached = default_root / str(engine[f"{kind}_model"])
                    if not paddle_model_directory_ready(
                        configured
                    ) and not paddle_model_directory_ready(cached):
                        errors.append(f"{label}: PaddleOCR {kind} 로컬 모델 없음")
        elif name == "easyocr":
            installed = package_version("easyocr")
            if not installed:
                errors.append(f"{label}: easyocr 패키지 없음")
            expected = str(engine.get("package_version", ""))
            if installed and expected and installed != expected:
                errors.append(
                    f"{label}: easyocr 버전 불일치 (설치 {installed}, 요구 {expected})"
                )
            if not package_version("torch"):
                errors.append(f"{label}: torch 패키지 없음")
            if not allow_model_download:
                model_dir = _project_path(engine["model_dir"])
                for kind in ("detector", "recognizer"):
                    model_file = model_dir / str(engine[f"{kind}_file"])
                    if not model_file.is_file() or model_file.stat().st_size == 0:
                        errors.append(f"{label}: EasyOCR {kind} 로컬 모델 없음")
    return errors


def _scan(args: argparse.Namespace) -> int:
    if not args.execute:
        raise SystemExit(
            "scan은 실제 OCR과 모델 로딩을 수행합니다. 확인 후 --execute를 명시하세요."
        )
    config = load_config(args.config)
    inputs, manifest, discovery = _resolved_sources(args, config)
    plan = build_plan(discovery, config)
    if plan.color_candidates == 0:
        raise SystemExit("OCR할 컬러 텍스처 후보가 없습니다. plan 결과를 확인하세요.")
    if discovery.missing and not args.allow_missing:
        raise SystemExit(
            f"manifest/입력 누락 {len(discovery.missing)}개가 있습니다. "
            "plan으로 확인하거나 의도한 누락이면 --allow-missing을 명시하세요."
        )
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit은 1 이상이어야 합니다")

    preflight_errors = _scan_preflight(config, bool(args.allow_model_download))
    if preflight_errors:
        raise SystemExit(
            "OCR 실행 환경이 준비되지 않았습니다:\n- " + "\n- ".join(preflight_errors)
        )

    output = _project_path(args.output)
    started = _utc_now()
    run_id = _new_run_id(args.run_id, started)
    run_dir = output / "runs" / run_id
    primary, fallback = create_configured_engines(
        config,
        allow_model_download=args.allow_model_download,
        project_root=PROJECT_ROOT,
    )
    if args.allow_model_download:
        try:
            primary.prepare()
            if fallback is not None:
                fallback.prepare()
        except Exception as exc:
            raise SystemExit(f"OCR 모델 준비/다운로드 실패: {type(exc).__name__}: {exc}") from exc
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SystemExit(
            f"이미 존재하거나 다른 scan이 사용 중인 run입니다: {run_dir}. "
            "다른 --run-id를 사용하세요."
        ) from exc
    run = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": _iso(started),
        "finished_at": None,
        "inputs": [str(value) for value in inputs],
        "manifest": str(manifest) if manifest else None,
        "plan": plan.to_dict(),
        "missing": discovery.missing,
        "config": config,
        "allow_model_download": bool(args.allow_model_download),
        "allow_missing": bool(args.allow_missing),
        "force": bool(args.force),
        "limit": args.limit,
    }
    _atomic_json(run_dir / "run.in_progress.json", run)
    with ResultCache(output / "cache.sqlite3") as cache:
        results = scan_sources(
            discovery,
            config,
            primary,
            fallback,
            cache,
            run_dir,
            force=args.force,
            limit=args.limit,
            progress=print,
        )
    run["finished_at"] = _iso(_utc_now())
    summary = write_reports(run_dir, run, results)
    in_progress = run_dir / "run.in_progress.json"
    if in_progress.exists():
        in_progress.unlink()
    _atomic_json(
        output / "latest.json",
        {"run_id": run_id, "run_dir": f"runs/{run_id}", "summary": summary},
    )
    error_count = int(summary.get("processing", {}).get("error", 0))
    print(f"{'완료' if error_count == 0 else '오류 포함 종료'}: {run_dir}")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if error_count == 0 else 3


def _resolve_run(output: Path, value: str | None) -> Path:
    if value:
        candidate = _project_path(value)
        if candidate.is_dir():
            return candidate
        candidate = output / "runs" / value
        if candidate.is_dir():
            return candidate.resolve()
        raise FileNotFoundError(f"run을 찾을 수 없습니다: {value}")
    latest_path = output / "latest.json"
    if not latest_path.is_file():
        raise FileNotFoundError(f"latest 실행 정보가 없습니다: {latest_path}")
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    run_dir = Path(str(latest["run_dir"]))
    if not run_dir.is_absolute():
        run_dir = output / run_dir
    if not run_dir.is_dir():
        raise FileNotFoundError(f"latest run 폴더가 없습니다: {run_dir}")
    return run_dir.resolve()


def _status(args: argparse.Namespace) -> int:
    output = _project_path(args.output)
    latest_path = output / "latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8")) if latest_path.is_file() else None
    cache_path = output / "cache.sqlite3"
    cache_results = 0
    reviews = 0
    if cache_path.is_file():
        connection = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
        try:
            cache_results = int(connection.execute("SELECT COUNT(*) FROM ocr_results").fetchone()[0])
            reviews = int(connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0])
        finally:
            connection.close()
    payload = {
        "output": str(output),
        "latest": latest,
        "cached_ocr_results": cache_results,
        "reviews": reviews,
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"출력: {output}")
        print(f"latest: {latest.get('run_id') if latest else '없음'}")
        print(f"OCR 캐시: {cache_results}, 검수 결정: {reviews}")
        if latest and latest.get("summary"):
            print(json.dumps(latest["summary"], ensure_ascii=False, sort_keys=True))
    return 0


def _read_run(output: Path, run_value: str | None) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    run_dir = _resolve_run(output, run_value)
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    results = load_results(run_dir / "results.jsonl")
    cache_path = output / "cache.sqlite3"
    if cache_path.is_file():
        connection = sqlite3.connect(f"file:{cache_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            reviews = {
                str(row["asset_id"]): dict(row)
                for row in connection.execute("SELECT * FROM reviews ORDER BY asset_id")
            }
        finally:
            connection.close()
        for row in results:
            row["review"] = reviews.get(
                str(row.get("asset_id", "")),
                {"decision": None, "note": "", "reviewer": "", "reviewed_at": None},
            )
    return run_dir, run, results


def _queue(args: argparse.Namespace) -> int:
    output = _project_path(args.output)
    run_dir, _, results = _read_run(output, args.run)
    tiers = set(args.tiers or sorted(CANDIDATE_TIERS))
    queue = []
    for row in results:
        tier = effective_tier(row)
        processing = str(row.get("processing", {}).get("status", ""))
        if tier not in tiers and processing not in tiers:
            continue
        preview = row.get("preview")
        queue.append(
            {
                "asset_id": row.get("asset_id"),
                "tier": tier,
                "score": row.get("classification", {}).get("score", 0.0),
                "preview": str((run_dir / preview).resolve()) if preview else None,
                "source": row.get("representative_source"),
                "ocr_text": [
                    item.get("text") for item in row.get("detections", []) if item.get("text")
                ],
                "references": row.get("references", []),
            }
        )
    queue.sort(key=lambda row: (row["tier"], -float(row["score"] or 0), str(row["asset_id"])))
    if args.limit is not None:
        queue = queue[: max(0, args.limit)]
    if args.as_json:
        print(json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for row in queue:
            print(f"{row['asset_id']}\t{row['tier']}\t{row['score']:.3f}\t{row['preview'] or row['source']}")
        print(f"후보 {len(queue)}개")
    return 0


def _refresh_report(output: Path, run_value: str | None = None) -> tuple[Path, dict[str, Any]]:
    run_dir, run, results = _read_run(output, run_value)
    summary = write_reports(run_dir, run, results)
    latest_path = output / "latest.json"
    if latest_path.is_file():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        latest_run = Path(str(latest.get("run_dir", "")))
        if not latest_run.is_absolute():
            latest_run = output / latest_run
        if latest_run.resolve() == run_dir.resolve():
            latest["summary"] = summary
            _atomic_json(latest_path, latest)
    return run_dir, summary


def _review(args: argparse.Namespace) -> int:
    output = _project_path(args.output)
    cache_path = output / "cache.sqlite3"
    if not cache_path.is_file():
        raise SystemExit(f"OCR 캐시가 없습니다: {cache_path}")
    with ResultCache(cache_path) as cache:
        cache.set_review(args.asset_id, args.decision, args.note, args.reviewer)
    print(f"검수 저장: {args.asset_id} -> {args.decision}")
    try:
        run_dir, _ = _refresh_report(output)
        print(f"보고서 갱신: {run_dir / 'report.html'}")
    except FileNotFoundError:
        pass
    return 0


def _report(args: argparse.Namespace) -> int:
    output = _project_path(args.output)
    run_dir, summary = _refresh_report(output, args.run)
    print(f"보고서 갱신: {run_dir / 'report.html'}")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


def _source_path(value: str) -> Path:
    path = resolve_project_path(value, PROJECT_ROOT)
    if path is None:
        raise ValueError("원본 경로가 비어 있습니다")
    return path


def _verified_material_source(row: Mapping[str, Any]) -> tuple[Path, str] | None:
    references = sorted(
        (ref for ref in row.get("references", []) if isinstance(ref, Mapping)),
        key=lambda ref: str(ref.get("path", "")).casefold(),
    )
    for reference in references:
        expected = str(reference.get("file_sha256", ""))
        if not expected:
            continue
        source = _source_path(str(reference.get("path", "")))
        if source.is_file() and file_sha256(source) == expected:
            return source, expected
    return None


def _material_groups(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    references = row.get("references", [])
    metadata = [ref.get("metadata", {}) for ref in references if isinstance(ref, Mapping)]
    known_types = {
        str(item.get("asset_type", "unknown"))
        for item in metadata
        if str(item.get("asset_type", "unknown")) in {"map", "item"}
    }

    def groups_for(asset_type: str | None) -> list[str]:
        return sorted(
            {
                str(group)
                for item in metadata
                if asset_type is None
                or str(item.get("asset_type", "unknown")) == asset_type
                for group in item.get("groups", [])
                if str(group).strip()
            }
        )

    destinations: list[tuple[str, str]] = []
    if "map" in known_types:
        groups = groups_for("map")
        destinations.append(
            ("maps", groups[0] if len(groups) == 1 else ("_shared" if groups else "_unassigned"))
        )
    if "item" in known_types:
        groups = groups_for("item")
        destinations.append(
            (
                "items",
                groups[0] if len(groups) == 1 else ("_shared" if groups else "_uncategorized"),
            )
        )
    if destinations:
        return destinations

    groups = groups_for(None)
    return [("unknown", groups[0] if len(groups) == 1 else "_unassigned")]


def _copy_verified(source: Path, target: Path, expected_hash: str, asset_id: str) -> None:
    temporary = target.with_name(f".{target.name}.{asset_id}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        if file_sha256(temporary) != expected_hash:
            raise RuntimeError(f"복사 검증 실패: {source}")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sidecar_matches(sidecar: Path, asset_id: str) -> bool:
    if not sidecar.is_file():
        return False
    try:
        value = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, Mapping) and str(value.get("asset_id", "")) == asset_id


def _materialize(args: argparse.Namespace) -> int:
    if not args.execute:
        raise SystemExit("파일 복사를 수행하려면 --execute를 명시하세요.")
    output = _project_path(args.output)
    _, _, results = _read_run(output, args.run)
    destination = _project_path(args.destination)
    tiers = set(args.tiers or sorted(CANDIDATE_TIERS))
    copied = 0
    repaired = 0
    skipped_existing = 0
    stale_sources = 0
    for row in results:
        if effective_tier(row) not in tiers:
            continue
        if row.get("processing", {}).get("status") != "ok":
            continue
        verified = _verified_material_source(row)
        if verified is None:
            stale_sources += 1
            continue
        source, expected_hash = verified
        asset_id = str(row["asset_id"])
        safe_name = sanitize_component(source.name, stable_id=asset_id)
        for root_name, group_name in _material_groups(row):
            group_id = f"group:{root_name}:{group_name}"
            target_dir = safe_join(destination, root_name, group_name, stable_id=group_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{asset_id}__{safe_name}"
            sidecar = target.with_suffix(target.suffix + ".json")

            if (target.exists() and not target.is_file()) or (
                sidecar.exists() and not sidecar.is_file()
            ):
                skipped_existing += 1
                continue

            if not args.overwrite:
                if target.is_file() and sidecar.is_file():
                    skipped_existing += 1
                    continue
                if target.is_file():
                    if file_sha256(target) == expected_hash:
                        _atomic_json(sidecar, row)
                        repaired += 1
                    else:
                        skipped_existing += 1
                    continue
                if sidecar.is_file():
                    if _sidecar_matches(sidecar, asset_id):
                        _copy_verified(source, target, expected_hash, asset_id)
                        _atomic_json(sidecar, row)
                        repaired += 1
                    else:
                        skipped_existing += 1
                    continue

            _copy_verified(source, target, expected_hash, asset_id)
            _atomic_json(sidecar, row)
            copied += 1
    print(
        f"선택 텍스처 파일 {copied}개 복사, 부분 완료 {repaired}개 복구, "
        f"기존 파일 {skipped_existing}개 보존, "
        f"원본 변경/누락 {stale_sources}개 제외: {destination}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "doctor": _doctor,
        "plan": _plan,
        "scan": _scan,
        "status": _status,
        "queue": _queue,
        "review": _review,
        "report": _report,
        "materialize": _materialize,
    }
    try:
        return handlers[args.command](args)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2
