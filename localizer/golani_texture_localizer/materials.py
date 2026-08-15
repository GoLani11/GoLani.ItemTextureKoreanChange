from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from .inventory import load_inventory, record_for_target
from .models import CollectionProfile, TargetSpec
from .names import safe_bundle_name
from .paths import ProjectPaths
from .review import approval_path, load_review, sha256_file, verify_approval


DIFFUSE_PROPERTIES = {"_MainTex", "_BaseMap", "_BaseColorMap"}
NORMAL_PROPERTIES = {"_BumpMap", "_NormalMap"}
GLOSS_PROPERTIES = {"_SpecMap", "_GlossMap", "_MetallicGlossMap"}


def _target_bindings(
    inventory: dict[str, Any],
    target: TargetSpec,
    diffuse_record: dict[str, Any],
) -> list[dict[str, Any]]:
    materials = inventory.get("materials")
    if not isinstance(materials, list) or not materials:
        raise ValueError(
            f"{target.id}: inventory에 Material graph가 없어요. extract를 다시 실행해 주세요"
        )
    diffuse_id = int(diffuse_record["path_id"])
    bindings = []
    for material in materials:
        slots = material.get("texture_slots", [])
        if not any(
            slot.get("property") in DIFFUSE_PROPERTIES
            and slot.get("path_id") == diffuse_id
            and slot.get("texture_bundle_key") == diffuse_record["bundle_key"]
            for slot in slots
        ):
            continue
        bindings.append(material)
    return bindings


def _auxiliary_slots(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[tuple[str, str, int], dict[str, Any]] = {}
    for material in bindings:
        for slot in material["texture_slots"]:
            if slot.get("property") in NORMAL_PROPERTIES:
                role = "normal"
            elif slot.get("property") in GLOSS_PROPERTIES:
                role = "gloss"
            else:
                continue
            if not slot.get("path_id") or not slot.get("texture") or not slot.get("texture_bundle_key"):
                raise ValueError(
                    f"{material['material']}::{slot.get('property')} 외부/미해결 텍스처 연결은 자동 처리하지 않아요"
                )
            value = {
                "material_bundle_key": material["bundle_key"],
                "material": material["material"],
                "material_path_id": material["path_id"],
                "property": slot["property"],
                "path_id": int(slot["path_id"]),
                "texture": slot["texture"],
                "texture_bundle_key": slot["texture_bundle_key"],
                "role": role,
                "scale": slot["scale"],
                "offset": slot["offset"],
            }
            found[(role, str(slot["texture_bundle_key"]), int(slot["path_id"]))] = value
    return list(found.values())


def _binding_signature(
    bindings: list[dict[str, Any]],
) -> list[tuple[str, str, str, str | None, int, str | None]]:
    relevant = DIFFUSE_PROPERTIES | NORMAL_PROPERTIES | GLOSS_PROPERTIES
    return sorted(
        (
            material["bundle_key"],
            material["material"],
            slot["property"],
            slot.get("texture_bundle_key"),
            int(slot["path_id"]),
            slot.get("texture"),
        )
        for material in bindings
        for slot in material["texture_slots"]
        if slot.get("property") in relevant and slot.get("path_id")
    )


def _all_consumers(inventory: dict[str, Any], bundle_key: str, path_id: int) -> list[dict[str, Any]]:
    consumers = []
    for material in inventory.get("materials", []):
        for slot in material.get("texture_slots", []):
            if slot.get("texture_bundle_key") == bundle_key and slot.get("path_id") == path_id:
                consumers.append(
                    {
                        "material": material["material"],
                        "material_bundle_key": material["bundle_key"],
                        "material_path_id": material["path_id"],
                        "property": slot["property"],
                    }
                )
    return consumers


def _slot_consumers(inventory: dict[str, Any], slot: dict[str, Any]) -> list[dict[str, Any]]:
    return _all_consumers(
        inventory,
        str(slot["texture_bundle_key"]),
        int(slot["path_id"]),
    )


def _register_auxiliary_plan(
    plans: dict[tuple[str, int, str], dict[str, Any]],
    slot: dict[str, Any],
    *,
    target_id: str,
    policy: str,
    old_text_mask_sha256: str,
    operation_signature: str = "",
) -> bool:
    key = (str(slot["texture_bundle_key"]), int(slot["path_id"]), str(slot["role"]))
    existing = plans.get(key)
    signature = (policy, old_text_mask_sha256, operation_signature)
    if existing is None:
        plans[key] = {
            "policy": policy,
            "old_text_mask_sha256": old_text_mask_sha256,
            "operation_signature": operation_signature,
            "target_ids": [target_id],
        }
        return True
    current = (
        existing["policy"],
        existing["old_text_mask_sha256"],
        existing.get("operation_signature", ""),
    )
    if current != signature:
        raise ValueError(
            f"공유 보조맵 {key[0]}::{key[1]} 정책/마스크가 target마다 달라요: "
            f"{existing['target_ids']}={current}, {target_id}={signature}"
        )
    existing["target_ids"].append(target_id)
    return False


def _material_mask_descriptor(
    material_data: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    descriptors = material_data.get("material_masks")
    if not isinstance(descriptors, dict):
        raise ValueError("neutralize_old_text 정책에는 material_masks가 필요해요")
    descriptor = descriptors.get(key)
    if not isinstance(descriptor, dict):
        raise ValueError(f"{key}: 재질 전용 old-text 마스크를 명시해야 해요")
    path = descriptor.get("path")
    checksum = descriptor.get("sha256")
    if not isinstance(path, str) or not path or not isinstance(checksum, str) or len(checksum) != 64:
        raise ValueError(f"{key}: 재질 전용 마스크 path/SHA-256이 잘못됐어요")
    method = descriptor.get("method")
    if method != "inpaint":
        raise ValueError(f"{key}: 재질 마스크 method는 inpaint여야 해요")
    return {"path": path, "sha256": checksum, "method": method}


def _map_record(inventory: dict[str, Any], bundle_key: str, path_id: int) -> dict[str, Any]:
    matches = [
        record
        for record in inventory["records"]
        if record["bundle_key"] == bundle_key and int(record["path_id"]) == path_id
    ]
    if len(matches) != 1:
        raise ValueError(f"Texture2D path ID {path_id}를 정확히 하나 찾지 못했어요")
    return matches[0]


def _mask(paths: ProjectPaths, descriptor: dict[str, Any], expected_size: tuple[int, int]) -> np.ndarray:
    path = (paths.root / descriptor["path"]).resolve()
    if sha256_file(path) != descriptor["sha256"]:
        raise ValueError(f"보조맵 마스크 SHA-256이 달라요: {path}")
    with Image.open(path) as image_file:
        if image_file.size != expected_size or image_file.mode not in {"1", "L"}:
            raise ValueError(f"보조맵 마스크 규격이 원본과 달라요: {path}")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(int(value) for value in np.unique(values)).issubset({0, 255}):
        raise ValueError(f"보조맵 마스크는 0/255만 사용해야 해요: {path}")
    return values == 255


def _neutralize_map(source: Path, mask: np.ndarray, role: str) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(source) as source_file:
        mode = source_file.mode
        image = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
    if mask.shape != image.shape[:2]:
        raise ValueError(f"{role} 마스크 크기가 맵과 달라요")
    output = image.copy()
    hard_mask = mask.astype(np.uint8) * 255
    if not mask.any():
        return Image.fromarray(output, "RGBA"), {"changed_pixels": 0, "changed_outside_mask": 0}
    radius = max(1, round(min(mask.shape) / 512 * 3))
    channels = (1, 3) if role == "normal" else (0, 1, 2, 3)
    if role not in {"normal", "gloss"}:
        raise ValueError(f"지원하지 않는 보조맵 역할이에요: {role}")
    for channel in channels:
        output[..., channel] = cv2.inpaint(
            image[..., channel], hard_mask, radius, cv2.INPAINT_TELEA
        )
    if role == "normal":
        # Tarkov의 DXT5nm packing에서 X=A, Y=G예요. R/B는 원본 그대로 둬요.
        x = output[..., 3].astype(np.float32) / 127.5 - 1.0
        y = output[..., 1].astype(np.float32) / 127.5 - 1.0
        length = np.maximum(1.0, np.sqrt(x * x + y * y))
        normalized_x = np.clip((x / length + 1.0) * 127.5, 0, 255).astype(np.uint8)
        normalized_y = np.clip((y / length + 1.0) * 127.5, 0, 255).astype(np.uint8)
        output[..., 3][mask] = normalized_x[mask]
        output[..., 1][mask] = normalized_y[mask]
    # gloss는 채널 레이아웃을 추측해 회색맵으로 재작성하지 않고 네 채널을 각각 복원해요.
    changed = np.any(output != image, axis=2)
    return Image.fromarray(output, "RGBA"), {
        "changed_pixels": int(changed.sum()),
        "changed_outside_mask": int((changed & ~mask).sum()),
        "mode_preserved": mode == "RGBA",
        "method": "inpaint",
    }


def _packed_normal_lighting(
    image: np.ndarray,
    light: tuple[float, float, float],
) -> np.ndarray:
    if image.ndim != 3 or image.shape[2] != 4:
        raise ValueError("normal 조명 진단 입력은 RGBA여야 해요")
    light_vector = np.asarray(light, dtype=np.float32)
    length = float(np.linalg.norm(light_vector))
    if length == 0:
        raise ValueError("normal 조명 방향 벡터가 0이에요")
    light_vector /= length
    x = image[..., 3].astype(np.float32) / 127.5 - 1.0
    y = image[..., 1].astype(np.float32) / 127.5 - 1.0
    z = np.sqrt(np.maximum(0.0, 1.0 - x * x - y * y))
    normal_length = np.maximum(1e-6, np.sqrt(x * x + y * y + z * z))
    shade = (
        x / normal_length * light_vector[0]
        + y / normal_length * light_vector[1]
        + z / normal_length * light_vector[2]
    )
    value = np.clip(np.round((shade * 0.5 + 0.5) * 255.0), 0, 255).astype(np.uint8)
    return np.repeat(value[..., None], 3, axis=2)


def derive_approved_materials(
    profile: CollectionProfile,
    paths: ProjectPaths,
    *,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    inventory = load_inventory(paths.inventory)
    outputs: list[dict[str, Any]] = []
    validated_targets: list[dict[str, Any]] = []
    auxiliary_plans: dict[tuple[str, int, str], dict[str, Any]] = {}
    for target in profile.targets:
        if target_ids is not None and target.id not in target_ids:
            continue
        approved = paths.approved / f"{target.id}.png"
        if target.action != "localize" or not approved.is_file():
            continue
        diffuse = record_for_target(inventory, target)
        source_diffuse = Path(diffuse["source_png"])
        approval = verify_approval(paths, target, source_diffuse, approved)
        review_path, review = load_review(paths, target.id, through="material")
        bindings = _target_bindings(inventory, target, diffuse)
        material_data = review["stages"]["material_validation"]["data"]
        graph_scope = material_data.get("graph_scope")
        if graph_scope != "resolved":
            raise ValueError(f"{target.id}: material graph_scope 판정이 없어요")
        if not bindings:
            raise ValueError(f"{target.id}: 실제 Material 연결을 찾지 못했어요")
        actual_aux = _auxiliary_slots(bindings)
        reviewed_bindings = material_data["bindings"]
        actual_signature = _binding_signature(bindings)
        reviewed_signature = sorted(
            (
                value.get("material_bundle_key"),
                value.get("material"),
                value.get("property"),
                value.get("texture_bundle_key"),
                int(value.get("path_id", 0)),
                value.get("texture"),
            )
            for value in reviewed_bindings
            if isinstance(value, dict)
        )
        if actual_signature != reviewed_signature:
            raise ValueError(f"{target.id}: 검토한 D/N/G 연결이 현재 Material graph와 달라요")
        policies = material_data.get("policies")
        if not isinstance(policies, dict):
            raise ValueError(f"{target.id}: 보조맵별 policies가 없어요")
        masks = review["stages"]["edit_plan"]["data"]["masks"]
        if material_data.get("text_mask_sha256") != masks["new_text"]["sha256"]:
            raise ValueError(f"{target.id}: material 글자 마스크가 edit plan과 달라요")
        validated_targets.append(
            {
                "target_id": target.id,
                "approved_sha256": approval["candidate_sha256"],
                "approval_sha256": sha256_file(approval_path(paths, target.id)),
                "review_sha256": sha256_file(review_path),
                "material_bindings": [list(value) for value in actual_signature],
            }
        )
        for slot in actual_aux:
            key = f"{slot['material']}::{slot['property']}"
            policy = policies.get(key)
            if policy not in {"preserve", "neutralize_old_text"}:
                raise ValueError(f"{target.id}: {key} 정책을 명시해야 해요")
            consumers = _slot_consumers(inventory, slot)
            reviewed_consumers = material_data.get("shared_consumers", {}).get(key)
            if reviewed_consumers != consumers:
                raise ValueError(f"{target.id}: {key} 공유 소비자 검토가 현재 graph와 달라요")
            material_mask = (
                _material_mask_descriptor(material_data, key)
                if policy == "neutralize_old_text"
                else None
            )
            first_operation = _register_auxiliary_plan(
                auxiliary_plans,
                slot,
                target_id=target.id,
                policy=policy,
                old_text_mask_sha256=(
                    material_mask["sha256"] if material_mask is not None else "preserve"
                ),
                operation_signature=(
                    json.dumps(material_mask, ensure_ascii=False, sort_keys=True)
                    if material_mask is not None
                    else "preserve"
                ),
            )
            if policy == "preserve":
                continue
            if len(consumers) > 1 and not material_data.get("shared_consumers_resolved"):
                raise ValueError(f"{target.id}: 공유 보조맵 {key} 충돌이 해결되지 않았어요")
            if not first_operation:
                continue
            record = _map_record(inventory, slot["texture_bundle_key"], slot["path_id"])
            source_map = Path(record["source_png"])
            with Image.open(source_map) as map_file:
                size = map_file.size
            if material_mask is None:
                raise AssertionError("neutralize_old_text 재질 마스크가 없어요")
            old_text = _mask(paths, material_mask, size)
            image, metrics = _neutralize_map(source_map, old_text, slot["role"])
            if metrics["changed_outside_mask"] != 0:
                raise AssertionError(f"{target.id} {key} 마스크 밖 픽셀이 바뀌었어요")
            if not metrics.get("mode_preserved", True):
                raise ValueError(f"{target.id} {key} 원본 색 모드가 RGBA가 아니에요")
            destination = paths.derived / safe_bundle_name(record["bundle_key"]) / f"{record['texture']}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination)
            outputs.append(
                {
                    "target_id": target.id,
                    "bundle_key": record["bundle_key"],
                    "texture": record["texture"],
                    "path_id": record["path_id"],
                    "role": slot["role"],
                    "policy": policy,
                    "source_png": str(source_map),
                    "source_sha256": sha256_file(source_map),
                    "approved_sha256": approval["candidate_sha256"],
                    "approval_sha256": sha256_file(approval_path(paths, target.id)),
                    "review": str(review_path),
                    "review_sha256": sha256_file(review_path),
                    "old_text_mask": material_mask["path"],
                    "old_text_mask_sha256": material_mask["sha256"],
                    "derived_png": str(destination),
                    "derived_sha256": sha256_file(destination),
                    "consumers": consumers,
                    "metrics": metrics,
                }
            )
    payload = {
        "schema_version": 2,
        "collection": profile.id,
        "partial": target_ids is not None,
        "target_ids": sorted(set(target_ids)) if target_ids is not None else None,
        "derived_count": len(outputs),
        "validated_targets": validated_targets,
        "auxiliary_plans": [
            {
                "texture_bundle_key": bundle_key,
                "path_id": path_id,
                "role": role,
                **plan,
            }
            for (bundle_key, path_id, role), plan in sorted(auxiliary_plans.items())
        ],
        "outputs": outputs,
    }
    paths.derived_manifest.parent.mkdir(parents=True, exist_ok=True)
    paths.derived_manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload
