from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .paths import ProjectPaths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _boundary_edges(
    triangles: np.ndarray,
    *,
    vertices: np.ndarray | None = None,
    uv0: np.ndarray | None = None,
    tolerance: float = 1e-5,
) -> np.ndarray:
    if triangles.ndim != 2 or triangles.shape[1] != 3:
        raise ValueError("triangle 배열은 Nx3이어야 해요")
    directed = np.concatenate(
        (triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]),
        axis=0,
    )
    canonical = directed
    if vertices is not None or uv0 is not None:
        if (
            vertices is None
            or uv0 is None
            or vertices.ndim != 2
            or vertices.shape[1] != 3
            or uv0.ndim != 2
            or uv0.shape[1] != 2
            or len(vertices) != len(uv0)
            or tolerance <= 0
        ):
            raise ValueError("중복 정점 병합용 vertices/uv0 규격이 잘못됐어요")
        # Unity mesh는 normal/tangent 경계 때문에 같은 위치·UV 정점을 여러 index로
        # 저장할 수 있어요. index만 비교하면 그 사이의 내부 삼각형 모서리까지 UV
        # seam으로 오인하므로, 3D 위치와 UV가 모두 같은 정점을 먼저 합쳐요.
        signature = np.concatenate((vertices, uv0), axis=1).astype(np.float64)
        quantized = np.rint(signature / tolerance).astype(np.int64)
        _, canonical_vertex = np.unique(quantized, axis=0, return_inverse=True)
        canonical = canonical_vertex[directed]
    normalized = np.sort(canonical, axis=1)
    _, inverse, counts = np.unique(normalized, axis=0, return_inverse=True, return_counts=True)
    return directed[counts[inverse] == 1]


def _repeat_shifts(first: float, second: float) -> range:
    lower = math.ceil(-max(first, second))
    upper = math.floor(1 - min(first, second))
    return range(lower, upper + 1)


def _draw_repeat_edge(
    draw: ImageDraw.ImageDraw,
    uv_a: np.ndarray,
    uv_b: np.ndarray,
    size: tuple[int, int],
) -> None:
    width, height = size
    for shift_u in _repeat_shifts(float(uv_a[0]), float(uv_b[0])):
        for shift_v in _repeat_shifts(float(uv_a[1]), float(uv_b[1])):
            a = uv_a + (shift_u, shift_v)
            b = uv_b + (shift_u, shift_v)
            draw.line(
                (
                    float(a[0]) * (width - 1),
                    (1 - float(a[1])) * (height - 1),
                    float(b[0]) * (width - 1),
                    (1 - float(b[1])) * (height - 1),
                ),
                fill=255,
                width=1,
            )


def _draw_repeat_triangle(
    draw: ImageDraw.ImageDraw,
    triangle: np.ndarray,
    size: tuple[int, int],
) -> None:
    width, height = size
    minimum = triangle.min(axis=0)
    maximum = triangle.max(axis=0)
    for shift_u in range(math.ceil(-float(maximum[0])), math.floor(1 - float(minimum[0])) + 1):
        for shift_v in range(
            math.ceil(-float(maximum[1])), math.floor(1 - float(minimum[1])) + 1
        ):
            shifted = triangle + (shift_u, shift_v)
            draw.polygon(
                [
                    (
                        float(value[0]) * (width - 1),
                        (1 - float(value[1])) * (height - 1),
                    )
                    for value in shifted
                ],
                fill=255,
            )


def _record_for_target(inventory: dict[str, Any], target_id: str) -> dict[str, Any]:
    matches = [record for record in inventory["records"] if record.get("target_id") == target_id]
    if len(matches) != 1:
        raise ValueError(f"{target_id} 원본 record는 정확히 하나여야 해요")
    return matches[0]


def generate_uv_review(
    paths: ProjectPaths,
    inventory: dict[str, Any],
    target_id: str,
    *,
    padding: int = 4,
) -> dict[str, Any]:
    if padding < 1:
        raise ValueError("UV seam padding은 1 이상이어야 해요")
    source_record = _record_for_target(inventory, target_id)
    source_path = Path(source_record["source_png"])
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if int(source_record.get("wrap_u", 0)) != 0 or int(source_record.get("wrap_v", 0)) != 0:
        raise ValueError(f"{target_id}의 Repeat 이외 wrap mode는 아직 안전하게 처리할 수 없어요")

    with Image.open(source_path) as source_file:
        source = source_file.convert("RGBA")
        size = source.size
    raw = Image.new("L", size, 0)
    draw = ImageDraw.Draw(raw)
    coverage = Image.new("L", size, 0)
    coverage_draw = ImageDraw.Draw(coverage)
    renderer_records = [
        renderer
        for renderer in inventory.get("renderers", [])
        if target_id in renderer.get("target_ids", [])
    ]
    if not renderer_records:
        raise ValueError(f"{target_id}의 Renderer/UV 기록이 없어요")

    mesh_hashes: dict[str, str] = {}
    boundary_edge_count = 0
    checked_submeshes = 0
    for renderer in renderer_records:
        mesh_path = Path(renderer["mesh_file"])
        if not mesh_path.is_file():
            raise FileNotFoundError(mesh_path)
        mesh_hashes[str(mesh_path)] = _sha256(mesh_path)
        with np.load(mesh_path) as mesh:
            vertices = np.asarray(mesh["vertices"], dtype=np.float32)
            uv0 = np.asarray(mesh["uv0"], dtype=np.float32)
            for submesh_index in renderer.get("target_submeshes", {}).get(target_id, []):
                key = f"triangles_{submesh_index}"
                if key not in mesh:
                    raise ValueError(f"{mesh_path}에 {key}가 없어요")
                triangles = np.asarray(mesh[key], dtype=np.int32)
                edges = _boundary_edges(triangles, vertices=vertices, uv0=uv0)
                checked_submeshes += 1
                boundary_edge_count += len(edges)
                for first, second in edges:
                    _draw_repeat_edge(draw, uv0[int(first)], uv0[int(second)], size)
                for triangle_indices in triangles:
                    _draw_repeat_triangle(coverage_draw, uv0[triangle_indices], size)

    if boundary_edge_count == 0:
        raise ValueError(f"{target_id}에서 보호할 UV 경계를 찾지 못했어요")
    kernel = padding * 2 + 1
    seam_guard = raw.filter(ImageFilter.MaxFilter(kernel))
    values = np.asarray(seam_guard, dtype=np.uint8)
    if not np.any(values):
        raise ValueError(f"{target_id}의 seam guard가 비어 있어요")
    coverage_values = np.asarray(coverage, dtype=np.uint8)
    if not np.any(coverage_values):
        raise ValueError(f"{target_id}의 UV coverage가 비어 있어요")

    review_dir = paths.reviews / target_id
    review_dir.mkdir(parents=True, exist_ok=True)
    mask_path = review_dir / "uv-seam-guard.png"
    overlay_path = review_dir / "uv-layout.png"
    coverage_path = review_dir / "uv-coverage.png"
    report_path = review_dir / "uv-report.json"
    seam_guard.save(mask_path, format="PNG", optimize=False)
    coverage.save(coverage_path, format="PNG", optimize=False)

    overlay = source.copy()
    tint = Image.new("RGBA", size, (255, 0, 0, 0))
    tint.putalpha(seam_guard.point(lambda value: 144 if value else 0))
    overlay.alpha_composite(tint)
    overlay.save(overlay_path, format="PNG", optimize=False)

    report = {
        "schema_version": 1,
        "target_id": target_id,
        "source": str(source_path),
        "source_sha256": _sha256(source_path),
        "width": size[0],
        "height": size[1],
        "padding_pixels": padding,
        "renderer_count": len(renderer_records),
        "checked_submeshes": checked_submeshes,
        "boundary_edge_count": boundary_edge_count,
        "guard_pixels": int(np.count_nonzero(values)),
        "coverage_pixels": int(np.count_nonzero(coverage_values)),
        "mesh_sha256": mesh_hashes,
        "mask": str(mask_path),
        "mask_sha256": _sha256(mask_path),
        "coverage": str(coverage_path),
        "coverage_sha256": _sha256(coverage_path),
        "overlay": str(overlay_path),
        "overlay_sha256": _sha256(overlay_path),
        "passed": True,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report": str(report_path), "report_sha256": _sha256(report_path)}
