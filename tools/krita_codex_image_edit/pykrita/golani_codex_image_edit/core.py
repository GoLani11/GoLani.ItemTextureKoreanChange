from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re


_MAX_PROMPT_LENGTH = 6000


@dataclass(frozen=True)
class CropRect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_list(self) -> list[int]:
        return [self.x, self.y, self.width, self.height]


def expanded_crop(
    selection: CropRect,
    canvas_width: int,
    canvas_height: int,
    padding: int,
    *,
    canvas_x: int = 0,
    canvas_y: int = 0,
) -> CropRect:
    if canvas_width < 1 or canvas_height < 1:
        raise ValueError("캔버스 크기가 올바르지 않아요")
    if selection.width < 1 or selection.height < 1:
        raise ValueError("선택 영역이 비어 있어요")
    if padding < 0:
        raise ValueError("문맥 여백은 0 이상이어야 해요")

    canvas_right = canvas_x + canvas_width
    canvas_bottom = canvas_y + canvas_height
    x0 = max(canvas_x, selection.x - padding)
    y0 = max(canvas_y, selection.y - padding)
    x1 = min(canvas_right, selection.right + padding)
    y1 = min(canvas_bottom, selection.bottom + padding)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("선택 영역이 캔버스 밖에 있어요")
    return CropRect(x0, y0, x1 - x0, y1 - y0)


def context_crop(
    selection: CropRect,
    canvas_width: int,
    canvas_height: int,
    padding: int,
    *,
    canvas_x: int = 0,
    canvas_y: int = 0,
) -> CropRect:
    """Expand a selection and make the context square whenever the canvas permits it."""

    expanded = expanded_crop(
        selection,
        canvas_width,
        canvas_height,
        padding,
        canvas_x=canvas_x,
        canvas_y=canvas_y,
    )
    side = max(selection.width, selection.height) + padding * 2
    if side > min(canvas_width, canvas_height):
        return expanded

    center_x = selection.x + selection.width / 2
    center_y = selection.y + selection.height / 2
    x = round(center_x - side / 2)
    y = round(center_y - side / 2)
    x = min(max(canvas_x, x), canvas_x + canvas_width - side)
    y = min(max(canvas_y, y), canvas_y + canvas_height - side)
    return CropRect(x, y, side, side)


def is_supported_srgb_profile(value: str) -> bool:
    """Allow only nonlinear sRGB profiles whose raw bytes are safe in QImage PNGs."""

    normalized = value.strip().lower().replace("_", "-")
    return normalized in {
        "srgb built-in",
        "srgb-elle-v2-srgbtrc.icc",
        "srgb-elle-v4-srgbtrc.icc",
    }


def find_spt_project_root(workspace: Path, document_file: str) -> Path | None:
    candidates = [workspace]
    if document_file:
        candidates.append(Path(document_file).expanduser())
    for candidate in candidates:
        start = candidate if candidate.is_dir() else candidate.parent
        for directory in (start, *start.parents):
            if (
                (directory / "profiles" / "food" / "collection.json").is_file()
                and (
                    directory
                    / ".agents"
                    / "skills"
                    / "localize-spt-food-textures"
                    / "SKILL.md"
                ).is_file()
            ):
                return directory
    return None


def masked_bgra_layer(
    generated_bgra: bytes,
    selection_mask: bytes,
    width: int,
    height: int,
) -> bytes:
    """Return a BGRA/U8 layer whose alpha is constrained by the saved selection.

    Krita exposes integer RGBA pixels in BGRA byte order. RGB values outside the
    selection are zeroed as well, so no generated color survives outside the
    mask even in a downstream tool that mishandles fully transparent RGB.
    """

    if width < 1 or height < 1:
        raise ValueError("레이어 크기가 올바르지 않아요")
    pixel_count = width * height
    if len(generated_bgra) != pixel_count * 4:
        raise ValueError("생성 이미지 픽셀 길이가 BGRA/U8 크기와 달라요")
    if len(selection_mask) != pixel_count:
        raise ValueError("선택 마스크 픽셀 길이가 크기와 달라요")
    if not any(selection_mask):
        raise ValueError("선택 마스크가 비어 있어요")

    output = bytearray(pixel_count * 4)
    has_visible_pixel = False
    for pixel_index, selectedness in enumerate(selection_mask):
        if selectedness == 0:
            continue
        offset = pixel_index * 4
        output[offset : offset + 3] = generated_bgra[offset : offset + 3]
        generated_alpha = generated_bgra[offset + 3]
        output_alpha = (generated_alpha * selectedness + 127) // 255
        output[offset + 3] = output_alpha
        has_visible_pixel = has_visible_pixel or output_alpha > 0
    if not has_visible_pixel:
        raise ValueError("생성 이미지가 선택 영역 안에서 완전히 투명해 적용할 내용이 없어요")
    return bytes(output)


def ensure_selected_pixels_are_opaque(
    source_bgra: bytes,
    selection_mask: bytes,
    width: int,
    height: int,
) -> None:
    pixel_count = width * height
    if len(source_bgra) != pixel_count * 4 or len(selection_mask) != pixel_count:
        raise ValueError("원본 또는 선택 마스크 픽셀 길이가 크기와 달라요")
    for pixel_index, selectedness in enumerate(selection_mask):
        if selectedness and source_bgra[pixel_index * 4 + 3] != 255:
            raise ValueError(
                "반투명 픽셀이 포함된 선택은 현재 안전하게 합성할 수 없어요. "
                "불투명 영역만 선택해 주세요"
            )


def validate_projection_invariants(
    before_bgra: bytes,
    after_bgra: bytes,
    selection_mask: bytes,
    width: int,
    height: int,
) -> None:
    pixel_count = width * height
    expected_bytes = pixel_count * 4
    if (
        len(before_bgra) != expected_bytes
        or len(after_bgra) != expected_bytes
        or len(selection_mask) != pixel_count
    ):
        raise ValueError("합성 검증 픽셀 길이가 크기와 달라요")

    outside_changes = 0
    alpha_changes = 0
    for pixel_index, selectedness in enumerate(selection_mask):
        offset = pixel_index * 4
        if (
            selectedness == 0
            and before_bgra[offset : offset + 4] != after_bgra[offset : offset + 4]
        ):
            outside_changes += 1
        if before_bgra[offset + 3] != after_bgra[offset + 3]:
            alpha_changes += 1
    if outside_changes or alpha_changes:
        raise ValueError(
            "합성 불변성 검사 실패: "
            f"선택 밖 변경 {outside_changes}px, 알파 변경 {alpha_changes}px"
        )


def validate_spt_mask_contract(
    old_text: bytes,
    new_text: bytes,
    editable: bytes,
    protected: bytes,
    seam_guard: bytes,
    width: int,
    height: int,
) -> None:
    pixel_count = width * height
    masks = (old_text, new_text, editable, protected, seam_guard)
    if width < 1 or height < 1 or any(len(mask) != pixel_count for mask in masks):
        raise ValueError("SPT 마스크 픽셀 길이가 원본 크기와 달라요")
    if not any(editable):
        raise ValueError("SPT editable 마스크가 비어 있어요")
    old_outside = new_outside = editable_protected = seam_unprotected = 0
    for index in range(pixel_count):
        if old_text[index] and not editable[index]:
            old_outside += 1
        if new_text[index] and not editable[index]:
            new_outside += 1
        if editable[index] and protected[index]:
            editable_protected += 1
        if seam_guard[index] and not protected[index]:
            seam_unprotected += 1
    if old_outside or new_outside or editable_protected or seam_unprotected:
        raise ValueError(
            "SPT 마스크 포함 관계가 올바르지 않아요: "
            f"old_text 밖 {old_outside}px, new_text 밖 {new_outside}px, "
            f"editable/protected 겹침 {editable_protected}px, "
            f"보호되지 않은 seam {seam_unprotected}px"
        )


def spt_panel_mask(
    editable: bytes,
    width: int,
    height: int,
    region_bboxes: list[tuple[int, int, int, int]],
    padding: int,
) -> bytes:
    if len(editable) != width * height:
        raise ValueError("SPT editable 마스크 픽셀 길이가 원본 크기와 달라요")
    if not region_bboxes:
        raise ValueError("SPT 라벨 면에 번역 영역이 없어요")
    if padding < 0:
        raise ValueError("SPT 라벨 면 마스크 여백은 0 이상이어야 해요")
    x0 = max(0, min(box[0] for box in region_bboxes) - padding)
    y0 = max(0, min(box[1] for box in region_bboxes) - padding)
    x1 = min(width, max(box[2] for box in region_bboxes) + padding)
    y1 = min(height, max(box[3] for box in region_bboxes) + padding)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("SPT 라벨 면 bbox가 원본 밖에 있어요")
    result = bytearray(width * height)
    for y in range(y0, y1):
        row_start = y * width + x0
        row_end = y * width + x1
        result[row_start:row_end] = editable[row_start:row_end]
    if not any(result):
        raise ValueError("이 라벨 면 bbox와 겹치는 editable 픽셀이 없어요")
    return bytes(result)


def ensure_selection_is_spt_subset(selection: bytes, allowed: bytes) -> bool:
    if len(selection) != len(allowed) or not selection:
        raise ValueError("현재 선택과 SPT 허용 마스크 크기가 달라요")
    if not any(selection):
        raise ValueError("현재 SPT 선택 영역이 비어 있어요")
    reduced = False
    for selectedness, allowedness in zip(selection, allowed):
        if selectedness > allowedness:
            raise ValueError(
                "현재 선택이 검증된 SPT editable 라벨 면 밖으로 넓어졌어요. "
                "선택을 줄이는 것만 허용돼요"
            )
        if selectedness != allowedness:
            reduced = True
    return reduced


def build_edit_prompt(
    instruction: str,
    width: int,
    height: int,
) -> str:
    normalized = instruction.strip()
    if not normalized:
        raise ValueError("수정 지시를 입력해 주세요")
    if len(normalized) > _MAX_PROMPT_LENGTH:
        raise ValueError(f"수정 지시는 {_MAX_PROMPT_LENGTH}자 이하여야 해요")
    if width < 1 or height < 1:
        raise ValueError("편집 이미지 크기가 올바르지 않아요")

    return f"""$imagegen
Use case: precise-object-edit
Asset type: non-destructive Krita selection preview
Primary request: {normalized}

Image 1 is the first attached image and the only edit target.
Image 2 is the second attached image, a spatial selection guide with exactly the same dimensions.
In Image 2, white and gray pixels are the only editable area; black pixels are protected.

Use the built-in image_gen tool exactly once with num_last_images_to_include=2.
Never pass referenced_image_paths; use only the two images already attached to this turn.
Return exactly one edited raster image with the same composition, crop, aspect ratio ({width}:{height}), and alignment as Image 1.
Change only the requested content inside the guided area. Preserve all black-mask regions, geometry, lighting, material, texture, wear, shadows, edges, and every unrelated detail.
Do not draw the mask, selection tint, guides, labels, explanations, borders, watermarks, or extra text into the result.
Do not use an API/SDK fallback, an OPENAI_API_KEY workflow, a shell command, or a local image-generation script.
This output is a pre-validation preview. Do not modify any project file; only generate the image artifact."""


def safe_stem(value: str, fallback: str = "untitled") -> str:
    stem = re.sub(r"[^0-9A-Za-z가-힣._-]+", "-", value.strip())
    stem = stem.strip(".-_")
    stem = stem[:80] or fallback
    windows_device = stem.split(".", 1)[0].upper()
    if windows_device in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        stem = f"_{stem}"
    return stem


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
