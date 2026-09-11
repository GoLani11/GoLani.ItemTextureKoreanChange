"""Prepare one immutable source snapshot and a small editable job description."""
from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import re
import shutil

from PIL import Image

from .bindings import target_maps
from .files import descriptor, local_path, read_json, sha256, verified_file, write_json
from .inventory import scan_collection, verify_material_dependency_snapshot
from .models import BundleSpec, _bundle_key
from .paths import ProjectPaths
from .uv import generate_uv_review


def dependency_keys(catalog: dict, key: str) -> list[str]:
    if key not in catalog or not isinstance(catalog[key], dict):
        raise ValueError(f"Windows.json에 번들이 없어요: {key}")
    values = catalog[key].get("Dependencies", [])
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValueError(f"번들 의존성 형식이 잘못됐어요: {key}")
    return sorted({_bundle_key(v) for v in values})


def prepare(profile, target_id: str, bundle_root: Path, output: Path) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", target_id):
        raise ValueError("품목 ID는 영문 소문자·숫자·밑줄·하이픈만 사용할 수 있어요")
    bundle_root, output = bundle_root.resolve(), output.resolve()
    if output == bundle_root or bundle_root in output.parents:
        raise ValueError("게임 원본 폴더에 작업 결과를 기록할 수 없어요")
    if output.exists():
        raise FileExistsError(f"기존 작업을 보존합니다. 새 작업 폴더를 지정하세요: {output}")
    target = profile.target_by_id(target_id)
    if not target.bundle_key:
        raise ValueError("프로필에 대상 bundle_key가 필요해요")
    catalog_sha256 = sha256(bundle_root / "Windows.json")
    catalog = read_json(bundle_root / "Windows.json")
    # Load the target and its declared bundle dependencies; external Material consumers
    # are discovered by the retained inventory scanner using the reverse dependency graph.
    selected = {target.bundle_key}
    pending = [target.bundle_key]
    while pending:
        key = pending.pop()
        for dep in dependency_keys(catalog, key):
            if dep.endswith(".bundle") and dep not in selected:
                selected.add(dep)
                pending.append(dep)
    subset = replace(profile, targets=(target,),
                     bundles=tuple(BundleSpec(key=k, label=k) for k in sorted(selected)))
    output.mkdir(parents=True)
    paths = ProjectPaths.create(profile.path.parents[2], output)
    inventory = scan_collection(subset, bundle_root, paths, extract=True)
    if inventory["missing_bundles"]:
        raise FileNotFoundError(f"원본 번들이 없어요: {inventory['missing_bundles']}")
    if inventory["material_dependency_snapshot"]["catalog"]["sha256"] != catalog_sha256:
        raise ValueError("추출 도중 게임 카탈로그가 변경됐어요")
    resolved = target_maps(inventory, target_id)
    uv = generate_uv_review(paths, inventory, target_id)
    if not uv["passed"]:
        raise ValueError("UV 경계 추출에 실패했어요")
    bundles = {}
    entries = []
    for value in resolved:
        record, role = value["record"], value["role"]
        key = record["bundle_key"]
        if key not in bundles:
            source = bundle_root / key
            saved = local_path(output, f"original/bundles/{key}")
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, saved)
            if sha256(saved) != record["bundle_sha256"]:
                raise ValueError("추출 도중 게임 원본이 변경됐어요")
            bundles[key] = {**descriptor(output, saved), "dependencies": dependency_keys(catalog, key)}
        token = hashlib.sha256(f"{key}:{record['assets_file']}:{record['path_id']}".encode()).hexdigest()[:12]
        map_id = f"{role}-{token}"
        directory = output / "maps" / map_id
        directory.mkdir(parents=True)
        with Image.open(record["source_png"]) as image:
            image.convert("RGBA").save(directory / "source.png")
        shutil.copy2(directory / "source.png", directory / "candidate.png")
        Image.new("L", (record["width"], record["height"]), 0).save(directory / "editable.png")
        entries.append({
            "id": map_id, "role": role, "texture": record["texture"],
            "bundle_key": key, "assets_file": record["assets_file"], "path_id": record["path_id"],
            "width": record["width"], "height": record["height"], "format": record["format"],
            "mip_count": record["mip_count"], "wrap_u": record["wrap_u"], "wrap_v": record["wrap_v"],
            "source": descriptor(output, directory / "source.png"),
            "candidate": f"maps/{map_id}/candidate.png", "editable": f"maps/{map_id}/editable.png",
            "preserved_channels": [0, 2] if role == "normal" else [3],
            "shared": value["shared"], "bindings": value["bindings"],
        })
    verify_material_dependency_snapshot(inventory, bundle_root)
    snapshot = {"schema_version": 1, "target_id": target_id, "name_ko": target.name_ko,
                "bundles": bundles, "maps": entries,
                "inventory": descriptor(output, paths.inventory),
                "uv_seam": descriptor(output, Path(uv["mask"])),
                "uv_coverage": descriptor(output, Path(uv["coverage"]))}
    write_json(output / "source.json", snapshot)
    job = {"schema_version": 1, "source": descriptor(output, output / "source.json"),
           "exact_text": list(target.exact_text), "notes": target.notes}
    write_json(output / "job.json", job)
    (output / "EDITING.md").write_text(
        f"# {target.name_ko} 작업\n\n원본·source.json은 수정하지 않습니다.\n"
        "maps/*/candidate.png를 원본 크기로 편집하고, editable.png에 허용한 문자 영역을 흰색으로 표시합니다.\n"
        "이미지 편집 도구에는 원본과 정확한 한글 문구를 주고 글꼴 인상·비율·색·배치를 지정합니다.\n"
        "생성 결과에서 승인한 글자만 원본 좌표로 옮깁니다. compose 명령으로 원본 알파와 나머지 픽셀을 보존할 수 있습니다.\n"
        "N/G는 동일 글자 알파에서 derive 명령으로 계산하거나 원본을 보존합니다.\n"
        "소스 식별과 파일 검사는 validate, 게임 확인용 산출물은 package로 만듭니다.\n"
        "원본과 같은 초기 후보는 무편집 왕복 확인용입니다. 번역 완료를 뜻하지 않습니다.\n",
        encoding="utf-8")
    return {"job": str(output / "job.json"), "target_id": target_id,
            "maps": len(entries), "bundles": len(bundles), "runtime_tested": False}


def load_job(path: Path) -> tuple[Path, dict, dict]:
    path = path.resolve()
    job = read_json(path)
    if job.get("schema_version") != 1:
        raise ValueError("지원하지 않는 job 형식이에요")
    snapshot = read_json(verified_file(path.parent, job["source"]))
    if snapshot.get("schema_version") != 1 or not snapshot.get("maps"):
        raise ValueError("원본 스냅샷이 불완전해요")
    if len({e["id"] for e in snapshot["maps"]}) != len(snapshot["maps"]):
        raise ValueError("중복된 map ID가 있어요")
    return path.parent, job, snapshot
