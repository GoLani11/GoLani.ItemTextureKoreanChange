"""Patch selected BC blocks while keeping the original UnityFS container intact."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np
from PIL import Image

from .files import atomic_bytes, local_path, verified_file
from .mips import _mip_chain, _roundtrip_limits, apply_mip_delta
from .unityfs import (bytes_equal_outside_ranges, find_directory_entry, layout_signature,
                      parse_unityfs_layout, patch_uncompressed_logical_range)
from .validation import rgba

BLOCK_BYTES = {10: 8, 12: 16, 25: 16}  # DXT1, DXT5, BC7


def find_texture(environment, entry: dict):
    matches = [obj for obj in environment.objects
               if obj.type.name == "Texture2D" and obj.path_id == entry["path_id"]
               and obj.assets_file.name == entry["assets_file"]]
    if len(matches) != 1:
        raise ValueError(f"정확한 Texture2D를 찾을 수 없어요: {entry['texture']}")
    obj = matches[0]
    texture = obj.read()
    actual = (texture.m_Name, texture.m_Width, texture.m_Height, int(texture.m_TextureFormat), texture.m_MipCount)
    wanted = (entry["texture"], entry["width"], entry["height"], entry["format"], entry["mip_count"])
    if actual != wanted:
        raise ValueError(f"원본 Texture2D 메타데이터가 달라요: {entry['texture']}")
    return obj, texture


def object_hashes(environment) -> dict:
    return {(o.assets_file.name, o.path_id): hashlib.sha256(o.get_raw_data()).hexdigest()
            for o in environment.objects}


def mip_lengths(width: int, height: int, count: int, texture_format: int) -> list[int]:
    if texture_format not in BLOCK_BYTES:
        raise ValueError(f"블록 보존을 지원하지 않는 TextureFormat이에요: {texture_format}")
    if count < 1 or count > int(math.log2(max(width, height))) + 1:
        raise ValueError("원본 밉 수가 잘못됐어요")
    result = []
    for _ in range(count):
        result.append(math.ceil(width / 4) * math.ceil(height / 4) * BLOCK_BYTES[texture_format])
        width, height = max(1, width // 2), max(1, height // 2)
    return result


def decode_mips(obj, texture, payload: bytes) -> list[Image.Image]:
    from UnityPy.export import Texture2DConverter
    lengths = mip_lengths(texture.m_Width, texture.m_Height, texture.m_MipCount, int(texture.m_TextureFormat))
    if sum(lengths) != len(payload):
        raise ValueError("원본 밉 배열과 stream 크기가 달라요")
    levels, offset = [], 0
    width, height = texture.m_Width, texture.m_Height
    for length in lengths:
        levels.append(Texture2DConverter.parse_image_data(
            payload[offset:offset + length], width, height, texture.m_TextureFormat,
            obj.version, obj.platform, texture.m_PlatformBlob, flip=True).convert("RGBA"))
        offset += length
        width, height = max(1, width // 2), max(1, height // 2)
    return levels


def encode_level(image: Image.Image, obj, texture) -> bytes:
    from UnityPy.export import Texture2DConverter
    fmt = int(texture.m_TextureFormat)
    if fmt in (10, 12):
        import ispc_texcomp
        flipped = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        padded = Texture2DConverter.pad_image(flipped, math.ceil(image.width / 4) * 4, math.ceil(image.height / 4) * 4)
        surface = ispc_texcomp.RGBASurface(padded.tobytes(), padded.width, padded.height)
        compressor = ispc_texcomp.compress_blocks_bc1 if fmt == 10 else ispc_texcomp.compress_blocks_bc3
        # ispc_texcomp 1.0.1 allocates twice the BC1 payload; retain only actual blocks.
        length = (padded.width // 4) * (padded.height // 4) * BLOCK_BYTES[fmt]
        return bytes(compressor(surface))[:length]
    encoded, actual_format = Texture2DConverter.image_to_texture2d(
        image, texture.m_TextureFormat, obj.platform, texture.m_PlatformBlob)
    if actual_format != texture.m_TextureFormat:
        raise ValueError("압축기가 원본 포맷을 변경했어요")
    return bytes(encoded)


def replace_blocks(original: bytes, encoded: bytes, changed: np.ndarray,
                   texture_format: int, preserve_alpha: bool) -> tuple[bytes, np.ndarray, int]:
    height, width = changed.shape
    bh, bw = math.ceil(height / 4), math.ceil(width / 4)
    block_bytes = BLOCK_BYTES[texture_format]
    if len(original) != len(encoded) or len(encoded) != bh * bw * block_bytes:
        raise ValueError("압축 블록 크기가 원본과 달라요")
    padded = np.zeros((bh * 4, bw * 4), dtype=bool)
    padded[:height, :width] = np.flipud(changed)
    selected = padded.reshape(bh, 4, bw, 4).any(axis=(1, 3)).ravel()
    before = np.frombuffer(original, np.uint8).reshape(-1, block_bytes)
    replacement = np.frombuffer(encoded, np.uint8).reshape(-1, block_bytes)
    output = before.copy()
    start = 8 if texture_format == 12 and preserve_alpha else 0
    output[selected, start:] = replacement[selected, start:]
    spatial = np.flipud(np.repeat(np.repeat(selected.reshape(bh, bw), 4, axis=0), 4, axis=1)[:height, :width])
    return output.tobytes(), spatial, int(selected.sum())


def edit_payload(obj, texture, candidate: np.ndarray, entry: dict, coverage: Image.Image | None,
                 output: Path) -> tuple[bytes, list[dict]]:
    original = bytes(texture.get_image_data())
    source_levels = decode_mips(obj, texture, original)
    source = np.array(source_levels[0])
    if not np.array_equal(source, candidate) and entry["shared"]:
        raise ValueError("공유 텍스처는 변경할 수 없어요")
    changed_top = np.any(source != candidate, axis=2)
    if not changed_top.any():
        reports = []
        output.mkdir(parents=True, exist_ok=True)
        for i, image in enumerate(source_levels):
            image.save(output / f"mip-{i:02d}.png")
            reports.append({"level": i, "size": list(image.size), "blocks_reencoded": 0,
                            "outside_blocks_equal": True, "preserved_channels_equal": True,
                            "edited_blocks_mae": [0.0] * 4, "edited_blocks_p99": [0.0] * 4,
                            "edited_blocks_max": [0.0] * 4,
                            "limits": list(_roundtrip_limits(entry["role"], image.width, image.height, 6.0))})
        return original, reports
    if changed_top.any() and entry["role"] == "normal" and entry["format"] != 12:
        raise ValueError("Normal 편집은 확인된 DXT5nm 형식만 지원해요")
    levels = _mip_chain(Image.fromarray(candidate), entry["role"], texture.m_MipCount, coverage=coverage)
    reference = _mip_chain(source_levels[0], entry["role"], texture.m_MipCount, coverage=coverage)
    # mip0 is the exact supplied candidate. Lower mips retain the game's original
    # filtered appearance and receive only this edit's filtered delta.
    levels = [levels[0]] + [apply_mip_delta(original, baseline, edited, entry["role"])
                            for original, baseline, edited in zip(source_levels[1:], reference[1:], levels[1:], strict=True)]
    parts, masks, counts = [], [], []
    lengths = mip_lengths(texture.m_Width, texture.m_Height, texture.m_MipCount, int(texture.m_TextureFormat))
    offset = 0
    for level, baseline, length in zip(levels, source_levels, lengths, strict=True):
        source_part = original[offset:offset + length]
        offset += length
        changed = np.any(np.array(level) != np.array(baseline), axis=2)
        if not changed_top.any() or not changed.any():
            parts.append(source_part)
            masks.append(np.zeros((level.height, level.width), dtype=bool))
            counts.append(0)
            continue
        part, mask, count = replace_blocks(source_part, encode_level(level, obj, texture), changed,
                                          entry["format"], 3 in entry["preserved_channels"])
        parts.append(part)
        masks.append(mask)
        counts.append(count)
    payload = b"".join(parts)
    decoded = decode_mips(obj, texture, payload)
    reports = []
    for i, (actual_image, before_image, intended_image, mask, count) in enumerate(
            zip(decoded, source_levels, levels, masks, counts, strict=True)):
        actual, before, intended = np.array(actual_image), np.array(before_image), np.array(intended_image)
        if not np.array_equal(actual[~mask], before[~mask]):
            raise ValueError("편집 블록 밖의 원본 밉 픽셀이 변경됐어요")
        for channel in entry["preserved_channels"]:
            if not np.array_equal(actual[..., channel], before[..., channel]):
                raise ValueError(f"{entry['texture']} mip {i}: 보존 채널 {channel}이 변경됐어요")
            intended[..., channel] = before[..., channel]
        error = np.abs(actual.astype(np.int16) - intended.astype(np.int16))[mask]
        mae = error.mean(axis=0) if len(error) else np.zeros(4)
        p99 = np.percentile(error, 99, axis=0) if len(error) else np.zeros(4)
        maximum = error.max(axis=0) if len(error) else np.zeros(4)
        limits = _roundtrip_limits(entry["role"], actual_image.width, actual_image.height, 6.0)
        if np.any(mae > limits[0]) or np.any(p99 > limits[1]) or np.any(maximum > limits[2]):
            raise ValueError(f"{entry['texture']} mip {i}: 편집 블록 압축 오차 초과, MAE={mae.tolist()}, p99={p99.tolist()}")
        output.mkdir(parents=True, exist_ok=True)
        actual_image.save(output / f"mip-{i:02d}.png")
        reports.append({"level": i, "size": list(actual_image.size), "blocks_reencoded": count,
                        "outside_blocks_equal": True, "preserved_channels_equal": True,
                        "edited_blocks_mae": mae.tolist(), "edited_blocks_p99": p99.tolist(),
                        "edited_blocks_max": maximum.tolist(), "limits": list(limits)})
    return payload, reports


def patch_bundle(root: Path, bundle_descriptor: dict, entries: list[dict], output: Path,
                 coverage_path: Path, roundtrip: Path) -> dict:
    import UnityPy
    source = verified_file(root, bundle_descriptor)
    raw = source.read_bytes()
    env = UnityPy.load(raw)
    layout = parse_unityfs_layout(raw, env.file)
    patched, ranges, reports, expected = raw, [], [], {}
    with Image.open(coverage_path) as image:
        coverage = image.copy()
    for entry in entries:
        obj, texture = find_texture(env, entry)
        if not np.array_equal(np.array(texture.image.convert("RGBA")), rgba(verified_file(root, entry["source"]))):
            raise ValueError("추출 PNG와 실제 원본 Texture2D가 달라요")
        candidate = rgba(local_path(root, entry["candidate"]), (entry["width"], entry["height"]))
        payload, mips = edit_payload(obj, texture, candidate, entry, coverage, roundtrip / entry["id"])
        stream = texture.m_StreamData
        if not stream.path or stream.offset < 0 or len(payload) != stream.size:
            raise ValueError("원본 stream 범위를 보존할 수 없어요")
        resource = find_directory_entry(layout, stream.path.rsplit("/", 1)[-1])
        if stream.offset + stream.size > resource.size:
            raise ValueError("텍스처 stream이 resource 밖이에요")
        logical_range = (resource.offset + stream.offset, resource.offset + stream.offset + stream.size)
        if any(max(logical_range[0], start) < min(logical_range[1], end) for start, end in expected):
            raise ValueError("서로 다른 텍스처의 payload가 겹쳐요")
        expected[logical_range] = (entry, hashlib.sha256(payload).hexdigest())
        if payload != bytes(texture.get_image_data()):
            patched, patches = patch_uncompressed_logical_range(patched, layout, logical_range[0], payload)
            ranges.extend((p.start, p.end) for p in patches)
        reports.append({"texture": entry["texture"], "role": entry["role"], "mips": mips})
    rebuilt = UnityPy.load(patched)
    if object_hashes(env) != object_hashes(rebuilt) or layout_signature(layout) != layout_signature(parse_unityfs_layout(patched, rebuilt.file)):
        raise ValueError("UnityFS 구조 또는 serialized object가 변경됐어요")
    if not bytes_equal_outside_ranges(raw, patched, ranges):
        raise ValueError("텍스처 payload 밖의 바이트가 변경됐어요")
    for entry, digest in expected.values():
        _, texture = find_texture(rebuilt, entry)
        if hashlib.sha256(bytes(texture.get_image_data())).hexdigest() != digest:
            raise ValueError("재개봉한 텍스처 payload가 달라요")
    atomic_bytes(output, patched)
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "output_sha256": hashlib.sha256(patched).hexdigest(),
            "layout_equal": True, "objects_equal": True, "outside_payloads_equal": True,
            "byte_identical": raw == patched, "textures": reports, "passed": True}
