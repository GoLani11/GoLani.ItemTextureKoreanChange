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
from .review import (
    approval_path,
    load_review,
    review_stage_sha256,
    sha256_file,
    verify_approval,
)


DIFFUSE_PROPERTIES = {"_MainTex", "_BaseMap", "_BaseColorMap"}
NORMAL_PROPERTIES = {"_BumpMap", "_NormalMap"}
GLOSS_PROPERTIES = {"_SpecMap", "_GlossMap", "_MetallicGlossMap"}
CHANNEL_INDEX = {"R": 0, "G": 1, "B": 2, "A": 3}


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
    found: list[dict[str, Any]] = []
    roles_by_texture: dict[tuple[str, int], set[str]] = {}
    for material in bindings:
        for slot in material["texture_slots"]:
            if slot.get("property") in NORMAL_PROPERTIES:
                role = "normal"
            elif slot.get("property") in GLOSS_PROPERTIES:
                role = "gloss"
            else:
                continue
            if (
                not slot.get("path_id")
                and not slot.get("texture")
                and not slot.get("texture_bundle_key")
            ):
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
            texture_key = (str(slot["texture_bundle_key"]), int(slot["path_id"]))
            roles_by_texture.setdefault(texture_key, set()).add(role)
            found.append(value)
    conflicting = sorted(key for key, roles in roles_by_texture.items() if len(roles) > 1)
    if conflicting:
        raise ValueError(f"같은 보조맵 PPtr가 Normal/Gloss 역할을 함께 사용해요: {conflicting}")
    return found


def _binding_signature(
    bindings: list[dict[str, Any]],
) -> list[tuple[Any, ...]]:
    relevant = DIFFUSE_PROPERTIES | NORMAL_PROPERTIES | GLOSS_PROPERTIES
    return sorted(
        (
            material["bundle_key"],
            material["material"],
            slot["property"],
            slot.get("texture_bundle_key"),
            int(slot["path_id"]),
            slot.get("texture"),
            tuple(float(value) for value in slot.get("scale", [1.0, 1.0])),
            tuple(float(value) for value in slot.get("offset", [0.0, 0.0])),
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
                        "scale": slot.get("scale", [1.0, 1.0]),
                        "offset": slot.get("offset", [0.0, 0.0]),
                    }
                )
    return consumers


def _slot_consumers(inventory: dict[str, Any], slot: dict[str, Any]) -> list[dict[str, Any]]:
    return _all_consumers(
        inventory,
        str(slot["texture_bundle_key"]),
        int(slot["path_id"]),
    )


def _verify_auxiliary_contract_entry(
    entry: Any,
    slot: dict[str, Any],
    record: dict[str, Any],
    source_map: Path,
    material_mask: dict[str, Any] | None,
    project_root: Path,
) -> tuple[int, ...] | None:
    if not isinstance(entry, dict):
        raise ValueError("보조맵 source-base 계약이 없어요")
    expected_identity = {
        "texture_bundle_key": record["bundle_key"],
        "path_id": int(record["path_id"]),
        "texture": record["texture"],
        "role": slot["role"],
        "width": int(record["width"]),
        "height": int(record["height"]),
        "format": int(record["format"]),
        "uv_scale": list(slot.get("scale", [1.0, 1.0])),
        "uv_offset": list(slot.get("offset", [0.0, 0.0])),
    }
    if entry.get("identity") != expected_identity:
        raise ValueError("보조맵 source-base identity나 UV ST가 현재 inventory와 달라요")
    source_descriptor = entry.get("source_map")
    if (
        not isinstance(source_descriptor, dict)
        or source_descriptor.get("sha256") != sha256_file(source_map)
    ):
        raise ValueError("보조맵 source-base SHA-256이 현재 원본과 달라요")
    source_path = source_descriptor.get("path")
    if not isinstance(source_path, str) or not source_path:
        raise ValueError("보조맵 source-base 경로가 없어요")
    contract_source = (project_root / source_path).resolve()
    try:
        contract_source.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError("보조맵 source-base 경로가 프로젝트 밖을 가리켜요") from exc
    if contract_source != source_map.resolve():
        raise ValueError("보조맵 source-base 경로가 현재 inventory 원본과 달라요")
    if entry.get("whole_map_generated") is not False:
        raise ValueError("보조맵 전체 이미지 생성은 허용하지 않아요")
    if material_mask is None:
        return None
    channel_contract = entry.get("channel_contract")
    if not isinstance(channel_contract, dict):
        raise ValueError("수정할 보조맵의 채널 계약이 없어요")
    used_channels = channel_contract.get("used_channels")
    if (
        not isinstance(used_channels, list)
        or not used_channels
        or any(channel not in CHANNEL_INDEX for channel in used_channels)
    ):
        raise ValueError("수정할 보조맵의 사용 채널 계약이 잘못됐어요")
    if slot["role"] == "normal" and (
        channel_contract.get("packing") != "dxt5nm-x-a-y-g"
        or used_channels != ["G", "A"]
    ):
        raise ValueError("Normal은 확인된 DXT5nm G/A 채널만 수정해야 해요")
    if material_mask["method"] == "patch":
        expected_signature = "patch-copy:v1"
    else:
        radius = max(1, round(min(int(record["width"]), int(record["height"])) / 512 * 3))
        expected_signature = f"opencv-telea:v1:radius={radius}"
    if entry.get("neutralization_signature") != expected_signature:
        raise ValueError("보조맵 중립화 알고리즘 계약이 현재 derive 구현과 달라요")
    return tuple(CHANNEL_INDEX[channel] for channel in used_channels)


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
    if method not in {"inpaint", "patch"}:
        raise ValueError(f"{key}: 재질 마스크 method는 inpaint 또는 patch여야 해요")
    result = {"path": path, "sha256": checksum, "method": method}
    if method == "patch":
        patch_path = descriptor.get("patch")
        patch_checksum = descriptor.get("patch_sha256")
        if (
            not isinstance(patch_path, str)
            or not patch_path
            or not isinstance(patch_checksum, str)
            or len(patch_checksum) != 64
        ):
            raise ValueError(f"{key}: patch path/SHA-256이 잘못됐어요")
        result.update({"patch": patch_path, "patch_sha256": patch_checksum})
    return result


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


def _neutralize_map(
    source: Path,
    mask: np.ndarray,
    role: str,
    *,
    patch: np.ndarray | None = None,
    channels: tuple[int, ...] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(source) as source_file:
        mode = source_file.mode
        image = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
    if mask.shape != image.shape[:2]:
        raise ValueError(f"{role} 마스크 크기가 맵과 달라요")
    output = image.copy()
    if channels is None:
        channels = (1, 3) if role == "normal" else (0, 1, 2, 3)
    if (
        not channels
        or len(set(channels)) != len(channels)
        or any(channel not in {0, 1, 2, 3} for channel in channels)
    ):
        raise ValueError(f"{role} 수정 채널이 잘못됐어요")
    if role not in {"normal", "gloss"}:
        raise ValueError(f"지원하지 않는 보조맵 역할이에요: {role}")
    if patch is not None:
        if patch.shape != image.shape or patch.dtype != np.uint8:
            raise ValueError(f"{role} 복원 patch 규격이 원본과 달라요")
        method = "patch"
    else:
        method = "inpaint"
    if not mask.any():
        return Image.fromarray(output, "RGBA"), {
            "changed_pixels": 0,
            "changed_outside_mask": 0,
            "changed_unselected_channels": 0,
            "selected_channels": ["RGBA"[channel] for channel in channels],
            "mode_preserved": mode == "RGBA",
            "method": method,
        }
    hard_mask = mask.astype(np.uint8) * 255
    radius = max(1, round(min(mask.shape) / 512 * 3))
    if patch is not None:
        for channel in channels:
            output[..., channel][mask] = patch[..., channel][mask]
    else:
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
    # Gloss는 확인된 채널만 독립 복원하고 나머지 채널은 원본 그대로 둬요.
    changed = np.any(output != image, axis=2)
    unselected = tuple(channel for channel in range(4) if channel not in channels)
    changed_unselected = (
        int((output[..., unselected] != image[..., unselected]).sum())
        if unselected
        else 0
    )
    return Image.fromarray(output, "RGBA"), {
        "changed_pixels": int(changed.sum()),
        "changed_outside_mask": int((changed & ~mask).sum()),
        "changed_unselected_channels": changed_unselected,
        "selected_channels": ["RGBA"[channel] for channel in channels],
        "mode_preserved": mode == "RGBA",
        "method": method,
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
        material_review_sha256 = review_stage_sha256(review, "material_validation")
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
                tuple(float(item) for item in value.get("scale", [])),
                tuple(float(item) for item in value.get("offset", [])),
            )
            for value in reviewed_bindings
            if isinstance(value, dict)
        )
        if actual_signature != reviewed_signature:
            raise ValueError(f"{target.id}: 검토한 D/N/G 연결이 현재 Material graph와 달라요")
        policies = material_data.get("policies")
        if not isinstance(policies, dict):
            raise ValueError(f"{target.id}: 보조맵별 policies가 없어요")
        contract = material_data.get("auxiliary_contract")
        contract_maps = contract.get("maps") if isinstance(contract, dict) else None
        if not isinstance(contract_maps, dict):
            raise ValueError(f"{target.id}: 보조맵 source-base 계약이 없어요")
        masks = review["stages"]["edit_plan"]["data"]["masks"]
        if material_data.get("text_mask_sha256") != masks["new_text"]["sha256"]:
            raise ValueError(f"{target.id}: material 글자 마스크가 edit plan과 달라요")
        validated_targets.append(
            {
                "target_id": target.id,
                "approved_sha256": approval["candidate_sha256"],
                "approval_sha256": sha256_file(approval_path(paths, target.id)),
                "review_sha256": sha256_file(review_path),
                "material_review_sha256": material_review_sha256,
                "material_bindings": [list(value) for value in actual_signature],
            }
        )
        for slot in actual_aux:
            key = f"{slot['material']}::{slot['property']}"
            policy = policies.get(key)
            if policy not in {"preserve", "neutralize_old_text"}:
                raise ValueError(f"{target.id}: {key} 정책을 명시해야 해요")
            consumers = _slot_consumers(inventory, slot)
            consumer_roles = {
                "normal"
                if consumer.get("property") in NORMAL_PROPERTIES
                else "gloss"
                if consumer.get("property") in GLOSS_PROPERTIES
                else "other"
                for consumer in consumers
            }
            if len(consumer_roles) != 1 or "other" in consumer_roles:
                raise ValueError(f"{target.id}: {key} 공유 PPtr의 재질 역할이 서로 달라요")
            reviewed_consumers = material_data.get("shared_consumers", {}).get(key)
            if reviewed_consumers != consumers:
                raise ValueError(f"{target.id}: {key} 공유 소비자 검토가 현재 graph와 달라요")
            material_mask = (
                _material_mask_descriptor(material_data, key)
                if policy == "neutralize_old_text"
                else None
            )
            record = _map_record(inventory, slot["texture_bundle_key"], slot["path_id"])
            source_map = Path(record["source_png"])
            channel_indices = _verify_auxiliary_contract_entry(
                contract_maps.get(key),
                slot,
                record,
                source_map,
                material_mask,
                paths.root,
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
                    json.dumps(
                        {
                            "material_mask": material_mask,
                            "identity": contract_maps[key]["identity"],
                            "channel_contract": contract_maps[key]["channel_contract"],
                            "neutralization_signature": contract_maps[key][
                                "neutralization_signature"
                            ],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
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
            with Image.open(source_map) as map_file:
                size = map_file.size
            if material_mask is None:
                raise AssertionError("neutralize_old_text 재질 마스크가 없어요")
            old_text = _mask(paths, material_mask, size)
            patch_values = None
            if material_mask["method"] == "patch":
                patch_path = (paths.root / material_mask["patch"]).resolve()
                if sha256_file(patch_path) != material_mask["patch_sha256"]:
                    raise ValueError(f"{target.id} {key} 복원 patch SHA-256이 달라요")
                with Image.open(patch_path) as patch_file:
                    if patch_file.mode != "RGBA" or patch_file.size != size:
                        raise ValueError(f"{target.id} {key} 복원 patch 규격이 원본과 달라요")
                    patch_values = np.asarray(patch_file, dtype=np.uint8)
            image, metrics = _neutralize_map(
                source_map,
                old_text,
                slot["role"],
                patch=patch_values,
                channels=channel_indices,
            )
            if metrics["changed_outside_mask"] != 0:
                raise AssertionError(f"{target.id} {key} 마스크 밖 픽셀이 바뀌었어요")
            if metrics["changed_unselected_channels"] != 0:
                raise AssertionError(f"{target.id} {key} 미사용 채널이 바뀌었어요")
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
                    "material_review_sha256": material_review_sha256,
                    "old_text_mask": material_mask["path"],
                    "old_text_mask_sha256": material_mask["sha256"],
                    "restoration_patch": material_mask.get("patch"),
                    "restoration_patch_sha256": material_mask.get("patch_sha256"),
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
