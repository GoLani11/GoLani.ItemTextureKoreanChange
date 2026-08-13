from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_auto_module():
    path = Path(__file__).resolve().parents[2] / "tools" / "auto.py"
    spec = importlib.util.spec_from_file_location("golani_auto", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_manifest_preserves_game_dependencies(tmp_path: Path) -> None:
    auto = _load_auto_module()
    catalog = tmp_path / "Windows.json"
    catalog.write_text(
        json.dumps(
            {
                "textures.bundle": {"Dependencies": []},
                "model.bundle": {"Dependencies": ["cubemaps", "shaders", "shaders"]},
            }
        ),
        encoding="utf-8",
    )

    result = auto._deployment_manifest(
        ["textures.bundle", "model.bundle", "textures.bundle"],
        str(catalog),
    )

    assert result == {
        "manifest": [
            {"key": "model.bundle", "dependencyKeys": ["cubemaps", "shaders"]},
            {"key": "textures.bundle", "dependencyKeys": []},
        ]
    }


def test_deployment_manifest_rejects_missing_catalog_entry(tmp_path: Path) -> None:
    auto = _load_auto_module()
    catalog = tmp_path / "Windows.json"
    catalog.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="missing.bundle"):
        auto._deployment_manifest(["missing.bundle"], str(catalog))


def test_client_bundle_root_rejects_unrelated_existing_directory(tmp_path: Path) -> None:
    auto = _load_auto_module()
    (tmp_path / "SPT_Runtime").mkdir()
    (tmp_path / "SPT").mkdir()

    with pytest.raises(FileExistsError, match="다른 폴더"):
        auto._ensure_spt_client_bundle_root(str(tmp_path))


def test_client_bundle_root_links_to_runtime(tmp_path: Path) -> None:
    auto = _load_auto_module()
    runtime_root = tmp_path / "SPT_Runtime"
    runtime_root.mkdir()

    result = Path(auto._ensure_spt_client_bundle_root(str(tmp_path)))

    assert result.samefile(runtime_root)
