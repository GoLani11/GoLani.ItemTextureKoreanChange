from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import numpy as np
from PIL import Image

from .auxiliary import (
    derive_linear_gloss,
    derive_packed_normal,
    pack_dxt5nm_xy,
    project_binary_mask,
    project_master_alpha,
    validate_same_uv_projection,
)
from .images import validate_approved
from .inventory import (
    _material_identity,
    _material_targets,
    load_inventory,
    record_for_target,
    verify_material_dependency_snapshot,
    verify_source_override_material_graph,
)
from .materials import (
    DIFFUSE_PROPERTIES,
    _all_consumers,
    _binding_signature,
    _canonical_master_lettering,
    _master_lettering_alpha,
    _material_mask_descriptor,
    _neutralize_map,
    _target_bindings,
)
from .models import CollectionProfile
from .names import safe_bundle_name
from .paths import ProjectPaths
from .review import (
    approval_path,
    load_review,
    review_stage_sha256,
    sha256_file,
    verify_approval,
)
from .unityfs import (
    bytes_equal_outside_ranges,
    find_directory_entry,
    layout_signature,
    merge_ranges,
    parse_unityfs_layout,
    patch_uncompressed_logical_range,
)


# BC 계열의 4x4 블록이 하위 mip 전체를 차지할 때 정상 no-op 왕복에서도
# 경계 p99가 48을 넘을 수 있어요. 실제 BC7 원본 왕복으로 교정한 하한이에요.
_ROUNDTRIP_P99_FLOOR = 64.0
_ROUNDTRIP_MAX_FLOOR = 128.0
_MATERIAL_ROI_P99_LIMIT = 24.0
_MATERIAL_ROI_MAX_LIMIT = 48.0
_MATERIAL_EFFECT_SUPPORT_RECALL = 0.85
_MATERIAL_WRONG_DIRECTION_ENERGY_MAX = 0.1
_MATERIAL_EFFECT_SUPPORT_PRECISION = 0.85
_MATERIAL_EFFECT_SUPPORT_IOU = 0.8
_MATERIAL_EFFECT_CENTER_ERROR_TEXELS = 0.5
_MATERIAL_EFFECT_MAX_CENTER_ERROR_TEXELS = 2.5
_MATERIAL_EFFECT_BBOX_ERROR_TEXELS = 2.0
_MATERIAL_EFFECT_MAX_BBOX_ERROR_TEXELS = 3.0
_MATERIAL_EFFECT_SUPPORT_BYTE_THRESHOLD = 2
_MATERIAL_EFFECT_LEAKED_ENERGY_MAX = 0.2
_MATERIAL_EFFECT_COARSE_LEAKED_ENERGY_MAX = 0.75
_MATERIAL_OLD_EFFECT_EARLY_MIP_LEAKED_ENERGY_MAX = 0.25
_MATERIAL_EFFECT_LEAKED_DELTA_MAX = 48
_MATERIAL_EFFECT_CUMULATIVE_LEAKED_ENERGY_MAX = {
    "old-effect-removal": 0.06,
    "new-effect": 0.1,
}
_MATERIAL_NEW_EFFECT_DIRECTION_RECALL = 0.85
_MATERIAL_OLD_RESIDUAL_MIN_REMOVAL = 8
_MATERIAL_OLD_RESIDUAL_ENERGY_MAX = 0.4
_MATERIAL_OLD_RESIDUAL_MIP0_ENERGY_MAX = 0.25
_MATERIAL_OLD_HIGH_RESIDUAL_RATIO = 0.8
_MATERIAL_OLD_HIGH_RESIDUAL_FRACTION_MAX = 0.1
_BC_BLOCK_SIZE_BY_FORMAT = {10: 4, 12: 4}  # Unity TextureFormat DXT1 / DXT5


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_derived_material_output(
    output: Any,
    *,
    expected_channels: list[str] | None = None,
    derived_root: Path | None = None,
) -> None:
    if not isinstance(output, dict):
        raise ValueError("derived 보조맵 산출물이 객체가 아니에요")
    policy = output.get("policy")
    if policy not in {"neutralize_old_text", "neutralize_and_derive"}:
        raise ValueError("derived 보조맵 산출물 정책이 현재 producer와 달라요")
    metrics = output.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError("derived 보조맵 실측값이 없어요")
    expected = {
        "changed_unselected_channels": 0,
        "mode_preserved": True,
    }
    if policy == "neutralize_old_text":
        expected["changed_outside_mask"] = 0
    else:
        expected.update(
            {
                "changed_outside_effect_union": 0,
                "changed_inside_protected": 0,
                "changed_inside_seam_guard": 0,
                "changed_outside_effect_mask": 0,
            }
        )
    for field, wanted in expected.items():
        if metrics.get(field) != wanted:
            raise ValueError(f"derived 보조맵 metrics.{field}는 {wanted!r}여야 해요")
    selected_channels = metrics.get("selected_channels")
    if (
        not isinstance(selected_channels, list)
        or not selected_channels
        or len(set(selected_channels)) != len(selected_channels)
        or any(channel not in {"R", "G", "B", "A"} for channel in selected_channels)
    ):
        raise ValueError("derived 보조맵 selected_channels가 잘못됐어요")
    if expected_channels is not None and selected_channels != expected_channels:
        raise ValueError("derived 보조맵 selected_channels가 현재 재질 계약과 달라요")
    if policy == "neutralize_old_text":
        return

    if metrics.get("projection_signature") != "continuous-alpha-same-st-integer-area:v1":
        raise ValueError("derived 보조맵 projection signature가 현재 producer와 달라요")
    alignment = metrics.get("alignment")
    if not isinstance(alignment, list) or not alignment:
        raise ValueError("derived 보조맵 영역별 정렬 실측값이 없어요")
    for item in alignment:
        if not isinstance(item, dict) or not isinstance(item.get("region_id"), str):
            raise ValueError("derived 보조맵 정렬 항목이 잘못됐어요")
        center = item.get("center_error_texels")
        bbox = item.get("bbox_edge_error_texels")
        if (
            not isinstance(center, (int, float))
            or isinstance(center, bool)
            or not math.isfinite(float(center))
            or not 0.0 <= float(center) <= 0.5
            or not isinstance(bbox, (int, float))
            or isinstance(bbox, bool)
            or not math.isfinite(float(bbox))
            or not 0.0 <= float(bbox) <= 1.0
            or item.get("rotation_error_deg") != 0.0
        ):
            raise ValueError("derived 보조맵 정렬 오차가 허용치를 넘었어요")
    role = output.get("role")
    expected_signature = (
        "dxt5nm-rnm-height-from-master-alpha:v1"
        if role == "normal"
        else "linear-gloss-delta-from-master-alpha:v1"
    )
    if metrics.get("algorithm_signature") != expected_signature:
        raise ValueError("derived 보조맵 알고리즘 signature가 역할과 달라요")
    if role == "normal":
        maximum = metrics.get("max_packed_xy_length")
        if (
            not isinstance(maximum, (int, float))
            or isinstance(maximum, bool)
            or not math.isfinite(float(maximum))
            or not 0.0 <= float(maximum) <= 1.0
        ):
            raise ValueError("derived Normal의 DXT5nm XY가 단위 원 밖이에요")
        minimum_z = metrics.get("min_combined_z")
        if (
            not isinstance(minimum_z, (int, float))
            or isinstance(minimum_z, bool)
            or not math.isfinite(float(minimum_z))
            or not 0.05 <= float(minimum_z) <= 1.0
        ):
            raise ValueError("derived Normal이 DXT5nm 양의 Z 반구 안전 한계 밖이에요")
    elif role == "gloss":
        deltas = metrics.get("channel_deltas")
        if not isinstance(deltas, dict) or list(deltas) != selected_channels:
            raise ValueError("derived Gloss delta 채널이 selected_channels와 달라요")
    else:
        raise ValueError("derived 보조맵 role이 normal/gloss가 아니에요")
    derivation = output.get("derivation")
    if not isinstance(derivation, dict) or derivation.get("producer") != expected_signature:
        raise ValueError("derived 보조맵 derivation 증거가 현재 producer와 달라요")
    parameters = derivation.get("effect_parameters")
    if not isinstance(parameters, dict):
        raise ValueError("derived 보조맵 effect_parameters가 없어요")
    if role == "gloss":
        expected_deltas = parameters.get("channel_deltas")
        if deltas != expected_deltas or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not -255.0 <= float(value) <= 255.0
            for value in deltas.values()
        ):
            raise ValueError("derived Gloss delta가 현재 effect_parameters와 달라요")
    masters = derivation.get("master_lettering")
    if not isinstance(masters, list) or not masters:
        raise ValueError("derived 보조맵 master lettering 증거가 없어요")
    for master in masters:
        if not isinstance(master, dict) or any(
            not _valid_sha256(master.get(field))
            for field in ("selected_lettering_sha256", "lettering_mask_sha256")
        ):
            raise ValueError("derived 보조맵 master lettering SHA가 잘못됐어요")
    alignment_ids = {item["region_id"] for item in alignment}
    master_ids = {master.get("region_id") for master in masters}
    if alignment_ids != master_ids or None in master_ids or len(master_ids) != len(masters):
        raise ValueError("derived 보조맵 정렬 영역이 master lettering 전체와 달라요")
    for name in (
        "neutral_base",
        "projected_master_alpha",
        "effect_mask",
        "projected_protected",
        "projected_seam_guard",
    ):
        descriptor = derivation.get(name)
        if not isinstance(descriptor, dict):
            raise ValueError(f"derived 보조맵 {name} 증거가 없어요")
        path = Path(str(descriptor.get("path", ""))).resolve()
        if derived_root is not None:
            try:
                path.relative_to(derived_root.resolve())
            except ValueError as exc:
                raise ValueError(f"derived 보조맵 {name} 경로가 derived 밖이에요") from exc
        if not path.is_file() or descriptor.get("sha256") != _sha256_file(path):
            raise ValueError(f"derived 보조맵 {name} SHA가 현재 파일과 달라요")


def _validate_derived_material_pixels(
    output: dict[str, Any],
    *,
    expected_channels: list[str],
    paths: ProjectPaths,
    expected_projected_alpha: np.ndarray | None = None,
    expected_projected_protected: np.ndarray | None = None,
    expected_projected_seam_guard: np.ndarray | None = None,
    expected_neutral: np.ndarray | None = None,
) -> None:
    channel_indices = tuple("RGBA".index(channel) for channel in expected_channels)
    unselected = tuple(index for index in range(4) if index not in channel_indices)

    def rgba(path: Path, label: str) -> np.ndarray:
        with Image.open(path) as image_file:
            if image_file.mode != "RGBA":
                raise ValueError(f"{label}는 RGBA PNG여야 해요")
            return np.asarray(image_file, dtype=np.uint8)

    def single_channel(path: Path, size: tuple[int, int], label: str) -> np.ndarray:
        with Image.open(path) as image_file:
            if image_file.mode not in {"1", "L"} or image_file.size != size:
                raise ValueError(f"{label} 규격이 derived 보조맵과 달라요")
            return np.asarray(image_file.convert("L"), dtype=np.uint8)

    source_path = Path(str(output.get("source_png", ""))).resolve()
    derived_path = Path(str(output.get("derived_png", ""))).resolve()
    try:
        derived_path.relative_to(paths.derived.resolve())
    except ValueError as exc:
        raise ValueError("derived 보조맵 PNG 경로가 workspace/derived 밖이에요") from exc
    source = rgba(source_path, "보조맵 source")
    derived = rgba(derived_path, "derived 보조맵")
    if source.shape != derived.shape:
        raise ValueError("derived 보조맵 크기가 source와 달라요")
    size = (source.shape[1], source.shape[0])

    old_mask_value = output.get("old_text_mask")
    if not isinstance(old_mask_value, str):
        raise ValueError("derived 보조맵 old-effect mask 경로가 없어요")
    old_mask_path = (paths.root / old_mask_value).resolve()
    try:
        old_mask_path.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise ValueError("derived 보조맵 old-effect mask가 프로젝트 밖이에요") from exc
    if output.get("old_text_mask_sha256") != _sha256_file(old_mask_path):
        raise ValueError("derived 보조맵 old-effect mask SHA가 달라요")
    old_values = single_channel(old_mask_path, size, "old-effect mask")
    if not set(int(value) for value in np.unique(old_values)).issubset({0, 255}):
        raise ValueError("old-effect mask는 0/255만 사용해야 해요")
    old_mask = old_values == 255
    changed = np.any(derived != source, axis=2)
    if unselected and np.any(derived[..., unselected] != source[..., unselected]):
        raise ValueError("derived 보조맵의 미사용 채널이 source와 달라요")
    if output.get("policy") == "neutralize_old_text":
        if expected_neutral is not None and not np.array_equal(derived, expected_neutral):
            raise ValueError("neutralize derived 보조맵이 현재 계약으로 재실행한 결과와 달라요")
        if np.any(changed & ~old_mask):
            raise ValueError("neutralize derived 보조맵이 old-effect mask 밖을 바꿨어요")
        return

    derivation = output["derivation"]
    descriptors = {
        name: Path(str(derivation[name]["path"])).resolve()
        for name in (
            "neutral_base",
            "projected_master_alpha",
            "effect_mask",
            "projected_protected",
            "projected_seam_guard",
        )
    }
    neutral = rgba(descriptors["neutral_base"], "neutral base")
    if neutral.shape != source.shape:
        raise ValueError("neutral base 크기가 source와 달라요")
    if expected_neutral is not None and not np.array_equal(neutral, expected_neutral):
        raise ValueError("neutral base가 현재 material 계약으로 재실행한 결과와 달라요")
    neutral_changed = np.any(neutral != source, axis=2)
    if np.any(neutral_changed & ~old_mask):
        raise ValueError("neutral base가 old-effect mask 밖을 바꿨어요")
    if unselected and np.any(neutral[..., unselected] != source[..., unselected]):
        raise ValueError("neutral base의 미사용 채널이 source와 달라요")

    projected_alpha = single_channel(
        descriptors["projected_master_alpha"], size, "projected master alpha"
    )
    if not projected_alpha.any():
        raise ValueError("projected master alpha가 비어 있어요")
    if expected_projected_alpha is not None and not np.array_equal(
        projected_alpha, expected_projected_alpha
    ):
        raise ValueError("projected master alpha가 현재 승인 lettering에서 다시 계산한 값과 달라요")
    binary_values: dict[str, np.ndarray] = {}
    for name in ("effect_mask", "projected_protected", "projected_seam_guard"):
        values = single_channel(descriptors[name], size, name)
        if not set(int(value) for value in np.unique(values)).issubset({0, 255}):
            raise ValueError(f"{name}는 0/255만 사용해야 해요")
        binary_values[name] = values == 255
    effect_mask = binary_values["effect_mask"]
    protected = binary_values["projected_protected"]
    seam_guard = binary_values["projected_seam_guard"]
    if expected_projected_protected is not None and not np.array_equal(
        protected, expected_projected_protected
    ):
        raise ValueError("projected protected가 현재 edit plan에서 다시 계산한 값과 달라요")
    if expected_projected_seam_guard is not None and not np.array_equal(
        seam_guard, expected_projected_seam_guard
    ):
        raise ValueError("projected seam guard가 현재 edit plan에서 다시 계산한 값과 달라요")
    if not effect_mask.any() or np.any(effect_mask & (protected | seam_guard)):
        raise ValueError("derived effect mask가 비었거나 보호/seam을 침범해요")

    parameters = derivation.get("effect_parameters")
    if output.get("role") == "normal":
        recomputed, recomputed_effect, _ = derive_packed_normal(
            neutral,
            projected_alpha,
            height_scale_texels=parameters["height_scale_texels"],
            polarity=parameters["polarity"],
            bevel_passes=parameters["bevel_passes"],
        )
    else:
        recomputed, recomputed_effect, _ = derive_linear_gloss(
            neutral,
            projected_alpha,
            channel_deltas=parameters["channel_deltas"],
        )
    if not np.array_equal(recomputed, derived) or not np.array_equal(
        recomputed_effect, effect_mask
    ):
        raise ValueError("derived 보조맵 픽셀이 결정적 producer 재실행 결과와 달라요")
    effect_union = old_mask | effect_mask
    if np.any(changed & ~effect_union):
        raise ValueError("derived 보조맵이 old/new effect union 밖을 바꿨어요")
    if np.any(changed & protected) or np.any(changed & seam_guard):
        raise ValueError("derived 보조맵이 protected/seam guard를 바꿨어요")

    metrics = output.get("metrics", {})
    measured_counts = {
        "changed_pixels": int(changed.sum()),
        "changed_outside_effect_union": int((changed & ~effect_union).sum()),
        "changed_inside_protected": int((changed & protected).sum()),
        "changed_inside_seam_guard": int((changed & seam_guard).sum()),
    }
    if any(metrics.get(field) != value for field, value in measured_counts.items()):
        raise ValueError("derived 보조맵 변경 픽셀 실측값이 manifest와 달라요")


def _verified_project_mask(
    paths: ProjectPaths,
    descriptor: Any,
    size: tuple[int, int],
    label: str,
) -> np.ndarray:
    if not isinstance(descriptor, dict):
        raise ValueError(f"{label} descriptor가 없어요")
    value = descriptor.get("path")
    checksum = descriptor.get("sha256")
    if not isinstance(value, str) or not value or not _valid_sha256(checksum):
        raise ValueError(f"{label} path/SHA-256이 잘못됐어요")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} 경로는 프로젝트 내부 상대 경로여야 해요")
    path = (paths.root / relative).resolve()
    try:
        path.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} 경로가 프로젝트 밖이에요") from exc
    if not path.is_file() or _sha256_file(path) != checksum:
        raise ValueError(f"{label} 파일/SHA-256이 현재 review와 달라요")
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"} or image_file.size != size:
            raise ValueError(f"{label} 규격이 현재 텍스처와 달라요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(int(value) for value in np.unique(values)).issubset({0, 255}):
        raise ValueError(f"{label}는 0/255만 사용해야 해요")
    return values == 255


def _verified_project_rgba(
    paths: ProjectPaths,
    value: Any,
    checksum: Any,
    size: tuple[int, int],
    label: str,
) -> np.ndarray:
    if not isinstance(value, str) or not value or not _valid_sha256(checksum):
        raise ValueError(f"{label} path/SHA-256이 잘못됐어요")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} 경로는 프로젝트 내부 상대 경로여야 해요")
    path = (paths.root / relative).resolve()
    try:
        path.relative_to(paths.root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} 경로가 프로젝트 밖이에요") from exc
    if not path.is_file() or _sha256_file(path) != checksum:
        raise ValueError(f"{label} 파일/SHA-256이 현재 review와 달라요")
    with Image.open(path) as image_file:
        if image_file.mode != "RGBA" or image_file.size != size:
            raise ValueError(f"{label}는 보조맵과 같은 크기의 RGBA PNG여야 해요")
        return np.asarray(image_file, dtype=np.uint8)


def _binary_mask_file(path: Path, size: tuple[int, int], label: str) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(f"{label} 파일이 없어요: {path}")
    with Image.open(path) as image_file:
        if image_file.mode not in {"1", "L"} or image_file.size != size:
            raise ValueError(f"{label} 규격이 보조맵과 달라요")
        values = np.asarray(image_file.convert("L"), dtype=np.uint8)
    if not set(int(value) for value in np.unique(values)).issubset({0, 255}):
        raise ValueError(f"{label}는 0/255만 사용해야 해요")
    return values == 255


def _constraint_mask(value: Any, size: tuple[int, int], label: str) -> np.ndarray:
    if isinstance(value, np.ndarray):
        if value.dtype != np.bool_ or value.shape != (size[1], size[0]):
            raise ValueError(f"{label} bool 배열 규격이 보조맵과 달라요")
        return value.copy()
    return _binary_mask_file(Path(str(value or "")), size, label)


def _constraint_rgba(value: Any, size: tuple[int, int], label: str) -> Image.Image:
    if isinstance(value, np.ndarray):
        if value.dtype != np.uint8 or value.shape != (size[1], size[0], 4):
            raise ValueError(f"{label} uint8 RGBA 배열 규격이 보조맵과 달라요")
        return Image.fromarray(value.copy(), "RGBA")
    path = Path(str(value or ""))
    if not path.is_file():
        raise FileNotFoundError(f"{label} 파일이 없어요: {path}")
    with Image.open(path) as image_file:
        if image_file.mode != "RGBA" or image_file.size != size:
            raise ValueError(f"{label}는 보조맵과 같은 크기의 RGBA PNG여야 해요")
        return image_file.copy()


def _review_projection_contract(
    inventory: Mapping[str, Any],
    *,
    target_id: str,
    material_data: Mapping[str, Any],
    contract_key: str,
    contract_entry: Mapping[str, Any],
) -> tuple[tuple[int, int], tuple[int, int]]:
    diffuse_records = [
        record
        for record in inventory.get("records", [])
        if record.get("target_id") == target_id
    ]
    if len(diffuse_records) != 1:
        raise ValueError(f"{target_id} Diffuse inventory record가 정확히 하나가 아니에요")
    diffuse_record = diffuse_records[0]
    bindings = material_data.get("bindings")
    if not isinstance(bindings, list):
        raise ValueError(f"{target_id} material bindings가 없어요")
    auxiliary = [
        value
        for value in bindings
        if isinstance(value, dict)
        and f"{value.get('material')}::{value.get('property')}" == contract_key
        and value.get("texture_bundle_key")
        == contract_entry.get("identity", {}).get("texture_bundle_key")
        and int(value.get("path_id", 0))
        == int(contract_entry.get("identity", {}).get("path_id", 0))
    ]
    if not auxiliary:
        raise ValueError(f"{target_id}::{contract_key} 보조맵 binding이 없어요")
    diffuse: list[dict[str, Any]] = []
    for auxiliary_binding in auxiliary:
        matching_diffuse = [
            value
            for value in bindings
            if isinstance(value, dict)
            and value.get("material_bundle_key")
            == auxiliary_binding.get("material_bundle_key")
            and value.get("material_assets_file")
            == auxiliary_binding.get("material_assets_file")
            and int(value.get("material_path_id", 0))
            == int(auxiliary_binding.get("material_path_id", 0))
            and value.get("material") == auxiliary_binding.get("material")
            and value.get("property") in DIFFUSE_PROPERTIES
            and int(value.get("path_id", 0)) != 0
        ]
        if not matching_diffuse or any(
            value.get("texture_bundle_key") != diffuse_record.get("bundle_key")
            or int(value.get("path_id", 0))
            != int(diffuse_record.get("path_id", 0))
            for value in matching_diffuse
        ):
            raise ValueError(
                f"{target_id}::{contract_key} 같은 Material의 모든 Diffuse가 "
                "현재 target이 아니에요"
            )
        diffuse.extend(matching_diffuse)
    diffuse_st = {
        (
            tuple(float(item) for item in value.get("scale", [])),
            tuple(float(item) for item in value.get("offset", [])),
        )
        for value in diffuse
    }
    if len(diffuse_st) != 1:
        raise ValueError(f"{target_id}::{contract_key} Diffuse UV ST가 모호해요")
    diffuse_scale, diffuse_offset = next(iter(diffuse_st))
    auxiliary_st = {
        (
            tuple(float(item) for item in value.get("scale", [])),
            tuple(float(item) for item in value.get("offset", [])),
        )
        for value in auxiliary
    }
    if len(auxiliary_st) != 1:
        raise ValueError(f"{target_id}::{contract_key} 보조맵 UV ST가 모호해요")
    auxiliary_scale, auxiliary_offset = next(iter(auxiliary_st))
    validate_same_uv_projection(
        diffuse_scale,
        diffuse_offset,
        auxiliary_scale,
        auxiliary_offset,
    )
    identity = contract_entry.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError(f"{target_id}::{contract_key} 보조맵 identity가 없어요")
    auxiliary_records = [
        record
        for record in inventory.get("records", [])
        if record.get("bundle_key") == identity.get("texture_bundle_key")
        and int(record.get("path_id", 0)) == int(identity.get("path_id", 0))
    ]
    if len(auxiliary_records) != 1:
        raise ValueError(f"{target_id}::{contract_key} 보조맵 inventory record가 모호해요")
    auxiliary_record = auxiliary_records[0]
    current_auxiliary_identity = {
        "texture_bundle_key": auxiliary_record.get("bundle_key"),
        "path_id": int(auxiliary_record.get("path_id", 0)),
        "texture": auxiliary_record.get("texture"),
        "role": auxiliary_record.get("role"),
        "width": int(auxiliary_record.get("width", 0)),
        "height": int(auxiliary_record.get("height", 0)),
        "format": int(auxiliary_record.get("format", -1)),
    }
    if any(
        identity.get(field) != value
        for field, value in current_auxiliary_identity.items()
    ):
        raise ValueError(
            f"{target_id}::{contract_key} 보조맵 identity가 현재 inventory와 달라요"
        )
    for label, record in (("Diffuse", diffuse_record), ("보조맵", auxiliary_record)):
        if (
            not isinstance(record.get("wrap_u"), int)
            or isinstance(record.get("wrap_u"), bool)
            or not isinstance(record.get("wrap_v"), int)
            or isinstance(record.get("wrap_v"), bool)
            or int(record["wrap_u"]) != 0
            or int(record["wrap_v"]) != 0
        ):
            raise ValueError(
                f"{target_id}::{contract_key} v1 파생은 {label} U/V Repeat wrap만 지원해요"
            )
    source_size = (int(diffuse_record["width"]), int(diffuse_record["height"]))
    target_size = (int(auxiliary_record["width"]), int(auxiliary_record["height"]))
    # 해상도 관계까지 pure projection 함수로 fail-closed 검증해요.
    project_binary_mask(np.zeros((source_size[1], source_size[0]), dtype=bool), target_size)
    return source_size, target_size


def _find_texture(environment: Any, name: str):
    matches = []
    for obj in environment.objects:
        if obj.type.name != "Texture2D":
            continue
        texture = obj.read()
        if texture.m_Name == name:
            matches.append((obj, texture))
    if len(matches) != 1:
        raise ValueError(f"Texture2D {name!r}는 하나여야 해요. 현재 {len(matches)}개예요")
    return matches[0]


def _object_hashes(environment: Any) -> dict[str, str]:
    return {
        f"{obj.assets_file.name}:{obj.path_id}:{obj.type.name}": _sha256_bytes(obj.get_raw_data())
        for obj in environment.objects
    }


def _texture_metadata(texture: Any) -> tuple[Any, ...]:
    stream = texture.m_StreamData
    return (
        texture.m_Name,
        texture.m_Width,
        texture.m_Height,
        int(texture.m_TextureFormat),
        texture.m_MipCount,
        texture.m_CompleteImageSize,
        stream.path,
        stream.offset,
        stream.size,
    )


def _srgb_to_linear(values: np.ndarray) -> np.ndarray:
    values = values.astype(np.float32) / 255.0
    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0.0, 1.0)
    encoded = np.where(values <= 0.0031308, values * 12.92, 1.055 * values ** (1 / 2.4) - 0.055)
    return np.clip(np.round(encoded * 255), 0, 255).astype(np.uint8)


def _resize_float(values: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    return cv2.resize(values, size, interpolation=cv2.INTER_AREA)


def _roundtrip_limits(
    role: str,
    width: int,
    height: int,
    max_mae: float,
) -> tuple[float, float, float]:
    """역할·밉 크기별 정상 블록 압축 왕복 한계를 돌려줘요."""

    minimum = min(width, height)
    mae_limit = max_mae
    p99_limit = max(_ROUNDTRIP_P99_FLOOR, max_mae * 8.0)
    if role == "diffuse":
        # 4x4는 BC7 한 블록이 mip 전체라 UV padding 뒤의 유효한 색 분포에서도
        # Mayo 원본 no-op 왕복 최대 channel MAE 20.875가 측정됐어요.
        if minimum == 4:
            mae_limit = max(mae_limit, 24.0)
        elif minimum <= 2:
            mae_limit = max(mae_limit, 16.0)
        elif minimum <= 16:
            # 16x16은 BC7 블록이 16개뿐이라 강한 고대비 패키지 디자인에서
            # 무편집 원본도 channel MAE 10.56, 검증 후보는 13.05가 측정됐어요.
            # p99/max 한계는 그대로 유지해 국소적인 큰 파손은 계속 차단해요.
            mae_limit = max(mae_limit, 16.0)
        elif minimum <= 32:
            mae_limit = max(mae_limit, 12.0)
        elif minimum <= 64:
            mae_limit = max(mae_limit, 8.0)
        if minimum <= 32:
            p99_limit = max(p99_limit, 80.0)
    elif role == "gloss" and minimum == 8:
        # Aquamari 원본 Gloss의 정상 BC 왕복에서 8x8 mip channel MAE가
        # 7.125였어요. p99/max 한계는 유지한 채 MAE만 원본 교정값으로 올려요.
        mae_limit = max(mae_limit, 8.0)
    return mae_limit, p99_limit, _ROUNDTRIP_MAX_FLOOR


def _coverage_values(coverage: Image.Image, size: tuple[int, int]) -> np.ndarray:
    if coverage.mode not in {"1", "L"}:
        raise ValueError("UV coverage는 단일 채널 마스크여야 해요")
    values = np.asarray(coverage.convert("L"), dtype=np.uint8)
    if not set(int(value) for value in np.unique(values)).issubset({0, 255}):
        raise ValueError("UV coverage는 0/255만 사용해야 해요")
    if coverage.size != size:
        source_width, source_height = coverage.size
        target_width, target_height = size
        if (
            source_width % target_width != 0
            or source_height % target_height != 0
            or source_width // target_width != source_height // target_height
        ):
            raise ValueError(
                "UV coverage와 텍스처 해상도는 같은 종횡비의 정수 축소 관계여야 해요"
            )
        factor = source_width // target_width
        if factor < 1 or factor & (factor - 1):
            raise ValueError("UV coverage 축소 배율은 2의 거듭제곱이어야 해요")
        # 보조맵이 diffuse보다 작을 때 UV island가 걸친 픽셀을 잃지 않도록
        # 각 블록의 합집합(max pooling)을 사용해 보수적으로 축소해요.
        values = values.reshape(
            target_height,
            factor,
            target_width,
            factor,
        ).max(axis=(1, 3))
    result = values == 255
    if not result.any():
        raise ValueError("UV coverage가 비어 있어요")
    return result


def _pad_uv_outside(image: Image.Image, coverage: np.ndarray) -> Image.Image:
    """UV island 밖을 가장 가까운 island texel로 채워 하위 mip의 atlas bleed를 막아요."""

    import cv2

    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    if coverage.shape != rgba.shape[:2]:
        raise ValueError("UV coverage 크기가 mip과 달라요")
    if not coverage.any():
        raise ValueError("UV coverage가 비어 있어요")
    if coverage.all():
        return Image.fromarray(rgba.copy(), "RGBA")

    # distanceTransformWithLabels의 PIXEL label은 각 0 픽셀(coverage)에 대응해요.
    outside = np.where(coverage, 0, 255).astype(np.uint8)
    _, labels = cv2.distanceTransformWithLabels(
        outside,
        cv2.DIST_L2,
        5,
        labelType=cv2.DIST_LABEL_PIXEL,
    )
    covered_flat = np.flatnonzero(coverage)
    covered_labels = labels.reshape(-1)[covered_flat]
    maximum_label = int(labels.max())
    nearest = np.full(maximum_label + 1, -1, dtype=np.int64)
    nearest[covered_labels] = covered_flat
    source_indices = nearest[labels]
    if np.any(source_indices < 0):
        raise AssertionError("UV coverage의 최근접 texel을 찾지 못했어요")
    output = rgba.copy().reshape(-1, 4)
    outside_flat = ~coverage.reshape(-1)
    output[outside_flat] = rgba.reshape(-1, 4)[source_indices.reshape(-1)[outside_flat]]
    return Image.fromarray(output.reshape(rgba.shape), "RGBA")


def _resize_coverage(coverage: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    resized = cv2.resize(
        coverage.astype(np.uint8) * 255,
        size,
        interpolation=cv2.INTER_AREA,
    )
    result = resized > 0
    if not result.any():
        raise AssertionError("하위 mip에서 UV coverage가 사라졌어요")
    return result


def _next_mip(image: Image.Image, role: str) -> Image.Image:
    size = (max(1, image.width // 2), max(1, image.height // 2))
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    if role == "diffuse":
        rgb = _linear_to_srgb(_resize_float(_srgb_to_linear(rgba[..., :3]), size))
        alpha = np.clip(np.round(_resize_float(rgba[..., 3].astype(np.float32), size)), 0, 255).astype(np.uint8)
        return Image.fromarray(np.dstack((rgb, alpha)), "RGBA")
    if role == "normal":
        x = rgba[..., 3].astype(np.float32) / 127.5 - 1.0
        y = rgba[..., 1].astype(np.float32) / 127.5 - 1.0
        z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
        vector = _resize_float(np.dstack((x, y, z)), size)
        length = np.linalg.norm(vector, axis=2, keepdims=True)
        vector /= np.maximum(length, 1e-8)
        other = np.clip(np.round(_resize_float(rgba[..., [0, 2]].astype(np.float32), size)), 0, 255).astype(np.uint8)
        output = np.empty((size[1], size[0], 4), dtype=np.uint8)
        output[..., 0] = other[..., 0]
        packed_x, packed_y, _ = pack_dxt5nm_xy(vector)
        output[..., 1] = packed_y
        output[..., 2] = other[..., 1]
        output[..., 3] = packed_x
        return Image.fromarray(output, "RGBA")
    if role == "gloss":
        values = np.clip(np.round(_resize_float(rgba.astype(np.float32), size)), 0, 255).astype(np.uint8)
        return Image.fromarray(values, "RGBA")
    raise ValueError(f"지원하지 않는 텍스처 역할이에요: {role}")


def _mip_chain(
    image: Image.Image,
    role: str,
    count: int,
    *,
    coverage: Image.Image | None = None,
) -> list[Image.Image]:
    if count < 1:
        raise ValueError("mip 수는 1 이상이어야 해요")
    levels = [image.convert("RGBA")]
    coverage_values = _coverage_values(coverage, image.size) if coverage is not None else None
    while len(levels) < count:
        current = levels[-1]
        if coverage_values is not None:
            current = _pad_uv_outside(current, coverage_values)
        next_level = _next_mip(current, role)
        if coverage_values is not None:
            coverage_values = _resize_coverage(coverage_values, next_level.size)
            next_level = _pad_uv_outside(next_level, coverage_values)
        levels.append(next_level)
    return levels


def _pad_binary_outside(mask: np.ndarray, coverage: np.ndarray) -> np.ndarray:
    rgba = np.repeat((mask.astype(np.uint8) * 255)[..., None], 4, axis=2)
    padded = np.asarray(
        _pad_uv_outside(Image.fromarray(rgba, "RGBA"), coverage), dtype=np.uint8
    )
    return padded[..., 0] > 0


def _resize_binary_union(mask: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    import cv2

    return (
        cv2.resize(
            mask.astype(np.uint8) * 255,
            size,
            interpolation=cv2.INTER_AREA,
        )
        > 0
    )


def _binary_mask_mip_chain(
    mask: np.ndarray,
    count: int,
    *,
    coverage_levels: list[np.ndarray] | None = None,
) -> list[np.ndarray]:
    if mask.ndim != 2 or mask.dtype != np.bool_:
        raise ValueError("effect mask는 bool 2차원 배열이어야 해요")
    if count < 1:
        raise ValueError("effect mask mip 수는 1 이상이어야 해요")
    if coverage_levels is not None and len(coverage_levels) != count:
        raise ValueError("effect mask와 UV coverage mip 수가 달라요")
    levels = [mask.copy()]
    while len(levels) < count:
        index = len(levels) - 1
        current = levels[-1]
        if coverage_levels is not None:
            current = _pad_binary_outside(current, coverage_levels[index])
        size = (max(1, current.shape[1] // 2), max(1, current.shape[0] // 2))
        next_level = _resize_binary_union(current, size)
        if coverage_levels is not None:
            next_level = _pad_binary_outside(
                next_level, coverage_levels[index + 1]
            )
        levels.append(next_level)
    return levels


def _expand_to_bc_blocks(mask: np.ndarray, block_size: int = 4) -> np.ndarray:
    if mask.ndim != 2 or mask.dtype != np.bool_:
        raise ValueError("BC effect mask는 bool 2차원 배열이어야 해요")
    if block_size < 1:
        raise ValueError("BC block 크기는 1 이상이어야 해요")
    expanded = np.zeros_like(mask)
    for y in range(0, mask.shape[0], block_size):
        for x in range(0, mask.shape[1], block_size):
            block = mask[y : y + block_size, x : x + block_size]
            if block.any():
                expanded[y : y + block_size, x : x + block_size] = True
    return expanded


def _validate_auxiliary_mip_invariants(
    source: Image.Image,
    edited: Image.Image,
    *,
    role: str,
    count: int,
    coverage: Image.Image,
    selected_channels: list[str],
    effect_union: np.ndarray,
    protected: np.ndarray,
    seam_guard: np.ndarray,
) -> tuple[
    list[Image.Image],
    list[Image.Image],
    list[np.ndarray],
    list[np.ndarray],
    list[np.ndarray],
    list[np.ndarray],
]:
    if role not in {"normal", "gloss"}:
        raise ValueError("mip 불변성 검사는 normal/gloss에만 적용돼요")
    expected_shape = (source.height, source.width)
    if (
        source.size != edited.size
        or effect_union.shape != expected_shape
        or protected.shape != expected_shape
        or seam_guard.shape != expected_shape
    ):
        raise ValueError("보조맵 source/edit/effect mask 크기가 달라요")
    if (
        not selected_channels
        or len(set(selected_channels)) != len(selected_channels)
        or any(channel not in "RGBA" for channel in selected_channels)
    ):
        raise ValueError("보조맵 selected_channels가 잘못됐어요")
    coverage_levels = [_coverage_values(coverage, source.size)]
    while len(coverage_levels) < count:
        size = (
            max(1, coverage_levels[-1].shape[1] // 2),
            max(1, coverage_levels[-1].shape[0] // 2),
        )
        coverage_levels.append(_resize_coverage(coverage_levels[-1], size))
    source_levels = _mip_chain(source, role, count, coverage=coverage)
    edited_levels = _mip_chain(edited, role, count, coverage=coverage)
    effect_levels = _binary_mask_mip_chain(
        effect_union, count, coverage_levels=coverage_levels
    )
    protected_levels = _binary_mask_mip_chain(
        protected, count, coverage_levels=coverage_levels
    )
    seam_levels = _binary_mask_mip_chain(
        seam_guard, count, coverage_levels=coverage_levels
    )

    selected = tuple("RGBA".index(channel) for channel in selected_channels)
    unselected = tuple(index for index in range(4) if index not in selected)
    for index, (source_level, edited_level, allowed, protected_level, seam_level, covered) in enumerate(
        zip(
            source_levels,
            edited_levels,
            effect_levels,
            protected_levels,
            seam_levels,
            coverage_levels,
            strict=True,
        )
    ):
        source_values = np.asarray(source_level, dtype=np.uint8)
        edited_values = np.asarray(edited_level, dtype=np.uint8)
        if unselected and not np.array_equal(
            source_values[..., unselected], edited_values[..., unselected]
        ):
            raise ValueError(f"보조맵 mip {index}에서 미사용 채널이 no-op chain과 달라요")
        changed = np.any(source_values != edited_values, axis=2)
        domain = np.ones_like(covered) if index == 0 else covered
        if np.any(changed & domain & ~allowed):
            raise ValueError(f"보조맵 mip {index}가 effect union 밖을 바꿨어요")
        protected_change = changed & (protected_level | seam_level)
        if index > 0:
            protected_change &= ~allowed
        if np.any(protected_change):
            raise ValueError(f"보조맵 mip {index}가 protected/seam guard를 바꿨어요")
    return (
        source_levels,
        edited_levels,
        effect_levels,
        protected_levels,
        seam_levels,
        coverage_levels,
    )


def _validate_compressed_auxiliary_invariants(
    intended_source_levels: list[Image.Image],
    intended_neutral_levels: list[Image.Image],
    intended_edited_levels: list[Image.Image],
    source_roundtrip_levels: list[Image.Image],
    neutral_roundtrip_levels: list[Image.Image],
    edited_roundtrip_levels: list[Image.Image],
    old_effect_levels: list[np.ndarray],
    new_effect_levels: list[np.ndarray],
    protected_levels: list[np.ndarray],
    seam_levels: list[np.ndarray],
    coverage_levels: list[np.ndarray],
    *,
    role: str,
    selected_channels: list[str],
    block_size: int,
    max_mae: float,
) -> list[dict[str, Any]]:
    if not (
        len(intended_source_levels)
        == len(intended_neutral_levels)
        == len(intended_edited_levels)
        == len(source_roundtrip_levels)
        == len(neutral_roundtrip_levels)
        == len(edited_roundtrip_levels)
        == len(old_effect_levels)
        == len(new_effect_levels)
        == len(protected_levels)
        == len(seam_levels)
        == len(coverage_levels)
    ):
        raise ValueError("압축 후 보조맵 mip 검증 입력 수가 달라요")
    if role not in {"normal", "gloss"}:
        raise ValueError("압축 ROI 검증 역할은 normal/gloss여야 해요")
    selected = tuple("RGBA".index(channel) for channel in selected_channels)

    def decode_normal(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        x = values[..., 3].astype(np.float64) / 127.5 - 1.0
        y = values[..., 1].astype(np.float64) / 127.5 - 1.0
        length = np.sqrt(x * x + y * y)
        z = np.sqrt(np.maximum(0.0, 1.0 - length * length))
        return np.dstack((x, y, z)), length

    def support_geometry(
        support: np.ndarray, delta: np.ndarray
    ) -> tuple[float, float, tuple[int, int, int, int]]:
        rows, columns = np.nonzero(support)
        weights = np.abs(delta).sum(axis=2, dtype=np.float64)
        selected_weights = weights[support]
        total = float(selected_weights.sum())
        if rows.size == 0 or total <= 0.0:
            raise ValueError("보조맵 효과 support geometry가 비어 있어요")
        center_x = float(((columns + 0.5) * selected_weights).sum() / total)
        center_y = float(((rows + 0.5) * selected_weights).sum() / total)
        return center_x, center_y, (
            int(columns.min()),
            int(rows.min()),
            int(columns.max()) + 1,
            int(rows.max()) + 1,
        )

    reports: list[dict[str, Any]] = []
    leaked_energy_totals = {
        "old-effect-removal": [0.0, 0.0],
        "new-effect": [0.0, 0.0],
    }
    for index, (
        intended_source,
        intended_neutral,
        intended_edited,
        source_roundtrip,
        neutral_roundtrip,
        edited_roundtrip,
        old_effect,
        new_effect,
        protected,
        seam,
        covered,
    ) in enumerate(
        zip(
            intended_source_levels,
            intended_neutral_levels,
            intended_edited_levels,
            source_roundtrip_levels,
            neutral_roundtrip_levels,
            edited_roundtrip_levels,
            old_effect_levels,
            new_effect_levels,
            protected_levels,
            seam_levels,
            coverage_levels,
            strict=True,
        )
    ):
        intended_source_values = np.asarray(intended_source, dtype=np.uint8)
        intended_neutral_values = np.asarray(intended_neutral, dtype=np.uint8)
        intended_edited_values = np.asarray(intended_edited, dtype=np.uint8)
        source_values = np.asarray(source_roundtrip, dtype=np.uint8)
        neutral_values = np.asarray(neutral_roundtrip, dtype=np.uint8)
        edited_values = np.asarray(edited_roundtrip, dtype=np.uint8)
        if not (
            intended_source_values.shape
            == intended_neutral_values.shape
            == intended_edited_values.shape
            == source_values.shape
            == neutral_values.shape
            == edited_values.shape
        ):
            raise ValueError(f"압축 후 보조맵 mip {index} shape가 달라요")
        effect = old_effect | new_effect
        changed = np.any(source_values != edited_values, axis=2)
        allowed_blocks = _expand_to_bc_blocks(effect, block_size)
        changed_outside = changed & covered & ~allowed_blocks
        if changed_outside.any():
            raise ValueError(
                f"압축 후 보조맵 mip {index}가 effect 교차 BC 블록 밖을 바꿨어요"
            )
        protected_changes = changed & (protected | seam)
        changed_protected = protected_changes & ~allowed_blocks
        if changed_protected.any():
            raise ValueError(
                f"압축 후 보조맵 mip {index}가 protected/seam guard를 바꿨어요"
            )

        effect_domain = effect & covered
        normal_validation_domain = effect_domain | (
            changed & covered & ~(protected | seam)
        )
        normal_metrics: dict[str, float] | None = None
        intended_normal: np.ndarray | None = None
        actual_normal: np.ndarray | None = None
        if role == "normal" and normal_validation_domain.any():
            intended_normal, _ = decode_normal(intended_edited_values)
            actual_normal, actual_xy_length = decode_normal(edited_values)
            maximum_xy = float(
                actual_xy_length[normal_validation_domain].max(initial=0.0)
            )
            minimum_z = float(
                actual_normal[..., 2][normal_validation_domain].min(initial=1.0)
            )
            if maximum_xy > 1.0 or minimum_z < 0.05:
                raise ValueError(
                    f"압축 후 Normal mip {index}가 DXT5nm 단위 원/양의 Z 한계를 벗어났어요"
                )
            dot = np.sum(intended_normal * actual_normal, axis=2)
            angles = np.degrees(
                np.arccos(np.clip(dot[normal_validation_domain], -1.0, 1.0))
            )
            angle_p95 = float(np.percentile(angles, 95))
            angle_p99 = float(np.percentile(angles, 99))
            angle_max = float(angles.max(initial=0.0))
            if angle_p95 > 15.0 or angle_p99 > 25.0 or angle_max > 45.0:
                raise ValueError(f"압축 후 Normal mip {index} 효과 ROI 각도 오차가 너무 커요")
            normal_metrics = {
                "max_packed_xy_length": maximum_xy,
                "min_z": minimum_z,
                "angle_p95_deg": angle_p95,
                "angle_p99_deg": angle_p99,
                "angle_max_deg": angle_max,
            }

        stage_reports: dict[str, dict[str, float | int]] = {}
        stage_values = (
            (
                "old-effect-removal",
                intended_source_values,
                intended_neutral_values,
                source_values,
                neutral_values,
                old_effect,
            ),
            (
                "new-effect",
                intended_neutral_values,
                intended_edited_values,
                neutral_values,
                edited_values,
                new_effect,
            ),
        )
        for (
            stage_name,
            intended_before,
            intended_after,
            actual_before,
            actual_after,
            stage_mask,
        ) in stage_values:
            stage_domain = stage_mask & covered
            intended_delta = (
                intended_after[..., selected].astype(np.int16)
                - intended_before[..., selected].astype(np.int16)
            )
            actual_delta = (
                actual_after[..., selected].astype(np.int16)
                - actual_before[..., selected].astype(np.int16)
            )
            intended_support = stage_domain & np.any(intended_delta != 0, axis=2)
            stage_allowed_blocks = _expand_to_bc_blocks(stage_mask, block_size)
            actual_support = (
                covered
                & stage_allowed_blocks
                & np.any(actual_delta != 0, axis=2)
            )
            geometry_threshold = (
                0
                if intended_support.any()
                and int(np.abs(intended_delta[intended_support]).max(initial=0))
                <= _MATERIAL_EFFECT_SUPPORT_BYTE_THRESHOLD
                else _MATERIAL_EFFECT_SUPPORT_BYTE_THRESHOLD
            )
            intended_geometry_support = stage_domain & np.any(
                np.abs(intended_delta) > geometry_threshold, axis=2
            )
            actual_geometry_support = covered & stage_allowed_blocks & np.any(
                np.abs(actual_delta) > geometry_threshold, axis=2
            )
            support_recall = 1.0
            support_precision = 1.0
            support_iou = 1.0
            center_error_texels = 0.0
            bbox_error_texels = 0.0
            leaked_energy_ratio = 0.0
            leaked_delta_max = 0
            energy_ratio = 1.0
            delta_p95 = 0.0
            delta_p99 = 0.0
            delta_max = 0
            direct_p95 = 0.0
            direct_p99 = 0.0
            direct_max = 0
            direction_recall = 1.0
            wrong_direction_energy_ratio = 0.0
            if index == 0 and stage_domain.any() and not intended_support.any():
                raise ValueError(
                    f"{stage_name}가 mip0에서 실제 선택 채널 변경을 만들지 못했어요"
                )
            if intended_support.any():
                intended_signal_energy = float(
                    np.abs(intended_delta[intended_support]).sum()
                )
                meaningful_signal = (
                    stage_name == "new-effect"
                    or index == 0
                    or (
                        intended_support.sum() >= 32
                        and intended_signal_energy >= 256.0
                    )
                )
                if not actual_support.any():
                    if meaningful_signal:
                        raise ValueError(
                            f"압축 후 보조맵 mip {index}에서 {stage_name} 효과가 사라졌어요"
                        )
                    stage_reports[stage_name] = {
                        "intended_effect_texels": int(intended_support.sum()),
                        "support_recall": 0.0,
                        "support_precision": 0.0,
                        "support_iou": 0.0,
                        "center_error_texels": 0.0,
                        "bbox_edge_error_texels": 0.0,
                        "leaked_energy_ratio": 0.0,
                        "leaked_delta_max": 0,
                        "energy_ratio": 0.0,
                        "delta_error_p95": 0.0,
                        "delta_error_p99": 0.0,
                        "delta_error_max": 0,
                        "direct_roundtrip_p99": 0.0,
                        "direct_roundtrip_p95": 0.0,
                        "direct_roundtrip_max": 0,
                        "direction_recall": 0.0,
                        "wrong_direction_energy_ratio": 0.0,
                        "quantized_away_below_signal_floor": True,
                    }
                    continue
                support_recall = float(
                    (intended_support & actual_support).sum() / intended_support.sum()
                )
                if (
                    meaningful_signal
                    and support_recall < _MATERIAL_EFFECT_SUPPORT_RECALL
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index} {stage_name} support 보존율이 "
                        f"{_MATERIAL_EFFECT_SUPPORT_RECALL} 미만이에요"
                    )
                if intended_geometry_support.any():
                    if not actual_geometry_support.any():
                        raise ValueError(
                            f"압축 후 보조맵 mip {index} {stage_name}의 "
                            "유효 edge support가 사라졌어요"
                        )
                    support_precision = float(
                        (intended_geometry_support & actual_geometry_support).sum()
                        / actual_geometry_support.sum()
                    )
                    geometry_union = (
                        intended_geometry_support | actual_geometry_support
                    )
                    support_iou = float(
                        (intended_geometry_support & actual_geometry_support).sum()
                        / geometry_union.sum()
                    )
                    if index == 0 and (
                        support_precision < _MATERIAL_EFFECT_SUPPORT_PRECISION
                        or support_iou < _MATERIAL_EFFECT_SUPPORT_IOU
                    ):
                        raise ValueError(
                            f"압축 후 보조맵 mip {index} {stage_name} 효과가 "
                            "ROI 밖으로 번지거나 edge를 잃었어요"
                        )
                    intended_center_x, intended_center_y, intended_bbox = (
                        support_geometry(intended_geometry_support, intended_delta)
                    )
                    actual_center_x, actual_center_y, actual_bbox = support_geometry(
                        actual_geometry_support, actual_delta
                    )
                    center_error_texels = max(
                        abs(actual_center_x - intended_center_x),
                        abs(actual_center_y - intended_center_y),
                    )
                    bbox_error_texels = max(
                        abs(actual - expected)
                        for actual, expected in zip(
                            actual_bbox, intended_bbox, strict=True
                        )
                    )
                    intended_geometry_energy = float(
                        np.abs(intended_delta)[intended_geometry_support].sum()
                    )
                    leaked_values = np.abs(actual_delta)[
                        actual_geometry_support & ~intended_geometry_support
                    ]
                    leaked_energy = float(leaked_values.sum())
                    leaked_delta_max = int(leaked_values.max(initial=0))
                    leaked_energy_ratio = leaked_energy / max(
                        intended_geometry_energy, 1.0
                    )
                    leaked_energy_totals[stage_name][0] += leaked_energy
                    leaked_energy_totals[stage_name][1] += intended_geometry_energy
                    meaningful_geometry = (
                        index == 0
                        or intended_geometry_support.sum() >= 32
                        or intended_geometry_energy >= 256.0
                    )
                    leaked_energy_limit = (
                        _MATERIAL_EFFECT_LEAKED_ENERGY_MAX
                        if index == 0
                        else _MATERIAL_EFFECT_COARSE_LEAKED_ENERGY_MAX
                    )
                    if (
                        meaningful_geometry
                        and (index == 0 or stage_name == "new-effect")
                        and leaked_energy_ratio > leaked_energy_limit
                    ):
                        raise ValueError(
                            f"압축 후 보조맵 mip {index} {stage_name} effect edge에 "
                            "과도한 bleed가 생겼어요"
                        )
                    if leaked_delta_max > _MATERIAL_EFFECT_LEAKED_DELTA_MAX:
                        raise ValueError(
                            f"압축 후 보조맵 mip {index} {stage_name} effect edge에 "
                            "국소 최대 bleed가 너무 커요"
                        )
                    if (
                        stage_name == "old-effect-removal"
                        and index > 0
                        and meaningful_geometry
                        and index <= 2
                        and leaked_energy_ratio
                        > _MATERIAL_OLD_EFFECT_EARLY_MIP_LEAKED_ENERGY_MAX
                    ):
                        raise ValueError(
                            f"압축 후 보조맵 mip {index} {stage_name} effect edge에 "
                            "과도한 bleed가 생겼어요"
                        )
                    center_error_limit = min(
                        _MATERIAL_EFFECT_MAX_CENTER_ERROR_TEXELS,
                        _MATERIAL_EFFECT_CENTER_ERROR_TEXELS + 0.5 * index,
                    )
                    bbox_error_limit = min(
                        _MATERIAL_EFFECT_MAX_BBOX_ERROR_TEXELS,
                        _MATERIAL_EFFECT_BBOX_ERROR_TEXELS + index // 3,
                    )
                    if (
                        center_error_texels > center_error_limit
                        or bbox_error_texels > bbox_error_limit
                    ):
                        raise ValueError(
                            f"압축 후 보조맵 mip {index} {stage_name} "
                            "effect edge/중심이 허용 정렬 오차를 벗어났어요"
                        )
                intended_vector = intended_delta[intended_support].astype(np.float64)
                actual_vector = actual_delta[intended_support].astype(np.float64)
                if (
                    meaningful_signal
                    and float(np.sum(intended_vector * actual_vector)) <= 0.0
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index} {stage_name} 방향이 뒤집혔어요"
                    )
                intended_energy = float(np.abs(intended_vector).sum())
                actual_energy = float(np.abs(actual_vector).sum())
                directed_components = intended_vector != 0.0
                same_direction = (
                    intended_vector[directed_components]
                    * actual_vector[directed_components]
                    > 0.0
                )
                direction_recall = float(same_direction.mean())
                wrong_direction_energy_ratio = float(
                    np.abs(
                        actual_vector[directed_components][~same_direction]
                    ).sum()
                    / max(intended_energy, 1.0)
                )
                if (
                    meaningful_signal
                    and wrong_direction_energy_ratio
                    > _MATERIAL_WRONG_DIRECTION_ENERGY_MAX
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index} {stage_name}의 "
                        "국소 효과 방향이 뒤집혔어요"
                    )
                if (
                    stage_name == "new-effect"
                    and direction_recall < _MATERIAL_NEW_EFFECT_DIRECTION_RECALL
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index} {stage_name}의 "
                        "방향 support 보존율이 너무 낮아요"
                    )
                energy_ratio = actual_energy / max(intended_energy, 1.0)
                if index == 0:
                    energy_minimum, energy_maximum = 0.75, 1.25
                elif index == 1:
                    energy_minimum, energy_maximum = 0.65, 1.5
                elif index == 2:
                    energy_minimum, energy_maximum = 0.5, 2.0
                else:
                    energy_minimum, energy_maximum = 0.2, 6.0
                if meaningful_signal and not (
                    energy_minimum <= energy_ratio <= energy_maximum
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index} {stage_name} 강도가 허용 범위를 벗어났어요"
                    )
                delta_error = np.abs(actual_vector - intended_vector)
                delta_p95 = float(np.percentile(delta_error, 95))
                delta_p99 = float(np.percentile(delta_error, 99))
                delta_max = int(delta_error.max(initial=0.0))
                if (
                    float(delta_error.mean()) > max(8.0, max_mae * 2.0)
                    or delta_p99 > _ROUNDTRIP_P99_FLOOR
                    or delta_max > _ROUNDTRIP_MAX_FLOOR
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index} {stage_name} delta 오차가 너무 커요"
                    )
            if role == "gloss" and stage_domain.any():
                direct_error = np.abs(
                    intended_after[..., selected].astype(np.int16)
                    - actual_after[..., selected].astype(np.int16)
                )[stage_domain]
                direct_p95 = float(np.percentile(direct_error, 95))
                direct_p99 = float(np.percentile(direct_error, 99))
                direct_max = int(direct_error.max(initial=0))
                if (
                    direct_p99 > _MATERIAL_ROI_P99_LIMIT
                    or direct_max > _MATERIAL_ROI_MAX_LIMIT
                ):
                    raise ValueError(
                        f"압축 후 Gloss mip {index} {stage_name} scalar ROI가 손상됐어요"
                    )
            stage_reports[stage_name] = {
                "intended_effect_texels": int(intended_support.sum()),
                "support_recall": round(support_recall, 6),
                "support_precision": round(support_precision, 6),
                "support_iou": round(support_iou, 6),
                "center_error_texels": round(center_error_texels, 6),
                "bbox_edge_error_texels": round(bbox_error_texels, 6),
                "leaked_energy_ratio": round(leaked_energy_ratio, 6),
                "leaked_delta_max": leaked_delta_max,
                "energy_ratio": round(energy_ratio, 6),
                "delta_error_p95": round(delta_p95, 6),
                "delta_error_p99": round(delta_p99, 6),
                "delta_error_max": delta_max,
                "direct_roundtrip_p99": round(direct_p99, 6),
                "direct_roundtrip_p95": round(direct_p95, 6),
                "direct_roundtrip_max": direct_max,
                "direction_recall": round(direction_recall, 6),
                "wrong_direction_energy_ratio": round(
                    wrong_direction_energy_ratio, 6
                ),
                "quantized_away_below_signal_floor": False,
            }

        final_edge_p95 = 0.0
        final_edge_p99 = 0.0
        final_edge_max = 0
        final_edge_domain = (
            allowed_blocks & covered & ~effect & ~(protected | seam)
        )
        if role == "gloss" and final_edge_domain.any():
            intended_total_delta = (
                intended_edited_values[..., selected].astype(np.int16)
                - intended_source_values[..., selected].astype(np.int16)
            )
            actual_total_delta = (
                edited_values[..., selected].astype(np.int16)
                - source_values[..., selected].astype(np.int16)
            )
            final_edge_error = np.abs(actual_total_delta - intended_total_delta)[
                final_edge_domain
            ]
            final_edge_p95 = float(np.percentile(final_edge_error, 95))
            final_edge_p99 = float(np.percentile(final_edge_error, 99))
            final_edge_max = int(final_edge_error.max(initial=0))
            if final_edge_max > _MATERIAL_EFFECT_LEAKED_DELTA_MAX:
                raise ValueError(
                    f"압축 후 Gloss mip {index} 최종 edge bleed가 너무 커요"
                )

        old_residual_energy_ratio = 0.0
        old_high_residual_fraction = 0.0
        old_domain = old_effect & covered
        if old_domain.any():
            intended_removal = (
                intended_neutral_values[..., selected].astype(np.int16)
                - intended_source_values[..., selected].astype(np.int16)
            )
            removed_components = old_domain[..., None] & (
                np.abs(intended_removal) >= _MATERIAL_OLD_RESIDUAL_MIN_REMOVAL
            )
            if removed_components.any():
                final_residual = np.abs(
                    edited_values[..., selected].astype(np.int16)
                    - intended_edited_values[..., selected].astype(np.int16)
                )
                old_residual_energy_ratio = float(
                    final_residual[removed_components].astype(np.float64).sum()
                    / np.abs(
                        intended_removal[removed_components]
                    ).astype(np.float64).sum()
                )
                residual_ratios = (
                    final_residual[removed_components].astype(np.float64)
                    / np.abs(
                        intended_removal[removed_components]
                    ).astype(np.float64)
                )
                old_high_residual_fraction = float(
                    (residual_ratios >= _MATERIAL_OLD_HIGH_RESIDUAL_RATIO).mean()
                )
                significant_removal_components = int(removed_components.sum())
                significant_removal_pixels = int(
                    np.any(removed_components, axis=2).sum()
                )
                significant_removal_fraction = significant_removal_pixels / max(
                    int(covered.sum()), 1
                )
                residual_energy_limit = _MATERIAL_OLD_RESIDUAL_ENERGY_MAX
                if index == 0:
                    residual_energy_limit = _MATERIAL_OLD_RESIDUAL_MIP0_ENERGY_MAX
                enforce_residual = (
                    role == "gloss"
                    or index == 0
                    or significant_removal_components >= 4
                    or significant_removal_fraction >= 0.05
                )
                high_residual_fraction_limit = (
                    _MATERIAL_OLD_HIGH_RESIDUAL_FRACTION_MAX
                    if index == 0
                    else 0.25 if index <= 5 else 0.5
                )
                if (
                    (
                        enforce_residual
                        and old_residual_energy_ratio > residual_energy_limit
                    )
                    or (
                        enforce_residual
                        and old_high_residual_fraction
                        > high_residual_fraction_limit
                    )
                ):
                    raise ValueError(
                        f"압축 후 보조맵 mip {index}에서 old-effect 잔류 에너지가 "
                        "허용 범위를 벗어났어요"
                    )

        protected_domain = (protected | seam) & covered
        protected_p95 = 0.0
        protected_p99 = 0.0
        protected_max = 0
        protected_angle_p95 = 0.0
        protected_angle_p99 = 0.0
        protected_angle_max = 0.0
        if protected_domain.any():
            protected_error = np.abs(
                source_values.astype(np.int16) - edited_values.astype(np.int16)
            )[protected_domain]
            _, protected_p99_limit, protected_max_limit = _roundtrip_limits(
                role,
                intended_source.width,
                intended_source.height,
                max_mae,
            )
            protected_p99 = float(np.percentile(protected_error, 99))
            protected_p95 = float(np.percentile(protected_error, 95))
            protected_max = int(protected_error.max(initial=0))
            protected_p99_limit = min(
                protected_p99_limit, _MATERIAL_ROI_P99_LIMIT
            )
            if role == "gloss":
                protected_max_limit = min(
                    protected_max_limit,
                    12.0 if index == 0 else 20.0 if index == 1 else 32.0,
                )
            else:
                protected_max_limit = min(protected_max_limit, 64.0)
            if protected_p99 > protected_p99_limit or protected_max > protected_max_limit:
                raise ValueError(
                    f"압축 후 보조맵 mip {index} protected/seam ROI가 손상됐어요"
                )
            if role == "normal":
                intended_protected, source_protected_xy = decode_normal(source_values)
                actual_protected, protected_xy_length = decode_normal(edited_values)
                source_protected_z = intended_protected[..., 2]
                actual_protected_z = actual_protected[..., 2]
                newly_invalid = protected_domain & (
                    (
                        (source_protected_xy <= 1.0)
                        & (protected_xy_length > 1.0)
                    )
                    | (
                        (source_protected_z >= 0.05)
                        & (actual_protected_z < 0.05)
                    )
                )
                if newly_invalid.any():
                    raise ValueError(
                        f"압축 후 Normal mip {index} protected/seam이 새로 "
                        "DXT5nm 단위 원/양의 Z 한계를 벗어났어요"
                    )
                protected_dot = np.sum(
                    intended_protected * actual_protected, axis=2
                )
                protected_angles = np.degrees(
                    np.arccos(
                        np.clip(protected_dot[protected_domain], -1.0, 1.0)
                    )
                )
                protected_angle_p95 = float(
                    np.percentile(protected_angles, 95)
                )
                protected_angle_p99 = float(
                    np.percentile(protected_angles, 99)
                )
                protected_angle_max = float(
                    protected_angles.max(initial=0.0)
                )
                if (
                    protected_angle_p95 > (10.0 if index == 0 else 15.0)
                    or protected_angle_p99 > (15.0 if index == 0 else 25.0)
                    or protected_angle_max > (15.0 if index == 0 else 45.0)
                ):
                    raise ValueError(
                        f"압축 후 Normal mip {index} protected/seam ROI 각도가 손상됐어요"
                    )

        final_gloss_p95 = 0.0
        final_gloss_p99 = 0.0
        final_gloss_max = 0
        if role == "gloss" and effect_domain.any():
            final_gloss_error = np.abs(
                intended_edited_values[..., selected].astype(np.int16)
                - edited_values[..., selected].astype(np.int16)
            )[effect_domain]
            final_gloss_p95 = float(np.percentile(final_gloss_error, 95))
            final_gloss_p99 = float(np.percentile(final_gloss_error, 99))
            final_gloss_max = int(final_gloss_error.max(initial=0))
            if (
                final_gloss_p99 > _MATERIAL_ROI_P99_LIMIT
                or final_gloss_max > _MATERIAL_ROI_MAX_LIMIT
            ):
                raise ValueError(
                    f"압축 후 Gloss mip {index} 최종 effect ROI가 손상됐어요"
                )
        reports.append(
            {
                "level": index,
                "changed_outside_effect_blocks": int(changed_outside.sum()),
                "changed_inside_protected_or_seam": int(protected_changes.sum()),
                "checked_unaffected_texels": int((covered & ~allowed_blocks).sum()),
                "stages": stage_reports,
                "protected_roundtrip_p95": round(protected_p95, 6),
                "protected_roundtrip_p99": round(protected_p99, 6),
                "protected_roundtrip_max": protected_max,
                "protected_normal_angle_p95_deg": round(
                    protected_angle_p95, 6
                ),
                "protected_normal_angle_p99_deg": round(
                    protected_angle_p99, 6
                ),
                "protected_normal_angle_max_deg": round(
                    protected_angle_max, 6
                ),
                "final_gloss_roundtrip_p95": round(final_gloss_p95, 6),
                "final_gloss_roundtrip_p99": round(final_gloss_p99, 6),
                "final_gloss_roundtrip_max": final_gloss_max,
                "final_edge_bleed_p95": round(final_edge_p95, 6),
                "final_edge_bleed_p99": round(final_edge_p99, 6),
                "final_edge_bleed_max": final_edge_max,
                "old_effect_residual_energy_ratio": round(
                    old_residual_energy_ratio, 6
                ),
                "old_effect_high_residual_fraction": round(
                    old_high_residual_fraction, 6
                ),
                "normal": normal_metrics,
            }
        )
    cumulative_leak_ratios: dict[str, float] = {}
    for stage_name, (leaked_energy, intended_energy) in leaked_energy_totals.items():
        ratio = leaked_energy / max(intended_energy, 1.0)
        cumulative_leak_ratios[stage_name] = round(ratio, 6)
        if (
            intended_energy > 0.0
            and ratio > _MATERIAL_EFFECT_CUMULATIVE_LEAKED_ENERGY_MAX[stage_name]
        ):
            raise ValueError(
                f"압축 후 보조맵 {stage_name}의 전체 mip 누적 edge bleed가 "
                "허용 범위를 벗어났어요"
            )
    if reports:
        reports[-1]["cumulative_leaked_energy_ratio"] = cumulative_leak_ratios
    return reports


def _encode_mip_parts(
    obj: Any,
    texture: Any,
    image: Image.Image,
    role: str,
    coverage: Image.Image | None = None,
) -> tuple[list[bytes], list[Image.Image]]:
    from UnityPy.export import Texture2DConverter

    parts: list[bytes] = []
    levels = _mip_chain(image, role, texture.m_MipCount, coverage=coverage)
    for index, level in enumerate(levels):
        encoded, actual_format = Texture2DConverter.image_to_texture2d(
            level,
            texture.m_TextureFormat,
            obj.platform,
            texture.m_PlatformBlob,
        )
        if actual_format != texture.m_TextureFormat:
            raise ValueError(f"mip {index} 포맷이 {actual_format}으로 바뀌었어요")
        parts.append(encoded)
    return parts, levels


def _encode_mip_chain(
    obj: Any,
    texture: Any,
    image: Image.Image,
    role: str,
    coverage: Image.Image | None = None,
) -> bytes:
    parts, _ = _encode_mip_parts(obj, texture, image, role, coverage)
    return b"".join(parts)


def _decode_mip_parts(
    obj: Any,
    texture: Any,
    payload: bytes,
    part_lengths: list[int],
) -> list[Image.Image]:
    from UnityPy.export import Texture2DConverter

    if len(part_lengths) != texture.m_MipCount or sum(part_lengths) != len(payload):
        raise ValueError("mip payload 길이 명세가 실제 데이터와 달라요")
    levels: list[Image.Image] = []
    width, height = texture.m_Width, texture.m_Height
    offset = 0
    version = getattr(obj, "version", (0, 0, 0, 0))
    for index, length in enumerate(part_lengths):
        part = payload[offset : offset + length]
        offset += length
        try:
            level = Texture2DConverter.parse_image_data(
                part,
                width,
                height,
                texture.m_TextureFormat,
                version,
                obj.platform,
                texture.m_PlatformBlob,
                flip=True,
            ).convert("RGBA")
        except Exception as exc:
            raise ValueError(f"mip {index} 디코딩에 실패했어요: {exc}") from exc
        levels.append(level)
        width, height = max(1, width // 2), max(1, height // 2)
    return levels


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        if temporary.read_bytes() != value:
            raise AssertionError("임시 bundle 쓰기 검증에 실패했어요")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _verified_source_bundle(
    bundle_key: str,
    bundle_root: Path,
    override: Mapping[str, Any] | None,
    records: list[dict[str, Any]],
) -> Path:
    if override is not None:
        source_value = override.get("path")
        expected_sha256 = override.get("sha256")
        if not all(isinstance(value, str) and value for value in (source_value, expected_sha256)):
            raise ValueError(f"source override 필수 필드가 누락됐어요: {bundle_key}")
        source = Path(source_value).expanduser().resolve()
        fallback = (bundle_root / Path(bundle_key)).expanduser().resolve()
        if (
            (not source.is_file() or _sha256_file(source) != expected_sha256)
            and fallback.is_file()
            and _sha256_file(fallback) == expected_sha256
        ):
            source = fallback
    else:
        source = (bundle_root / Path(bundle_key)).expanduser().resolve()
        hashes = {
            record.get("bundle_sha256")
            for record in records
            if record.get("bundle_key") == bundle_key and record.get("bundle_sha256")
        }
        if len(hashes) != 1:
            raise ValueError(f"inventory의 원본 bundle 해시가 하나가 아니에요: {bundle_key}")
        expected_sha256 = hashes.pop()

    if not source.is_file():
        raise FileNotFoundError(f"원본 bundle을 찾지 못했어요: {source}")
    if _sha256_file(source) != expected_sha256:
        raise ValueError(f"inventory 이후 원본 bundle이 변경됐어요: {source}")
    return source


def _verified_uv_coverage(
    paths: ProjectPaths,
    inventory: dict[str, Any],
    target_id: str,
) -> Path:
    records = [record for record in inventory["records"] if record.get("target_id") == target_id]
    if len(records) != 1:
        raise ValueError(f"{target_id} 원본 record는 정확히 하나여야 해요")
    source = Path(records[0]["source_png"])
    report_path = paths.reviews / target_id / "uv-report.json"
    if not report_path.is_file():
        raise FileNotFoundError(f"{target_id} UV 검토 보고서가 없어요: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("schema_version") != 1
        or report.get("target_id") != target_id
        or report.get("passed") is not True
    ):
        raise ValueError(f"{target_id} UV 검토 보고서가 통과 상태가 아니에요")
    if not source.is_file() or report.get("source_sha256") != _sha256_file(source):
        raise ValueError(f"{target_id} UV 검토 뒤 원본이 변경됐어요")
    for mesh_value, expected_sha256 in report.get("mesh_sha256", {}).items():
        mesh = Path(mesh_value)
        if not mesh.is_file() or _sha256_file(mesh) != expected_sha256:
            raise ValueError(f"{target_id} UV 검토 뒤 mesh가 변경됐어요: {mesh}")
    coverage = Path(str(report.get("coverage", "")))
    if not coverage.is_file() or report.get("coverage_sha256") != _sha256_file(coverage):
        raise ValueError(f"{target_id} UV coverage가 없거나 SHA가 달라요")
    with Image.open(coverage) as coverage_file:
        expected_size = (int(records[0]["width"]), int(records[0]["height"]))
        _coverage_values(coverage_file, expected_size)
    return coverage


def _combined_uv_coverage(
    paths: list[Path],
    size: tuple[int, int],
) -> tuple[Image.Image, list[dict[str, str]]]:
    if not paths:
        raise ValueError("Texture2D의 UV coverage가 명시되지 않았어요")
    combined = np.zeros((size[1], size[0]), dtype=bool)
    sources = []
    for path in dict.fromkeys(value.expanduser().resolve() for value in paths):
        with Image.open(path) as coverage_file:
            values = _coverage_values(coverage_file, size)
        combined |= values
        sources.append({"path": str(path), "sha256": _sha256_file(path)})
    return Image.fromarray(combined.astype(np.uint8) * 255, "L"), sources


def patch_bundle_exact(
    source_bundle: Path,
    output_bundle: Path,
    replacements: Mapping[str, Path],
    *,
    roundtrip_dir: Path | None = None,
    max_mae: float = 6.0,
    roles: Mapping[str, str] | None = None,
    coverage_masks: Mapping[str, list[Path]] | None = None,
    edit_constraints: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    import UnityPy

    if not replacements:
        raise ValueError("교체할 Texture2D가 없어요")
    if not math.isfinite(max_mae) or max_mae <= 0:
        raise ValueError("max_mae는 양의 유한값이어야 해요")
    source_bundle = source_bundle.expanduser().resolve()
    output_bundle = output_bundle.expanduser().resolve()
    if source_bundle == output_bundle:
        raise ValueError("원본 bundle과 출력 bundle은 달라야 해요")
    original_bytes = source_bundle.read_bytes()
    environment = UnityPy.load(original_bytes)
    bundle = environment.file
    original_layout = parse_unityfs_layout(original_bytes, bundle)
    before_objects = _object_hashes(environment)
    rebuilt_bytes = original_bytes
    physical_ranges: list[tuple[int, int]] = []
    expected: dict[str, dict[str, Any]] = {}

    for texture_name, image_path in sorted(replacements.items()):
        obj, texture = _find_texture(environment, texture_name)
        metadata = _texture_metadata(texture)
        with Image.open(image_path) as image_file:
            image = image_file.convert("RGBA")
        if image.size != (texture.m_Width, texture.m_Height):
            raise ValueError(
                f"{texture_name} 크기 {image.size} != 원본 {(texture.m_Width, texture.m_Height)}"
            )
        stream = texture.m_StreamData
        if not stream.path or stream.offset < 0 or stream.size <= 0:
            raise ValueError(f"{texture_name}에 유효한 streamed image data가 없어요")
        role = (roles or {}).get(texture_name)
        if role not in {"diffuse", "normal", "gloss"}:
            raise ValueError(f"{texture_name}의 diffuse/normal/gloss 역할이 명시되지 않았어요")
        coverage, coverage_sources = _combined_uv_coverage(
            list((coverage_masks or {}).get(texture_name, [])),
            image.size,
        )
        auxiliary_validation: dict[str, Any] | None = None
        if role in {"normal", "gloss"}:
            constraint = (edit_constraints or {}).get(texture_name)
            if not isinstance(constraint, Mapping):
                raise ValueError(
                    f"{texture_name} 보조맵의 source/effect/protected mip 계약이 없어요"
                )
            source_value = constraint.get("source_image")
            if not isinstance(source_value, Path):
                source_value = Path(str(source_value or ""))
            with Image.open(source_value) as source_file:
                if source_file.mode != "RGBA" or source_file.size != image.size:
                    raise ValueError(f"{texture_name} source mip0 규격이 현재 보조맵과 달라요")
                source_image = source_file.copy()
            neutral_image = _constraint_rgba(
                constraint.get("neutral_image"),
                image.size,
                f"{texture_name} neutral base",
            )
            old_effect = _constraint_mask(
                constraint.get("old_effect_mask"),
                image.size,
                f"{texture_name} old-effect mask",
            )
            new_effect = _constraint_mask(
                constraint.get("new_effect_mask"),
                image.size,
                f"{texture_name} new-effect mask",
            )
            protected = _constraint_mask(
                constraint.get("protected_mask"),
                image.size,
                f"{texture_name} protected mask",
            )
            seam_guard = _constraint_mask(
                constraint.get("seam_guard_mask"),
                image.size,
                f"{texture_name} seam guard mask",
            )
            selected_channels = constraint.get("selected_channels")
            if not isinstance(selected_channels, list):
                raise ValueError(f"{texture_name} selected_channels가 없어요")
            (
                source_mips,
                neutral_mips,
                old_effect_mips,
                protected_mips,
                seam_mips,
                coverage_mips,
            ) = _validate_auxiliary_mip_invariants(
                source_image,
                neutral_image,
                role=role,
                count=texture.m_MipCount,
                coverage=coverage,
                selected_channels=selected_channels,
                effect_union=old_effect,
                protected=protected,
                seam_guard=seam_guard,
            )
            (
                second_neutral_mips,
                validated_mips,
                new_effect_mips,
                second_protected_mips,
                second_seam_mips,
                second_coverage_mips,
            ) = _validate_auxiliary_mip_invariants(
                neutral_image,
                image,
                role=role,
                count=texture.m_MipCount,
                coverage=coverage,
                selected_channels=selected_channels,
                effect_union=new_effect,
                protected=protected,
                seam_guard=seam_guard,
            )
            for label, before, after in (
                ("neutral", neutral_mips, second_neutral_mips),
                ("protected", protected_mips, second_protected_mips),
                ("seam", seam_mips, second_seam_mips),
                ("coverage", coverage_mips, second_coverage_mips),
            ):
                if any(
                    not np.array_equal(np.asarray(left), np.asarray(right))
                    for left, right in zip(before, after, strict=True)
                ):
                    raise AssertionError(f"{texture_name} {label} mip chain이 비결정적이에요")
            texture_format = int(texture.m_TextureFormat)
            block_size = _BC_BLOCK_SIZE_BY_FORMAT.get(texture_format)
            if block_size is None:
                raise ValueError(
                    f"{texture_name} TextureFormat {texture_format}의 압축 블록 범위를 "
                    "검증할 수 없어 자동 재패킹을 중단해요"
                )
            source_parts, encoded_source_mips = _encode_mip_parts(
                obj,
                texture,
                source_image,
                role,
                coverage,
            )
            if any(
                not np.array_equal(np.asarray(before), np.asarray(after))
                for before, after in zip(source_mips, encoded_source_mips, strict=True)
            ):
                raise AssertionError(f"{texture_name} source no-op mip chain이 비결정적이에요")
            source_roundtrip = _decode_mip_parts(
                obj,
                texture,
                b"".join(source_parts),
                [len(part) for part in source_parts],
            )
            neutral_parts, encoded_neutral_mips = _encode_mip_parts(
                obj,
                texture,
                neutral_image,
                role,
                coverage,
            )
            if any(
                not np.array_equal(np.asarray(before), np.asarray(after))
                for before, after in zip(
                    neutral_mips, encoded_neutral_mips, strict=True
                )
            ):
                raise AssertionError(f"{texture_name} neutral mip chain이 비결정적이에요")
            neutral_roundtrip = _decode_mip_parts(
                obj,
                texture,
                b"".join(neutral_parts),
                [len(part) for part in neutral_parts],
            )
            auxiliary_validation = {
                "source_mips": source_mips,
                "neutral_mips": neutral_mips,
                "validated_mips": validated_mips,
                "source_roundtrip": source_roundtrip,
                "neutral_roundtrip": neutral_roundtrip,
                "old_effect_mips": old_effect_mips,
                "new_effect_mips": new_effect_mips,
                "protected_mips": protected_mips,
                "seam_mips": seam_mips,
                "coverage_mips": coverage_mips,
                "block_size": block_size,
                "selected_channels": selected_channels,
            }
        mip_parts, intended_mips = _encode_mip_parts(
            obj,
            texture,
            image,
            role,
            coverage,
        )
        if auxiliary_validation is not None and any(
            not np.array_equal(np.asarray(before), np.asarray(after))
            for before, after in zip(
                auxiliary_validation["validated_mips"], intended_mips, strict=True
            )
        ):
            raise AssertionError(f"{texture_name} edited mip chain이 비결정적이에요")
        payload = b"".join(mip_parts)
        if len(payload) != stream.size:
            raise ValueError(f"{texture_name} payload {len(payload)} != stream size {stream.size}")
        resource_name = stream.path.rsplit("/", 1)[-1]
        if resource_name not in bundle.files:
            raise ValueError(f"bundle 안에 stream resource가 없어요: {resource_name}")
        resource_entry = find_directory_entry(original_layout, resource_name)
        if stream.offset + stream.size > resource_entry.size:
            raise ValueError(f"{texture_name} stream 범위가 resource entry 밖이에요")
        rebuilt_bytes, patches = patch_uncompressed_logical_range(
            rebuilt_bytes,
            original_layout,
            resource_entry.offset + stream.offset,
            payload,
        )
        physical_ranges.extend((patch.start, patch.end) for patch in patches)
        expected[texture_name] = {
            "metadata": metadata,
            "mips": intended_mips,
            "mip_part_lengths": [len(part) for part in mip_parts],
            "image_path": image_path,
            "payload_size": len(payload),
            "resource": resource_name,
            "role": role,
            "coverage_sources": coverage_sources,
            "auxiliary_validation": auxiliary_validation,
        }

    merged_ranges = merge_ranges(physical_ranges)
    if not bytes_equal_outside_ranges(original_bytes, rebuilt_bytes, merged_ranges):
        raise AssertionError("대상 stream payload 밖의 bundle bytes가 달라졌어요")

    rebuilt = UnityPy.load(rebuilt_bytes)
    if before_objects != _object_hashes(rebuilt):
        raise AssertionError("serialized object가 변경됐어요")
    rebuilt_layout = parse_unityfs_layout(rebuilt_bytes, rebuilt.file)
    if layout_signature(original_layout) != layout_signature(rebuilt_layout):
        raise AssertionError("UnityFS layout이 변경됐어요")

    texture_reports = []
    for texture_name, info in expected.items():
        rebuilt_obj, texture = _find_texture(rebuilt, texture_name)
        if _texture_metadata(texture) != info["metadata"]:
            raise AssertionError(f"{texture_name} metadata가 변경됐어요")
        roundtrip_mips = _decode_mip_parts(
            rebuilt_obj,
            texture,
            bytes(texture.get_image_data()),
            info["mip_part_lengths"],
        )
        if len(roundtrip_mips) != len(info["mips"]):
            raise AssertionError(f"{texture_name} 왕복 mip 수가 달라요")
        auxiliary_mip_reports = None
        if info["auxiliary_validation"] is not None:
            validation = info["auxiliary_validation"]
            auxiliary_mip_reports = _validate_compressed_auxiliary_invariants(
                validation["source_mips"],
                validation["neutral_mips"],
                validation["validated_mips"],
                validation["source_roundtrip"],
                validation["neutral_roundtrip"],
                roundtrip_mips,
                validation["old_effect_mips"],
                validation["new_effect_mips"],
                validation["protected_mips"],
                validation["seam_mips"],
                validation["coverage_mips"],
                role=info["role"],
                selected_channels=validation["selected_channels"],
                block_size=validation["block_size"],
                max_mae=max_mae,
            )
        mip_reports = []
        for index, (intended_image, roundtrip) in enumerate(
            zip(info["mips"], roundtrip_mips, strict=True)
        ):
            intended = np.asarray(intended_image, dtype=np.int16)
            actual = np.asarray(roundtrip, dtype=np.int16)
            if intended.shape != actual.shape:
                raise AssertionError(f"{texture_name} mip {index} 왕복 이미지 shape가 달라요")
            absolute = np.abs(intended - actual)
            channel_mae = absolute.mean(axis=(0, 1))
            p95 = np.percentile(absolute, 95, axis=(0, 1))
            p99 = np.percentile(absolute, 99, axis=(0, 1))
            maximum = absolute.max(axis=(0, 1))
            mae_limit, localized_p99_limit, maximum_limit = _roundtrip_limits(
                info["role"],
                intended_image.width,
                intended_image.height,
                max_mae,
            )
            if np.any(channel_mae > mae_limit):
                raise AssertionError(
                    f"{texture_name} mip {index} 왕복 MAE {channel_mae.tolist()}가 "
                    f"제한 {mae_limit}를 넘었어요"
                )
            if np.any(p99 > localized_p99_limit):
                raise AssertionError(
                    f"{texture_name} mip {index} 왕복 p99 {p99.tolist()}가 "
                    f"제한 {localized_p99_limit}를 넘었어요"
                )
            if np.any(maximum > maximum_limit):
                raise AssertionError(
                    f"{texture_name} mip {index} 왕복 최대 오차 {maximum.tolist()}가 "
                    f"제한 {maximum_limit}를 넘었어요"
                )
            roundtrip_path = None
            if roundtrip_dir is not None:
                suffix = "" if index == 0 else f".mip-{index:02d}"
                roundtrip_path = roundtrip_dir / f"{texture_name}{suffix}.png"
                roundtrip_path.parent.mkdir(parents=True, exist_ok=True)
                roundtrip.save(roundtrip_path)
            mip_reports.append(
                {
                    "level": index,
                    "width": intended_image.width,
                    "height": intended_image.height,
                    "payload_size": info["mip_part_lengths"][index],
                    "channel_mae": [round(float(value), 6) for value in channel_mae],
                    "channel_p95": [round(float(value), 6) for value in p95],
                    "channel_p99": [round(float(value), 6) for value in p99],
                    "channel_max": [int(value) for value in maximum],
                    "limits": {
                        "channel_mae": mae_limit,
                        "channel_p99": localized_p99_limit,
                        "channel_max": maximum_limit,
                    },
                    "roundtrip": str(roundtrip_path) if roundtrip_path else None,
                }
            )
        top = mip_reports[0]
        texture_reports.append(
            {
                "texture": texture_name,
                "source_image": str(info["image_path"]),
                "payload_size": info["payload_size"],
                "resource": info["resource"],
                "channel_mae": top["channel_mae"],
                "channel_p95": top["channel_p95"],
                "channel_p99": top["channel_p99"],
                "channel_max": top["channel_max"],
                "mip_filter": {
                    "diffuse": "uv-nearest-pad+linear-light-area",
                    "normal": "uv-nearest-pad+vector-area-renormalized",
                    "gloss": "uv-nearest-pad+linear-area",
                }[info["role"]],
                "uv_coverage": info["coverage_sources"],
                "uv_padding_verified": True,
                "roundtrip": top["roundtrip"],
                "checked_mips": [report["level"] for report in mip_reports],
                "missing_mips": 0,
                "mips": mip_reports,
                "material_edit_invariants": (
                    {
                        "selected_channels": info["auxiliary_validation"][
                            "selected_channels"
                        ],
                        "precompression_unselected_channels_equal": True,
                        "precompression_outside_effect_union_equal": True,
                        "postcompression_outside_effect_blocks_equal": True,
                        "compression_block_size": info["auxiliary_validation"][
                            "block_size"
                        ],
                        "mips": auxiliary_mip_reports,
                    }
                    if auxiliary_mip_reports is not None
                    else None
                ),
            }
        )

    _atomic_write(output_bundle, rebuilt_bytes)
    if output_bundle.read_bytes() != rebuilt_bytes:
        raise AssertionError("공개된 bundle bytes가 검증본과 달라요")
    return {
        "source_bundle": str(source_bundle),
        "source_sha256": _sha256_bytes(original_bytes),
        "output_bundle": str(output_bundle),
        "output_sha256": _sha256_bytes(rebuilt_bytes),
        "bundle_size": len(rebuilt_bytes),
        "physical_patch_ranges": merged_ranges,
        "bytes_equal_outside_payloads": True,
        "layout_equal": True,
        "serialized_objects_equal": True,
        "textures": texture_reports,
        "passed": True,
    }


def _prune_stale_preserved_auxiliary_outputs(
    paths: ProjectPaths,
    inventory: dict[str, Any],
    auxiliary_plans: Mapping[tuple[Any, int, Any], dict[str, Any]],
    selected_target_ids: set[str],
    current_bundle_keys: set[str],
) -> list[dict[str, Any]]:
    """현재 보존 정책과 충돌하는 예전 N/G 출력만 증명 가능한 경우 제거해요."""

    material_targets = _material_targets(
        inventory.get("records", []), inventory.get("materials", [])
    )
    candidates: dict[str, set[str]] = {}
    for (bundle_key, path_id, role), plan in auxiliary_plans.items():
        if (
            not isinstance(bundle_key, str)
            or bundle_key in current_bundle_keys
            or role not in {"normal", "gloss"}
            or plan.get("policy") != "preserve"
        ):
            continue
        consumer_identities = {
            _material_identity(material)
            for material in inventory.get("materials", [])
            if any(
                slot.get("texture_bundle_key") == bundle_key
                and int(slot.get("path_id", 0)) == path_id
                for slot in material.get("texture_slots", [])
            )
        }
        if not consumer_identities or any(
            identity not in material_targets for identity in consumer_identities
        ):
            continue
        consumer_targets = {
            target_id
            for identity in consumer_identities
            for target_id in material_targets[identity]
        }
        if not consumer_targets or not consumer_targets.issubset(selected_target_ids):
            continue
        records = [
            record
            for record in inventory.get("records", [])
            if record.get("bundle_key") == bundle_key
            and int(record.get("path_id", 0)) == path_id
            and record.get("role") == role
        ]
        if len(records) == 1:
            candidates.setdefault(bundle_key, set()).add(str(records[0]["texture"]))

    pruned: list[dict[str, Any]] = []
    roundtrip_root = (paths.reports / "roundtrip").resolve()
    for bundle_key, texture_names in sorted(candidates.items()):
        output = (paths.bundles / Path(bundle_key)).resolve()
        report_path = (
            paths.reports / "bundles" / f"{safe_bundle_name(bundle_key)}.json"
        ).resolve()
        if not output.is_file() or not report_path.is_file():
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        reported_output = Path(str(report.get("output_bundle", ""))).resolve()
        reported_textures = {
            str(value.get("texture"))
            for value in report.get("textures", [])
            if isinstance(value, dict) and value.get("texture")
        }
        if (
            reported_output != output
            or not reported_textures
            or not reported_textures.issubset(texture_names)
            or report.get("output_sha256") != _sha256_file(output)
        ):
            continue
        roundtrip_paths: set[Path] = set()
        for texture_report in report.get("textures", []):
            if not isinstance(texture_report, dict):
                continue
            values = [texture_report.get("roundtrip")]
            values.extend(
                mip.get("roundtrip")
                for mip in texture_report.get("mips", [])
                if isinstance(mip, dict)
            )
            for value in values:
                if not isinstance(value, str) or not value:
                    continue
                candidate = Path(value).resolve()
                if candidate.is_file() and roundtrip_root in candidate.parents:
                    roundtrip_paths.add(candidate)
        output_sha256 = _sha256_file(output)
        output.unlink()
        report_path.unlink()
        for candidate in roundtrip_paths:
            candidate.unlink()
        pruned.append(
            {
                "bundle_key": bundle_key,
                "output_sha256": output_sha256,
                "textures": sorted(reported_textures),
                "reason": "current-material-policy-preserve",
            }
        )
    return pruned


def _validate_auxiliary_plan_manifest(
    expected: Mapping[tuple[str, int, str], Mapping[str, Any]],
    raw_plans: Any,
    raw_outputs: Any,
    derived_count: Any,
) -> tuple[
    dict[tuple[str, int, str], dict[str, Any]],
    list[dict[str, Any]],
]:
    if not isinstance(raw_plans, list) or any(
        not isinstance(value, dict) for value in raw_plans
    ):
        raise ValueError("derived manifest의 auxiliary_plans가 객체 배열이 아니에요")
    if not isinstance(raw_outputs, list) or any(
        not isinstance(value, dict) for value in raw_outputs
    ):
        raise ValueError("derived manifest의 outputs가 객체 배열이 아니에요")
    if (
        not isinstance(derived_count, int)
        or isinstance(derived_count, bool)
        or derived_count != len(raw_outputs)
    ):
        raise ValueError("derived manifest의 derived_count가 outputs 수와 달라요")
    actual = {
        (
            str(value.get("texture_bundle_key")),
            int(value.get("path_id", 0)),
            str(value.get("role")),
        ): value
        for value in raw_plans
    }
    if len(actual) != len(raw_plans):
        raise ValueError("derived manifest의 보조맵 계획 identity가 중복됐어요")
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(
            "derived manifest의 보조맵 계획이 현재 material 계약과 달라요: "
            f"missing={missing}, extra={extra}"
        )
    for key, expected_plan in expected.items():
        actual_plan = actual[key]
        for field in ("policy", "old_text_mask_sha256", "operation_signature"):
            if actual_plan.get(field) != expected_plan.get(field):
                raise ValueError(f"derived 보조맵 {key} {field}가 현재 계약과 달라요")
        actual_targets = actual_plan.get("target_ids")
        expected_targets = expected_plan.get("target_ids")
        if (
            not isinstance(actual_targets, list)
            or len(actual_targets) != len(set(actual_targets))
            or set(actual_targets) != set(expected_targets)
        ):
            raise ValueError(f"derived 보조맵 {key} target_ids가 현재 계약과 달라요")
    return actual, raw_outputs


def repack_collection(
    profile: CollectionProfile,
    paths: ProjectPaths,
    bundle_root: Path,
    *,
    max_mae: float = 6.0,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    image_report = validate_approved(profile, paths, target_ids=target_ids)
    if not image_report["passed"]:
        failed = [report["target_id"] for report in image_report["reports"] if not report["passed"]]
        raise ValueError(
            f"승인 이미지 게이트가 실패해 재패킹을 중단해요: "
            f"missing={image_report['missing']}, failed={failed}"
        )
    inventory = load_inventory(paths.inventory)
    verify_material_dependency_snapshot(inventory, bundle_root)
    overrides = inventory.get("source_bundle_overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("inventory source_bundle_overrides가 객체가 아니에요")
    for bundle_key, override in sorted(overrides.items()):
        if not isinstance(bundle_key, str) or not bundle_key:
            raise ValueError("inventory source override bundle key가 잘못됐어요")
        source = _verified_source_bundle(
            bundle_key,
            bundle_root,
            override,
            inventory.get("records", []),
        )
        verify_source_override_material_graph(
            override,
            source,
            inventory.get("materials", []),
            bundle_key,
        )
    groups: dict[str, dict[str, Path]] = {}
    roles: dict[str, dict[str, str]] = {}
    coverage_masks: dict[str, dict[str, list[Path]]] = {}
    edit_constraints: dict[str, dict[str, dict[str, Any]]] = {}
    for target in profile.targets:
        if target_ids is not None and target.id not in target_ids:
            continue
        approved = paths.approved / f"{target.id}.png"
        if target.action != "localize" or not approved.is_file():
            continue
        record = record_for_target(inventory, target)
        groups.setdefault(record["bundle_key"], {})[record["texture"]] = approved
        roles.setdefault(record["bundle_key"], {})[record["texture"]] = "diffuse"
        coverage_masks.setdefault(record["bundle_key"], {}).setdefault(
            record["texture"], []
        ).append(_verified_uv_coverage(paths, inventory, target.id))

    if not paths.derived_manifest.is_file():
        raise ValueError("현재 승인 SHA에 묶인 D/N/G derived manifest가 없어요")
    derived = json.loads(paths.derived_manifest.read_text(encoding="utf-8"))
    if derived.get("schema_version") != 3:
        raise ValueError("예전 derived manifest는 재사용할 수 없어요. derive를 다시 실행해 주세요")
    expected_targets = {
        target.id
        for target in profile.targets
        if target.action == "localize" and (target_ids is None or target.id in target_ids)
    }
    validated = {
        value.get("target_id"): value
        for value in derived.get("validated_targets", [])
        if isinstance(value, dict)
    }
    profile_target_ids = {target.id for target in profile.targets}
    unknown_validated = set(validated) - profile_target_ids
    if unknown_validated:
        raise ValueError(
            f"derived manifest에 profile 밖 검증 대상이 있어요: {sorted(unknown_validated)}"
        )
    missing_material_validation = expected_targets - set(validated)
    if missing_material_validation:
        raise ValueError(
            f"재질 게이트를 통과하지 않은 대상이 있어요: {sorted(missing_material_validation)}"
        )
    material_review_hashes: dict[str, str] = {}
    material_contract_maps: dict[str, dict[str, Any]] = {}
    material_master_lettering: dict[str, list[dict[str, Any]]] = {}
    material_stage_data: dict[str, dict[str, Any]] = {}
    material_edit_plans: dict[str, dict[str, Any]] = {}

    def bind_material_target(target_id: str) -> None:
        if target_id in material_contract_maps:
            return
        if target_id not in validated:
            raise ValueError(f"{target_id} 재질 검증 기록이 derived manifest에 없어요")
        value = validated[target_id]
        target = profile.target_by_id(target_id)
        approved = paths.approved / f"{target.id}.png"
        if value.get("approved_sha256") != sha256_file(approved):
            raise ValueError(f"{target.id} 재질 검증 뒤 승인본이 변경됐어요")
        if value.get("approval_sha256") != sha256_file(approval_path(paths, target.id)):
            raise ValueError(f"{target.id} 재질 검증 뒤 승인 증거가 변경됐어요")
        _, current_review = load_review(paths, target.id, through="material")
        current_material_hash = review_stage_sha256(
            current_review, "material_validation"
        )
        if value.get("material_review_sha256") != current_material_hash:
            raise ValueError(f"{target.id} 파생 뒤 재질 검증 단계가 변경됐어요")
        material_review_hashes[target_id] = current_material_hash
        material_data = current_review["stages"]["material_validation"]["data"]
        edit_data = current_review["stages"]["edit_plan"]["data"]
        if not isinstance(material_data, dict) or not isinstance(edit_data, dict):
            raise ValueError(f"{target.id} 현재 material/edit-plan 데이터가 잘못됐어요")
        diffuse_record = record_for_target(inventory, target)
        current_bindings = _target_bindings(inventory, target, diffuse_record)
        for material_bundle_key in sorted(
            {str(binding["bundle_key"]) for binding in current_bindings}
        ):
            _verified_source_bundle(
                material_bundle_key,
                bundle_root,
                overrides.get(material_bundle_key),
                inventory.get("materials", []),
            )
        current_signature = json.loads(
            json.dumps(
                [list(item) for item in _binding_signature(current_bindings)],
                ensure_ascii=False,
            )
        )
        if value.get("material_bindings") != current_signature:
            raise ValueError(f"{target.id} 파생 뒤 현재 Material graph가 변경됐어요")
        material_stage_data[target_id] = material_data
        material_edit_plans[target_id] = edit_data
        contract = material_data.get("auxiliary_contract")
        maps = contract.get("maps") if isinstance(contract, dict) else None
        if not isinstance(maps, dict):
            raise ValueError(f"{target.id} 보조맵 source-base 계약이 없어요")
        reviewed_consumers = material_data.get("shared_consumers")
        if not isinstance(reviewed_consumers, dict):
            raise ValueError(f"{target.id} 공유 보조맵 소비자 계약이 없어요")
        for contract_key, entry in maps.items():
            identity = entry.get("identity") if isinstance(entry, dict) else None
            if not isinstance(identity, dict):
                raise ValueError(f"{target.id}::{contract_key} 보조맵 identity가 없어요")
            current_consumers = _all_consumers(
                inventory,
                str(identity.get("texture_bundle_key")),
                int(identity.get("path_id", 0)),
            )
            if reviewed_consumers.get(contract_key) != current_consumers:
                raise ValueError(
                    f"{target.id}::{contract_key} 파생 뒤 공유 소비자가 변경됐어요"
                )
        material_contract_maps[target_id] = maps
        masters = contract.get("master_lettering") if isinstance(contract, dict) else None
        if not isinstance(masters, list) or not masters:
            raise ValueError(f"{target.id} 보조맵 master lettering 계약이 없어요")
        material_master_lettering[target_id] = masters

    manifest_scope_targets = set(validated)
    for target_id in sorted(manifest_scope_targets):
        bind_material_target(target_id)

    expected_auxiliary_plans: dict[tuple[str, int, str], dict[str, Any]] = {}
    for target_id in sorted(manifest_scope_targets):
        material_data = material_stage_data[target_id]
        policies = material_data.get("policies")
        if not isinstance(policies, dict):
            raise ValueError(f"{target_id} 현재 보조맵 policies가 없어요")
        for contract_key, entry in material_contract_maps[target_id].items():
            if not isinstance(entry, dict) or not isinstance(entry.get("identity"), dict):
                raise ValueError(f"{target_id}::{contract_key} 보조맵 identity가 없어요")
            identity = entry["identity"]
            role = identity.get("role")
            if role not in {"normal", "gloss"}:
                raise ValueError(f"{target_id}::{contract_key} 보조맵 역할이 잘못됐어요")
            policy = policies.get(contract_key)
            if policy not in {
                "preserve",
                "neutralize_old_text",
                "neutralize_and_derive",
            }:
                raise ValueError(f"{target_id}::{contract_key} 보조맵 policy가 잘못됐어요")
            if policy == "preserve":
                old_mask_sha = "preserve"
                operation_signature = "preserve"
            else:
                material_mask = _material_mask_descriptor(material_data, contract_key)
                old_mask_sha = material_mask["sha256"]
                operation_signature = json.dumps(
                    {
                        "material_mask": material_mask,
                        "identity": entry.get("identity"),
                        "channel_contract": entry.get("channel_contract"),
                        "neutralization_signature": entry.get(
                            "neutralization_signature"
                        ),
                        "derivation": (
                            entry.get("derivation")
                            if policy == "neutralize_and_derive"
                            else None
                        ),
                        "master_lettering": (
                            _canonical_master_lettering(
                                material_master_lettering[target_id]
                            )
                            if policy == "neutralize_and_derive"
                            else []
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            plan_key = (
                str(identity.get("texture_bundle_key")),
                int(identity.get("path_id", 0)),
                str(role),
            )
            signature = (policy, old_mask_sha, operation_signature)
            existing = expected_auxiliary_plans.get(plan_key)
            if existing is None:
                expected_auxiliary_plans[plan_key] = {
                    "policy": policy,
                    "old_text_mask_sha256": old_mask_sha,
                    "operation_signature": operation_signature,
                    "target_ids": [target_id],
                }
            else:
                current = (
                    existing.get("policy"),
                    existing.get("old_text_mask_sha256"),
                    existing.get("operation_signature"),
                )
                if current != signature:
                    raise ValueError(
                        f"공유 보조맵 {plan_key}의 현재 target별 계약이 달라요"
                    )
                if target_id not in existing["target_ids"]:
                    existing["target_ids"].append(target_id)

    auxiliary_plans, manifest_outputs = _validate_auxiliary_plan_manifest(
        expected_auxiliary_plans,
        derived.get("auxiliary_plans"),
        derived.get("outputs"),
        derived.get("derived_count"),
    )
    output_targets = {output.get("target_id") for output in manifest_outputs}
    unknown = output_targets - profile_target_ids
    if unknown:
        raise ValueError(f"derived manifest에 profile 밖 대상이 있어요: {sorted(unknown)}")
    outputs_by_plan: dict[tuple[Any, int, Any], list[dict[str, Any]]] = {}
    for output in manifest_outputs:
        key = (output.get("bundle_key"), int(output.get("path_id", 0)), output.get("role"))
        plan = auxiliary_plans.get(key)
        if not isinstance(plan, dict):
            raise ValueError(f"derived 보조맵 {key}에 대응하는 실행 계획이 없어요")
        target_ids_value = plan.get("target_ids")
        if (
            not isinstance(target_ids_value, list)
            or not target_ids_value
            or output.get("target_id") not in target_ids_value
        ):
            raise ValueError(f"derived 보조맵 {key} target 계획이 산출물과 달라요")
        outputs_by_plan.setdefault(key, []).append(output)
    selected_outputs: list[dict[str, Any]] = []
    for key, plan in auxiliary_plans.items():
        target_ids_value = plan.get("target_ids")
        if (
            not isinstance(target_ids_value, list)
            or not target_ids_value
            or any(target_id not in profile_target_ids for target_id in target_ids_value)
        ):
            raise ValueError(f"derived 보조맵 {key} target_ids가 잘못됐어요")
        matches = outputs_by_plan.get(key, [])
        if plan.get("policy") == "preserve":
            if matches:
                raise ValueError(f"preserve 보조맵 {key}에 파생 산출물이 있으면 안 돼요")
            continue
        if len(matches) != 1:
            raise ValueError(f"derived 보조맵 {key} 산출물은 정확히 하나여야 해요")
        if set(target_ids_value) & expected_targets:
            selected_outputs.append(matches[0])
    for output in selected_outputs:
        target = profile.target_by_id(output["target_id"])
        bind_material_target(target.id)
        plan_key = (
            output.get("bundle_key"),
            int(output.get("path_id", 0)),
            output.get("role"),
        )
        plan = auxiliary_plans[plan_key]
        for consumer_target_id in plan["target_ids"]:
            bind_material_target(str(consumer_target_id))
        if output.get("policy") == "neutralize_and_derive":
            output_derivation = output.get("derivation")
            output_masters = (
                output_derivation.get("master_lettering")
                if isinstance(output_derivation, dict)
                else None
            )

            expected_master_sets = {
                json.dumps(
                    _canonical_master_lettering(
                        material_master_lettering[str(consumer_target_id)]
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for consumer_target_id in plan["target_ids"]
            }
            if (
                len(expected_master_sets) != 1
                or _canonical_master_lettering(output_masters)
                != json.loads(next(iter(expected_master_sets)))
            ):
                raise ValueError(
                    f"{target.id}::{output.get('texture')} master lettering이 현재 review와 달라요"
                )
        matching_contracts = [
            (str(consumer_target_id), key, entry)
            for consumer_target_id in plan["target_ids"]
            for key, entry in material_contract_maps[str(consumer_target_id)].items()
            if isinstance(entry, dict)
            and isinstance(entry.get("identity"), dict)
            and entry["identity"].get("texture_bundle_key") == output.get("bundle_key")
            and entry["identity"].get("path_id") == output.get("path_id")
            and entry["identity"].get("texture") == output.get("texture")
            and entry["identity"].get("role") == output.get("role")
        ]
        channel_sets = {
            tuple(entry.get("channel_contract", {}).get("used_channels", []))
            for _, _, entry in matching_contracts
        }
        matched_target_ids = {value[0] for value in matching_contracts}
        if (
            len(matching_contracts) < 1
            or len(channel_sets) != 1
            or matched_target_ids != {str(value) for value in plan["target_ids"]}
        ):
            raise ValueError(f"{target.id}::{output.get('texture')} 재질 채널 계약이 모호해요")
        source_map = Path(output["source_png"])
        current_mask_descriptors: list[dict[str, Any]] = []
        for consumer_target_id, contract_key, entry in matching_contracts:
            if entry.get("policy") != output.get("policy"):
                raise ValueError(f"{target.id}::{output.get('texture')} policy 계약이 달라요")
            material_masks = material_stage_data[consumer_target_id].get("material_masks")
            mask_descriptor = (
                material_masks.get(contract_key)
                if isinstance(material_masks, dict)
                else None
            )
            if not isinstance(mask_descriptor, dict):
                raise ValueError(
                    f"{consumer_target_id}::{contract_key} 현재 old-effect mask 계약이 없어요"
                )
            if (
                entry.get("source_effect_mask_sha256")
                != mask_descriptor.get("sha256")
                or output.get("old_text_mask_sha256")
                != mask_descriptor.get("sha256")
                or output.get("old_text_mask") != mask_descriptor.get("path")
                or plan.get("old_text_mask_sha256") != mask_descriptor.get("sha256")
            ):
                raise ValueError(
                    f"{consumer_target_id}::{contract_key} old-effect mask가 현재 material 계약과 달라요"
                )
            _verified_project_mask(
                paths,
                mask_descriptor,
                (int(entry["identity"]["width"]), int(entry["identity"]["height"])),
                f"{consumer_target_id}::{contract_key} old-effect mask",
            )
            current_mask_descriptors.append(mask_descriptor)
            source_descriptor = entry.get("source_map")
            if (
                not isinstance(source_descriptor, dict)
                or source_descriptor.get("sha256") != output.get("source_sha256")
            ):
                raise ValueError(f"{target.id}::{output.get('texture')} source-map 계약이 달라요")
            relative = Path(str(source_descriptor.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"{target.id}::{output.get('texture')} source-map 경로가 잘못됐어요")
            if (paths.root / relative).resolve() != source_map.resolve():
                raise ValueError(f"{target.id}::{output.get('texture')} source-map 경로가 달라요")
            if output.get("policy") == "neutralize_and_derive":
                contract_derivation = entry.get("derivation")
                output_derivation = output.get("derivation")
                if not isinstance(contract_derivation, dict) or not isinstance(
                    output_derivation, dict
                ):
                    raise ValueError(
                        f"{target.id}::{output.get('texture')} 파생 계약/증거가 없어요"
                    )
                comparisons = {
                    "producer": contract_derivation.get("producer"),
                    "projection": contract_derivation.get("projection"),
                    "physical_component": contract_derivation.get("physical_component"),
                    "effect_parameters": contract_derivation.get("effect_parameters"),
                    "effect_measurement": contract_derivation.get("effect_measurement"),
                    "alignment_limits": contract_derivation.get("alignment_limits"),
                }
                if any(
                    output_derivation.get(field) != value
                    for field, value in comparisons.items()
                ):
                    raise ValueError(
                        f"{target.id}::{output.get('texture')} 파생 증거가 material 계약과 달라요"
                    )
        canonical_masks = {
            json.dumps(descriptor, ensure_ascii=False, sort_keys=True)
            for descriptor in current_mask_descriptors
        }
        if len(canonical_masks) != 1:
            raise ValueError(
                f"{target.id}::{output.get('texture')} 공유 소비자의 old-effect mask가 달라요"
            )

        expected_channels = list(next(iter(channel_sets)))
        map_size = (
            int(matching_contracts[0][2]["identity"]["width"]),
            int(matching_contracts[0][2]["identity"]["height"]),
        )
        current_mask_descriptor = current_mask_descriptors[0]
        if current_mask_descriptor.get("method") not in {"inpaint", "patch"}:
            raise ValueError(
                f"{target.id}::{output.get('texture')} old-effect mask method가 잘못됐어요"
            )
        current_old_mask = _verified_project_mask(
            paths,
            current_mask_descriptor,
            map_size,
            f"{target.id}::{output.get('texture')} old-effect mask",
        )
        patch_values = None
        if current_mask_descriptor.get("method") == "patch":
            patch_values = _verified_project_rgba(
                paths,
                current_mask_descriptor.get("patch"),
                current_mask_descriptor.get("patch_sha256"),
                map_size,
                f"{target.id}::{output.get('texture')} restoration patch",
            )
            if (
                output.get("restoration_patch") != current_mask_descriptor.get("patch")
                or output.get("restoration_patch_sha256")
                != current_mask_descriptor.get("patch_sha256")
            ):
                raise ValueError(
                    f"{target.id}::{output.get('texture')} restoration patch가 현재 계약과 달라요"
                )
        elif output.get("restoration_patch") is not None or output.get(
            "restoration_patch_sha256"
        ) is not None:
            raise ValueError(
                f"{target.id}::{output.get('texture')} inpaint 계약에 patch가 기록됐어요"
            )
        neutral_image, _ = _neutralize_map(
            source_map,
            current_old_mask,
            str(output.get("role")),
            patch=patch_values,
            channels=tuple("RGBA".index(channel) for channel in expected_channels),
        )
        expected_neutral = np.asarray(neutral_image, dtype=np.uint8)

        projection_sizes: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
        for consumer_target_id, contract_key, entry in matching_contracts:
            sizes = _review_projection_contract(
                inventory,
                target_id=consumer_target_id,
                material_data=material_stage_data[consumer_target_id],
                contract_key=contract_key,
                contract_entry=entry,
            )
            if sizes[1] != map_size:
                raise ValueError(
                    f"{consumer_target_id}::{contract_key} 보조맵 크기가 현재 output과 달라요"
                )
            previous = projection_sizes.get(consumer_target_id)
            if previous is not None and previous != sizes:
                raise ValueError(
                    f"{consumer_target_id}::{contract_key} projection 계약이 다른 binding과 달라요"
                )
            projection_sizes[consumer_target_id] = sizes
        source_sizes = {value[0] for value in projection_sizes.values()}
        if len(source_sizes) != 1:
            raise ValueError(
                f"{target.id}::{output.get('texture')} 공유 소비자의 Diffuse 크기가 달라요"
            )
        source_size = next(iter(source_sizes))

        protected_sets: list[tuple[np.ndarray, np.ndarray]] = []
        for consumer_target_id in plan["target_ids"]:
            edit_data = material_edit_plans[str(consumer_target_id)]
            masks = edit_data.get("masks")
            if not isinstance(masks, dict):
                raise ValueError(f"{consumer_target_id} 현재 edit-plan mask가 없어요")
            protected_source = _verified_project_mask(
                paths,
                masks.get("protected"),
                source_size,
                f"{consumer_target_id} protected mask",
            )
            seam_source = _verified_project_mask(
                paths,
                masks.get("seam_guard"),
                source_size,
                f"{consumer_target_id} seam guard",
            )
            protected_sets.append(
                (
                    project_binary_mask(protected_source, map_size),
                    project_binary_mask(seam_source, map_size),
                )
            )
        expected_projected_protected, expected_projected_seam = protected_sets[0]
        if any(
            not all(
                np.array_equal(left, right)
                for left, right in zip(protected_sets[0], value, strict=True)
            )
            for value in protected_sets[1:]
        ):
            raise ValueError(
                f"{target.id}::{output.get('texture')} 공유 소비자의 protected/seam이 달라요"
            )
        if np.any(
            current_old_mask
            & (expected_projected_protected | expected_projected_seam)
        ):
            raise ValueError(
                f"{target.id}::{output.get('texture')} old-effect mask가 protected/seam을 침범해요"
            )

        expected_projected_alpha = None
        if output.get("policy") == "neutralize_and_derive":
            output_derivation = output["derivation"]
            projection = output_derivation.get("projection")
            if not isinstance(projection, dict):
                raise ValueError(f"{target.id}::{output.get('texture')} projection 증거가 없어요")
            if projection.get("source_size") != list(source_size) or projection.get(
                "target_size"
            ) != list(map_size):
                raise ValueError(f"{target.id}::{output.get('texture')} projection 크기가 잘못됐어요")
            projected_masters: list[np.ndarray] = []
            for consumer_target_id in plan["target_ids"]:
                edit_data = material_edit_plans[str(consumer_target_id)]
                master_alpha, _, master_records = _master_lettering_alpha(
                    paths, edit_data, source_size
                )
                if _canonical_master_lettering(
                    master_records
                ) != _canonical_master_lettering(
                    material_master_lettering[str(consumer_target_id)]
                ):
                    raise ValueError(
                        f"{consumer_target_id} 현재 selected lettering이 material 계약과 달라요"
                    )
                projected_masters.append(project_master_alpha(master_alpha, map_size))
            expected_projected_alpha = projected_masters[0]
            if any(
                not np.array_equal(expected_projected_alpha, value)
                for value in projected_masters[1:]
            ):
                raise ValueError(
                    f"{target.id}::{output.get('texture')} 공유 소비자의 master alpha가 달라요"
                )
        _validate_derived_material_output(
            output,
            expected_channels=expected_channels,
            derived_root=paths.derived,
        )
        _validate_derived_material_pixels(
            output,
            expected_channels=expected_channels,
            paths=paths,
            expected_projected_alpha=expected_projected_alpha,
            expected_projected_protected=expected_projected_protected,
            expected_projected_seam_guard=expected_projected_seam,
            expected_neutral=expected_neutral,
        )
        old_effect_mask = (paths.root / str(current_mask_descriptor["path"])).resolve()
        new_effect_mask: Any = np.zeros((map_size[1], map_size[0]), dtype=bool)
        if output.get("policy") == "neutralize_and_derive":
            new_effect_mask = Path(output["derivation"]["effect_mask"]["path"]).resolve()
        protected_constraint = expected_projected_protected
        seam_constraint = expected_projected_seam
        if protected_constraint is None or seam_constraint is None:
            raise AssertionError("보조맵 protected/seam mip 계약을 만들지 못했어요")
        texture_constraints = edit_constraints.setdefault(output["bundle_key"], {})
        if output["texture"] in texture_constraints:
            raise ValueError(f"{output['texture']} 보조맵 mip 계약이 중복됐어요")
        texture_constraints[output["texture"]] = {
            "source_image": source_map,
            "neutral_image": expected_neutral,
            "selected_channels": expected_channels,
            "old_effect_mask": old_effect_mask,
            "new_effect_mask": new_effect_mask,
            "protected_mask": protected_constraint,
            "seam_guard_mask": seam_constraint,
        }
        approved = paths.approved / f"{target.id}.png"
        source_record = record_for_target(inventory, target)
        verify_approval(paths, target, Path(source_record["source_png"]), approved)
        source = Path(output["derived_png"])
        checks = {
            "approved_sha256": sha256_file(approved),
            "approval_sha256": sha256_file(approval_path(paths, target.id)),
            "material_review_sha256": material_review_hashes[target.id],
            "source_sha256": sha256_file(source_map),
            "derived_sha256": sha256_file(source),
        }
        for field, current in checks.items():
            if output.get(field) != current:
                raise ValueError(
                    f"{target.id}::{output['texture']} derived {field}가 현재 입력과 달라요"
                )
        groups.setdefault(output["bundle_key"], {})[output["texture"]] = source
        roles.setdefault(output["bundle_key"], {})[output["texture"]] = output["role"]
        plan = auxiliary_plans.get(
            (output["bundle_key"], int(output["path_id"]), output["role"])
        )
        if not isinstance(plan, dict) or not plan.get("target_ids"):
            raise ValueError(
                f"{output['texture']} 보조맵의 실제 target/UV coverage 계획이 없어요"
            )
        destination_coverages = coverage_masks.setdefault(output["bundle_key"], {}).setdefault(
            output["texture"], []
        )
        for target_id in plan["target_ids"]:
            destination_coverages.append(
                _verified_uv_coverage(paths, inventory, str(target_id))
            )

    reports = []
    for bundle_key, replacements in groups.items():
        source = _verified_source_bundle(
            bundle_key,
            bundle_root,
            overrides.get(bundle_key),
            inventory["records"],
        )
        output = paths.bundles / Path(bundle_key)
        roundtrip = paths.reports / "roundtrip" / safe_bundle_name(bundle_key)
        report = patch_bundle_exact(
            source,
            output,
            replacements,
            roundtrip_dir=roundtrip,
            max_mae=max_mae,
            roles=roles[bundle_key],
            coverage_masks=coverage_masks[bundle_key],
            edit_constraints=edit_constraints.get(bundle_key, {}),
        )
        report["bundle_key"] = bundle_key
        report_path = paths.reports / "bundles" / f"{safe_bundle_name(bundle_key)}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        reports.append(report)

    pruned_stale_outputs = _prune_stale_preserved_auxiliary_outputs(
        paths,
        inventory,
        auxiliary_plans,
        expected_targets,
        set(groups),
    )

    payload = {
        "schema_version": 1,
        "collection": profile.id,
        "derived_manifest": str(paths.derived_manifest),
        "derived_manifest_sha256": sha256_file(paths.derived_manifest),
        "partial": target_ids is not None,
        "target_ids": sorted(target_ids) if target_ids is not None else None,
        "bundle_count": len(reports),
        "texture_count": sum(len(report["textures"]) for report in reports),
        "passed": all(report["passed"] for report in reports),
        "bundles": reports,
        "pruned_stale_outputs": pruned_stale_outputs,
    }
    suffix = "" if target_ids is None else "." + "+".join(sorted(target_ids))
    summary = paths.reports / f"repack{suffix}.json"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload
