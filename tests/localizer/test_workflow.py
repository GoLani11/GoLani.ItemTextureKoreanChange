from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.bindings import check_projection, target_maps
from golani_texture_localizer.cli import main
from golani_texture_localizer.editing import compose, derive
from golani_texture_localizer.files import descriptor, local_path, read_json, sha256, write_json
from golani_texture_localizer.jobs import dependency_keys
from golani_texture_localizer.packaging import MOD_NAME, SERVER_FILES, package, release, verify_package
from golani_texture_localizer.textures import decode_mips, edit_payload, encode_level, mip_lengths, replace_blocks
from golani_texture_localizer.validation import validate


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "workspace" / "items" / "sample"
    root.mkdir(parents=True)
    (root / "source.bundle").write_bytes(b"original bundle")
    write_json(root / "inventory.json", {})
    Image.new("L", (16, 16), 0).save(root / "seam.png")
    Image.new("L", (16, 16), 255).save(root / "coverage.png")
    entries = []
    for role, color in [("diffuse", (80, 100, 120, 180)), ("normal", (255, 128, 255, 128)), ("gloss", (60, 60, 60, 255))]:
        d = root / role
        d.mkdir()
        Image.new("RGBA", (16, 16), color).save(d / "source.png")
        Image.new("RGBA", (16, 16), color).save(d / "candidate.png")
        Image.new("L", (16, 16), 0).save(d / "editable.png")
        entries.append({"id": role, "texture": role, "role": role, "bundle_key": "item.bundle",
                        "width": 16, "height": 16, "format": 12 if role == "normal" else 10,
                        "source": descriptor(root, d / "source.png"), "candidate": f"{role}/candidate.png",
                        "editable": f"{role}/editable.png", "preserved_channels": [0, 2] if role == "normal" else [3],
                        "shared": False, "wrap_u": 0, "wrap_v": 0,
                        "bindings": [{"diffuse_scale": [1, 1], "diffuse_offset": [0, 0], "scale": [1, 1], "offset": [0, 0]}]})
    snapshot = {"schema_version": 1, "target_id": "sample", "maps": entries,
                "bundles": {"item.bundle": {**descriptor(root, root / "source.bundle"), "dependencies": ["shaders"]}},
                "inventory": descriptor(root, root / "inventory.json"),
                "uv_seam": descriptor(root, root / "seam.png"), "uv_coverage": descriptor(root, root / "coverage.png")}
    write_json(root / "source.json", snapshot)
    write_json(root / "job.json", {"schema_version": 1, "source": descriptor(root, root / "source.json")})
    return root / "job.json"


def edit(path, box=(5, 5, 7, 7), color=(130, 150, 170, 180)):
    with Image.open(path) as image:
        image.paste(color, box)
        image.save(path)


def test_unchanged_source_passes_without_claiming_visual_or_runtime_approval(job):
    report = validate(job)
    assert report["passed"] and report["changed_pixels"] == 0
    assert not report["visual_reviewed"] and not report["runtime_tested"]


def test_modified_pixels_require_explicit_mask_and_preserved_alpha(job):
    root = job.parent
    edit(root / "diffuse/candidate.png")
    report = validate(job)
    assert not report["passed"] and report["maps"][0]["outside_editable_changed_pixels"] == 4
    edit(root / "diffuse/editable.png", color=255)
    assert validate(job)["passed"]
    edit(root / "diffuse/candidate.png", color=(130, 150, 170, 0))
    assert not validate(job)["passed"]


def test_same_size_rgb_is_rejected_instead_of_fabricating_alpha(job):
    Image.new("RGB", (16, 16)).save(job.parent / "diffuse/candidate.png")
    with pytest.raises(ValueError, match="RGBA"):
        validate(job)


def test_stale_source_snapshot_is_rejected(job):
    edit(job.parent / "diffuse/source.png")
    with pytest.raises(ValueError, match="기준 파일"):
        validate(job)


def test_unsafe_job_paths_are_rejected(tmp_path):
    for value in ["../image.png", "C:/image.png", "/image.png", "images\\image.png"]:
        with pytest.raises(ValueError):
            local_path(tmp_path, value)


def test_validate_cli_returns_failure_exit_code(job):
    edit(job.parent / "diffuse/candidate.png")
    project = job.parents[3]
    assert main(["--project-root", str(project), "validate", str(job)]) == 1


def composition(job):
    root = job.parent
    Image.new("RGBA", (16, 16), (80, 100, 120, 0)).save(root / "background.png")
    Image.new("L", (16, 16), 0).save(root / "old.png")
    Image.new("RGBA", (16, 16), (255, 255, 255, 0)).save(root / "letters.png")
    edit(root / "letters.png", color=(140, 160, 180, 255))
    path = root / "compose.json"
    write_json(path, {"map_id": "diffuse", "old_text": "old.png", "background": "background.png", "lettering": "letters.png"})
    compose(job, path)
    return root


def test_compose_preserves_source_alpha_and_nonlettering_pixels(job):
    root = composition(job)
    report = validate(job)
    assert report["passed"] and report["changed_pixels"] == 4
    assert np.array(Image.open(root / "diffuse/candidate.png"))[5, 5].tolist() == [140, 160, 180, 180]


def test_derive_uses_composed_alpha_and_preserves_channels(job):
    root = composition(job)
    recipe = root / "derive.json"
    write_json(recipe, {"maps": [
        {"map_id": "normal", "neutral": "normal/source.png", "old_effect": "old.png", "height_scale_texels": .2, "polarity": 1, "bevel_passes": 0},
        {"map_id": "gloss", "neutral": "gloss/source.png", "old_effect": "old.png", "channel_deltas": {"R": 10, "G": 10, "B": 10}},
    ]})
    derive(job, recipe)
    assert validate(job)["passed"]
    source = np.array(Image.open(root / "gloss/source.png"))
    result = np.array(Image.open(root / "gloss/candidate.png"))
    assert np.count_nonzero(np.any(source != result, axis=2)) == 4
    edit(root / "diffuse/candidate.png", color=(1, 2, 3, 180))
    with pytest.raises(ValueError, match="기준 파일"):
        derive(job, recipe)


def test_projection_rejects_shared_map_or_different_uv(job):
    entry = read_json(job.parent / "source.json")["maps"][1]
    entry["shared"] = True
    with pytest.raises(ValueError, match="공유"):
        check_projection(entry)
    entry["shared"] = False
    entry["bindings"][0]["offset"] = [.5, 0]
    with pytest.raises(ValueError, match="scale/offset"):
        check_projection(entry)


def material_inventory():
    main = {"property": "_MainTex", "path_id": 1, "texture_bundle_key": "color.bundle", "external_assets_file": None, "scale": [1, 1], "offset": [0, 0]}
    normal = {**main, "property": "_BumpMap", "path_id": 2, "texture_bundle_key": "maps.bundle", "external_assets_file": "MAPS"}
    records = [{"bundle_key": "color.bundle", "path_id": 1, "assets_file": "COLOR", "target_id": "sample", "texture": "unknown-A"},
               {"bundle_key": "maps.bundle", "path_id": 2, "assets_file": "MAPS", "texture": "unknown-B"}]
    return {"records": records, "materials": [{"texture_slots": [main, normal], "bundle_key": "color.bundle", "assets_file": "COLOR", "path_id": 9, "material": "sample"}]}


def test_separate_bundle_roles_are_resolved_from_material_pointers():
    data = material_inventory()
    result = target_maps(data, "sample")
    assert [(e["role"], e["record"]["texture"]) for e in result] == [("diffuse", "unknown-A"), ("normal", "unknown-B")]
    other = deepcopy(data["materials"][0])
    other["texture_slots"][0]["path_id"] = 4
    data["materials"].append(other)
    assert target_maps(data, "sample")[1]["shared"]


def test_ambiguous_assets_file_is_rejected():
    data = material_inventory()
    data["records"][1]["assets_file"] = "OTHER"
    with pytest.raises(ValueError, match="serialized"):
        target_maps(data, "sample")


def test_manifest_uses_catalog_dependencies_and_rejects_missing_entry():
    assert dependency_keys({"a.bundle": {"Dependencies": ["shaders", "cubemaps", "shaders"]}}, "a.bundle") == ["cubemaps", "shaders"]
    with pytest.raises(ValueError, match="Windows.json"):
        dependency_keys({}, "missing.bundle")


def test_bc_patch_flips_rows_preserves_alpha_and_other_blocks():
    source = bytes(range(64))
    mask = np.zeros((8, 8), bool)
    mask[0, 0] = True
    result, spatial, count = replace_blocks(source, bytes([255] * 64), mask, 12, True)
    assert count == 1 and spatial[:4, :4].all() and not spatial[4:, :].any()
    assert result[:40] == source[:40] and result[40:48] == bytes([255] * 8) and result[48:] == source[48:]


def test_small_and_rectangular_mip_payload_lengths():
    assert mip_lengths(8, 4, 4, 10) == [16, 8, 8, 8]
    with pytest.raises(ValueError):
        mip_lengths(4, 4, 1, 999)


@pytest.mark.parametrize("fmt,role,color", [(10, "diffuse", (80, 100, 120, 255)), (12, "diffuse", (80, 100, 120, 80)), (12, "normal", (255, 128, 255, 128))])
def test_unchanged_texture_keeps_every_original_compressed_mip(tmp_path, fmt, role, color):
    from UnityPy.enums import BuildTarget, TextureFormat
    obj = SimpleNamespace(platform=BuildTarget.StandaloneWindows64, version=(2019, 4, 39, 1))
    tex = SimpleNamespace(m_Width=8, m_Height=8, m_MipCount=4, m_TextureFormat=TextureFormat(fmt), m_PlatformBlob=[])
    original = b"".join(encode_level(Image.new("RGBA", (n, n), color), obj, tex) for n in [8, 4, 2, 1])
    tex.get_image_data = lambda: original
    candidate = np.array(decode_mips(obj, tex, original)[0])
    entry = {"role": role, "format": fmt, "shared": False, "preserved_channels": [0, 2] if role == "normal" else [3], "texture": "sample"}
    output, reports = edit_payload(obj, tex, candidate, entry, None, tmp_path / "mips")
    assert output == original and all(r["blocks_reencoded"] == 0 for r in reports)


@pytest.fixture
def test_package(job, tmp_path, monkeypatch):
    import golani_texture_localizer.packaging as module
    composition(job)
    server = tmp_path / "server"
    server.mkdir()
    for name in SERVER_FILES:
        (server / name).write_bytes(name.encode())
    monkeypatch.setattr(module, "build_server", lambda *a: server)
    def patch(root, source, entries, output, coverage, roundtrip):
        output.parent.mkdir(parents=True)
        output.write_bytes(b"tested fixture bundle")
        return {"passed": True}
    monkeypatch.setattr(module, "patch_bundle", patch)
    result = package(job, tmp_path)
    return Path(result["package"])


def test_package_is_item_scoped_and_never_claims_runtime_approval(test_package):
    report = verify_package(test_package)
    assert report["file_validation_passed"] and not report["runtime_tested"]
    manifest = read_json(test_package / MOD_NAME / "bundles.json")
    assert manifest == {"manifest": [{"key": "item.bundle", "dependencyKeys": ["shaders"]}]}


def test_package_tampering_and_extra_files_are_rejected(test_package):
    extra = test_package / "extra.dll"
    extra.write_bytes(b"extra")
    with pytest.raises(ValueError, match="기록되지 않은"):
        verify_package(test_package)
    extra.unlink()
    (test_package / MOD_NAME / SERVER_FILES[0]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="변경"):
        verify_package(test_package)


def test_release_requires_review_for_this_exact_package(test_package, tmp_path):
    review = tmp_path / "review.json"
    write_json(review, {"runtime_tested": False})
    with pytest.raises(ValueError, match="시각·게임"):
        release(test_package, review, tmp_path / "release")
    evidence = tmp_path / "game-check.txt"
    evidence.write_text("Synthetic evidence for a unit test; not a real game approval.")
    write_json(review, {"package_sha256": sha256(test_package / "package.json"), "runtime_tested": True,
                        "visual_reviewed": True, "reviewer": "test fixture", "notes": "unit test",
                        "evidence": [descriptor(tmp_path, evidence)]})
    report = release(test_package, review, tmp_path / "release")
    assert report["kind"] == "release"
    assert not (tmp_path / "release" / MOD_NAME / "TEST-ONLY.txt").exists()
    assert (test_package / MOD_NAME / "TEST-ONLY.txt").exists()
