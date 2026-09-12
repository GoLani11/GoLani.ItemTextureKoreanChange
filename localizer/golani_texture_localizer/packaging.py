"""Build one item's test mod, then promote only a reviewed immutable package."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .files import descriptor, local_path, read_json, sha256, verified_file, write_json
from .jobs import load_job
from .textures import patch_bundle
from .validation import validate

MOD_NAME = "GoLani-ItemTextureKoreanChange"
SERVER_FILES = ("GoLani.ItemTextureKoreanChange.dll", "GoLani.ItemTextureKoreanChange.deps.json")


def producer_signature() -> dict:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {"code_sha256": digest.hexdigest(), "dependencies": {
        name: importlib.metadata.version(name)
        for name in ("UnityPy", "ispc_texcomp", "Pillow", "numpy", "opencv-python-headless")}}


def build_server(project_root: Path, dotnet: str = "dotnet") -> Path:
    project = project_root / "mod" / "GoLani.ItemTextureKoreanChange.csproj"
    command = str(Path(dotnet).resolve()) if Path(dotnet).is_file() else dotnet
    subprocess.run([command, "build", str(project), "-c", "Release", "--nologo"], check=True)
    output = project.parent / "bin" / "Release" / "net10.0"
    for name in SERVER_FILES:
        if not (output / name).is_file():
            raise FileNotFoundError(f"서버 빌드 결과가 없어요: {name}")
    return output


def verify_package(root: Path) -> dict:
    report = read_json(root / "package.json")
    if report.get("schema_version") != 1 or report.get("kind") != "test" or report.get("file_validation_passed") is not True:
        raise ValueError("유효한 테스트 패키지가 아니에요")
    if not report.get("files"):
        raise ValueError("패키지 파일 목록이 없어요")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p != root / "package.json"}
    if actual != set(report["files"]):
        raise ValueError("패키지에 기록되지 않은 파일 또는 누락된 파일이 있어요")
    for name, expected in report["files"].items():
        if sha256(local_path(root, name)) != expected:
            raise ValueError(f"패키지 생성 뒤 파일이 변경됐어요: {name}")
    return report


def package(job_path: Path, project_root: Path, *, dotnet: str = "dotnet") -> dict:
    root, _, snapshot = load_job(job_path)
    validation = validate(job_path)
    if not validation["passed"]:
        raise ValueError("파일 보존 검사를 통과하지 못했어요. reports/validation.json을 확인하세요")
    server = build_server(project_root, dotnet)
    server_hashes = {name: sha256(server / name) for name in SERVER_FILES}
    producer = producer_signature()
    digest = hashlib.sha256(json.dumps({"inputs": validation["inputs"], "server": server_hashes, "producer": producer}, sort_keys=True).encode()).hexdigest()[:16]
    parent = root / "packages"
    parent.mkdir(exist_ok=True)
    output = parent / digest
    if output.exists():
        return {"package": str(output), **verify_package(output)}
    temporary = Path(tempfile.mkdtemp(dir=parent, prefix=".building-"))
    try:
        mod = temporary / MOD_NAME
        mod.mkdir()
        manifests, bundles = [], []
        coverage = verified_file(root, snapshot["uv_coverage"])
        for key, original in snapshot["bundles"].items():
            entries = [e for e in snapshot["maps"] if e["bundle_key"] == key]
            report = patch_bundle(root, original, entries, local_path(mod, f"bundles/{key}"),
                                  coverage, temporary / "roundtrip")
            bundles.append({"key": key, **report})
            manifests.append({"key": key, "dependencyKeys": original["dependencies"]})
        for name in SERVER_FILES:
            shutil.copy2(server / name, mod / name)
            if sha256(mod / name) != server_hashes[name]:
                raise ValueError("패키징 도중 서버 빌드가 변경됐어요")
        write_json(mod / "bundles.json", {"manifest": manifests})
        (mod / "TEST-ONLY.txt").write_text(
            "게임 확인용 테스트 패키지입니다. 파일 검증만 완료했으며 시각·게임 검증은 미완료입니다.\n"
            f"D 제작 방식: {validation['diffuse_mode']} / 보존 검사: {validation['diffuse_preservation']}\n"
            + ("전체 생성 D를 채택했으며 비문자 RGB와 UV 경계 픽셀 보존을 보증하지 않습니다.\n"
               if validation["diffuse_mode"] == "generated-full" else "") +
            "설치 위치: SPT/SPT_Runtime/user/mods/GoLani-ItemTextureKoreanChange\n"
            "기존 설치를 별도 보관한 뒤 사용하고, SPT 런처의 임시 파일을 삭제하세요.\n"
            "원복: SPT 종료 후 테스트 모드 폴더를 mods 밖으로 옮기고 기존 설치를 복원합니다.\n", encoding="utf-8")
        if validate(job_path, write=False)["inputs"] != validation["inputs"] or producer_signature() != producer:
            raise ValueError("패키징 도중 편집 입력이 변경됐어요")
        report = {"schema_version": 1, "kind": "test", "target_id": snapshot["target_id"],
                  "diffuse_mode": validation["diffuse_mode"], "diffuse_preservation": validation["diffuse_preservation"],
                  "inputs": validation["inputs"], "producer": producer, "file_validation_passed": True,
                  "visual_reviewed": False, "runtime_tested": False,
                  "changed_pixels": validation["changed_pixels"], "bundles": bundles,
                  "files": {p.relative_to(temporary).as_posix(): sha256(p) for p in temporary.rglob("*") if p.is_file()}}
        write_json(temporary / "package.json", report)
        verify_package(temporary)
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"package": str(output), **report}


def release(package_root: Path, review_path: Path, output: Path) -> dict:
    package_root, output = package_root.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError("기존 배포본을 덮어쓰지 않습니다. 새 출력 경로를 지정하세요")
    if package_root == output or package_root in output.parents:
        raise ValueError("배포본은 테스트 패키지 밖에 만들어야 해요")
    package_report = verify_package(package_root)
    if package_report.get("changed_pixels", 0) <= 0:
        raise ValueError("무편집 원본 패키지는 현지화 배포본으로 승격할 수 없어요")
    review = read_json(review_path)
    if (review.get("package_sha256") != sha256(package_root / "package.json")
            or review.get("visual_reviewed") is not True or review.get("runtime_tested") is not True
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or not isinstance(review.get("notes"), str) or not review["notes"].strip()
            or not isinstance(review.get("evidence"), list) or not review["evidence"]):
        raise ValueError("이 패키지의 시각·게임 확인 기록과 증거가 필요해요")
    evidence = [verified_file(review_path.resolve().parent, item) for item in review["evidence"]]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=output.parent, prefix=".release-"))
    try:
        shutil.copytree(package_root / MOD_NAME, temporary / MOD_NAME)
        for name, digest in package_report["files"].items():
            if name.startswith(MOD_NAME + "/") and sha256(local_path(temporary, name)) != digest:
                raise ValueError(f"배포본 복사 뒤 파일이 달라요: {name}")
        (temporary / MOD_NAME / "TEST-ONLY.txt").unlink()
        write_json(temporary / "review.json", review)
        for i, path in enumerate(evidence):
            destination = temporary / "evidence" / f"{i:02d}{path.suffix}"
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(path, destination)
            if sha256(destination) != review["evidence"][i]["sha256"]:
                raise ValueError("복사 도중 검증 증거가 변경됐어요")
        verify_package(package_root)
        report = {"schema_version": 1, "kind": "release", "target_id": package_report["target_id"],
                  "diffuse_mode": package_report.get("diffuse_mode", "masked"),
                  "diffuse_preservation": package_report.get("diffuse_preservation", "outside-mask"),
                  "package_sha256": review["package_sha256"], "visual_reviewed": True, "runtime_tested": True,
                  "files": {p.relative_to(temporary).as_posix(): sha256(p) for p in temporary.rglob("*") if p.is_file()}}
        write_json(temporary / "release.json", report)
        temporary.rename(output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {"release": str(output), **report}
