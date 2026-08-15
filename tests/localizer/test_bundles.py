from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from golani_texture_localizer.bundles import (
    _coverage_values,
    _sha256_file,
    _verified_source_bundle,
)


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


def test_uv_coverage_uses_conservative_integer_downscale_for_auxiliary_map() -> None:
    values = np.zeros((4, 4), dtype=np.uint8)
    values[0, 0] = 255
    values[1, 3] = 255
    values[3, 2] = 255

    result = _coverage_values(Image.fromarray(values, "L"), (2, 2))

    np.testing.assert_array_equal(
        result,
        np.asarray([[True, True], [False, True]], dtype=bool),
    )


@pytest.mark.parametrize("size", [(3, 2), (2, 1), (8, 8)])
def test_uv_coverage_rejects_unsafe_size_conversion(size: tuple[int, int]) -> None:
    coverage = Image.new("L", (4, 4), 255)

    with pytest.raises(ValueError, match="UV coverage"):
        _coverage_values(coverage, size)
