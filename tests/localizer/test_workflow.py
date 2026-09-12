from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.bindings import check_projection, target_maps
from golani_texture_localizer.cli import main
from golani_texture_localizer.drafts import adopt_draft
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


def replace_snapshot(job, snapshot):
    write_json(job.parent / "source.json", snapshot)
    value = read_json(job)
    value["source"] = descriptor(job.parent, job.parent / "source.json")
    write_json(job, value)


def selected_draft(job, color=(120, 90, 80), mode="RGB"):
    path = job.parent / "draft.png"
    Image.new(mode, (8, 8), color).save(path)
    return adopt_draft(job, path)


def generated_recipe(job, roles=("gloss",)):
    root = job.parent
    Image.new("RGBA", (16, 16), (255, 255, 255, 0)).save(root / "letters.png")
    edit(root / "letters.png", color=(255, 255, 255, 255))
    Image.new("L", (16, 16), 0).save(root / "old.png")
    recipe = {"lettering": "letters.png", "diffuse_sha256": sha256(root / "diffuse/candidate.png"),
              "maps": [{"map_id": role, "neutral": f"{role}/source.png", "old_effect": "old.png",
                        **({"channel_deltas": {"R": 10, "G": 10, "B": 10}} if role == "gloss" else
                           {"height_scale_texels": .2, "polarity": 1, "bevel_passes": 0})} for role in roles]}
    write_json(root / "derive.json", recipe)
    return root / "derive.json"


@pytest.mark.parametrize("mode,color", [("RGB", (120, 90, 80)), ("RGBA", (120, 90, 80, 0))])
def test_adopt_draft_resizes_rgb_restores_varying_alpha_and_preserves_inputs(job, mode, color):
    root = job.parent
    original = np.array(Image.open(root / "diffuse/source.png"))
    original[..., 3] = np.arange(256, dtype=np.uint8).reshape(16, 16)
    Image.fromarray(original).save(root / "diffuse/source.png")
    snapshot = read_json(root / "source.json")
    snapshot["maps"][0]["source"] = descriptor(root, root / "diffuse/source.png")
    replace_snapshot(job, snapshot)
    before = {p: sha256(p) for p in root.rglob("*.png") if p.name != "candidate.png" or p.parent.name != "diffuse"}
    report = selected_draft(job, color, mode)
    assert all(sha256(p) == digest for p, digest in before.items())
    candidate = np.array(Image.open(root / "diffuse/candidate.png"))
    assert candidate.shape == (16, 16, 4) and np.array_equal(candidate[..., 3], original[..., 3])
    assert np.all(candidate[..., :3] == color[:3])
    assert report["resampling"] == "Lanczos" and report["input_size"] == [8, 8]
    assert (root / report["input"]["path"]).read_bytes() == (root / "draft.png").read_bytes()
    assert not np.any(np.array(Image.open(root / "diffuse/editable.png")))
    assert not report["nontext_pixel_preservation_verified"]
    assert validate(job)["passed"]


def test_adopt_non_square_source_uses_original_dimensions(job):
    root = job.parent
    snapshot = read_json(root / "source.json")
    entry = snapshot["maps"][0]
    Image.new("RGBA", (16, 8), (20, 30, 40, 80)).save(root / entry["source"]["path"])
    Image.new("L", (16, 8), 0).save(root / entry["editable"])
    entry.update(width=16, height=8, source=descriptor(root, root / entry["source"]["path"]))
    snapshot["maps"] = [entry]  # A real target need not have both auxiliary maps.
    for name in ("uv_seam", "uv_coverage"):
        path = root / snapshot[name]["path"]
        Image.new("L", (16, 8), 0 if name == "uv_seam" else 255).save(path)
        snapshot[name] = descriptor(root, path)
    replace_snapshot(job, snapshot)
    Image.new("RGB", (8, 4), (100, 90, 80)).save(root / "draft.png")
    assert adopt_draft(job, root / "draft.png")["output_size"] == [16, 8]
    assert validate(job)["passed"]


@pytest.mark.parametrize("mode,size", [("RGB", (8, 4)), ("L", (8, 8))])
def test_bad_draft_fails_without_changing_job_or_candidates(job, mode, size):
    root = job.parent
    Image.new(mode, size).save(root / "draft.png")
    before = {p: sha256(p) for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        adopt_draft(job, root / "draft.png")
    assert {p: sha256(p) for p in root.rglob("*") if p.is_file()} == before


def test_adopt_rejects_input_candidate_alias(job):
    path = job.parent / "diffuse/candidate.png"
    before = sha256(path)
    with pytest.raises(ValueError, match="겹쳐"):
        adopt_draft(job, path)
    assert sha256(path) == before


def test_adopt_rejects_source_candidate_alias(job):
    snapshot = read_json(job.parent / "source.json")
    snapshot["maps"][0]["candidate"] = snapshot["maps"][0]["source"]["path"]
    replace_snapshot(job, snapshot)
    before = sha256(job.parent / "diffuse/source.png")
    with pytest.raises(ValueError, match="겹쳐"):
        selected_draft(job)
    assert sha256(job.parent / "diffuse/source.png") == before


def test_adopt_rejects_shared_diffuse_without_mutation(job):
    snapshot = read_json(job.parent / "source.json")
    snapshot["maps"][0]["shared"] = True
    replace_snapshot(job, snapshot)
    before = sha256(job.parent / "diffuse/candidate.png")
    with pytest.raises(ValueError, match="공유 D"):
        selected_draft(job)
    assert sha256(job.parent / "diffuse/candidate.png") == before


@pytest.mark.parametrize("mode", ["unknown", None, [], "generated-full"])
def test_unknown_or_unrecorded_diffuse_policy_is_rejected(job, mode):
    value = read_json(job)
    value["diffuse_mode"] = mode
    write_json(job, value)
    with pytest.raises(ValueError):
        validate(job)


def test_full_d_diagnostics_report_seam_changes_without_faking_a_mask(job):
    root = job.parent
    Image.new("L", (16, 16), 255).save(root / "seam.png")
    snapshot = read_json(root / "source.json")
    snapshot["uv_seam"] = descriptor(root, root / "seam.png")
    replace_snapshot(job, snapshot)
    selected_draft(job)
    report = validate(job)
    diffuse = report["maps"][0]
    assert report["passed"] and report["diffuse_preservation"] == "unverified-nontext"
    assert diffuse["rgb_changed_pixels"] == diffuse["seam_changed_pixels"] == 256
    assert diffuse["outside_editable_changed_pixels"] == 256 and diffuse["editable_pixels"] == 0
    assert not diffuse["nontext_pixel_preservation_verified"] and not report["runtime_tested"]


def test_adoption_record_and_selected_candidate_are_hash_pinned(job):
    selected_draft(job)
    edit(job.parent / "diffuse/candidate.png", color=(1, 2, 3, 0))
    with pytest.raises(ValueError, match="기준 파일"):
        validate(job)


def test_full_d_still_rejects_modified_alpha_with_updated_candidate_hash(job):
    root = job.parent
    selected_draft(job)
    edit(root / "diffuse/candidate.png", color=(120, 90, 80, 0))
    value = read_json(job)
    path = root / value["generated_diffuse"]["path"]
    adoption = read_json(path)
    adoption["candidate"] = descriptor(root, root / "diffuse/candidate.png")
    write_json(path, adoption)
    value["generated_diffuse"] = descriptor(root, path)
    write_json(job, value)
    assert not validate(job)["passed"]


def test_generated_does_not_allow_untracked_ng_or_masked_composition(job):
    selected_draft(job)
    root = job.parent
    edit(root / "gloss/candidate.png", color=(70, 70, 70, 255))
    edit(root / "gloss/editable.png", color=255)
    with pytest.raises(ValueError, match="derive 기록"):
        validate(job)
    with pytest.raises(ValueError, match="compose"):
        composition(job)


def test_generated_derive_pins_d_and_lettering_and_keeps_separate_map_records(job):
    selected_draft(job)
    derive(job, generated_recipe(job, ("gloss",)))
    assert validate(job)["passed"]
    derive(job, generated_recipe(job, ("normal",)))
    assert validate(job)["passed"]
    assert len(read_json(job.parent / "reports/derivation.json")["maps"]) == 2
    edit(job.parent / "letters.png", color=(1, 1, 1, 0))
    with pytest.raises(ValueError, match="기준 파일"):
        validate(job)


def test_reselecting_d_preserves_ng_but_invalidates_old_derivation(job):
    selected_draft(job)
    recipe = generated_recipe(job)
    derive(job, recipe)
    gloss = job.parent / "gloss/candidate.png"
    before = sha256(gloss)
    selected_draft(job, color=(20, 30, 40))
    assert sha256(gloss) == before
    with pytest.raises(ValueError, match="연결되지"):
        validate(job)
    with pytest.raises(ValueError, match="D 해시"):
        derive(job, recipe)


def test_broken_previous_derivation_fails_before_overwriting_ng(job):
    selected_draft(job)
    recipe = generated_recipe(job)
    path = job.parent / "reports/derivation.json"
    write_json(path, {"maps": [None]})
    before = sha256(job.parent / "gloss/candidate.png")
    with pytest.raises(ValueError, match="기존 N/G"):
        derive(job, recipe)
    assert sha256(job.parent / "gloss/candidate.png") == before


@pytest.mark.parametrize("restriction", ["shared", "offset", "alpha", "outside-mask"])
def test_generated_mode_keeps_auxiliary_restrictions(job, restriction):
    snapshot = read_json(job.parent / "source.json")
    gloss = snapshot["maps"][2]
    if restriction == "shared":
        gloss["shared"] = True
    elif restriction == "offset":
        gloss["bindings"][0]["offset"] = [.5, 0]
    replace_snapshot(job, snapshot)
    selected_draft(job)
    recipe_path = generated_recipe(job)
    if restriction in {"alpha", "outside-mask"}:
        neutral = job.parent / "neutral.png"
        Image.new("RGBA", (16, 16), (60, 60, 60, 255)).save(neutral)
        edit(neutral, color=(60, 60, 60, 0) if restriction == "alpha" else (90, 90, 90, 255))
        recipe = read_json(recipe_path)
        recipe["maps"][0]["neutral"] = "neutral.png"
        write_json(recipe_path, recipe)
    with pytest.raises(ValueError):
        derive(job, recipe_path)


def test_generated_package_records_policy_without_changing_old_package(job, test_package):
    selected_draft(job)
    result = package(job, job.parents[3])
    output = Path(result["package"])
    assert output != test_package
    assert result["file_validation_passed"] and result["diffuse_mode"] == "generated-full"
    assert result["diffuse_preservation"] == "unverified-nontext" and not result["runtime_tested"]
    assert "generated-full" in (output / MOD_NAME / "TEST-ONLY.txt").read_text(encoding="utf-8")
    assert verify_package(test_package)["diffuse_mode"] == "masked"


def test_adopt_draft_cli(job):
    draft = job.parent / "draft.png"
    Image.new("RGB", (8, 8), (100, 90, 80)).save(draft)
    assert main(["--project-root", str(job.parents[3]), "adopt-draft", str(job), str(draft)]) == 0
    assert validate(job)["passed"]
