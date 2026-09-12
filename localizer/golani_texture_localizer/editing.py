"""Compose approved image layers and derive effects from the same lettering alpha."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from .auxiliary import derive_linear_gloss, derive_packed_normal, project_binary_mask, project_master_alpha
from .bindings import check_projection
from .drafts import diffuse_entry, diffuse_mode, verify_adoption
from .files import atomic_bytes, descriptor, local_path, read_json, verified_file, write_json
from .jobs import load_job
from .validation import compare_pixels, mask, rgba


def save_image(path: Path, array: np.ndarray) -> None:
    buffer = BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    atomic_bytes(path, buffer.getvalue())


def map_entry(snapshot: dict, map_id: str) -> dict:
    matches = [e for e in snapshot["maps"] if e["id"] == map_id]
    if len(matches) != 1:
        raise ValueError(f"작업에 없는 map ID예요: {map_id}")
    return matches[0]


def seam_for(root: Path, snapshot: dict, size: tuple[int, int]) -> np.ndarray:
    with Image.open(verified_file(root, snapshot["uv_seam"])) as image:
        return project_binary_mask(np.array(image.convert("L")) != 0, size)


def compose(job_path: Path, recipe_path: Path) -> dict:
    root, job, snapshot = load_job(job_path)
    if diffuse_mode(job) != "masked":
        raise ValueError("전체 D 작업에는 compose를 섞지 않습니다. 문자 합성은 새 작업에서 진행하세요")
    recipe = read_json(recipe_path)
    entry = map_entry(snapshot, recipe["map_id"])
    if entry["role"] != "diffuse":
        raise ValueError("compose는 컬러 글자 레이어를 합성하는 명령이에요")
    size = (entry["width"], entry["height"])
    source = rgba(verified_file(root, entry["source"]), size)
    old_path, background_path, lettering_path = [local_path(root, recipe[k]) for k in ("old_text", "background", "lettering")]
    old = mask(old_path, size)
    background, lettering = rgba(background_path, size), rgba(lettering_path, size)
    output = source.copy()
    output[old, :3] = background[old, :3]
    alpha = lettering[..., 3:4].astype(np.float32) / 255.0
    output[..., :3] = np.rint(output[..., :3] * (1 - alpha) + lettering[..., :3] * alpha).astype(np.uint8)
    editable = old | (lettering[..., 3] > 0)
    result = compare_pixels(source, output, editable, entry["preserved_channels"], seam_for(root, snapshot, size), entry["shared"])
    if not result["passed"]:
        raise ValueError(f"합성 결과가 보존 조건을 위반해요: {result}")
    save_image(local_path(root, entry["candidate"]), output)
    save_image(local_path(root, entry["editable"]), editable.astype(np.uint8) * 255)
    report = {"map_id": entry["id"], "source": entry["source"],
              "lettering": descriptor(root, lettering_path), "background": descriptor(root, background_path),
              "old_text": descriptor(root, old_path), "candidate": descriptor(root, local_path(root, entry["candidate"])),
              "validation": result}
    write_json(root / "reports" / "lettering.json", report)
    return report


def derive(job_path: Path, recipe_path: Path) -> dict:
    root, job, snapshot = load_job(job_path)
    recipe = read_json(recipe_path)
    mode = diffuse_mode(job)
    if mode == "generated-full":
        adoption = verify_adoption(root, job, snapshot)
        diffuse = diffuse_entry(snapshot)
        if recipe.get("diffuse_sha256") != adoption["candidate"]["sha256"]:
            raise ValueError("글자 레시피의 D 해시가 현재 시안과 달라요")
        lettering_path = local_path(root, recipe["lettering"])
        alpha = rgba(lettering_path, (diffuse["width"], diffuse["height"]))[..., 3]
        basis = {"diffuse_mode": mode, "adoption": job["generated_diffuse"],
                 "diffuse": adoption["candidate"], "lettering": descriptor(root, lettering_path)}
    else:
        lettering = read_json(root / "reports" / "lettering.json")
        diffuse = map_entry(snapshot, lettering["map_id"])
        verified_file(root, lettering["candidate"])
        if lettering["source"] != diffuse["source"]:
            raise ValueError("글자 기준 원본이 현재 작업과 달라요")
        alpha = rgba(verified_file(root, lettering["lettering"]))[..., 3]
    selection = recipe.get("selection")
    if selection:
        selected = mask(local_path(root, selection), (alpha.shape[1], alpha.shape[0]))
        alpha = np.where(selected, alpha, 0).astype(np.uint8)
    selection_descriptor = descriptor(root, local_path(root, selection)) if selection else None
    if mode == "generated-full":
        basis["selection"] = selection_descriptor
    if not alpha.any():
        raise ValueError("재질 효과를 만들 글자가 없어요")
    maps = recipe.get("maps", [])
    if not maps or len({e["map_id"] for e in maps}) != len(maps):
        raise ValueError("중복 없이 파생할 보조맵을 지정하세요")
    previous = []
    if mode == "generated-full":
        previous_path = root / "reports" / "derivation.json"
        previous = read_json(previous_path).get("maps", []) if previous_path.is_file() else []
        if not isinstance(previous, list) or any(not isinstance(r, dict) or "map_id" not in r for r in previous):
            raise ValueError("기존 N/G 파생 기록 형식이 잘못됐어요")
    prepared, reports = [], []
    for spec in maps:
        entry = map_entry(snapshot, spec["map_id"])
        if entry["role"] not in {"normal", "gloss"}:
            raise ValueError("derive는 Normal/Gloss만 처리해요")
        check_projection(entry)
        if diffuse["wrap_u"] != 0 or diffuse["wrap_v"] != 0:
            raise ValueError("Diffuse도 U/V Repeat여야 해요")
        size = (entry["width"], entry["height"])
        source = rgba(verified_file(root, entry["source"]), size)
        neutral_path, old_path = local_path(root, spec["neutral"]), local_path(root, spec["old_effect"])
        neutral, old = rgba(neutral_path, size), mask(old_path, size)
        seam = seam_for(root, snapshot, size)
        if not compare_pixels(source, neutral, old, entry["preserved_channels"], seam)["passed"]:
            raise ValueError("원문 요철 제거 영역 밖이 변경됐어요")
        projected = project_master_alpha(alpha, size)
        if entry["role"] == "normal":
            if entry["format"] != 12:
                raise ValueError("Normal 파생은 확인된 DXT5nm G/A 형식만 지원해요")
            output, effect, info = derive_packed_normal(neutral, projected,
                height_scale_texels=spec["height_scale_texels"], polarity=spec["polarity"],
                bevel_passes=spec.get("bevel_passes", 1))
        else:
            output, effect, info = derive_linear_gloss(neutral, projected, channel_deltas=spec["channel_deltas"])
        editable = old | effect
        result = compare_pixels(source, output, editable, entry["preserved_channels"], seam)
        if not result["passed"]:
            raise ValueError(f"파생 결과가 보존 조건을 위반해요: {result}")
        prepared.append((entry, output, editable))
        reports.append({"map_id": entry["id"], "neutral": descriptor(root, neutral_path),
                        "old_effect": descriptor(root, old_path), "parameters": spec, **info, "validation": result})
    for (entry, output, editable), record in zip(prepared, reports, strict=True):
        save_image(local_path(root, entry["candidate"]), output)
        save_image(local_path(root, entry["editable"]), editable.astype(np.uint8) * 255)
        if mode == "generated-full":
            record.update(inputs=basis, candidate=descriptor(root, local_path(root, entry["candidate"])),
                          editable=descriptor(root, local_path(root, entry["editable"])))
    if mode == "generated-full":
        # N and G may be derived in separate requests. Keep each map's own basis;
        # stale entries will fail validation instead of inheriting a new D hash.
        replaced = {r["map_id"] for r in reports}
        report = {"diffuse_mode": mode, "maps": [r for r in previous if r["map_id"] not in replaced] + reports}
    else:
        report = {"lettering": descriptor(root, root / "reports" / "lettering.json"),
                  "selection": selection_descriptor, "maps": reports}
    write_json(root / "reports" / "derivation.json", report)
    return report
