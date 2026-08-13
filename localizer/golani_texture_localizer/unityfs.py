from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Any


@dataclass(frozen=True)
class DataBlock:
    uncompressed_size: int
    compressed_size: int
    flags: int


@dataclass(frozen=True)
class DirectoryEntry:
    offset: int
    size: int
    flags: int
    path: str


@dataclass(frozen=True)
class UnityFSLayout:
    signature: str
    format_version: int
    player_version: str
    engine_version: str
    bundle_size: int
    compressed_info_size: int
    uncompressed_info_size: int
    archive_flags: int
    blocks_info_offset: int
    data_offset: int
    data_hash: bytes
    blocks: tuple[DataBlock, ...]
    entries: tuple[DirectoryEntry, ...]


@dataclass(frozen=True)
class PhysicalPatch:
    start: int
    end: int
    payload_start: int
    payload_end: int


def parse_unityfs_layout(bundle_bytes: bytes, bundle: Any) -> UnityFSLayout:
    from UnityPy.enums import ArchiveFlags
    from UnityPy.streams import EndianBinaryReader

    reader = EndianBinaryReader(bundle_bytes)
    signature = reader.read_string_to_null()
    if signature != "UnityFS":
        raise ValueError(f"UnityFS가 아닌 bundle이에요: {signature!r}")
    format_version = reader.read_u_int()
    player_version = reader.read_string_to_null()
    engine_version = reader.read_string_to_null()
    bundle_size = reader.read_long()
    compressed_info_size = reader.read_u_int()
    uncompressed_info_size = reader.read_u_int()
    archive_flags = reader.read_u_int()
    if bundle_size != len(bundle_bytes):
        raise ValueError(f"UnityFS header 크기 {bundle_size} != 실제 크기 {len(bundle_bytes)}")
    if bundle.decryptor is not None:
        raise ValueError("암호화된 UnityFS bundle은 안전하게 패치할 수 없어요")

    if bundle._uses_block_alignment:
        reader.align_stream(16)
    header_end = reader.Position
    blocks_info_at_end = bool(archive_flags & 0x80)
    blocks_info_offset = len(bundle_bytes) - compressed_info_size if blocks_info_at_end else header_end
    blocks_info_end = blocks_info_offset + compressed_info_size
    if not 0 <= blocks_info_offset <= blocks_info_end <= len(bundle_bytes):
        raise ValueError("UnityFS blocks-info 범위가 bundle 밖이에요")

    compressed_info = bundle_bytes[blocks_info_offset:blocks_info_end]
    info_bytes = bundle.decompress_data(compressed_info, uncompressed_info_size, bundle.dataflags)
    if len(info_bytes) != uncompressed_info_size:
        raise ValueError("UnityFS blocks-info 압축 해제 크기가 달라요")

    info_reader = EndianBinaryReader(info_bytes)
    data_hash = info_reader.read_bytes(16)
    block_count = info_reader.read_int()
    if block_count < 1:
        raise ValueError("UnityFS data block이 없어요")
    blocks = tuple(
        DataBlock(
            uncompressed_size=info_reader.read_u_int(),
            compressed_size=info_reader.read_u_int(),
            flags=info_reader.read_u_short(),
        )
        for _ in range(block_count)
    )
    entry_count = info_reader.read_int()
    if entry_count < 1:
        raise ValueError("UnityFS directory entry가 없어요")
    entries = tuple(
        DirectoryEntry(
            offset=info_reader.read_long(),
            size=info_reader.read_long(),
            flags=info_reader.read_u_int(),
            path=info_reader.read_string_to_null(),
        )
        for _ in range(entry_count)
    )

    data_offset = header_end if blocks_info_at_end else blocks_info_end
    if isinstance(bundle.dataflags, ArchiveFlags) and (
        bundle.dataflags & ArchiveFlags.BlockInfoNeedPaddingAtStart
    ):
        data_offset += (-data_offset) % 16
    compressed_data_size = sum(block.compressed_size for block in blocks)
    data_end = data_offset + compressed_data_size
    maximum_data_end = blocks_info_offset if blocks_info_at_end else len(bundle_bytes)
    if data_end > maximum_data_end:
        raise ValueError("UnityFS data block이 container 범위를 벗어나요")

    total_uncompressed_size = sum(block.uncompressed_size for block in blocks)
    for entry in entries:
        if entry.offset < 0 or entry.size < 0 or entry.offset + entry.size > total_uncompressed_size:
            raise ValueError(f"UnityFS directory entry 범위가 잘못됐어요: {entry.path!r}")
    return UnityFSLayout(
        signature=signature,
        format_version=format_version,
        player_version=player_version,
        engine_version=engine_version,
        bundle_size=bundle_size,
        compressed_info_size=compressed_info_size,
        uncompressed_info_size=uncompressed_info_size,
        archive_flags=archive_flags,
        blocks_info_offset=blocks_info_offset,
        data_offset=data_offset,
        data_hash=data_hash,
        blocks=blocks,
        entries=entries,
    )


def find_directory_entry(layout: UnityFSLayout, path: str) -> DirectoryEntry:
    matches = [entry for entry in layout.entries if entry.path == path]
    if len(matches) != 1:
        raise ValueError(f"UnityFS entry {path!r}는 하나여야 해요. 현재 {len(matches)}개예요")
    return matches[0]


def patch_uncompressed_logical_range(
    bundle_bytes: bytes,
    layout: UnityFSLayout,
    start: int,
    payload: bytes,
) -> tuple[bytes, tuple[PhysicalPatch, ...]]:
    from UnityPy.enums import CompressionFlags

    if any(layout.data_hash):
        raise ValueError("UnityFS data hash가 0이 아니어서 in-place patch가 hash를 깨뜨려요")
    end = start + len(payload)
    patched = bytearray(bundle_bytes)
    patches: list[PhysicalPatch] = []
    logical_cursor = 0
    physical_cursor = layout.data_offset
    covered = 0
    for index, block in enumerate(layout.blocks):
        logical_end = logical_cursor + block.uncompressed_size
        physical_end = physical_cursor + block.compressed_size
        overlap_start = max(start, logical_cursor)
        overlap_end = min(end, logical_end)
        if overlap_start < overlap_end:
            compression = CompressionFlags(block.flags & 0x3F)
            if compression != CompressionFlags.NONE:
                raise ValueError(
                    f"대상 stream이 압축된 UnityFS data block {index}({compression.name})과 겹쳐요"
                )
            if block.compressed_size != block.uncompressed_size:
                raise ValueError(f"uncompressed block {index}의 크기 필드가 일치하지 않아요")
            payload_start = overlap_start - start
            payload_end = overlap_end - start
            patch_start = physical_cursor + overlap_start - logical_cursor
            patch_end = patch_start + payload_end - payload_start
            patched[patch_start:patch_end] = payload[payload_start:payload_end]
            patches.append(
                PhysicalPatch(
                    start=patch_start,
                    end=patch_end,
                    payload_start=payload_start,
                    payload_end=payload_end,
                )
            )
            covered += payload_end - payload_start
        logical_cursor = logical_end
        physical_cursor = physical_end
    if covered != len(payload):
        raise ValueError(f"payload {len(payload)}바이트 중 {covered}바이트만 패치했어요")
    return bytes(patched), tuple(patches)


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if start < 0 or end < start:
            raise ValueError(f"잘못된 byte range: {(start, end)}")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def bytes_equal_outside_ranges(
    before: bytes,
    after: bytes,
    ranges: Iterable[tuple[int, int]],
) -> bool:
    if len(before) != len(after):
        return False
    cursor = 0
    for start, end in merge_ranges(ranges):
        if before[cursor:start] != after[cursor:start]:
            return False
        cursor = end
    return before[cursor:] == after[cursor:]


def layout_signature(layout: UnityFSLayout) -> tuple[Any, ...]:
    return (
        layout.signature,
        layout.format_version,
        layout.player_version,
        layout.engine_version,
        layout.bundle_size,
        layout.compressed_info_size,
        layout.uncompressed_info_size,
        layout.archive_flags,
        layout.blocks_info_offset,
        layout.data_offset,
        layout.data_hash,
        layout.blocks,
        layout.entries,
    )
