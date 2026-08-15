from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
from pathlib import Path
from typing import Any, Mapping, Protocol

from .config import PROJECT_ROOT, digest, resolve_project_path
from .scoring import Detection, normalize_confidence


class EngineUnavailable(RuntimeError):
    pass


class OcrEngine(Protocol):
    name: str

    @property
    def signature(self) -> str: ...

    def prepare(self) -> None: ...

    def recognize(self, rgb: Any, variant_id: str) -> list[Detection]: ...


def package_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def package_version_any(*distributions: str) -> str | None:
    for distribution in distributions:
        version = package_version(distribution)
        if version:
            return version
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def model_directory_ready(path: Path | None) -> bool:
    return bool(
        path
        and path.is_dir()
        and any(item.is_file() and item.stat().st_size > 0 for item in path.rglob("*"))
    )


def paddle_model_directory_ready(path: Path | None) -> bool:
    """Validate the two artifacts required by a Paddle inference model.

    Paddle's current PIR export uses ``inference.json`` plus
    ``inference.pdiparams``; older exports use ``inference.pdmodel`` with the
    same parameter file.  A stray README or half-finished download must not be
    treated as an offline-ready model.
    """

    if not path or not path.is_dir():
        return False
    parameters = path / "inference.pdiparams"
    programs = (path / "inference.json", path / "inference.pdmodel")
    return (
        parameters.is_file()
        and parameters.stat().st_size > 0
        and any(program.is_file() and program.stat().st_size > 0 for program in programs)
    )


def model_tree_digest(path: Path | None) -> str:
    if not model_directory_ready(path):
        return "missing"
    assert path is not None
    hasher = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        hasher.update(item.relative_to(path).as_posix().encode("utf-8"))
        hasher.update(b"\0")
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


class PaddleOcrEngine:
    name = "paddleocr"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        allow_model_download: bool = False,
        project_root: Path = PROJECT_ROOT,
    ):
        self.config = dict(config)
        self.allow_model_download = allow_model_download
        self.project_root = project_root
        self.detector_dir = resolve_project_path(config.get("detector_dir"), project_root)
        self.recognizer_dir = resolve_project_path(config.get("recognizer_dir"), project_root)
        default_root = Path.home() / ".paddlex" / "official_models"
        if not paddle_model_directory_ready(self.detector_dir):
            cached = default_root / str(config.get("detector_model", ""))
            if paddle_model_directory_ready(cached):
                self.detector_dir = cached
        if not paddle_model_directory_ready(self.recognizer_dir):
            cached = default_root / str(config.get("recognizer_model", ""))
            if paddle_model_directory_ready(cached):
                self.recognizer_dir = cached
        self._ocr: Any = None
        self._signature_cache: str | None = None

    @property
    def signature(self) -> str:
        if self._signature_cache is not None:
            return self._signature_cache
        value = {
            "name": self.name,
            "package": package_version("paddleocr") or "missing",
            "paddle": package_version_any("paddlepaddle", "paddlepaddle-gpu") or "missing",
            "detector_model": self.config.get("detector_model"),
            "recognizer_model": self.config.get("recognizer_model"),
            "model_revision": self.config.get("model_revision", "official"),
            "detector_weights": model_tree_digest(self.detector_dir),
            "recognizer_weights": model_tree_digest(self.recognizer_dir),
            "device": self.config.get("device", "cpu"),
            "engine": self.config.get("inference_engine", "paddle_static"),
        }
        self._signature_cache = f"paddleocr:{digest(value)}"
        return self._signature_cache

    def prepare(self) -> None:
        self._load()

    def _load(self) -> Any:
        if self._ocr is not None:
            return self._ocr
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise EngineUnavailable(
                "PaddleOCR가 설치되지 않았습니다. doctor와 docs/ocr-selection.md를 확인하세요."
            ) from exc

        local_models = paddle_model_directory_ready(
            self.detector_dir
        ) and paddle_model_directory_ready(self.recognizer_dir)
        if not local_models and not self.allow_model_download:
            raise EngineUnavailable(
                "PaddleOCR 로컬 모델이 없습니다. 모델 폴더를 준비하거나 "
                "scan에 --allow-model-download를 명시하세요."
            )

        kwargs: dict[str, Any] = {
            "text_detection_model_name": self.config["detector_model"],
            "text_recognition_model_name": self.config["recognizer_model"],
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
            "text_det_thresh": float(self.config.get("text_det_thresh", 0.2)),
            "text_det_box_thresh": float(self.config.get("text_det_box_thresh", 0.35)),
            "text_rec_score_thresh": float(self.config.get("text_rec_score_thresh", 0.0)),
            "engine": self.config.get("inference_engine", "paddle_static"),
            "device": self.config.get("device", "cpu"),
        }
        if local_models:
            kwargs["text_detection_model_dir"] = str(self.detector_dir)
            kwargs["text_recognition_model_dir"] = str(self.recognizer_dir)
        self._ocr = PaddleOCR(**kwargs)
        # Explicit local directories above are the weights actually passed to
        # PaddleOCR.  Keep those paths for the cache signature even when a
        # different copy also exists in PaddleX's default cache.  Only a
        # name-based load/download can switch the resolved paths to that cache.
        if not local_models:
            default_root = Path.home() / ".paddlex" / "official_models"
            cached_detector = default_root / str(self.config.get("detector_model", ""))
            cached_recognizer = default_root / str(self.config.get("recognizer_model", ""))
            if paddle_model_directory_ready(cached_detector):
                self.detector_dir = cached_detector
            if paddle_model_directory_ready(cached_recognizer):
                self.recognizer_dir = cached_recognizer
        self._signature_cache = None
        return self._ocr

    def recognize(self, rgb: Any, variant_id: str) -> list[Detection]:
        ocr = self._load()
        detections: list[Detection] = []
        # PaddleX 3.5 treats NumPy input as already decoded and its OCR pipeline
        # expects BGR, so convert explicitly (the shared preprocessor emits RGB).
        bgr = rgb[:, :, ::-1].copy()
        for result in ocr.predict(bgr):
            payload = result.json
            if callable(payload):
                payload = payload()
            if not isinstance(payload, Mapping):
                continue
            data = payload.get("res", payload)
            if not isinstance(data, Mapping):
                continue
            texts = _as_list(data.get("rec_texts"))
            scores = _as_list(data.get("rec_scores"))
            polygons = _as_list(data.get("rec_polys"))
            if not polygons and not texts:
                polygons = _as_list(data.get("dt_polys"))
                scores = _as_list(data.get("dt_scores"))
            for text, score, polygon in itertools.zip_longest(
                texts, scores, polygons, fillvalue=None
            ):
                if text is None and polygon is None:
                    continue
                detections.append(
                    Detection(
                        text="" if text is None else str(text),
                        confidence=normalize_confidence(score),
                        polygon=polygon or (),
                        engine=self.name,
                        variant=variant_id,
                    )
                )
        return detections


class EasyOcrEngine:
    name = "easyocr"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        allow_model_download: bool = False,
        project_root: Path = PROJECT_ROOT,
    ):
        self.config = dict(config)
        self.allow_model_download = allow_model_download
        self.project_root = project_root
        self.model_dir = resolve_project_path(config.get("model_dir"), project_root)
        self._reader: Any = None
        self._signature_cache: str | None = None

    @property
    def signature(self) -> str:
        if self._signature_cache is not None:
            return self._signature_cache
        value = {
            "name": self.name,
            "package": package_version("easyocr") or "missing",
            "torch": package_version("torch") or "missing",
            "detector_file": self.config.get("detector_file"),
            "recognizer_file": self.config.get("recognizer_file"),
            "model_revision": self.config.get("model_revision", "official"),
            "model_weights": model_tree_digest(self.model_dir),
            "gpu": self.config.get("gpu", False),
            "languages": ["ru", "en"],
        }
        self._signature_cache = f"easyocr:{digest(value)}"
        return self._signature_cache

    def prepare(self) -> None:
        self._load()

    def _required_models_present(self) -> bool:
        if not self.model_dir or not self.model_dir.is_dir():
            return False
        return all(
            (self.model_dir / str(self.config[name])).is_file()
            for name in ("detector_file", "recognizer_file")
        )

    def _load(self) -> Any:
        if self._reader is not None:
            return self._reader
        try:
            import easyocr
        except ImportError as exc:
            raise EngineUnavailable(
                "EasyOCR가 설치되지 않았습니다. doctor와 docs/ocr-selection.md를 확인하세요."
            ) from exc
        if not self._required_models_present() and not self.allow_model_download:
            raise EngineUnavailable(
                "EasyOCR 로컬 모델이 없습니다. 모델 폴더를 준비하거나 "
                "scan에 --allow-model-download를 명시하세요."
            )
        if not self.model_dir:
            raise EngineUnavailable("EasyOCR model_dir 설정이 없습니다")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._reader = easyocr.Reader(
            ["ru", "en"],
            gpu=self.config.get("gpu", False),
            model_storage_directory=str(self.model_dir),
            download_enabled=self.allow_model_download,
            detect_network="craft",
            recog_network="standard",
            verbose=False,
        )
        self._signature_cache = None
        return self._reader

    def recognize(self, rgb: Any, variant_id: str) -> list[Detection]:
        reader = self._load()
        bgr = rgb[:, :, ::-1].copy()
        raw = reader.readtext(
            bgr,
            detail=1,
            paragraph=False,
            rotation_info=None,
            min_size=int(self.config.get("min_size", 8)),
            text_threshold=float(self.config.get("text_threshold", 0.45)),
            low_text=float(self.config.get("low_text", 0.25)),
            link_threshold=float(self.config.get("link_threshold", 0.25)),
            workers=0,
            output_format="standard",
        )
        detections: list[Detection] = []
        for value in raw:
            if not isinstance(value, (list, tuple)) or len(value) < 3:
                continue
            polygon, text, score = value[0], value[1], value[2]
            detections.append(
                Detection(
                    text=str(text),
                    confidence=normalize_confidence(score),
                    polygon=polygon,
                    engine=self.name,
                    variant=variant_id,
                )
            )
        return detections


def create_engine(
    config: Mapping[str, Any],
    *,
    allow_model_download: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> OcrEngine:
    name = config.get("name")
    if name == "paddleocr":
        return PaddleOcrEngine(
            config, allow_model_download=allow_model_download, project_root=project_root
        )
    if name == "easyocr":
        return EasyOcrEngine(
            config, allow_model_download=allow_model_download, project_root=project_root
        )
    raise ValueError(f"지원하지 않는 OCR 엔진입니다: {name}")


def create_configured_engines(
    config: Mapping[str, Any],
    *,
    allow_model_download: bool = False,
    project_root: Path = PROJECT_ROOT,
) -> tuple[OcrEngine, OcrEngine | None]:
    engines = config["engines"]
    primary = create_engine(
        engines["primary"],
        allow_model_download=allow_model_download,
        project_root=project_root,
    )
    fallback_config = engines.get("fallback")
    fallback = None
    if fallback_config and fallback_config.get("enabled", True):
        fallback = create_engine(
            fallback_config,
            allow_model_download=allow_model_download,
            project_root=project_root,
        )
    return primary, fallback


def combined_signature(primary: OcrEngine, fallback: OcrEngine | None) -> str:
    return "|".join(
        value for value in (primary.signature, fallback.signature if fallback else None) if value
    )
