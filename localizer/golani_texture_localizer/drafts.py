"""Adopt a selected full diffuse draft without claiming non-text pixel preservation."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from .files import atomic_bytes, descriptor, local_path, read_json, verified_file, write_json
from .jobs import load_job


def diffuse_mode(job: dict) -> str:
    mode = job.get("diffuse_mode", "masked")
    if not isinstance(mode, str) or mode not in {"masked", "generated-full"}:
        raise ValueError(f"지원하지 않는 diffuse_mode예요: {mode!r}")
    if mode == "masked" and "generated_diffuse" in job:
        raise ValueError("masked 작업에 전체 D 채택 기록이 섞여 있어요")
    if mode == "generated-full" and not isinstance(job.get("generated_diffuse"), dict):
        raise ValueError("전체 D는 adopt-draft로 채택한 기록이 필요해요")
    return mode


def diffuse_entry(snapshot: dict) -> dict:
    entries = [e for e in snapshot["maps"] if e["role"] == "diffuse"]
    if len(entries) != 1:
        raise ValueError("전체 D 채택에는 컬러 맵이 정확히 하나 필요해요")
    return entries[0]


def verify_adoption(root: Path, job: dict, snapshot: dict) -> dict:
    if diffuse_mode(job) != "generated-full":
        raise ValueError("전체 D 채택 작업이 아니에요")
    report = read_json(verified_file(root, job["generated_diffuse"]))
    entry = diffuse_entry(snapshot)
    if (report.get("schema_version") != 1 or report.get("diffuse_mode") != "generated-full"
            or report.get("map_id") != entry["id"] or report.get("snapshot") != job["source"]
            or report.get("source") != entry["source"]
            or report.get("output_size") != [entry["width"], entry["height"]]):
        raise ValueError("전체 D 채택 기록과 현재 원본이 달라요")
    verified_file(root, report["source"])
    verified_file(root, report["input"])
    if verified_file(root, report["candidate"]) != local_path(root, entry["candidate"]):
        raise ValueError("전체 D 채택 기록의 후보 경로가 달라요")
    return report


def adopt_draft(job_path: Path, image_path: Path) -> dict:
    root, job, snapshot = load_job(job_path)
    diffuse_mode(job)
    entry = diffuse_entry(snapshot)
    source_path = verified_file(root, entry["source"])
    candidate_path = local_path(root, entry["candidate"])
    image_path = image_path.resolve()
    # Only the selected D candidate may be replaced; source files and other maps
    # remain inputs even if a hand-edited snapshot accidentally aliases paths.
    protected = {job_path.resolve(), image_path, verified_file(root, job["source"]),
                 verified_file(root, snapshot["inventory"]),
                 verified_file(root, snapshot["uv_seam"]), verified_file(root, snapshot["uv_coverage"])}
    protected.update(verified_file(root, value) for value in snapshot["bundles"].values())
    for other in snapshot["maps"]:
        protected.add(verified_file(root, other["source"]))
        protected.add(local_path(root, other["editable"]))
        if other["id"] != entry["id"]:
            protected.add(local_path(root, other["candidate"]))
    if candidate_path in protected:
        raise ValueError("D 후보 경로가 입력 원본 또는 다른 작업 파일과 겹쳐요")
    raw = image_path.read_bytes()
    input_hash = hashlib.sha256(raw).hexdigest()
    with Image.open(source_path) as source_image:
        size = (entry["width"], entry["height"])
        if source_image.mode != "RGBA" or source_image.size != size:
            raise ValueError("원본 크기의 RGBA D가 필요해요")
        source = source_image.copy()
    with Image.open(BytesIO(raw)) as image:
        if image.format != "PNG" or image.mode not in {"RGB", "RGBA"}:
            raise ValueError("시안은 RGB 또는 RGBA PNG여야 해요")
        input_size, input_mode = image.size, image.mode
        if image.width * size[1] != image.height * size[0]:
            raise ValueError("시안과 원본의 종횡비가 달라요. 자르거나 늘리지 않습니다")
        rgb = image.convert("RGB")
        if rgb.size != size:
            rgb = rgb.resize(size, Image.Resampling.LANCZOS)
    output = rgb.convert("RGBA")
    output.putalpha(source.getchannel("A"))
    if entry["shared"] and not np.array_equal(np.array(source), np.array(output)):
        raise ValueError("공유 D 텍스처는 변경할 수 없어요")
    buffer = BytesIO()
    output.save(buffer, format="PNG")
    saved_input = local_path(root, f"drafts/{input_hash}/input.png")
    report_path = local_path(root, f"drafts/{input_hash}/adoption.json")
    # Reading the already archived input is safe; overwriting another input is not.
    if (report_path in protected | {candidate_path}
            or saved_input in (protected - {image_path}) | {candidate_path}):
        raise ValueError("시안 보관 경로가 기존 작업 파일과 겹쳐요")
    if saved_input.exists():
        if saved_input.read_bytes() != raw:
            raise ValueError("보관된 시안의 해시가 달라요")
    else:
        atomic_bytes(saved_input, raw)
    atomic_bytes(candidate_path, buffer.getvalue())
    report = {"schema_version": 1, "diffuse_mode": "generated-full", "map_id": entry["id"],
              "snapshot": job["source"], "source": entry["source"],
              "input": descriptor(root, saved_input), "input_size": list(input_size), "input_mode": input_mode,
              "output_size": list(size), "resampling": "Lanczos" if input_size != size else "none",
              "candidate": descriptor(root, candidate_path), "original_alpha_equal": True,
              "nontext_pixel_preservation_verified": False, "runtime_tested": False}
    write_json(report_path, report)
    job.update(diffuse_mode="generated-full", generated_diffuse=descriptor(root, report_path))
    write_json(job_path, job)
    return report


def verify_generated_derivation(root: Path, job: dict, snapshot: dict, entry: dict) -> dict:
    """An edited N/G must still belong to the selected D and exact lettering inputs."""
    report_path = root / "reports" / "derivation.json"
    if not report_path.is_file():
        raise ValueError("전체 D의 수정된 N/G에는 derive 기록이 필요해요")
    report = read_json(report_path)
    matches = [m for m in report.get("maps", []) if m["map_id"] == entry["id"]]
    if len(matches) != 1 or "inputs" not in matches[0]:
        raise ValueError("이 N/G의 글자 기준 기록이 없어요. derive를 실행하세요")
    value = matches[0]
    basis = value["inputs"]
    diffuse = diffuse_entry(snapshot)
    if (basis.get("diffuse_mode") != "generated-full" or basis.get("adoption") != job["generated_diffuse"]
            or verified_file(root, basis["diffuse"]) != local_path(root, diffuse["candidate"])):
        raise ValueError("N/G가 현재 선택한 D와 연결되지 않아요. derive를 다시 실행하세요")
    verified_file(root, basis["lettering"])
    if basis.get("selection"):
        verified_file(root, basis["selection"])
    for key in ("neutral", "old_effect"):
        verified_file(root, value[key])
    for key in ("candidate", "editable"):
        if verified_file(root, value[key]) != local_path(root, entry[key]):
            raise ValueError("N/G 파생 결과의 경로가 달라요")
    return {"report": descriptor(root, report_path), "inputs": basis,
            "neutral": value["neutral"], "old_effect": value["old_effect"]}
