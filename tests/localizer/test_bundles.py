from pathlib import Path

import pytest

from golani_texture_localizer.bundles import _sha256_file, _verified_source_bundle


def test_source_bundle_must_match_inventory_hash(tmp_path: Path) -> None:
    bundle_key = "assets/content/item.bundle"
    source = tmp_path / bundle_key
    source.parent.mkdir(parents=True)
    source.write_bytes(b"inventory version")
    records = [{"bundle_key": bundle_key, "bundle_sha256": _sha256_file(source)}]

    assert _verified_source_bundle(bundle_key, tmp_path, None, records) == source.resolve()

    source.write_bytes(b"changed after inventory")
    with pytest.raises(ValueError, match="변경됐어요"):
        _verified_source_bundle(bundle_key, tmp_path, None, records)


def test_override_bundle_must_match_registered_hash(tmp_path: Path) -> None:
    source = tmp_path / "original.bundle"
    source.write_bytes(b"verified original")
    override = {"path": str(source), "sha256": _sha256_file(source)}

    assert _verified_source_bundle("item.bundle", tmp_path, override, []) == source.resolve()

    source.write_bytes(b"changed original")
    with pytest.raises(ValueError, match="변경됐어요"):
        _verified_source_bundle("item.bundle", tmp_path, override, [])


def test_missing_override_uses_matching_live_bundle(tmp_path: Path) -> None:
    bundle_key = "assets/content/item.bundle"
    live = tmp_path / bundle_key
    live.parent.mkdir(parents=True)
    live.write_bytes(b"verified original")
    override = {
        "path": str(tmp_path / "deleted-backup.bundle"),
        "sha256": _sha256_file(live),
    }

    assert _verified_source_bundle(bundle_key, tmp_path, override, []) == live.resolve()
