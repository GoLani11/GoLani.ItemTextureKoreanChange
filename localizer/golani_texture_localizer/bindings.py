"""Resolve editable maps using actual Material pointers, never filename suffixes."""
from __future__ import annotations

from .auxiliary import validate_same_uv_projection

PROPERTIES = {
    "_MainTex": "diffuse", "_BaseMap": "diffuse", "_BaseColorMap": "diffuse",
    "_BumpMap": "normal", "_NormalMap": "normal",
    "_SpecMap": "gloss", "_GlossMap": "gloss", "_MetallicGlossMap": "gloss",
}


def pointer(slot: dict) -> tuple:
    return slot.get("texture_bundle_key"), slot.get("path_id")


def target_maps(inventory: dict, target_id: str) -> list[dict]:
    records = inventory["records"]
    targets = [r for r in records if r.get("target_id") == target_id]
    if len(targets) != 1:
        raise ValueError(f"{target_id}의 원본 Texture2D가 하나여야 해요")
    target = targets[0]
    identity = (target["bundle_key"], target["path_id"])
    connected = []
    for material in inventory["materials"]:
        mains = [s for s in material["texture_slots"] if PROPERTIES.get(s["property"]) == "diffuse"]
        for main in mains:
            if pointer(main) == identity:
                connected.append((material, main))
    if not connected:
        raise ValueError("대상 텍스처를 사용하는 Material을 찾지 못했어요")
    found = {}
    for material, main in connected:
        for slot in material["texture_slots"]:
            role = PROPERTIES.get(slot["property"])
            if role is None or not slot["path_id"]:
                continue
            if role == "diffuse" and pointer(slot) != identity:
                raise ValueError("하나의 Material에 서로 다른 컬러 입력이 있어요")
            matches = [r for r in records if (r["bundle_key"], r["path_id"]) == pointer(slot)]
            if len(matches) != 1:
                raise ValueError(f"연결된 {slot['property']}의 실제 Texture2D를 확인할 수 없어요")
            record = matches[0]
            expected_assets = slot.get("external_assets_file") or material["assets_file"]
            if record["assets_file"].casefold() != expected_assets.casefold():
                raise ValueError("Material PPtr와 serialized assets file이 달라요")
            key = pointer(slot)
            if key not in found:
                found[key] = {"record": record, "role": role, "bindings": [], "shared": False}
            elif found[key]["role"] != role:
                raise ValueError("같은 Texture2D가 서로 다른 재질 역할로 연결돼 있어요")
            found[key]["bindings"].append({
                "material": material["material"], "bundle_key": material["bundle_key"],
                "assets_file": material["assets_file"], "path_id": material["path_id"],
                "property": slot["property"], "scale": slot["scale"], "offset": slot["offset"],
                "diffuse_scale": main["scale"], "diffuse_offset": main["offset"],
            })
    # Editing a shared auxiliary map would also change a different item's material.
    for key, value in found.items():
        for material in inventory["materials"]:
            if not any(pointer(s) == key for s in material["texture_slots"]):
                continue
            mains = [pointer(s) for s in material["texture_slots"]
                     if PROPERTIES.get(s["property"]) == "diffuse" and s["path_id"]]
            if mains != [identity]:
                value["shared"] = True
    return sorted(found.values(), key=lambda v: (v["role"], v["record"]["texture"]))


def check_projection(entry: dict) -> None:
    if entry["shared"]:
        raise ValueError("다른 컬러 텍스처와 공유하는 보조맵은 보존해야 해요")
    if entry["wrap_u"] != 0 or entry["wrap_v"] != 0:
        raise ValueError("보조맵 파생은 U/V Repeat에서만 지원해요")
    for binding in entry["bindings"]:
        validate_same_uv_projection(binding["diffuse_scale"], binding["diffuse_offset"],
                                    binding["scale"], binding["offset"])
