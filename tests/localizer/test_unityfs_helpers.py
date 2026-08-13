import re

from golani_texture_localizer.unityfs import (
    DataBlock,
    DirectoryEntry,
    UnityFSLayout,
    bytes_equal_outside_ranges,
    merge_ranges,
    rebase_unityfs_cab_exact,
)


def test_merge_ranges_combines_touching_and_overlapping_ranges() -> None:
    assert merge_ranges([(5, 8), (1, 3), (3, 4), (7, 10)]) == [(1, 4), (5, 10)]


def test_bytes_equal_outside_ranges_only_allows_declared_changes() -> None:
    before = b"abcdefghij"
    after = b"abXXefYYij"

    assert bytes_equal_outside_ranges(before, after, [(2, 4), (6, 8)])
    assert not bytes_equal_outside_ranges(before, after, [(2, 4)])


def test_rebase_unityfs_cab_uses_unique_same_length_identifier() -> None:
    source_cab = "CAB-0123456789abcdef0123456789abcdef"
    info = (source_cab + "\0" + source_cab + ".resS\0").encode("ascii")
    data = ("stream=" + source_cab + "/" + source_cab + ".resS").encode("ascii")
    bundle_bytes = info + data
    layout = UnityFSLayout(
        signature="UnityFS",
        format_version=8,
        player_version="5.x.x",
        engine_version="2022.3.43f1",
        bundle_size=len(bundle_bytes),
        compressed_info_size=len(info),
        uncompressed_info_size=len(info),
        archive_flags=0,
        blocks_info_offset=0,
        data_offset=len(info),
        data_hash=b"\0" * 16,
        blocks=(DataBlock(len(data), len(data), 0),),
        entries=(
            DirectoryEntry(0, 1, 4, source_cab),
            DirectoryEntry(1, len(data) - 1, 0, source_cab + ".resS"),
        ),
    )

    class UncompressedBundle:
        dataflags = 0

        @staticmethod
        def decompress_data(value: bytes, size: int, _flags: int) -> bytes:
            assert len(value) == size
            return value

    rebased, report = rebase_unityfs_cab_exact(
        bundle_bytes,
        UncompressedBundle(),
        layout,
    )

    assert len(rebased) == len(bundle_bytes)
    assert source_cab.encode("ascii") not in rebased
    assert re.fullmatch(r"CAB-[0-9a-f]{32}", report.output_cab)
    assert report.output_cab != report.source_cab
    assert report.blocks_info_occurrences == 2
    assert report.data_occurrences == 2
    assert bytes_equal_outside_ranges(bundle_bytes, rebased, report.physical_ranges)
