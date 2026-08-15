from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .scoring import file_sha256


class ImageDependencyError(RuntimeError):
    pass


class VariantLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageFingerprint:
    file_sha256: str
    pixel_sha256: str
    width: int
    height: int
    mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_sha256": self.file_sha256,
            "pixel_sha256": self.pixel_sha256,
            "width": self.width,
            "height": self.height,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class PreparedVariant:
    variant_id: str
    rgb: Any
    width: int
    height: int


def _pillow() -> tuple[Any, Any, Any]:
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise ImageDependencyError(
            "Pillow가 없습니다. OCR 전용 환경에 tools/requirements-ocr.txt를 설치하세요."
        ) from exc
    return Image, ImageOps, UnidentifiedImageError


def _numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:
        raise ImageDependencyError(
            "NumPy가 없습니다. OCR 전용 환경에 tools/requirements-ocr.txt를 설치하세요."
        ) from exc
    return np


def fingerprint_image(path: str | Path) -> ImageFingerprint:
    Image, _, _ = _pillow()
    source = Path(path)
    with Image.open(source) as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        hasher = hashlib.sha256()
        hasher.update(f"RGBA:{width}x{height}\0".encode("ascii"))
        rows_per_chunk = max(1, min(256, (4 * 1024 * 1024) // max(1, width * 4)))
        for top in range(0, height, rows_per_chunk):
            bottom = min(height, top + rows_per_chunk)
            hasher.update(rgba.crop((0, top, width, bottom)).tobytes())
    return ImageFingerprint(
        file_sha256=file_sha256(source),
        pixel_sha256=hasher.hexdigest(),
        width=width,
        height=height,
        mode="RGBA",
    )


def _positions(length: int, tile_size: int, overlap: int) -> list[int]:
    if length <= tile_size:
        return [0]
    step = tile_size - overlap
    values = list(range(0, max(1, length - tile_size + 1), step))
    end = length - tile_size
    if values[-1] != end:
        values.append(end)
    return values


def _tiles(image: Any, tile_size: int, overlap: int) -> Iterator[tuple[str, Any]]:
    xs = _positions(image.width, tile_size, overlap)
    ys = _positions(image.height, tile_size, overlap)
    for y in ys:
        for x in xs:
            right = min(image.width, x + tile_size)
            bottom = min(image.height, y + tile_size)
            yield f"tile_x{x}_y{y}_w{right - x}_h{bottom - y}", image.crop((x, y, right, bottom))


def _rotate(image: Any, angle: int, Image: Any) -> Any:
    transpose = {
        0: None,
        90: Image.Transpose.ROTATE_90,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_270,
    }[angle]
    return image if transpose is None else image.transpose(transpose)


def _maybe_upscale(image: Any, config: Mapping[str, Any], Image: Any) -> Any:
    minimum = int(config.get("upscale_min_side", 0))
    factor = float(config.get("upscale_factor", 1.0))
    if minimum <= 0 or factor <= 1.0 or min(image.size) >= minimum:
        return image
    target_factor = min(factor, minimum / max(1, min(image.size)))
    size = (
        max(1, round(image.width * target_factor)),
        max(1, round(image.height * target_factor)),
    )
    return image.resize(size, Image.Resampling.LANCZOS)


def _background_specs(rgba: Any, config: Mapping[str, Any]) -> list[tuple[str, tuple[int, int, int, int] | None]]:
    alpha_min, alpha_max = rgba.getchannel("A").getextrema()
    if alpha_min == 255 and alpha_max == 255:
        return [("opaque", None)]
    backgrounds: list[tuple[str, tuple[int, int, int, int] | None]] = []
    for name in config.get("alpha_backgrounds", ["white", "black"]):
        color = (255, 255, 255, 255) if str(name).lower() == "white" else (0, 0, 0, 255)
        backgrounds.append((str(name).lower(), color))
    return backgrounds or [("alpha_raw", None)]


def _render_background(rgba: Any, color: tuple[int, int, int, int] | None, Image: Any) -> Any:
    if color is None:
        return rgba.convert("RGB")
    canvas = Image.new("RGBA", rgba.size, color)
    return Image.alpha_composite(canvas, rgba).convert("RGB")


def iter_variants(path: str | Path, config: Mapping[str, Any]) -> Iterator[PreparedVariant]:
    Image, ImageOps, _ = _pillow()
    np = _numpy()
    tile_size = int(config["tile_size"])
    overlap = int(config["tile_overlap"])
    rotations = [int(value) for value in config["rotations"]]
    max_variants = int(config.get("max_variants", 0))
    emitted = 0

    with Image.open(path) as loaded:
        rgba = loaded.convert("RGBA")
        background_specs = _background_specs(rgba, config)
        tile_count = len(_positions(rgba.width, tile_size, overlap)) * len(
            _positions(rgba.height, tile_size, overlap)
        )
        overview_count = int(
            bool(config.get("include_overview", True) and max(rgba.size) > tile_size)
        )
        contrast_count = 2 if config.get("include_autocontrast", False) else 1
        total_variants = (
            len(background_specs)
            * (tile_count + overview_count)
            * contrast_count
            * len(rotations)
        )
        for background_name, color in background_specs:
            base = _render_background(rgba, color, Image)
            overview = None
            if config.get("include_overview", True) and max(base.size) > tile_size:
                overview = base.copy()
                overview.thumbnail(
                    (int(config.get("max_overview_side", 2048)),) * 2,
                    Image.Resampling.LANCZOS,
                )
            def stage_images() -> Iterator[tuple[str, Any]]:
                if overview is not None:
                    yield "overview", overview
                yield from _tiles(base, tile_size, overlap)

            for region_name, region in stage_images():
                contrast_variants = [("color", region)]
                if config.get("include_autocontrast", False):
                    contrasted = ImageOps.autocontrast(region.convert("L")).convert("RGB")
                    contrast_variants.append(("autocontrast", contrasted))
                for contrast_name, prepared in contrast_variants:
                    for angle in rotations:
                        transformed = _maybe_upscale(_rotate(prepared, angle, Image), config, Image)
                        array = np.asarray(transformed.convert("RGB"), dtype=np.uint8)
                        array = np.ascontiguousarray(array)
                        variant_id = f"{background_name}:{region_name}:{contrast_name}:r{angle}"
                        yield PreparedVariant(
                            variant_id=variant_id,
                            rgb=array,
                            width=transformed.width,
                            height=transformed.height,
                        )
                        emitted += 1
                        if max_variants and emitted >= max_variants and total_variants > max_variants:
                            raise VariantLimitExceeded(
                                f"전처리 variant {total_variants}개 중 {max_variants}개 제한에 도달했습니다. "
                                "누락 방지를 위해 max_variants를 0 또는 더 큰 값으로 설정하세요."
                            )


def make_preview(source: str | Path, destination: str | Path, max_side: int = 720) -> None:
    Image, _, _ = _pillow()
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(f".{destination_path.name}.tmp")
    try:
        with Image.open(source) as image:
            preview = image.convert("RGBA")
            background = Image.new("RGBA", preview.size, (40, 40, 40, 255))
            preview = Image.alpha_composite(background, preview).convert("RGB")
            preview.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            preview.save(temporary, format="PNG", optimize=True)
        os.replace(temporary, destination_path)
    finally:
        if temporary.exists():
            temporary.unlink()
