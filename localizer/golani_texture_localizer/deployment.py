from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .paths import ProjectPaths
from .models import CollectionProfile
from .review import load_review, sha256_file


MOD_NAME = "GoLani-ItemTextureKoreanChange"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_release(profile: CollectionProfile, paths: ProjectPaths) -> dict[str, Any]:
    profile_path = paths.root / "profiles" / "food" / "collection.json"
    if not profile_path.is_file():
        raise FileNotFoundError(f"현지화 profile이 없어요: {profile_path}")
    profile_sha256 = _sha256(profile_path)
    repack_path = paths.reports / "repack.json"
    if not repack_path.is_file():
        raise FileNotFoundError("repack 보고서가 없어요")
    repack = json.loads(repack_path.read_text(encoding="utf-8"))
    if repack.get("passed") is not True or repack.get("partial") is not False:
        raise ValueError("전체 재패킹 게이트가 통과되지 않았어요")
    bundles: dict[str, str] = {}
    for report in repack.get("bundles", []):
        key = report.get("bundle_key")
        output = Path(report.get("output_bundle", ""))
        if not isinstance(key, str) or not key or not output.is_file():
            raise ValueError("repack 보고서의 bundle 항목이 불완전해요")
        current = _sha256(output)
        if report.get("output_sha256") != current:
            raise ValueError(f"repack 뒤 bundle이 변경됐어요: {key}")
        bundles[key] = current
    if len(bundles) != repack.get("bundle_count") or not bundles:
        raise ValueError("repack bundle 수와 실제 release 입력이 달라요")
    project = paths.root / "GoLani.ItemTextureKoreanChange.csproj"
    subprocess.run(
        ["dotnet", "build", str(project), "-c", "Release"],
        cwd=paths.root,
        check=True,
    )
    server_files = {}
    for name in ("GoLani.ItemTextureKoreanChange.dll", "GoLani.ItemTextureKoreanChange.deps.json"):
        path = paths.root / "bin" / "Release" / "net10.0" / name
        if not path.is_file():
            raise FileNotFoundError(f"서버 모드 빌드 산출물이 없어요: {path}")
        server_files[name] = _sha256(path)
    reviews: dict[str, str] = {}
    for target in profile.targets:
        review_path, _ = load_review(paths, target.id, through="release")
        reviews[target.id] = sha256_file(review_path)
    release_id = hashlib.sha256(
        json.dumps(
            {
                "profile_sha256": profile_sha256,
                "repack_report_sha256": _sha256(repack_path),
                "bundles": bundles,
                "reviews": reviews,
                "server_files": server_files,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    release_root = paths.releases / release_id
    release_bundles = release_root / "bundles"
    if release_root.exists():
        manifest_path = release_root / "release.json"
        if not manifest_path.is_file():
            raise FileExistsError(f"불완전한 release 디렉터리가 있어요: {release_root}")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            existing.get("bundles") != bundles
            or existing.get("review_hashes") != reviews
            or existing.get("server_files") != server_files
            or existing.get("profile_sha256") != profile_sha256
            or existing.get("repack_report_sha256") != _sha256(repack_path)
        ):
            raise ValueError(f"같은 release ID의 내용이 달라요: {release_root}")
        return existing
    paths.releases.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{release_id}.", dir=paths.releases))
    try:
        for key, expected in bundles.items():
            source = paths.bundles / Path(key)
            destination = temporary / "bundles" / Path(key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if _sha256(destination) != expected:
                raise AssertionError(f"release 복사 뒤 bundle SHA가 달라요: {key}")
        for name, expected in server_files.items():
            source = paths.root / "bin" / "Release" / "net10.0" / name
            destination = temporary / name
            shutil.copy2(source, destination)
            if _sha256(destination) != expected:
                raise AssertionError(f"release 서버 파일 SHA가 달라요: {name}")
        payload = {
            "schema_version": 2,
            "release_id": release_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "profile": str(profile_path),
            "profile_sha256": profile_sha256,
            "repack_report": str(repack_path),
            "repack_report_sha256": _sha256(repack_path),
            "bundle_count": len(bundles),
            "bundles": bundles,
            "review_hashes": reviews,
            "server_files": server_files,
            "passed": True,
        }
        (temporary / "release.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, release_root)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    paths.latest_release.parent.mkdir(parents=True, exist_ok=True)
    paths.latest_release.write_text(
        json.dumps({"schema_version": 1, "release_id": release_id}, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _load_release(paths: ProjectPaths, release_id: str) -> tuple[Path, dict[str, Any]]:
    if release_id == "latest":
        if not paths.latest_release.is_file():
            raise FileNotFoundError("latest release 포인터가 없어요")
        latest = json.loads(paths.latest_release.read_text(encoding="utf-8"))
        release_id = str(latest.get("release_id", ""))
    if not release_id or "/" in release_id or "\\" in release_id or ".." in release_id:
        raise ValueError("안전하지 않은 release ID예요")
    root = paths.releases / release_id
    manifest_path = root / "release.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"release를 찾지 못했어요: {release_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 2
        or manifest.get("passed") is not True
        or manifest.get("release_id") != release_id
    ):
        raise ValueError("유효한 release manifest가 아니에요")
    current_inputs = {
        "profile_sha256": paths.root / "profiles" / "food" / "collection.json",
        "repack_report_sha256": paths.reports / "repack.json",
    }
    for field, path in current_inputs.items():
        if not path.is_file() or manifest.get(field) != _sha256(path):
            raise ValueError(f"현재 작업과 다른 오래된 release예요: {field}")
    review_hashes = manifest.get("review_hashes")
    if not isinstance(review_hashes, dict) or not review_hashes:
        raise ValueError("release의 review 해시가 비어 있어요")
    for target_id, expected in review_hashes.items():
        current = paths.reviews / str(target_id) / "review.json"
        if not current.is_file() or not isinstance(expected, str) or _sha256(current) != expected:
            raise ValueError(f"현재 작업과 다른 오래된 release예요: review {target_id}")
    for key, expected in manifest.get("bundles", {}).items():
        source = root / "bundles" / Path(key)
        if not source.is_file() or _sha256(source) != expected:
            raise ValueError(f"release bundle이 누락되거나 변경됐어요: {key}")
    for name, expected in manifest.get("server_files", {}).items():
        source = root / name
        if not source.is_file() or _sha256(source) != expected:
            raise ValueError(f"release 서버 파일이 누락되거나 변경됐어요: {name}")
    return root, manifest


def deploy_release(
    paths: ProjectPaths,
    spt_root: Path,
    *,
    release_id: str,
    execute: bool,
) -> dict[str, Any]:
    release_root, manifest = _load_release(paths, release_id)
    destination = spt_root.expanduser().resolve() / "SPT_Runtime" / "user" / "mods" / MOD_NAME
    plan = {
        "release_id": manifest["release_id"],
        "source": str(release_root),
        "destination": str(destination),
        "bundle_count": manifest["bundle_count"],
        "execute": execute,
    }
    if not execute:
        return plan
    runtime = spt_root.expanduser().resolve() / "SPT_Runtime"
    if not runtime.is_dir():
        raise FileNotFoundError(f"SPT Runtime을 찾지 못했어요: {runtime}")
    catalog = (
        spt_root.expanduser().resolve()
        / "EscapeFromTarkov_Data"
        / "StreamingAssets"
        / "Windows"
        / "Windows.json"
    )
    if not catalog.is_file():
        raise FileNotFoundError(f"원본 bundle 카탈로그를 찾지 못했어요: {catalog}")
    catalog_data = json.loads(catalog.read_text(encoding="utf-8"))
    missing_catalog = sorted(set(manifest["bundles"]) - set(catalog_data))
    if missing_catalog:
        raise ValueError(f"원본 카탈로그에 없는 bundle이 있어요: {missing_catalog}")
    backup = paths.workspace / "deploy-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    if destination.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(destination, backup)
    temporary = destination.with_name(f".{destination.name}.{manifest['release_id']}.tmp")
    if temporary.exists():
        raise FileExistsError(f"이전 임시 배포 폴더가 남아 있어요: {temporary}")
    temporary.mkdir(parents=True)
    try:
        shutil.copytree(release_root / "bundles", temporary / "bundles")
        for name in manifest["server_files"]:
            shutil.copy2(release_root / name, temporary / name)
        bundle_manifest = {
            "manifest": [
                {
                    "key": key,
                    "dependencyKeys": sorted(set(catalog_data[key].get("Dependencies", []))),
                }
                for key in sorted(manifest["bundles"])
            ]
        }
        (temporary / "bundles.json").write_text(
            json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        for key, expected in manifest["bundles"].items():
            if _sha256(temporary / "bundles" / Path(key)) != expected:
                raise AssertionError(f"배포 임시본 SHA가 달라요: {key}")
        if destination.exists():
            retired = destination.with_name(f".{destination.name}.previous")
            if retired.exists():
                raise FileExistsError(f"이전 배포 교체 폴더가 남아 있어요: {retired}")
            os.replace(destination, retired)
            try:
                os.replace(temporary, destination)
            except Exception:
                os.replace(retired, destination)
                raise
            shutil.rmtree(retired)
        else:
            os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    plan["backup"] = str(backup) if backup.exists() else None
    plan["installed"] = True
    return plan
