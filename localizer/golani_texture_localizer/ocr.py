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
        backgrounds = ["opaque"] if alpha.getextrema() == (255, 255) else config["alpha_backgrounds"]
        for background in backgrounds:
            if background == "opaque":
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


def _iou(left: list[int], right: list[int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    if not intersection:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / max(1, left_area + right_area - intersection)


def _deduplicate(detections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: list[list[dict[str, Any]]] = []
    for detection in sorted(detections, key=lambda value: -value["confidence"]):
        cluster = next(
            (
                current
                for current in clusters
                if any(_iou(item["bbox"], detection["bbox"]) >= 0.55 for item in current)
            ),
            None,
        )
        if cluster is None:
            clusters.append([detection])
        else:
            cluster.append(detection)
    chosen: list[dict[str, Any]] = []
    for cluster in clusters:
        best = dict(max(cluster, key=lambda value: value["confidence"]))
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


def run_ocr(
    project_root: Path,
    image_path: Path,
    output_path: Path,
    *,
    phase: str,
) -> dict[str, Any]:
    if phase not in {"source", "candidate"}:
        raise ValueError("OCR phase는 source 또는 candidate여야 해요")
    config = load_ocr_config(project_root)
    recognizers = config[f"{phase}_recognizers"]
    pipelines = [
        (recognizer, _paddle_pipeline(config["detector"], recognizer, config.get("device", "cpu")))
        for recognizer in recognizers
    ]
    try:
        import easyocr
    except ImportError as exc:
        raise OcrUnavailable("EasyOCR가 OCR 전용 환경에 설치되지 않았어요") from exc
    easy_root = _easy_cache(project_root)
    readers = [
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
        for languages in config["easyocr_languages"][phase]
    ]
    with Image.open(image_path) as source_file:
        image_size = source_file.size
    minimum = float(config["minimum_confidence"])
    detections: list[dict[str, Any]] = []
    errors: list[str] = []
    variant_count = 0
    for variant in _variants(image_path, config):
        variant_count += 1
        bgr = variant.rgb[..., ::-1].copy()
        for recognizer, pipeline in pipelines:
            try:
                for result in pipeline.predict(bgr):
                    data = _result_mapping(result)
                    texts = list(data.get("rec_texts", []))
                    scores = list(data.get("rec_scores", []))
                    polygons = list(data.get("rec_polys", data.get("dt_polys", [])))
                    for text, score, polygon in zip(texts, scores, polygons):
                        confidence = float(score)
                        if confidence < minimum or not str(text).strip():
                            continue
                        mapped = _inverse_points(
                            polygon, variant.rotation, variant.width, variant.height, variant.x, variant.y
                        )
                        detections.append(
                            {
                                "text": unicodedata.normalize("NFC", str(text)),
                                "script": _script(str(text)),
                                "confidence": round(confidence, 6),
                                "engine": "paddleocr",
                                "model_signature": f"{config['detector']}+{recognizer}",
                                "variant": variant.id,
                                "polygon": mapped,
                                "bbox": _bbox(mapped, image_size),
                                "rotation_deg": (360 - variant.rotation) % 360,
                                "direction": "left-to-right",
                                "face": "unreviewed",
                                "artwork_direction": "unreviewed",
                            }
                        )
            except Exception as exc:
                errors.append(f"paddle/{recognizer}/{variant.id}: {type(exc).__name__}: {exc}")
        for languages, reader in readers:
            try:
                for polygon, text, score in reader.readtext(
                    variant.rgb,
                    detail=1,
                    paragraph=False,
                    min_size=8,
                    workers=0,
                ):
                    confidence = float(score)
                    if confidence < minimum or not str(text).strip():
                        continue
                    mapped = _inverse_points(
                        polygon, variant.rotation, variant.width, variant.height, variant.x, variant.y
                    )
                    detections.append(
                        {
                            "text": unicodedata.normalize("NFC", str(text)),
                            "script": _script(str(text)),
                            "confidence": round(confidence, 6),
                            "engine": "easyocr",
                            "model_signature": f"easyocr-1.7.2:{'+'.join(languages)}",
                            "variant": variant.id,
                            "polygon": mapped,
                            "bbox": _bbox(mapped, image_size),
                            "rotation_deg": (360 - variant.rotation) % 360,
                            "direction": "left-to-right",
                            "face": "unreviewed",
                            "artwork_direction": "unreviewed",
                        }
                    )
            except Exception as exc:
                errors.append(f"easyocr/{'+'.join(languages)}/{variant.id}: {type(exc).__name__}: {exc}")
    compact = _deduplicate(detections)
    if not compact and errors:
        raise RuntimeError("모든 OCR 호출이 실패했어요: " + " | ".join(errors[:3]))
    report = {
        "schema_version": 1,
        "phase": phase,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image": str(image_path.resolve()),
        "image_sha256": _sha256(image_path),
        "width": image_size[0],
        "height": image_size[1],
        "engine_signature": {
            "paddleocr": _package_version("paddleocr"),
            "paddlepaddle": _package_version("paddlepaddle"),
            "easyocr": _package_version("easyocr"),
            "torch": _package_version("torch"),
            "detector": config["detector"],
            "recognizers": recognizers,
        },
        "variant_count": variant_count,
        "detections": compact,
        "errors": errors,
        "requires_independent_visual_review": True,
        "status": "error" if errors else "completed",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
