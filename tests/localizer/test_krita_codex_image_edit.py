from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
PYKRITA = ROOT / "tools/krita_codex_image_edit/pykrita"
if str(PYKRITA) not in sys.path:
    sys.path.insert(0, str(PYKRITA))

from golani_codex_image_edit.core import (  # noqa: E402
    CropRect,
    build_edit_prompt,
    context_crop,
    ensure_selected_pixels_are_opaque,
    find_spt_project_root,
    is_supported_srgb_profile,
    masked_bgra_layer,
    safe_stem,
    validate_projection_invariants,
)


def test_context_crop_is_square_and_clamped_when_canvas_allows_it() -> None:
    crop = context_crop(CropRect(5, 40, 100, 60), 500, 400, 50)

    assert crop.width == crop.height == 200
    assert crop.x == 0
    assert crop.y == 0
    assert crop.x <= 5 and crop.right >= 105
    assert crop.y <= 40 and crop.bottom >= 100


def test_context_crop_keeps_safe_rectangle_when_square_cannot_fit() -> None:
    crop = context_crop(CropRect(0, 40, 600, 100), 600, 300, 80)

    assert crop == CropRect(0, 0, 600, 220)


def test_context_crop_respects_nonzero_document_bounds() -> None:
    crop = context_crop(
        CropRect(-85, 220, 20, 30),
        300,
        400,
        40,
        canvas_x=-100,
        canvas_y=200,
    )

    assert crop == CropRect(-100, 200, 110, 110)


def test_masked_layer_has_no_generated_bytes_outside_selection() -> None:
    generated = bytes(
        [
            10,
            20,
            30,
            255,
            40,
            50,
            60,
            200,
            70,
            80,
            90,
            255,
        ]
    )
    result = masked_bgra_layer(generated, bytes([0, 128, 255]), 3, 1)

    assert result[:4] == b"\x00\x00\x00\x00"
    assert result[4:7] == bytes([40, 50, 60])
    assert result[7] == 100
    assert result[8:] == bytes([70, 80, 90, 255])


def test_masked_layer_rejects_fully_transparent_result() -> None:
    with pytest.raises(ValueError, match="완전히 투명"):
        masked_bgra_layer(bytes([10, 20, 30, 0]), bytes([255]), 1, 1)


def test_selected_translucent_source_pixel_is_blocked() -> None:
    source = bytes([1, 2, 3, 255, 4, 5, 6, 128])

    ensure_selected_pixels_are_opaque(source, bytes([255, 0]), 2, 1)
    with pytest.raises(ValueError, match="반투명"):
        ensure_selected_pixels_are_opaque(source, bytes([0, 255]), 2, 1)


def test_projection_invariants_detect_outside_or_alpha_change() -> None:
    before = bytes([1, 2, 3, 255, 4, 5, 6, 255])
    valid = bytes([1, 2, 3, 255, 40, 50, 60, 255])
    validate_projection_invariants(before, valid, bytes([0, 255]), 2, 1)

    outside_changed = bytes([9, 2, 3, 255, 40, 50, 60, 255])
    with pytest.raises(ValueError, match="선택 밖 변경 1px"):
        validate_projection_invariants(before, outside_changed, bytes([0, 255]), 2, 1)

    alpha_changed = bytes([1, 2, 3, 255, 40, 50, 60, 254])
    with pytest.raises(ValueError, match="알파 변경 1px"):
        validate_projection_invariants(before, alpha_changed, bytes([0, 255]), 2, 1)


def test_prompt_forces_built_in_imagegen_and_repeats_invariants() -> None:
    prompt = build_edit_prompt(
        "선택한 제품명만 ‘타르콜라’로 바꿔줘",
        512,
        512,
    )

    assert prompt.startswith("$imagegen\n")
    assert "선택한 제품명만 ‘타르콜라’로 바꿔줘" in prompt
    assert "/job/source.png" not in prompt and "/job/mask.png" not in prompt
    assert "referenced_image_paths" in prompt
    assert "Never pass referenced_image_paths" in prompt
    assert "num_last_images_to_include=2" in prompt
    assert "built-in image_gen tool exactly once" in prompt
    assert "Do not use an API/SDK fallback" in prompt
    assert "black pixels are protected" in prompt


def test_safe_stem_removes_path_syntax() -> None:
    assert safe_stem("  캔 라벨 / front:*?  ") == "캔-라벨-front"
    assert safe_stem("...") == "untitled"
    assert safe_stem("CON.png") == "_CON.png"


def test_only_nonlinear_srgb_profiles_are_supported() -> None:
    assert is_supported_srgb_profile("sRGB-elle-V2-srgbtrc.icc")
    assert is_supported_srgb_profile("sRGB built-in")
    assert not is_supported_srgb_profile("sRGB-elle-V2-g10.icc")
    assert not is_supported_srgb_profile("Display P3")


def test_spt_repository_workspace_or_document_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    profile = root / "profiles" / "food" / "collection.json"
    skill = (
        root
        / ".agents"
        / "skills"
        / "localize-spt-food-textures"
        / "SKILL.md"
    )
    profile.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")
    skill.write_text("# test", encoding="utf-8")

    assert find_spt_project_root(root / "workspace" / "krita", "") == root
    assert find_spt_project_root(tmp_path / "generic", str(root / "source.png")) == root
    assert find_spt_project_root(tmp_path / "generic", "") is None


def test_plugin_package_has_krita_import_layout(tmp_path: Path) -> None:
    module_path = ROOT / "tools/krita_codex_image_edit/package.py"
    spec = importlib.util.spec_from_file_location("krita_codex_packager", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    output = module.package_plugin(tmp_path / "plugin.zip")
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())

    assert "golani_codex_image_edit.desktop" in names
    assert "golani_codex_image_edit/__init__.py" in names
    assert "golani_codex_image_edit/docker.py" in names
    assert "golani_codex_image_edit/app_server.py" in names
    assert "LICENSE" in names
    assert all("__pycache__" not in name for name in names)
