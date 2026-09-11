from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


RESULT_SCHEMA_VERSION = 1
PREPROCESS_SCHEMA_VERSION = 1
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_WINDOWS_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_WHITESPACE = re.compile(r"\s+")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    return str(value)


def normalize_confidence(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, score))


@dataclass(frozen=True)
class Detection:
    text: str
    confidence: float
    polygon: Any = ()
    engine: str = ""
    variant: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Detection":
        return cls(
            text=unicodedata.normalize("NFC", str(value.get("text", ""))),
            confidence=normalize_confidence(
                value.get("confidence", value.get("score", 0.0))
            ),
            polygon=_json_safe(value.get("polygon", value.get("box", ()))),
            engine=str(value.get("engine", "")),
            variant=str(value.get("variant", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "polygon": _json_safe(self.polygon),
            "engine": self.engine,
            "variant": self.variant,
        }


@dataclass(frozen=True)
class ScriptEvidence:
    latin: int = 0
    cyrillic: int = 0

    @property
    def total(self) -> int:
        return self.latin + self.cyrillic

    @property
    def scripts(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.latin:
            names.append("latin")
        if self.cyrillic:
            names.append("cyrillic")
        return tuple(names)


@dataclass(frozen=True)
class Classification:
    tier: str
    score: float
    scripts: tuple[str, ...]
    target_letter_count: int
    engine_count: int
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "score": self.score,
            "scripts": list(self.scripts),
            "target_letter_count": self.target_letter_count,
            "engine_count": self.engine_count,
            "reason_codes": list(self.reason_codes),
        }


def detect_scripts(text: str) -> ScriptEvidence:
    latin = 0
    cyrillic = 0
    for char in unicodedata.normalize("NFC", str(text)):
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" in name:
            latin += 1
        elif "CYRILLIC" in name:
            cyrillic += 1
    return ScriptEvidence(latin=latin, cyrillic=cyrillic)


def _agreement_text(text: str) -> str:
    return "".join(
        char.casefold()
        for char in unicodedata.normalize("NFC", str(text))
        if char.isalnum()
    )


def _has_engine_agreement(
    rows: Sequence[tuple[Detection, ScriptEvidence]],
    thresholds: Mapping[str, Any],
) -> bool:
    minimum_score = float(thresholds["agreement_score"])
    minimum_similarity = float(thresholds.get("agreement_text_similarity", 0.8))
    eligible = [
        detection
        for detection, evidence in rows
        if evidence.total
        and detection.engine
        and detection.confidence >= minimum_score
        and _agreement_text(detection.text)
    ]
    for index, left in enumerate(eligible):
        left_text = _agreement_text(left.text)
        for right in eligible[index + 1 :]:
            if left.engine == right.engine:
                continue
            right_text = _agreement_text(right.text)
            similarity = SequenceMatcher(None, left_text, right_text).ratio()
            if similarity >= minimum_similarity:
                return True
    return False


def classify_detections(
    detections: Iterable[Detection | Mapping[str, Any]],
    thresholds: Mapping[str, Any],
) -> Classification:
    normalized = [
        item if isinstance(item, Detection) else Detection.from_mapping(item)
        for item in detections
    ]
    target_rows: list[tuple[Detection, ScriptEvidence]] = []
    for detection in normalized:
        evidence = detect_scripts(detection.text)
        if evidence.total:
            target_rows.append((detection, evidence))

    if not target_rows:
        detector_only = any(
            not item.text.strip()
            and item.confidence >= float(thresholds.get("review_score", 0.0))
            for item in normalized
        )
        if detector_only:
            return Classification(
                tier="needs_review",
                score=max((item.confidence for item in normalized), default=0.0),
                scripts=(),
                target_letter_count=0,
                engine_count=len({item.engine for item in normalized if item.engine}),
                reason_codes=("detector_only",),
            )
        return Classification(
            tier="rejected",
            score=0.0,
            scripts=(),
            target_letter_count=0,
            engine_count=len({item.engine for item in normalized if item.engine}),
            reason_codes=("no_latin_or_cyrillic",),
        )

    best_detection, best_evidence = max(
        target_rows,
        key=lambda row: (row[0].confidence, row[1].total, row[0].text),
    )
    scripts = tuple(
        script
        for script in ("latin", "cyrillic")
        if any(script in evidence.scripts for _, evidence in target_rows)
    )
    all_engines = {detection.engine for detection, _ in target_rows if detection.engine}
    score = best_detection.confidence
    letter_count = best_evidence.total
    reasons = ["target_script"]

    if (
        score >= float(thresholds["confirmed_score"])
        and letter_count >= int(thresholds["confirmed_min_letters"])
    ):
        tier = "confirmed"
        reasons.append("primary_high")
    elif _has_engine_agreement(target_rows, thresholds):
        tier = "confirmed"
        reasons.append("engine_agreement")
    elif (
        score >= float(thresholds["probable_score"])
        and letter_count >= int(thresholds["probable_min_letters"])
    ):
        tier = "probable"
        reasons.append("primary_medium")
    else:
        tier = "needs_review"
        reasons.append("weak_target_evidence")

    return Classification(
        tier=tier,
        score=score,
        scripts=scripts,
        target_letter_count=letter_count,
        engine_count=len(all_engines),
        reason_codes=tuple(reasons),
    )


def compact_detections(
    detections: Iterable[Detection | Mapping[str, Any]],
    max_items: int = 200,
) -> list[Detection]:
    """Keep the strongest duplicate OCR evidence from rotated/tiled variants."""

    normalized = [
        item if isinstance(item, Detection) else Detection.from_mapping(item)
        for item in detections
    ]
    normalized.sort(
        key=lambda item: (
            -int(detect_scripts(item.text).total > 0),
            -item.confidence,
            item.engine,
            item.text.casefold(),
            item.variant,
        )
    )
    selected: list[Detection] = []
    seen: set[tuple[str, str]] = set()
    for item in normalized:
        key = (item.engine, unicodedata.normalize("NFC", item.text).casefold())
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= max_items:
            break
    return selected


def file_sha256(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def make_asset_id(content_hash: str) -> str:
    return f"tex_{content_hash[:20]}"


def make_cache_key(
    pixel_hash: str,
    engine_signature: str,
    preprocess_digest: str,
) -> str:
    value = {
        "pixel_hash": pixel_hash,
        "engine_signature": engine_signature,
        "preprocess_digest": preprocess_digest,
        "result_schema": RESULT_SCHEMA_VERSION,
        "preprocess_schema": PREPROCESS_SCHEMA_VERSION,
    }
    packed = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(packed).hexdigest()


def sanitize_component(name: str, stable_id: str = "", max_length: int = 120) -> str:
    original = unicodedata.normalize("NFC", str(name))
    trailing_controls = "".join(chr(value) for value in range(32))
    trimmed = original.rstrip(" ." + trailing_controls)
    raw_suffix = Path(trimmed).suffix
    if (
        raw_suffix
        and len(raw_suffix) <= 16
        and not _INVALID_WINDOWS_CHARS.search(raw_suffix)
    ):
        suffix = raw_suffix
        raw_stem = trimmed[: -len(raw_suffix)]
    else:
        suffix = ""
        raw_stem = trimmed
    stem = _INVALID_WINDOWS_CHARS.sub("_", raw_stem)
    stem = _WHITESPACE.sub(" ", stem).strip(" .")
    if stem in {"", ".", ".."}:
        stem = "asset"
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    cleaned = f"{stem}{suffix}"

    changed = cleaned != original
    digest_source = stable_id or original
    short_hash = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:8]
    if len(cleaned) > max_length:
        keep = max(1, max_length - len(suffix) - len(short_hash) - 2)
        cleaned = f"{stem[:keep]}__{short_hash}{suffix}"
        changed = False
    elif changed:
        keep = max(1, max_length - len(suffix) - len(short_hash) - 2)
        cleaned = f"{stem[:keep]}__{short_hash}{suffix}"
    return cleaned


def safe_join(root: str | os.PathLike[str], *parts: str, stable_id: str = "") -> Path:
    root_path = Path(root).resolve()
    safe_parts: list[str] = []
    for part in parts:
        raw = str(part)
        if (
            Path(raw).is_absolute()
            or re.match(r"^[A-Za-z]:[\\/]", raw)
            or raw.startswith(("\\", "/"))
            or ".." in re.split(r"[\\/]", raw)
        ):
            raise ValueError(f"안전하지 않은 출력 경로 조각: {raw}")
        safe_parts.append(sanitize_component(raw, stable_id=stable_id))
    candidate = root_path.joinpath(*safe_parts).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"출력 경로가 루트를 벗어납니다: {candidate}") from exc
    return candidate
