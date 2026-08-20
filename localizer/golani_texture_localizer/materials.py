from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from .auxiliary import (
    GLOSS_SIGNATURE,
    NORMAL_SIGNATURE,
    PROJECTION_SIGNATURE,
    derive_linear_gloss,
    derive_packed_normal,
    pack_dxt5nm_xy,
    project_binary_mask,
    project_master_alpha,
    projection_alignment_metrics,
    validate_same_uv_projection,
)
from .inventory import _material_identity, load_inventory, record_for_target
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
DERIVATION_POLICIES = {"preserve", "neutralize_old_text", "neutralize_and_derive"}
_V_AXIS = "png-top-left+unity-v-up"
_PHYSICAL_COMPONENT = "all-selected-lettering-alpha"


def _canonical_master_lettering(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list) or not values:
        raise ValueError("master lettering 계약이 비어 있거나 배열이 아니에요")
    result: list[dict[str, str]] = []
    region_ids: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("master lettering 항목이 객체가 아니에요")
        region_id = value.get("region_id")
        selected_sha = value.get("selected_lettering_sha256")
        mask_sha = value.get("lettering_mask_sha256")
        if not isinstance(region_id, str) or not region_id or region_id in region_ids:
            raise ValueError("master lettering region_id가 비어 있거나 중복됐어요")
        for label, checksum in (
            ("selected_lettering_sha256", selected_sha),
            ("lettering_mask_sha256", mask_sha),
        ):
            if not isinstance(checksum, str) or len(checksum) != 64:
                raise ValueError(f"master lettering {label}가 SHA-256이 아니에요")
            try:
                int(checksum, 16)
            except ValueError as exc:
                raise ValueError(
                    f"master lettering {label}가 SHA-256이 아니에요"
                ) from exc
        region_ids.add(region_id)
        result.append(
            {
                "region_id": region_id,
                "selected_lettering_sha256": selected_sha,
                "lettering_mask_sha256": mask_sha,
            }
        )
    return sorted(result, key=lambda value: value["region_id"])


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
                "material_assets_file": material["assets_file"],
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
    contract_signatures: dict[str, set[tuple[Any, ...]]] = {}
    for value in found:
        key = f"{value['material']}::{value['property']}"
        contract_signatures.setdefault(key, set()).add(
            (
                value["role"],
                value["texture_bundle_key"],
                int(value["path_id"]),
                value["texture"],
                tuple(float(item) for item in value["scale"]),
                tuple(float(item) for item in value["offset"]),
            )
        )
    conflicting_contracts = sorted(
        key for key, signatures in contract_signatures.items() if len(signatures) != 1
    )
    if conflicting_contracts:
        raise ValueError(
            "같은 Material 이름::property 계약 키가 서로 다른 보조맵/ST를 가리켜요: "
            f"{conflicting_contracts}"
        )
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
            material["assets_file"],
            int(material["path_id"]),
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
                        "material_assets_file": material["assets_file"],
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


def _diffuse_slot_for_auxiliary(
    bindings: list[dict[str, Any]],
    auxiliary_slot: dict[str, Any],
    diffuse_record: dict[str, Any],
) -> dict[str, Any]:
    materials = [
        material
        for material in bindings
        if material.get("bundle_key") == auxiliary_slot.get("material_bundle_key")
        and material.get("assets_file")
        == auxiliary_slot.get("material_assets_file")
        and material.get("material") == auxiliary_slot.get("material")
        and int(material.get("path_id", 0)) == int(auxiliary_slot.get("material_path_id", 0))
    ]
    if len(materials) != 1:
        raise ValueError("보조맵과 같은 Material의 Diffuse 슬롯을 정확히 하나 찾지 못했어요")
    slots = [
        slot
        for slot in materials[0].get("texture_slots", [])
        if slot.get("property") in DIFFUSE_PROPERTIES
        and slot.get("texture_bundle_key") == diffuse_record.get("bundle_key")
        and int(slot.get("path_id", 0)) == int(diffuse_record.get("path_id", 0))
    ]
    signatures = {
        (
            tuple(float(value) for value in slot.get("scale", [])),
            tuple(float(value) for value in slot.get("offset", [])),
        )
        for slot in slots
    }
    if len(slots) < 1 or len(signatures) != 1:
        raise ValueError("보조맵과 같은 Material의 Diffuse UV ST가 모호해요")
    return slots[0]


def _consumer_target_ids(
    inventory: dict[str, Any],
    consumers: list[dict[str, Any]],
) -> set[str]:
    records = {
        (str(record["bundle_key"]), int(record["path_id"])): str(record["target_id"])
        for record in inventory.get("records", [])
        if record.get("target_id")
    }
    material_ids = {
        (
            str(consumer["material_bundle_key"]),
            str(consumer["material_assets_file"]),
            int(consumer["material_path_id"]),
        )
        for consumer in consumers
    }
    result: set[str] = set()
    resolved_material_ids: set[tuple[str, str, int]] = set()
    for material in inventory.get("materials", []):
        identity = _material_identity(material)
        if identity not in material_ids:
            continue
        diffuse_slots = [
            slot
            for slot in material.get("texture_slots", [])
            if slot.get("property") in DIFFUSE_PROPERTIES
            and int(slot.get("path_id", 0)) != 0
        ]
        material_targets: set[str] = set()
        fully_mapped = bool(diffuse_slots)
        for slot in diffuse_slots:
            target_id = records.get(
                (str(slot.get("texture_bundle_key")), int(slot.get("path_id", 0)))
            )
            if target_id:
                material_targets.add(target_id)
            else:
                fully_mapped = False
        if fully_mapped and material_targets:
            result.update(material_targets)
            resolved_material_ids.add(identity)
    if resolved_material_ids != material_ids:
        result.add("__unmapped_consumer__")
    return result


def _project_artifact_path(
    paths: ProjectPaths,
    descriptor: Any,
    label: str,
) -> Path:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{label} artifact가 없어요")
    value = descriptor.get("path")
    checksum = descriptor.get("sha256")
    if not isinstance(value, str) or not value or not isinstance(checksum, str):
        raise ValueError(f"{label} path/SHA-256이 잘못됐어요")
    path = (paths.root / value).resolve()
    try:
        path.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} 경로가 프로젝트 밖을 가리켜요") from exc
    if not path.is_file() or sha256_file(path) != checksum:
        raise ValueError(f"{label} 현재 파일 SHA-256이 기록과 달라요")
    return path


def _verify_effect_measurement(
    paths: ProjectPaths,
    descriptor: Any,
    *,
    role: str,
    parameters: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    path = _project_artifact_path(paths, descriptor, "effect_measurement")
    try:
        measurement = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("effect_measurement는 UTF-8 JSON이어야 해요") from exc
    source_map = entry.get("source_map")
    expected_method = (
        "controlled-lighting-fit" if role == "normal" else "source-effect-sampling"
    )
    expected = {
        "schema_version": 1,
        "role": role,
        "method": expected_method,
        "source_map_sha256": (
            source_map.get("sha256") if isinstance(source_map, dict) else None
        ),
        "source_effect_mask_sha256": entry.get("source_effect_mask_sha256"),
        "measured_parameters": parameters,
    }
    if not isinstance(measurement, dict) or any(
        measurement.get(field) != value for field, value in expected.items()
    ):
        raise ValueError("effect_measurement 내용이 source/effect_parameters 계약과 달라요")
    sample_count = measurement.get("sample_count")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 1:
        raise ValueError("effect_measurement.sample_count는 1 이상이어야 해요")


def _master_lettering_alpha(
    paths: ProjectPaths,
    edit_data: dict[str, Any],
    expected_size: tuple[int, int],
) -> tuple[np.ndarray, dict[str, np.ndarray], list[dict[str, str]]]:
    compositor = edit_data.get("compositor")
    regions = compositor.get("regions") if isinstance(compositor, dict) else None
    if not isinstance(regions, list) or not regions:
        raise ValueError("승인된 compositor selected_lettering 영역이 없어요")
    combined = np.zeros((expected_size[1], expected_size[0]), dtype=np.uint8)
    occupied = np.zeros_like(combined, dtype=bool)
    per_region: dict[str, np.ndarray] = {}
    records: list[dict[str, str]] = []
    for index, region in enumerate(regions):
        label = f"compositor.regions[{index}]"
        if not isinstance(region, dict) or not isinstance(region.get("region_id"), str):
            raise ValueError(f"{label}.region_id가 잘못됐어요")
        region_id = str(region["region_id"])
        if not region_id or region_id in per_region:
            raise ValueError(f"{label}.region_id가 비어 있거나 중복됐어요")
        lettering_path = _project_artifact_path(
            paths, region.get("selected_lettering"), f"{label}.selected_lettering"
        )
        mask_path = _project_artifact_path(
            paths, region.get("lettering_mask"), f"{label}.lettering_mask"
        )
        with Image.open(lettering_path) as lettering_file:
            if lettering_file.mode != "RGBA" or lettering_file.size != expected_size:
                raise ValueError(f"{label}.selected_lettering 규격이 Diffuse mip0와 달라요")
            alpha = np.asarray(lettering_file, dtype=np.uint8)[..., 3]
        with Image.open(mask_path) as mask_file:
            if mask_file.mode not in {"1", "L"} or mask_file.size != expected_size:
                raise ValueError(f"{label}.lettering_mask 규격이 Diffuse mip0와 달라요")
            mask_values = np.asarray(mask_file.convert("L"), dtype=np.uint8)
        if not set(int(value) for value in np.unique(mask_values)).issubset({0, 255}):
            raise ValueError(f"{label}.lettering_mask는 0/255만 사용해야 해요")
        visible = alpha > 0
        if not visible.any() or not np.array_equal(visible, mask_values == 255):
            raise ValueError(f"{label} 연속 알파 support가 lettering_mask와 달라요")
        if np.any(visible & occupied):
            raise ValueError(f"{label} master lettering이 다른 영역과 겹쳐요")
        combined[visible] = alpha[visible]
        occupied |= visible
        per_region[region_id] = alpha
        records.append(
            {
                "region_id": region_id,
                "selected_lettering_sha256": sha256_file(lettering_path),
                "lettering_mask_sha256": sha256_file(mask_path),
            }
        )
    return combined, per_region, _canonical_master_lettering(records)


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
    texture_format = int(record["format"])
    if slot["role"] == "normal":
        if (
            texture_format != 12
            or channel_contract.get("packing") != "dxt5nm-x-a-y-g"
            or used_channels != ["G", "A"]
        ):
            raise ValueError("Normal은 TextureFormat DXT5(12)의 확인된 G/A만 수정해야 해요")
    elif slot["role"] == "gloss":
        if texture_format not in {10, 12}:
            raise ValueError("Gloss v1은 DXT1(10) 또는 DXT5(12)만 지원해요")
        if texture_format == 10 and any(channel == "A" for channel in used_channels):
            raise ValueError("DXT1 Gloss는 연속 alpha 효과를 표현할 수 없어 RGB만 수정해야 해요")
    if material_mask["method"] == "patch":
        expected_signature = "patch-copy:v1"
    else:
        radius = max(1, round(min(int(record["width"]), int(record["height"])) / 512 * 3))
        expected_signature = f"opencv-telea:v1:radius={radius}"
    if entry.get("neutralization_signature") != expected_signature:
        raise ValueError("보조맵 중립화 알고리즘 계약이 현재 derive 구현과 달라요")
    return tuple(CHANNEL_INDEX[channel] for channel in used_channels)


def _verify_derivation_contract(
    paths: ProjectPaths,
    entry: dict[str, Any],
    *,
    role: str,
    used_channels: tuple[int, ...],
    diffuse_record: dict[str, Any],
    diffuse_slot: dict[str, Any],
    auxiliary_record: dict[str, Any],
    auxiliary_slot: dict[str, Any],
    master_records: list[dict[str, str]],
) -> dict[str, Any]:
    derivation = entry.get("derivation")
    if not isinstance(derivation, dict) or derivation.get("schema_version") != 1:
        raise ValueError("neutralize_and_derive에는 derivation v1 계약이 필요해요")
    expected_signature = NORMAL_SIGNATURE if role == "normal" else GLOSS_SIGNATURE
    expected_effect = "master-alpha-relief" if role == "normal" else "master-alpha-gloss"
    if derivation.get("producer") != expected_signature:
        raise ValueError("보조맵 파생 producer signature가 현재 구현과 달라요")
    if entry.get("effect_kind") != expected_effect:
        raise ValueError(f"{role} effect_kind가 현재 파생 역할과 달라요")
    if derivation.get("physical_component") != _PHYSICAL_COMPONENT:
        raise ValueError(
            "v1 파생은 selected_lettering 전체 알파가 물리 효과라는 검증이 필요해요"
        )
    for label, record in (
        ("Diffuse", diffuse_record),
        ("보조맵", auxiliary_record),
    ):
        if int(record.get("wrap_u", -1)) != 0 or int(record.get("wrap_v", -1)) != 0:
            raise ValueError(f"v1 파생은 {label} U/V Repeat wrap만 지원해요")
    expected_regions = sorted(record["region_id"] for record in master_records)
    if derivation.get("master_region_ids") != expected_regions:
        raise ValueError("보조맵 파생 영역이 승인된 master lettering 전체와 달라요")

    projection = derivation.get("projection")
    expected_projection = {
        "signature": PROJECTION_SIGNATURE,
        "source_size": [int(diffuse_record["width"]), int(diffuse_record["height"])],
        "target_size": [int(auxiliary_record["width"]), int(auxiliary_record["height"])],
        "diffuse_uv_scale": list(diffuse_slot.get("scale", [1.0, 1.0])),
        "diffuse_uv_offset": list(diffuse_slot.get("offset", [0.0, 0.0])),
        "auxiliary_uv_scale": list(auxiliary_slot.get("scale", [1.0, 1.0])),
        "auxiliary_uv_offset": list(auxiliary_slot.get("offset", [0.0, 0.0])),
        "v_axis": _V_AXIS,
        "texel_center_sampling": True,
    }
    if projection != expected_projection:
        raise ValueError("보조맵 파생 projection 계약이 현재 Material/Texture와 달라요")
    validate_same_uv_projection(
        expected_projection["diffuse_uv_scale"],
        expected_projection["diffuse_uv_offset"],
        expected_projection["auxiliary_uv_scale"],
        expected_projection["auxiliary_uv_offset"],
    )
    limits = derivation.get("alignment_limits")
    if limits != {
        "center_error_texels": 0.5,
        "bbox_edge_error_texels": 1.0,
        "rotation_error_deg": 0.0,
    }:
        raise ValueError("보조맵 파생 정렬 허용치가 안전 계약과 달라요")
    parameters = derivation.get("effect_parameters")
    if not isinstance(parameters, dict):
        raise ValueError("보조맵 effect_parameters가 없어요")
    channel_names = ["RGBA"[index] for index in used_channels]
    if role == "normal":
        if channel_names != ["G", "A"] or set(parameters) != {
            "height_scale_texels",
            "polarity",
            "bevel_passes",
        }:
            raise ValueError("Normal effect_parameters 또는 G/A 채널 계약이 잘못됐어요")
        # 수치 범위 검증은 pure producer에서도 다시 수행해요.
        probe_alpha = np.zeros((3, 3), dtype=np.uint8)
        probe_alpha[1, 1] = 255
        derive_packed_normal(
            np.full((3, 3, 4), 128, dtype=np.uint8),
            probe_alpha,
            height_scale_texels=parameters.get("height_scale_texels"),
            polarity=parameters.get("polarity"),
            bevel_passes=parameters.get("bevel_passes"),
        )
    else:
        deltas = parameters.get("channel_deltas")
        if set(parameters) != {"channel_deltas"} or not isinstance(deltas, dict):
            raise ValueError("Gloss effect_parameters.channel_deltas가 없어요")
        if list(deltas) != channel_names:
            raise ValueError("Gloss delta 채널 순서가 검증된 used_channels와 달라요")
        derive_linear_gloss(
            np.full((1, 1, 4), 128, dtype=np.uint8),
            np.full((1, 1), 255, dtype=np.uint8),
            channel_deltas=deltas,
        )
    _verify_effect_measurement(
        paths,
        derivation.get("effect_measurement"),
        role=role,
        parameters=parameters,
        entry=entry,
    )
    return derivation


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
    if target_id not in existing["target_ids"]:
        existing["target_ids"].append(target_id)
    return False


def _material_mask_descriptor(
    material_data: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    descriptors = material_data.get("material_masks")
    if not isinstance(descriptors, dict):
        raise ValueError("중립화/파생 정책에는 material_masks가 필요해요")
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
        normalized_x = x / length
        normalized_y = y / length
        normalized_z = np.sqrt(
            np.maximum(0.0, 1.0 - normalized_x * normalized_x - normalized_y * normalized_y)
        )
        packed_x, packed_y, _ = pack_dxt5nm_xy(
            np.dstack((normalized_x, normalized_y, normalized_z))
        )
        output[..., 3][mask] = packed_x[mask]
        output[..., 1][mask] = packed_y[mask]
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
    if inventory.get("schema_version") != 3:
        raise ValueError(
            "Material identity 증거가 없는 구형 inventory예요. extract를 다시 실행해 주세요"
        )
    outputs: list[dict[str, Any]] = []
    validated_targets: list[dict[str, Any]] = []
    auxiliary_plans: dict[tuple[str, int, str], dict[str, Any]] = {}
    selected_scope = {
        target.id
        for target in profile.targets
        if target.action == "localize"
        and (target_ids is None or target.id in target_ids)
        and (paths.approved / f"{target.id}.png").is_file()
    }
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
                value.get("material_assets_file"),
                int(value.get("material_path_id", 0)),
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
        edit_data = review["stages"]["edit_plan"]["data"]
        masks = edit_data["masks"]
        if material_data.get("text_mask_sha256") != masks["new_text"]["sha256"]:
            raise ValueError(f"{target.id}: material 글자 마스크가 edit plan과 달라요")
        master_cache: tuple[
            np.ndarray, dict[str, np.ndarray], list[dict[str, str]]
        ] | None = None
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
            if policy not in DERIVATION_POLICIES:
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
                if policy != "preserve"
                else None
            )
            record = _map_record(inventory, slot["texture_bundle_key"], slot["path_id"])
            source_map = Path(record["source_png"])
            entry = contract_maps.get(key)
            channel_indices = _verify_auxiliary_contract_entry(
                entry,
                slot,
                record,
                source_map,
                material_mask,
                paths.root,
            )
            derivation: dict[str, Any] | None = None
            diffuse_slot: dict[str, Any] | None = None
            master_alpha: np.ndarray | None = None
            region_alphas: dict[str, np.ndarray] = {}
            master_records: list[dict[str, str]] = []
            if policy != "preserve":
                consumer_targets = _consumer_target_ids(inventory, consumers)
                if (
                    "__unmapped_consumer__" in consumer_targets
                    or not consumer_targets
                    or not consumer_targets.issubset(selected_scope)
                ):
                    raise ValueError(
                        f"{target.id}: {key} 공유 PPtr의 모든 Diffuse 소비자를 "
                        "같은 derive 범위에서 해소해야 해요"
                    )
            if policy == "neutralize_and_derive":
                if consumer_targets != {target.id}:
                    raise ValueError(
                        f"{target.id}: {key} 파생 PPtr는 다른 Diffuse target과 "
                        "공유할 수 없어요"
                    )
                if master_cache is None:
                    master_cache = _master_lettering_alpha(
                        paths,
                        edit_data,
                        (int(diffuse["width"]), int(diffuse["height"])),
                    )
                master_alpha, region_alphas, master_records = master_cache
                diffuse_slot = _diffuse_slot_for_auxiliary(bindings, slot, diffuse)
                if channel_indices is None or not isinstance(entry, dict):
                    raise AssertionError("파생 보조맵 채널/계약이 없어요")
                derivation = _verify_derivation_contract(
                    paths,
                    entry,
                    role=slot["role"],
                    used_channels=channel_indices,
                    diffuse_record=diffuse,
                    diffuse_slot=diffuse_slot,
                    auxiliary_record=record,
                    auxiliary_slot=slot,
                    master_records=master_records,
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
                            "identity": entry["identity"],
                            "channel_contract": entry["channel_contract"],
                            "neutralization_signature": entry[
                                "neutralization_signature"
                            ],
                            "derivation": derivation,
                            "master_lettering": master_records,
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
                raise AssertionError("중립화/파생 재질 마스크가 없어요")
            old_text = _mask(paths, material_mask, size)
            if diffuse_slot is None:
                diffuse_slot = _diffuse_slot_for_auxiliary(bindings, slot, diffuse)
            validate_same_uv_projection(
                diffuse_slot.get("scale", [1.0, 1.0]),
                diffuse_slot.get("offset", [0.0, 0.0]),
                slot.get("scale", [1.0, 1.0]),
                slot.get("offset", [0.0, 0.0]),
            )

            def projected_edit_mask(name: str) -> np.ndarray:
                source_values = _mask(
                    paths,
                    masks[name],
                    (int(diffuse["width"]), int(diffuse["height"])),
                )
                return project_binary_mask(source_values, size)

            protected = projected_edit_mask("protected")
            seam_guard = projected_edit_mask("seam_guard")
            if np.any(old_text & (protected | seam_guard)):
                raise ValueError(f"{target.id} {key} old-effect 마스크가 보호/seam을 침범해요")
            patch_values = None
            if material_mask["method"] == "patch":
                patch_path = (paths.root / material_mask["patch"]).resolve()
                if sha256_file(patch_path) != material_mask["patch_sha256"]:
                    raise ValueError(f"{target.id} {key} 복원 patch SHA-256이 달라요")
                with Image.open(patch_path) as patch_file:
                    if patch_file.mode != "RGBA" or patch_file.size != size:
                        raise ValueError(f"{target.id} {key} 복원 patch 규격이 원본과 달라요")
                    patch_values = np.asarray(patch_file, dtype=np.uint8)
            neutral_image, neutral_metrics = _neutralize_map(
                source_map,
                old_text,
                slot["role"],
                patch=patch_values,
                channels=channel_indices,
            )
            if old_text.any() and neutral_metrics["changed_pixels"] == 0:
                raise ValueError(
                    f"{target.id} {key} old-effect 중립화가 실제 픽셀 변경을 만들지 못했어요"
                )
            if neutral_metrics["changed_outside_mask"] != 0:
                raise AssertionError(f"{target.id} {key} 마스크 밖 픽셀이 바뀌었어요")
            if neutral_metrics["changed_unselected_channels"] != 0:
                raise AssertionError(f"{target.id} {key} 미사용 채널이 바뀌었어요")
            if not neutral_metrics.get("mode_preserved", True):
                raise ValueError(f"{target.id} {key} 원본 색 모드가 RGBA가 아니에요")
            image = neutral_image
            metrics: dict[str, Any] = neutral_metrics
            derivation_report: dict[str, Any] | None = None
            artifact_reports: dict[str, Any] = {}
            destination_dir = paths.derived / safe_bundle_name(record["bundle_key"])
            destination_dir.mkdir(parents=True, exist_ok=True)
            if policy == "neutralize_and_derive":
                if (
                    derivation is None
                    or master_alpha is None
                    or channel_indices is None
                    or not region_alphas
                ):
                    raise AssertionError("파생 보조맵 master/계약이 없어요")
                projected = project_master_alpha(master_alpha, size)
                alignment = []
                limits = derivation["alignment_limits"]
                for region_id in derivation["master_region_ids"]:
                    projected_region = project_master_alpha(region_alphas[region_id], size)
                    measured = projection_alignment_metrics(
                        region_alphas[region_id], projected_region
                    )
                    if (
                        measured["center_error_texels"] > limits["center_error_texels"]
                        or measured["bbox_edge_error_texels"]
                        > limits["bbox_edge_error_texels"]
                        or measured["rotation_error_deg"] != limits["rotation_error_deg"]
                    ):
                        raise ValueError(
                            f"{target.id} {key}::{region_id} UV 정렬 허용치를 넘었어요"
                        )
                    alignment.append({"region_id": region_id, **measured})

                neutral_values = np.asarray(neutral_image, dtype=np.uint8)
                parameters = derivation["effect_parameters"]
                if slot["role"] == "normal":
                    derived_values, effect_mask, role_metrics = derive_packed_normal(
                        neutral_values,
                        projected,
                        height_scale_texels=parameters["height_scale_texels"],
                        polarity=parameters["polarity"],
                        bevel_passes=parameters["bevel_passes"],
                    )
                else:
                    derived_values, effect_mask, role_metrics = derive_linear_gloss(
                        neutral_values,
                        projected,
                        channel_deltas=parameters["channel_deltas"],
                    )
                if not effect_mask.any():
                    raise ValueError(f"{target.id} {key} 파생 effect mask가 비어 있어요")
                if np.any(effect_mask & protected):
                    raise ValueError(f"{target.id} {key} 파생 효과가 protected를 침범해요")
                if np.any(effect_mask & seam_guard):
                    raise ValueError(f"{target.id} {key} 파생 효과가 seam guard를 침범해요")
                with Image.open(source_map) as source_file:
                    source_values = np.asarray(source_file.convert("RGBA"), dtype=np.uint8)
                changed = np.any(derived_values != source_values, axis=2)
                effect_union = old_text | effect_mask
                metrics = {
                    **role_metrics,
                    "mode_preserved": True,
                    "changed_pixels": int(changed.sum()),
                    "changed_outside_effect_union": int((changed & ~effect_union).sum()),
                    "changed_inside_protected": int((changed & protected).sum()),
                    "changed_inside_seam_guard": int((changed & seam_guard).sum()),
                    "neutralization": neutral_metrics,
                    "alignment": alignment,
                    "projection_signature": PROJECTION_SIGNATURE,
                }
                if any(
                    metrics[field] != 0
                    for field in (
                        "changed_outside_effect_union",
                        "changed_inside_protected",
                        "changed_inside_seam_guard",
                        "changed_unselected_channels",
                        "changed_outside_effect_mask",
                    )
                ):
                    raise AssertionError(f"{target.id} {key} 파생 불변성이 깨졌어요")
                image = Image.fromarray(derived_values, "RGBA")
                neutral_path = destination_dir / f"{record['texture']}.neutral-base.png"
                projected_path = destination_dir / f"{record['texture']}.master-alpha.png"
                effect_path = destination_dir / f"{record['texture']}.effect-mask.png"
                protected_path = (
                    destination_dir / f"{record['texture']}.projected-protected.png"
                )
                seam_path = (
                    destination_dir / f"{record['texture']}.projected-seam-guard.png"
                )
                neutral_image.save(neutral_path)
                Image.fromarray(projected, "L").save(projected_path)
                Image.fromarray(effect_mask.astype(np.uint8) * 255, "L").save(effect_path)
                Image.fromarray(protected.astype(np.uint8) * 255, "L").save(protected_path)
                Image.fromarray(seam_guard.astype(np.uint8) * 255, "L").save(seam_path)
                artifact_reports = {
                    "neutral_base": {
                        "path": str(neutral_path),
                        "sha256": sha256_file(neutral_path),
                    },
                    "projected_master_alpha": {
                        "path": str(projected_path),
                        "sha256": sha256_file(projected_path),
                    },
                    "effect_mask": {
                        "path": str(effect_path),
                        "sha256": sha256_file(effect_path),
                    },
                    "projected_protected": {
                        "path": str(protected_path),
                        "sha256": sha256_file(protected_path),
                    },
                    "projected_seam_guard": {
                        "path": str(seam_path),
                        "sha256": sha256_file(seam_path),
                    },
                }
                derivation_report = {
                    "producer": derivation["producer"],
                    "master_lettering": master_records,
                    "projection": derivation["projection"],
                    "physical_component": derivation["physical_component"],
                    "effect_parameters": derivation["effect_parameters"],
                    "effect_measurement": derivation["effect_measurement"],
                    "alignment_limits": derivation["alignment_limits"],
                    **artifact_reports,
                }

            destination = destination_dir / f"{record['texture']}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination)
            output = {
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
            if derivation_report is not None:
                output["derivation"] = derivation_report
            outputs.append(output)
    payload = {
        "schema_version": 3,
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
