from __future__ import annotations

import json
from pathlib import Path

import pytest

from golani_texture_localizer.deployment import _load_release, deploy_release
from golani_texture_localizer.paths import ProjectPaths


def test_deploy_requires_hash_pinned_release(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    paths.latest_release.parent.mkdir(parents=True)
    paths.latest_release.write_text(
        json.dumps({"schema_version": 1, "release_id": "deadbeef"}), encoding="utf-8"
    )

    with pytest.raises(FileNotFoundError, match="release"):
        _load_release(paths, "latest")


def test_deploy_dry_run_does_not_touch_spt(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    release = paths.releases / "abc123"
    bundle = release / "bundles" / "items" / "sample.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"verified")
    import hashlib

    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    (release / "release.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "abc123",
                "bundle_count": 1,
                "bundles": {"items/sample.bundle": digest},
                "server_files": {},
                "passed": True,
            }
        ),
        encoding="utf-8",
    )
    spt_root = tmp_path / "SPT"

    result = deploy_release(paths, spt_root, release_id="abc123", execute=False)

    assert result["execute"] is False
    assert not spt_root.exists()
