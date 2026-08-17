from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping

import numpy as np
from PIL import Image


class OcrUnavailable(RuntimeError):
    pass


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_ocr_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "profiles" / "food" / "ocr.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError(f"지원하지 않는 OCR 설정이에요: {path}")
    return data


def _paddle_cache() -> Path:
    return Path.home() / ".paddlex" / "official_models"


def _easy_cache(project_root: Path) -> Path:
    return project_root / "work" / "ocr-models" / "easyocr"


def ocr_doctor(project_root: Path) -> dict[str, Any]:
    config = load_ocr_config(project_root)
    packages = {
        name: _package_version(name)
        for name in ("paddleocr", "paddlepaddle", "easyocr", "torch", "Pillow", "numpy")
    }
    model_names = {
        config["detector"],
        *config["source_recognizers"],
        *config["candidate_recognizers"],
    }
    paddle_models = {
        name: {
            "path": str(_paddle_cache() / name),
            "present": (_paddle_cache() / name / "inference.pdiparams").is_file()
            and any(
                (_paddle_cache() / name / program).is_file()
                for program in ("inference.json", "inference.pdmodel")
            ),
        }
        for name in sorted(model_names)
    }
    easy_root = _easy_cache(project_root)
    easy_models = sorted(path.name for path in easy_root.glob("*.pth")) if easy_root.is_dir() else []
    package_ready = all(packages.get(name) for name in ("paddleocr", "paddlepaddle", "easyocr", "torch"))
    return {
        "schema_version": 1,
        "packages": packages,
        "paddle_models": paddle_models,
        "easyocr_model_directory": str(easy_root),
        "easyocr_models": easy_models,
        "packages_ready": package_ready,
        "paddle_models_ready": all(value["present"] for value in paddle_models.values()),
        "offline_ready": package_ready
        and all(value["present"] for value in paddle_models.values())
        and bool(easy_models),
    }


def _paddle_pipeline(detector: str, recognizer: str, device: str):
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise OcrUnavailable("PaddleOCR가 OCR 전용 환경에 설치되지 않았어요") from exc
    return PaddleOCR(
        text_detection_model_name=detector,
        text_recognition_model_name=recognizer,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        text_det_thresh=0.2,
        text_det_box_thresh=0.35,
        text_rec_score_thresh=0.0,
        device=device,
    )


def setup_ocr_models(project_root: Path) -> dict[str, Any]:
    config = load_ocr_config(project_root)
    signatures: list[str] = []
    for recognizer in sorted(
        set(config["source_recognizers"]) | set(config["candidate_recognizers"])
    ):
        _paddle_pipeline(config["detector"], recognizer, config.get("device", "cpu"))
        signatures.append(f"paddle:{config['detector']}+{recognizer}")
    try:
        import easyocr
    except ImportError as exc:
        raise OcrUnavailable("EasyOCR가 OCR 전용 환경에 설치되지 않았어요") from exc
    easy_root = _easy_cache(project_root)
    easy_root.mkdir(parents=True, exist_ok=True)
    groups = {
        tuple(group)
        for phase in config["easyocr_languages"].values()
        for group in phase
    }
    for languages in sorted(groups):
        easyocr.Reader(
            list(languages),
            gpu=False,
            model_storage_directory=str(easy_root),
            download_enabled=True,
            verbose=False,
        )
        signatures.append(f"easyocr:{'+'.join(languages)}")
    result = ocr_doctor(project_root)
    result["loaded_engines"] = signatures
    return result


def _positions(length: int, tile_size: int, overlap: int) -> list[int]:
    if length <= tile_size:
        return [0]
    step = tile_size - overlap
    values = list(range(0, length - tile_size + 1, step))
    if values[-1] != length - tile_size:
        values.append(length - tile_size)
    return values


def _rotate_clockwise(array: np.ndarray, angle: int) -> np.ndarray:
    if angle == 0:
        return np.ascontiguousarray(array)
    return np.ascontiguousarray(np.rot90(array, -(angle // 90)))


def _inverse_points(
    points: Iterable[Iterable[float]],
    angle: int,
    width: int,
    height: int,
    offset_x: int,
    offset_y: int,
) -> list[list[float]]:
    output: list[list[float]] = []
    for raw_x, raw_y in points:
        x, y = float(raw_x), float(raw_y)
        if angle == 0:
            original_x, original_y = x, y
        elif angle == 90:
            original_x, original_y = y, height - x
        elif angle == 180:
            original_x, original_y = width - x, height - y
        elif angle == 270:
            original_x, original_y = width - y, x
        else:
            raise ValueError(f"지원하지 않는 회전값이에요: {angle}")
        output.append([round(original_x + offset_x, 3), round(original_y + offset_y, 3)])
    return output


@dataclass(frozen=True)
class Variant:
    id: str
    rgb: np.ndarray
    rotation: int
    x: int
    y: int
    width: int
    height: int


def _variants(path: Path, config: Mapping[str, Any]) -> Iterable[Variant]:
    tile_size = int(config["tile_size"])
    overlap = int(config["tile_overlap"])
    with Image.open(path) as loaded:
        rgba = loaded.convert("RGBA")
        alpha = rgba.getchannel("A")
        backgrounds = (
            ["rgb"]
            if config.get("alpha_semantics") == "material"
            else ["opaque"]
            if alpha.getextrema() == (255, 255)
            else config["alpha_backgrounds"]
        )
        for background in backgrounds:
            if background in {"opaque", "rgb"}:
                rgb_image = rgba.convert("RGB")
            else:
                value = 255 if background == "white" else 0
                canvas = Image.new("RGBA", rgba.size, (value, value, value, 255))
                rgb_image = Image.alpha_composite(canvas, rgba).convert("RGB")
            xs = _positions(rgb_image.width, tile_size, overlap)
            ys = _positions(rgb_image.height, tile_size, overlap)
            for y in ys:
                for x in xs:
                    tile = rgb_image.crop(
                        (x, y, min(x + tile_size, rgb_image.width), min(y + tile_size, rgb_image.height))
                    )
                    base = np.asarray(tile, dtype=np.uint8)
                    for angle in config["rotations_clockwise"]:
                        yield Variant(
                            id=f"{background}:x{x}:y{y}:r{angle}",
                            rgb=_rotate_clockwise(base, int(angle)),
                            rotation=int(angle),
                            x=x,
                            y=y,
                            width=tile.width,
                            height=tile.height,
                        )


def _result_mapping(result: Any) -> Mapping[str, Any]:
    value = result.json
    if callable(value):
        value = value()
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("res", value)
    return nested if isinstance(nested, Mapping) else {}


def _script(text: str) -> str:
    names = [unicodedata.name(char, "") for char in text if char.isalpha()]
    found = []
    for label, token in (("korean", "HANGUL"), ("cyrillic", "CYRILLIC"), ("latin", "LATIN")):
        if any(token in name for name in names):
            found.append(label)
    return "+".join(found) if found else "other"


def _bbox(polygon: list[list[float]], image_size: tuple[int, int]) -> list[int]:
    width, height = image_size
    xs = [value[0] for value in polygon]
    ys = [value[1] for value in polygon]
    return [
        max(0, int(math.floor(min(xs)))),
        max(0, int(math.floor(min(ys)))),
        min(width, int(math.ceil(max(xs)))),
        min(height, int(math.ceil(max(ys)))),
    ]


def _normalize(text: str) -> str:
    return "".join(char.casefold() for char in unicodedata.normalize("NFC", text) if char.isalnum())


def _literal(text: str) -> str:
    """Normalize Unicode and newlines without discarding meaningful characters."""
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _region_plan_sha256(regions: Iterable[Mapping[str, Any]] | None) -> str | None:
    if regions is None:
        return None
    packed = json.dumps(
        list(regions),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _otsu_threshold(grayscale: np.ndarray) -> int:
    histogram = np.bincount(grayscale.reshape(-1), minlength=256).astype(np.float64)
    total = float(histogram.sum())
    if total == 0:
        return 127
    weighted_total = float(np.dot(np.arange(256, dtype=np.float64), histogram))
    background_weight = 0.0
    background_sum = 0.0
    best_variance = -1.0
    best_threshold = 127
    for threshold in range(255):
        background_weight += histogram[threshold]
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += threshold * histogram[threshold]
        background_mean = background_sum / background_weight
        foreground_mean = (weighted_total - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            best_threshold = threshold
    return best_threshold


def _oriented_region_variants(
    rgba: Image.Image,
    region: Mapping[str, Any],
    backgrounds: Iterable[str],
    *,
    alpha_semantics: str = "opacity",
) -> list[tuple[str, np.ndarray]]:
    bbox = region.get("bbox")
    rotation = region.get("rotation_deg", 0)
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(value, int) for value in bbox)
        or not isinstance(rotation, (int, float))
    ):
        raise ValueError("OCR 영역의 bbox/rotation_deg가 잘못됐어요")
    x0, y0, x1, y1 = bbox
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > rgba.width or y1 > rgba.height:
        raise ValueError("OCR 영역 bbox가 이미지 밖이에요")
    crop = rgba.crop((x0, y0, x1, y1))
    if float(rotation) % 360:
        crop = crop.rotate(
            float(rotation),
            expand=True,
            resample=Image.Resampling.BICUBIC,
        )
    scale = max(1, min(4, math.ceil(72 / max(1, crop.height))))
    if scale > 1:
        crop = crop.resize(
            (crop.width * scale, crop.height * scale),
            Image.Resampling.LANCZOS,
        )

    variants: list[tuple[str, np.ndarray]] = []
    alpha = crop.getchannel("A")
    selected_backgrounds = (
        ["rgb"]
        if alpha_semantics == "material"
        else ["opaque"]
        if alpha.getextrema() == (255, 255)
        else list(backgrounds)
    )
    for background in selected_backgrounds:
        if background in {"opaque", "rgb"}:
            rgb = crop.convert("RGB")
        else:
            value = 255 if background == "white" else 0
            canvas = Image.new("RGBA", crop.size, (value, value, value, 255))
            rgb = Image.alpha_composite(canvas, crop).convert("RGB")
        rgb_values = np.asarray(rgb, dtype=np.uint8)
        variants.append((f"{background}:rotation{float(rotation):g}:scale{scale}", rgb_values))
        grayscale = np.asarray(rgb.convert("L"), dtype=np.uint8)
        if int(grayscale.max()) > int(grayscale.min()):
            threshold = _otsu_threshold(grayscale)
            binary = np.where(grayscale <= threshold, 0, 255).astype(np.uint8)
            variants.append(
                (
                    f"{background}:rotation{float(rotation):g}:otsu{threshold}:scale{scale}",
                    np.repeat(binary[..., None], 3, axis=2),
                )
            )
    return variants


def _reading_geometry(value: Mapping[str, Any]) -> tuple[float, float, float, float]:
    polygon = value.get("polygon")
    if not isinstance(polygon, (list, tuple)) or not polygon:
        return (0.0, 0.0, 1.0, 1.0)
    points = [
        (float(point[0]), float(point[1]))
        for point in polygon
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if not points:
        return (0.0, 0.0, 1.0, 1.0)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        max(1.0, max(xs) - min(xs)),
        max(1.0, max(ys) - min(ys)),
    )


def _ordered_region_tokens(values: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    entries = [(value, _reading_geometry(value)) for value in values]
    entries.sort(key=lambda item: (item[1][1], item[1][0]))
    lines: list[dict[str, Any]] = []
    for value, geometry in entries:
        center_y = geometry[1]
        height = geometry[3]
        line = next(
            (
                current
                for current in lines
                if abs(center_y - float(current["center_y"]))
                <= max(height, float(current["height"])) * 0.6
            ),
            None,
        )
        if line is None:
            line = {"center_y": center_y, "height": height, "values": []}
            lines.append(line)
        values_in_line = line["values"]
        assert isinstance(values_in_line, list)
        values_in_line.append((value, geometry))
        count = len(values_in_line)
        line["center_y"] = (
            float(line["center_y"]) * (count - 1) + center_y
        ) / count
        line["height"] = max(float(line["height"]), height)
    lines.sort(key=lambda line: float(line["center_y"]))
    ordered: list[tuple[int, dict[str, Any]]] = []
    for line_index, line in enumerate(lines):
        values_in_line = line["values"]
        assert isinstance(values_in_line, list)
        values_in_line.sort(key=lambda item: item[1][0])
        ordered.extend((line_index, value) for value, _ in values_in_line)
    return ordered


def _store_exact_region_readings(
    readings: dict[tuple[str, str, str], dict[str, Any]],
    values: list[dict[str, Any]],
) -> None:
    """Store literal, layout-aware candidates from one OCR invocation."""
    valid = [value for value in values if _literal(str(value.get("text", ""))).strip()]
    ordered = _ordered_region_tokens(valid)
    candidates = [value for _, value in ordered]
    for start in range(len(ordered)):
        for end in range(start + 2, len(ordered) + 1):
            span = ordered[start:end]
            pieces = [str(span[0][1]["text"]).strip()]
            for (previous_line, _), (line_index, value) in zip(span, span[1:]):
                pieces.append("\n" if line_index != previous_line else " ")
                pieces.append(str(value["text"]).strip())
            combined_text = "".join(pieces)
            candidates.append(
                {
                    "text": combined_text,
                    "script": _script(combined_text),
                    "confidence": round(
                        min(float(value["confidence"]) for _, value in span), 6
                    ),
                    "engine": span[0][1]["engine"],
                    "model_signature": span[0][1]["model_signature"],
                    "variant": span[0][1]["variant"],
                    "composite": True,
                    "components": [str(value["text"]) for _, value in span],
                    "layout_separator": "line-aware",
                }
            )
    for value in candidates:
        literal = _literal(str(value["text"]))
        key = (str(value["model_signature"]), str(value["variant"]), literal)
        if key not in readings or float(value["confidence"]) > float(
            readings[key]["confidence"]
        ):
            readings[key] = value


def _iou(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    if not intersection:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / max(1, left_area + right_area - intersection)


def _intersection_over_smaller(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    left_area = max(1, (left[2] - left[0]) * (left[3] - left[1]))
    right_area = max(1, (right[2] - right[0]) * (right[3] - right[1]))
    return intersection / min(left_area, right_area)


def _same_text_region(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if _iou(left["bbox"], right["bbox"]) >= 0.55:
        return True
    left_text = _normalize(str(left.get("text", "")))
    right_text = _normalize(str(right.get("text", "")))
    return (
        bool(left_text and right_text)
        and (left_text in right_text or right_text in left_text)
        and _intersection_over_smaller(left["bbox"], right["bbox"]) >= 0.85
    )


def _consensus_detection(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    readings: dict[str, list[dict[str, Any]]] = {}
    for item in cluster:
        normalized = _normalize(str(item.get("text", "")))
        if normalized:
            readings.setdefault(normalized, []).append(item)
    if not readings:
        return dict(max(cluster, key=lambda value: value["confidence"]))

    def reading_score(value: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, float, int]:
        normalized, items = value
        return (
            len({item["model_signature"] for item in items}),
            len({item["engine"] for item in items}),
            max(float(item["confidence"]) for item in items),
            len(normalized),
        )

    _, agreed_items = max(readings.items(), key=reading_score)
    orientations: dict[int, list[dict[str, Any]]] = {}
    for item in agreed_items:
        orientations.setdefault(int(item["rotation_deg"]), []).append(item)

    def orientation_score(value: tuple[int, list[dict[str, Any]]]) -> tuple[int, int, float, int]:
        rotation, items = value
        return (
            len({item["model_signature"] for item in items}),
            len({item["engine"] for item in items}),
            max(float(item["confidence"]) for item in items),
            -rotation,
        )

    _, oriented_items = max(orientations.items(), key=orientation_score)
    return dict(max(oriented_items, key=lambda value: value["confidence"]))


def _deduplicate(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: list[list[dict[str, Any]]] = []
    for detection in sorted(detections, key=lambda value: -value["confidence"]):
        cluster = next(
            (
                current
                for current in clusters
                if any(_same_text_region(item, detection) for item in current)
            ),
            None,
        )
        if cluster is None:
            clusters.append([detection])
        else:
            cluster.append(detection)
    chosen: list[dict[str, Any]] = []
    for cluster in clusters:
        best = _consensus_detection(cluster)
        alternatives: dict[tuple[str, str], dict[str, Any]] = {}
        for item in cluster:
            key = (item["model_signature"], _normalize(item["text"]))
            if key not in alternatives or item["confidence"] > alternatives[key]["confidence"]:
                alternatives[key] = {
                    "text": item["text"],
                    "confidence": item["confidence"],
                    "engine": item["engine"],
                    "model_signature": item["model_signature"],
                    "rotation_deg": item["rotation_deg"],
                }
        best["alternatives"] = sorted(
            alternatives.values(), key=lambda value: -value["confidence"]
        )[:8]
        conflict_floor = max(0.45, float(best["confidence"]) * 0.65)
        normalized_readings = {
            _normalize(item["text"])
            for item in best["alternatives"]
            if _normalize(item["text"]) and float(item["confidence"]) >= conflict_floor
        }
        agreeing_engines = {
            item["engine"]
            for item in best["alternatives"]
            if _normalize(item["text"]) == _normalize(best["text"])
        }
        best["engine_agreement"] = len(agreeing_engines) >= 2
        best["conflicting_readings"] = len(normalized_readings) > 1
        best["review_state"] = (
            "agreement"
            if best["engine_agreement"] and not best["conflicting_readings"]
            else "conflict"
            if best["conflicting_readings"]
            else "single_engine"
        )
        chosen.append(best)
    chosen = sorted(chosen, key=lambda value: (value["bbox"][1], value["bbox"][0]))
    for index, detection in enumerate(chosen, 1):
        detection["region_id"] = f"ocr-{index:03d}"
    return chosen


def _store_region_readings(
    readings: dict[tuple[str, str], dict[str, Any]],
    values: list[dict[str, Any]],
) -> None:
    """Keep tokens and their ordered composite from one OCR model invocation."""
    valid = [value for value in values if _normalize(str(value.get("text", "")))]
    candidates = list(valid)
    if len(valid) > 1:
        combined_text = " ".join(str(value["text"]).strip() for value in valid)
        candidates.append(
            {
                "text": combined_text,
                "script": _script(combined_text),
                "confidence": round(
                    min(float(value["confidence"]) for value in valid), 6
                ),
                "engine": valid[0]["engine"],
                "model_signature": valid[0]["model_signature"],
                "variant": valid[0]["variant"],
                "composite": True,
                "components": [str(value["text"]) for value in valid],
            }
        )
    for value in candidates:
        key = (str(value["model_signature"]), _normalize(str(value["text"])))
        if key not in readings or float(value["confidence"]) > float(
            readings[key]["confidence"]
        ):
            readings[key] = value


def run_ocr(
    project_root: Path,
    image_path: Path,
    output_path: Path,
    *,
    phase: str,
    regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    session = OcrSession(project_root, phase=phase)
    return session.run(image_path, output_path, regions=regions)


class OcrSession:
    """OCR 모델을 한 번만 올려 여러 텍스처를 같은 설정으로 판독해요."""

    def __init__(self, project_root: Path, *, phase: str):
        if phase not in {"source", "candidate"}:
            raise ValueError("OCR phase는 source 또는 candidate여야 해요")
        self.project_root = project_root
        self.phase = phase
        self.config = load_ocr_config(project_root)
        self.recognizers = self.config[f"{phase}_recognizers"]
        self.pipelines = [
            (
                recognizer,
                _paddle_pipeline(
                    self.config["detector"], recognizer, self.config.get("device", "cpu")
                ),
            )
            for recognizer in self.recognizers
        ]
        try:
            import easyocr
        except ImportError as exc:
            raise OcrUnavailable("EasyOCR가 OCR 전용 환경에 설치되지 않았어요") from exc
        easy_root = _easy_cache(project_root)
        self.readers = [
            (
                tuple(languages),
                easyocr.Reader(
                    languages,
                    gpu=False,
                    model_storage_directory=str(easy_root),
                    download_enabled=False,
                    verbose=False,
                ),
            )
            for languages in self.config["easyocr_languages"][phase]
        ]

    @property
    def engine_signature(self) -> dict[str, Any]:
        return {
            "paddleocr": _package_version("paddleocr"),
            "paddlepaddle": _package_version("paddlepaddle"),
            "easyocr": _package_version("easyocr"),
            "torch": _package_version("torch"),
            "detector": self.config["detector"],
            "recognizers": self.recognizers,
            "config_sha256": _sha256(
                self.project_root / "profiles" / "food" / "ocr.json"
            ),
        }

    def run(
        self,
        image_path: Path,
        output_path: Path,
        *,
        regions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return _run_ocr_session(self, image_path, output_path, regions=regions)


def reusable_ocr_report(
    session: OcrSession,
    image_path: Path,
    output_path: Path,
    *,
    regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if not regions:
        return None
    if not output_path.is_file():
        return None
    try:
        report = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "schema_version": 1,
        "phase": session.phase,
        "image_sha256": _sha256(image_path),
        "engine_signature": session.engine_signature,
        "region_plan_sha256": _region_plan_sha256(regions),
        "recognition_contract": "approved-regions+nfc-literal-v1",
        "scope": "approved-regions",
        "status": "completed",
        "errors": [],
    }
    if all(report.get(key) == value for key, value in expected.items()):
        return report
    return None


def _run_ocr_session(
    session: OcrSession,
    image_path: Path,
    output_path: Path,
    *,
    regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    phase = session.phase
    config = session.config
    recognizers = session.recognizers
    pipelines = session.pipelines
    readers = session.readers
    with Image.open(image_path) as source_file:
        rgba = source_file.convert("RGBA")
        image_size = rgba.size
    if not isinstance(regions, list) or not regions:
        raise ValueError(
            "OCR은 비전 판독으로 승인된 대상 영역에만 실행할 수 있어요"
        )
    minimum = float(config["minimum_confidence"])
    scoped_detections: list[dict[str, Any]] = []
    errors: list[str] = []
    variant_count = 0
    region_ocr: list[dict[str, Any]] = []
    for index, region in enumerate(regions or []):
        region_id = str(region.get("region_id", f"region-{index + 1:03d}"))
        expected_field = "final_text_ko" if phase == "candidate" else "text"
        expected_text = _literal(str(region.get(expected_field, "")))
        if not expected_text:
            raise ValueError(f"OCR 영역 {region_id}의 확정 문자열이 비어 있어요")
        readings: dict[tuple[str, str], dict[str, Any]] = {}
        exact_readings: dict[tuple[str, str, str], dict[str, Any]] = {}
        region_errors: list[str] = []
        try:
            variants = _oriented_region_variants(
                rgba,
                region,
                config.get("alpha_backgrounds", ["white", "black"]),
                alpha_semantics=str(config.get("alpha_semantics", "opacity")),
            )
        except Exception as exc:
            variants = []
            region_errors.append(f"prepare: {type(exc).__name__}: {exc}")
        variant_count += len(variants)
        for variant_id, rgb in variants:
            bgr = rgb[..., ::-1].copy()
            for recognizer, pipeline in pipelines:
                try:
                    for result in pipeline.predict(bgr):
                        data = _result_mapping(result)
                        pass_readings: list[dict[str, Any]] = []
                        for text, score, polygon in zip(
                            list(data.get("rec_texts", [])),
                            list(data.get("rec_scores", [])),
                            list(data.get("rec_polys", data.get("dt_polys", []))),
                        ):
                            confidence = float(score)
                            if confidence < minimum or not _literal(str(text)).strip():
                                continue
                            pass_readings.append(
                                {
                                    "text": _literal(str(text)),
                                    "script": _script(str(text)),
                                    "confidence": round(confidence, 6),
                                    "engine": "paddleocr",
                                    "model_signature": (
                                        f"{config['detector']}+{recognizer}"
                                    ),
                                    "variant": variant_id,
                                    "polygon": [
                                        [float(point[0]), float(point[1])]
                                        for point in polygon
                                    ],
                                }
                            )
                        _store_region_readings(readings, pass_readings)
                        _store_exact_region_readings(exact_readings, pass_readings)
                except Exception as exc:
                    region_errors.append(
                        f"paddle/{recognizer}/{variant_id}: {type(exc).__name__}: {exc}"
                    )
            for languages, reader in readers:
                try:
                    pass_readings = []
                    for polygon, text, score in reader.readtext(
                        rgb,
                        detail=1,
                        paragraph=False,
                        min_size=8,
                        workers=0,
                    ):
                        confidence = float(score)
                        if confidence < minimum or not _literal(str(text)).strip():
                            continue
                        signature = f"easyocr-1.7.2:{'+'.join(languages)}"
                        pass_readings.append(
                            {
                                "text": _literal(str(text)),
                                "script": _script(str(text)),
                                "confidence": round(confidence, 6),
                                "engine": "easyocr",
                                "model_signature": signature,
                                "variant": variant_id,
                                "polygon": [
                                    [float(point[0]), float(point[1])]
                                    for point in polygon
                                ],
                            }
                        )
                    _store_region_readings(readings, pass_readings)
                    _store_exact_region_readings(exact_readings, pass_readings)
                except Exception as exc:
                    region_errors.append(
                        f"easyocr/{'+'.join(languages)}/{variant_id}: {type(exc).__name__}: {exc}"
                    )
        ordered = sorted(exact_readings.values(), key=lambda value: -float(value["confidence"]))
        matching = [
            value for value in ordered if _literal(str(value["text"])) == expected_text
        ]
        best_by_model: dict[tuple[str, str], dict[str, Any]] = {}
        for value in ordered:
            key = (str(value["engine"]), str(value["model_signature"]))
            if key not in best_by_model:
                best_by_model[key] = value
        for value in best_by_model.values():
            scoped_detections.append(
                {
                    "region_id": region_id,
                    "text": value["text"],
                    "script": value["script"],
                    "confidence": value["confidence"],
                    "engine": value["engine"],
                    "model_signature": value["model_signature"],
                    "variant": value["variant"],
                    "bbox": region.get("bbox"),
                    "rotation_deg": float(region.get("rotation_deg", 0)),
                    "direction": region.get("direction"),
                    "scope": "approved-region",
                }
            )
        region_ocr.append(
            {
                "region_id": region_id,
                "expected_text": expected_text,
                "bbox": region.get("bbox"),
                "rotation_deg": float(region.get("rotation_deg", 0)),
                "direction": region.get("direction"),
                "deskew_rotation_deg": float(region.get("rotation_deg", 0)),
                "inverse_rotation_deg": -float(region.get("rotation_deg", 0)),
                "matched": bool(matching),
                "match_mode": "nfc-literal",
                "matching_engines": sorted({value["engine"] for value in matching}),
                "readings": ordered[:16],
                "errors": region_errors,
            }
        )
        errors.extend(f"region/{region_id}/{error}" for error in region_errors)
    if not scoped_detections and errors:
        raise RuntimeError("모든 OCR 호출이 실패했어요: " + " | ".join(errors[:3]))
    report = {
        "schema_version": 1,
        "phase": phase,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image": str(image_path.resolve()),
        "image_sha256": _sha256(image_path),
        "width": image_size[0],
        "height": image_size[1],
        "engine_signature": session.engine_signature,
        "region_plan_sha256": _region_plan_sha256(regions),
        "variant_count": variant_count,
        "recognition_contract": "approved-regions+nfc-literal-v1",
        "scope": "approved-regions",
        "detections": scoped_detections,
        "region_ocr": region_ocr,
        "errors": errors,
        "requires_independent_visual_review": True,
        "status": "error" if errors else "completed",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
