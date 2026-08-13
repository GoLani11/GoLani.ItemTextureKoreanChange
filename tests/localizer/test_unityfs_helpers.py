from golani_texture_localizer.unityfs import bytes_equal_outside_ranges, merge_ranges


def test_merge_ranges_combines_touching_and_overlapping_ranges() -> None:
    assert merge_ranges([(5, 8), (1, 3), (3, 4), (7, 10)]) == [(1, 4), (5, 10)]


def test_bytes_equal_outside_ranges_only_allows_declared_changes() -> None:
    before = b"abcdefghij"
    after = b"abXXefYYij"

    assert bytes_equal_outside_ranges(before, after, [(2, 4), (6, 8)])
    assert not bytes_equal_outside_ranges(before, after, [(2, 4)])
