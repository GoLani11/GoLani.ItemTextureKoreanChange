from pathlib import Path

import pytest

from golani_texture_localizer.models import BundleSpec, ProfileError
from golani_texture_localizer.names import texture_family, texture_role
from golani_texture_localizer.paths import ProjectPaths
from golani_texture_localizer.profile import load_profile


ROOT = Path(__file__).resolve().parents[2]


def test_food_profile_covers_full_inventory() -> None:
    profile = load_profile(ROOT / "profiles" / "food" / "collection.json")

    assert len(profile.bundles) == 41
    assert len(profile.targets) == 42
    assert {target.id for target in profile.targets if target.action == "preserve"} == {
        "moonshine",
        "purewater",
        "snacks-generic",
    }
    assert len({target.id for target in profile.targets}) == len(profile.targets)


def test_direction_sensitive_targets_document_natural_korean_layout() -> None:
    profile = load_profile(ROOT / "profiles" / "food" / "collection.json")

    for target_id in ("ratcola", "max-energy", "aquamari"):
        notes = profile.target_by_id(target_id).notes
        assert "회전" in notes
        assert "쌓" in notes or "적층" in notes
    assert profile.target_by_id("ratcola").exact_text == ("랫콜라", "샘 아저씨 특제")
    assert profile.target_by_id("herring").exact_text[0] == "태평양 청어"
    assert "자간" in profile.target_by_id("milk").notes
    assert profile.target_by_id("green-ice").exact_text == ("그린 아이스", "녹차")
    assert "자연스러운 어순" in profile.target_by_id("green-ice").notes
    assert profile.target_by_id("peas").exact_text[-1] == "완두콩"
    assert "그린피스" in profile.target_by_id("peas").notes


def test_texture_name_classification() -> None:
    assert texture_role("item_food_mayo_D") == "diffuse"
    assert texture_role("item_mre_LOD0_nrm") == "normal"
    assert texture_role("item_mre_LOD0_gloss") == "gloss"
    assert texture_family("item_mre_LOD0_diff") == "item_mre"
    assert texture_family("item_food_vodka_D") == "item_food_vodka"


@pytest.mark.parametrize("key", ["../outside.bundle", "/absolute.bundle", "C:/absolute.bundle"])
def test_bundle_key_rejects_paths_outside_bundle_root(key: str) -> None:
    with pytest.raises(ProfileError):
        BundleSpec.from_dict({"key": key})


def test_project_paths_rejects_git_directory(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with pytest.raises(ValueError):
        ProjectPaths.create(root, root / ".git" / "generated")
