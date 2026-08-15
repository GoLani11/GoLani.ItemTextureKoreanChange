from pathlib import Path

import numpy as np
from PIL import Image

from golani_texture_localizer.paths import ProjectPaths
from golani_texture_localizer.uv import _boundary_edges, _draw_repeat_triangle, generate_uv_review


def test_boundary_edges_remove_shared_triangle_edge() -> None:
    triangles = np.asarray([[0, 1, 2], [2, 1, 3]], dtype=np.int32)

    edges = _boundary_edges(triangles)

    assert {tuple(sorted(value)) for value in edges.tolist()} == {
        (0, 1),
        (0, 2),
        (1, 3),
        (2, 3),
    }


def test_generate_uv_review_uses_only_target_submesh(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    Image.new("RGBA", (32, 32), (30, 40, 50, 255)).save(source)
    mesh = root / "mesh.npz"
    np.savez_compressed(
        mesh,
        uv0=np.asarray([[0.2, 0.2], [0.8, 0.2], [0.2, 0.8]], dtype=np.float32),
        triangles_0=np.asarray([[0, 1, 2]], dtype=np.int32),
    )
    inventory = {
        "records": [
            {
                "target_id": "sample",
                "source_png": str(source),
                "wrap_u": 0,
                "wrap_v": 0,
            }
        ],
        "renderers": [
            {
                "target_ids": ["sample"],
                "target_submeshes": {"sample": [0]},
                "mesh_file": str(mesh),
            }
        ],
    }

    report = generate_uv_review(paths, inventory, "sample", padding=2)

    assert report["passed"] is True
    assert report["checked_submeshes"] == 1
    assert report["boundary_edge_count"] == 3
    with Image.open(report["mask"]) as mask:
        assert set(np.unique(np.asarray(mask)).tolist()).issubset({0, 255})
        assert mask.getbbox() is not None
    with Image.open(report["coverage"]) as coverage:
        assert coverage.getbbox() is not None


def test_repeat_triangle_rasterizes_uv_coverage() -> None:
    image = Image.new("L", (32, 32), 0)
    from PIL import ImageDraw

    _draw_repeat_triangle(
        ImageDraw.Draw(image),
        np.asarray([[0.2, 0.2], [0.8, 0.2], [0.2, 0.8]], dtype=np.float32),
        image.size,
    )

    assert np.count_nonzero(np.asarray(image)) > 0
