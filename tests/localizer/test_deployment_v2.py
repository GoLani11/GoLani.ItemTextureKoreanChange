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
    profile = root / "profiles" / "food" / "collection.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")
    repack = paths.reports / "repack.json"
    repack.parent.mkdir(parents=True)
    repack.write_text("{}", encoding="utf-8")
    review = paths.reviews / "sample" / "review.json"
    review.parent.mkdir(parents=True)
    review.write_text("{}", encoding="utf-8")
    (release / "release.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "release_id": "abc123",
                "bundle_count": 1,
                "bundles": {"items/sample.bundle": digest},
                "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                "repack_report_sha256": hashlib.sha256(repack.read_bytes()).hexdigest(),
                "review_hashes": {"sample": hashlib.sha256(review.read_bytes()).hexdigest()},
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


def test_deploy_rejects_release_after_review_changes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    release = paths.releases / "abc123"
    bundle = release / "bundles" / "sample.bundle"
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(b"verified")
    import hashlib

    profile = root / "profiles" / "food" / "collection.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}", encoding="utf-8")
    repack = paths.reports / "repack.json"
    repack.parent.mkdir(parents=True)
    repack.write_text("{}", encoding="utf-8")
    review = paths.reviews / "sample" / "review.json"
    review.parent.mkdir(parents=True)
    review.write_text("before", encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "release_id": "abc123",
        "bundle_count": 1,
        "bundles": {"sample.bundle": hashlib.sha256(bundle.read_bytes()).hexdigest()},
        "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
        "repack_report_sha256": hashlib.sha256(repack.read_bytes()).hexdigest(),
        "review_hashes": {"sample": hashlib.sha256(review.read_bytes()).hexdigest()},
        "server_files": {},
        "passed": True,
    }
    (release / "release.json").write_text(json.dumps(manifest), encoding="utf-8")
    review.write_text("after", encoding="utf-8")

    with pytest.raises(ValueError, match="오래된 release"):
        _load_release(paths, "abc123")
