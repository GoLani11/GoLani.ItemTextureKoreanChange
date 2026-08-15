from __future__ import annotations

import copy
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .cache import ResultCache
from .config import PROJECT_ROOT, ocr_profile_digest
from .engines import OcrEngine, combined_signature
from .manifest import AssetSource, Discovery, color_filter_reason
from .preprocess import ImageFingerprint, fingerprint_image, iter_variants, make_preview
from .scoring import (
    Detection,
    classify_detections,
    compact_detections,
    make_asset_id,
    make_cache_key,
)


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class Plan:
    total_sources: int
    color_candidates: int
    skipped_non_color: int
    missing_sources: int
    duplicates_ignored: int
    asset_types: dict[str, int]
    skip_reasons: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_sources": self.total_sources,
            "color_candidates": self.color_candidates,
            "skipped_non_color": self.skipped_non_color,
            "missing_sources": self.missing_sources,
            "duplicates_ignored": self.duplicates_ignored,
            "asset_types": self.asset_types,
            "skip_reasons": self.skip_reasons,
        }


def build_plan(discovery: Discovery, config: Mapping[str, Any]) -> Plan:
    type_counts: Counter[str] = Counter()
    skip_counts: Counter[str] = Counter()
    candidates = 0
    for source in discovery.sources:
        type_counts[str(source.metadata.get("asset_type", "unknown"))] += 1
        reason = color_filter_reason(source, config["filter"])
        if reason:
            skip_counts[reason] += 1
        else:
            candidates += 1
    return Plan(
        total_sources=len(discovery.sources),
        color_candidates=candidates,
        skipped_non_color=sum(skip_counts.values()),
        missing_sources=len(discovery.missing),
        duplicates_ignored=discovery.duplicates_ignored,
        asset_types=dict(sorted(type_counts.items())),
        skip_reasons=dict(sorted(skip_counts.items())),
    )


def _classification_error() -> dict[str, Any]:
    return {
        "tier": "error",
        "score": 0.0,
        "scripts": [],
        "target_letter_count": 0,
        "engine_count": 0,
        "reason_codes": ["ocr_error"],
    }


def _run_engine(
    engine: OcrEngine,
    source: Path,
    preprocess_config: Mapping[str, Any],
    classification_config: Mapping[str, Any],
    detections: list[Detection],
    *,
    early_stop: bool,
) -> tuple[int, list[str]]:
    successes = 0
    errors: list[str] = []
    try:
        variants = iter_variants(source, preprocess_config)
        for variant in variants:
            try:
                detections.extend(engine.recognize(variant.rgb, variant.variant_id))
                successes += 1
            except Exception as exc:  # individual image/engine errors are report data
                errors.append(f"{engine.name}/{variant.variant_id}: {type(exc).__name__}: {exc}")
                break
            if early_stop:
                current = classify_detections(detections, classification_config)
                if current.tier == "confirmed":
                    break
    except Exception as exc:
        errors.append(f"{engine.name}: {type(exc).__name__}: {exc}")
    return successes, errors


def scan_one_image(
    source: Path,
    config: Mapping[str, Any],
    primary: OcrEngine,
    fallback: OcrEngine | None,
) -> dict[str, Any]:
    detections: list[Detection] = []
    errors: list[str] = []
    early_stop = bool(config.get("runtime", {}).get("early_stop_on_confirmed", True))
    primary_successes, primary_errors = _run_engine(
        primary,
        source,
        config["preprocess"],
        config["classification"],
        detections,
        early_stop=early_stop,
    )
    errors.extend(primary_errors)
    classification = classify_detections(detections, config["classification"])

    fallback_successes = 0
    if fallback is not None and classification.tier != "confirmed":
        fallback_successes, fallback_errors = _run_engine(
            fallback,
            source,
            config["preprocess"],
            config["classification"],
            detections,
            early_stop=early_stop,
        )
        errors.extend(fallback_errors)
        classification = classify_detections(detections, config["classification"])

    success_count = primary_successes + fallback_successes
    compacted = compact_detections(detections)
    if success_count == 0:
        return {
            "processing": {
                "status": "error",
                "error": " | ".join(errors) or "OCR 엔진이 결과를 반환하지 못했습니다",
                "warnings": [],
                "variant_calls": 0,
            },
            "classification": _classification_error(),
            "detections": [item.to_dict() for item in compacted],
        }
    if errors:
        processing = {
            "status": "error",
            "error": " | ".join(errors),
            "warnings": [],
            "variant_calls": success_count,
        }
    else:
        processing = {
            "status": "ok",
            "error": "",
            "warnings": [],
            "variant_calls": success_count,
        }
    return {
        "processing": processing,
        "classification": classification.to_dict(),
        "detections": [item.to_dict() for item in compacted],
    }


def _reference(source: AssetSource, fingerprint: ImageFingerprint | None = None) -> dict[str, Any]:
    value = source.to_dict(PROJECT_ROOT)
    if fingerprint:
        value["file_sha256"] = fingerprint.file_sha256
    return value


def _attach_reviews(
    results: list[dict[str, Any]], cache: ResultCache
) -> list[dict[str, Any]]:
    reviews = cache.get_reviews(row.get("asset_id", "") for row in results)
    for row in results:
        review = reviews.get(str(row.get("asset_id", "")))
        if review:
            row["review"] = review
        else:
            row["review"] = {
                "decision": None,
                "note": "",
                "reviewer": "",
                "reviewed_at": None,
            }
    return results


def scan_sources(
    discovery: Discovery,
    config: Mapping[str, Any],
    primary: OcrEngine,
    fallback: OcrEngine | None,
    cache: ResultCache,
    output_dir: str | Path,
    *,
    force: bool = False,
    limit: int | None = None,
    progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    output = Path(output_dir).resolve()
    preview_dir = output / "previews"
    results: list[dict[str, Any]] = []
    groups: dict[str, list[tuple[AssetSource, ImageFingerprint]]] = defaultdict(list)

    for source in discovery.sources:
        reason = color_filter_reason(source, config["filter"])
        if reason:
            results.append(
                {
                    "asset_id": source.source_id,
                    "representative_source": source.to_dict(PROJECT_ROOT)["path"],
                    "fingerprint": None,
                    "references": [_reference(source)],
                    "processing": {
                        "status": "skipped",
                        "error": "",
                        "warnings": [],
                        "reason": reason,
                        "variant_calls": 0,
                    },
                    "classification": {
                        "tier": "skipped",
                        "score": 0.0,
                        "scripts": [],
                        "target_letter_count": 0,
                        "engine_count": 0,
                        "reason_codes": [reason],
                    },
                    "detections": [],
                }
            )
            continue
        if limit is not None and len(groups) >= max(0, limit):
            # Calibration runs must not decode the entire corpus before honoring
            # their limit. Sources are deterministically sorted by discovery.
            break
        try:
            fingerprint = fingerprint_image(source.path)
        except Exception as exc:
            results.append(
                {
                    "asset_id": source.source_id,
                    "representative_source": source.to_dict(PROJECT_ROOT)["path"],
                    "fingerprint": None,
                    "references": [_reference(source)],
                    "processing": {
                        "status": "error",
                        "error": f"이미지 디코딩 실패: {type(exc).__name__}: {exc}",
                        "warnings": [],
                        "variant_calls": 0,
                    },
                    "classification": _classification_error(),
                    "detections": [],
                }
            )
            continue
        groups[fingerprint.pixel_sha256].append((source, fingerprint))

    ordered_groups = sorted(groups.items(), key=lambda row: row[0])
    profile_digest = ocr_profile_digest(config)
    signature = combined_signature(primary, fallback)

    for index, (pixel_hash, members) in enumerate(ordered_groups, 1):
        members.sort(key=lambda row: (str(row[0].path).casefold(), row[0].source_id))
        representative, representative_fingerprint = members[0]
        asset_id = make_asset_id(pixel_hash)
        cache_key = make_cache_key(pixel_hash, signature, profile_digest)
        if progress:
            progress(f"[{index}/{len(ordered_groups)}] {representative.path.name}")

        cached = None if force else cache.get_result(cache_key)
        if cached is None:
            raw_result = scan_one_image(representative.path, config, primary, fallback)
            if raw_result["processing"]["status"] == "ok":
                cache.put_result(
                    cache_key,
                    pixel_hash,
                    profile_digest,
                    signature,
                    raw_result,
                )
            cache_state = "miss"
        else:
            raw_result = copy.deepcopy(cached)
            cache_state = "hit"

        result = copy.deepcopy(raw_result)
        result.update(
            {
                "asset_id": asset_id,
                "representative_source": representative.to_dict(PROJECT_ROOT)["path"],
                "fingerprint": representative_fingerprint.to_dict(),
                "references": [_reference(source, fingerprint) for source, fingerprint in members],
                "cache": {"key": cache_key, "state": cache_state},
            }
        )
        tier = result.get("classification", {}).get("tier")
        if result.get("processing", {}).get("status") in {"ok", "error"} and tier not in {
            "rejected",
            "error",
        }:
            preview_name = f"{asset_id}.png"
            preview_path = preview_dir / preview_name
            try:
                if force or not preview_path.is_file():
                    make_preview(
                        representative.path,
                        preview_path,
                        int(config.get("runtime", {}).get("preview_max_side", 720)),
                    )
                result["preview"] = f"previews/{preview_name}"
            except Exception as exc:
                result["processing"].setdefault("warnings", []).append(
                    f"미리보기 생성 실패: {type(exc).__name__}: {exc}"
                )
        results.append(result)

    return _attach_reviews(results, cache)
