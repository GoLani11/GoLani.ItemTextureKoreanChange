"""Opt-in real asset checks. SPT is read-only; all outputs stay in workspace/checks."""
from pathlib import Path
import shutil
import uuid

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.files import read_json, sha256, write_json
from golani_texture_localizer.inventory import _resolve_portable_path
from golani_texture_localizer.jobs import prepare
from golani_texture_localizer.packaging import package
from golani_texture_localizer.paths import game_bundle_root
from golani_texture_localizer.profile import load_profile
from golani_texture_localizer.validation import validate


@pytest.mark.game_assets
@pytest.mark.parametrize("target,bundle_count", [("mayo", 1), ("sausage", 2)])
def test_original_roundtrip_and_masked_edit(request, target, bundle_count):
    game = request.config.getoption("--spt-root")
    if not game:
        pytest.skip("Use --spt-root to verify local game assets")
    project = Path(__file__).resolve().parents[2]
    root = project / "workspace" / "checks" / f"{target}-{uuid.uuid4().hex[:8]}"
    bundle_root = game_bundle_root(_resolve_portable_path(game))
    prepare(load_profile(project / "profiles/food/collection.json"), target, bundle_root, root)
    job = root / "job.json"
    snapshot = read_json(root / "source.json")
    assert len(snapshot["bundles"]) == bundle_count
    source_hashes = {key: sha256(bundle_root / key) for key in snapshot["bundles"]}
    dotnet = request.config.getoption("--dotnet")
    before = package(job, project, dotnet=dotnet)
    assert all(b["byte_identical"] for b in before["bundles"])
    diffuse = next(e for e in snapshot["maps"] if e["role"] == "diffuse")
    source = np.array(Image.open(root / diffuse["candidate"]))
    seam = np.array(Image.open(root / snapshot["uv_seam"]["path"])) > 0
    coverage = np.array(Image.open(root / snapshot["uv_coverage"]["path"])) > 0
    editable = np.zeros(source.shape[:2], dtype=np.uint8)
    position = next((y, x) for y in range(8, source.shape[0] - 8, 8)
                    for x in range(8, source.shape[1] - 8, 8)
                    if coverage[y:y+8, x:x+8].all() and not seam[y:y+8, x:x+8].any())
    y, x = position
    candidate = source.copy()
    # Synthetic +8 RGB patch tests the encoder; this is not a localization candidate.
    candidate[y:y+8, x:x+8, :3] = np.clip(source[y:y+8, x:x+8, :3].astype(np.int16) + 8, 0, 255)
    Image.fromarray(candidate).save(root / diffuse["candidate"])
    assert not validate(job)["passed"]
    editable[y:y+8, x:x+8] = 255
    Image.fromarray(editable).save(root / diffuse["editable"])
    assert validate(job)["passed"]
    after = package(job, project, dotnet=dotnet)
    assert after["changed_pixels"] > 0
    assert any(not b["byte_identical"] for b in after["bundles"])
    assert {key: sha256(bundle_root / key) for key in snapshot["bundles"]} == source_hashes
    assert not after["runtime_tested"]
    write_json(root / "integration-result.json", {
        "target": target, "passed": True, "bundle_count": bundle_count,
        "original_roundtrip_byte_identical": True, "outside_mask_rejected": True,
        "masked_edit_package": after["package"], "source_bundles_unchanged": True,
        "runtime_tested": False, "note": "Synthetic encoder test, not a Korean texture release."})
