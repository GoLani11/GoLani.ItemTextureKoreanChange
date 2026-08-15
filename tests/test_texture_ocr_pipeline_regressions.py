import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.texture_ocr.cache import ResultCache
from tools.texture_ocr.config import ocr_profile_digest
from tools.texture_ocr.engines import combined_signature
from tools.texture_ocr.manifest import AssetSource, Discovery
from tools.texture_ocr.pipeline import scan_one_image, scan_sources
from tools.texture_ocr.preprocess import (
    ImageFingerprint,
    PreparedVariant,
    VariantLimitExceeded,
)
from tools.texture_ocr.scoring import (
    Detection,
    classify_detections,
    make_cache_key,
)


CLASSIFICATION = {
    "confirmed_score": 0.80,
    "probable_score": 0.45,
    "review_score": 0.15,
    "agreement_score": 0.45,
    "confirmed_min_letters": 3,
    "probable_min_letters": 2,
}


def scan_config():
    return {
        "cache_schema": 1,
        "filter": {"skip_non_color": False},
        "preprocess": {"profile": "synthetic-v1"},
        "classification": dict(CLASSIFICATION),
        "engines": {"primary": {"name": "fake", "profile": "v1"}},
        "runtime": {
            "early_stop_on_confirmed": False,
            "preview_max_side": 64,
        },
    }


class FailingSecondVariantEngine:
    name = "fake"
    signature = "fake:v1"

    def __init__(self):
        self.calls = 0

    def recognize(self, rgb, variant_id):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second variant failed")
        return [
            Detection(
                "AB",
                0.50,
                engine=self.name,
                variant=variant_id,
            )
        ]


class SuccessfulEngine:
    name = "fake"
    signature = "fake:v1"

    def recognize(self, rgb, variant_id):
        return [
            Detection(
                "AB",
                0.50,
                engine=self.name,
                variant=variant_id,
            )
        ]


def variants(count=2):
    return [
        PreparedVariant(f"variant-{index}", object(), 8, 8)
        for index in range(count)
    ]


class PartialFailureTests(unittest.TestCase):
    def test_successful_variant_followed_by_engine_failure_is_processing_error(self):
        engine = FailingSecondVariantEngine()
        with mock.patch(
            "tools.texture_ocr.pipeline.iter_variants",
            return_value=iter(variants()),
        ):
            result = scan_one_image(Path("synthetic.png"), scan_config(), engine, None)

        self.assertEqual(engine.calls, 2)
        self.assertEqual(result["processing"]["status"], "error")
        self.assertEqual(result["processing"]["variant_calls"], 1)
        self.assertIn("RuntimeError: second variant failed", result["processing"]["error"])
        self.assertEqual(result["classification"]["tier"], "probable")
        self.assertEqual([row["text"] for row in result["detections"]], ["AB"])

    def test_variant_cap_after_partial_work_is_error_and_not_cacheable(self):
        def capped_variants(*args, **kwargs):
            yield variants(1)[0]
            raise VariantLimitExceeded("synthetic variant cap reached")

        with mock.patch(
            "tools.texture_ocr.pipeline.iter_variants",
            side_effect=capped_variants,
        ):
            result = scan_one_image(
                Path("synthetic.png"), scan_config(), SuccessfulEngine(), None
            )

        self.assertEqual(result["processing"]["status"], "error")
        self.assertEqual(result["processing"]["variant_calls"], 1)
        self.assertIn("VariantLimitExceeded", result["processing"]["error"])

        with tempfile.TemporaryDirectory() as temporary, ResultCache(
            Path(temporary) / "cache.sqlite3"
        ) as cache:
            with self.assertRaises(ValueError):
                cache.put_result("key", "pixels", "profile", "engine", result)
            self.assertEqual(cache.result_count(), 0)


class CachedClassificationTests(unittest.TestCase):
    def test_cache_hit_keeps_full_evidence_classification_not_compact_reclassification(self):
        config = scan_config()
        engine = SuccessfulEngine()
        fingerprint = ImageFingerprint("file-hash", "pixel-hash", 32, 32, "RGBA")
        stored = {
            "processing": {
                "status": "ok",
                "error": "",
                "warnings": [],
                "variant_calls": 20,
            },
            # The complete, pre-compaction evidence confirmed this texture.
            "classification": {
                "tier": "confirmed",
                "score": 0.70,
                "scripts": ["latin"],
                "target_letter_count": 2,
                "engine_count": 2,
                "reason_codes": ["target_script", "engine_agreement"],
            },
            # Only compact display evidence remains; by itself it is probable.
            "detections": [
                {
                    "text": "AB",
                    "confidence": 0.50,
                    "polygon": [],
                    "engine": "fake",
                    "variant": "best-only",
                }
            ],
        }
        self.assertEqual(
            classify_detections(stored["detections"], config["classification"]).tier,
            "probable",
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = AssetSource(
                root / "source.png",
                "source-id",
                {"texture_name": "source_D", "asset_type": "item", "groups": []},
            )
            discovery = Discovery([source], [])
            profile = ocr_profile_digest(config)
            signature = combined_signature(engine, None)
            key = make_cache_key(fingerprint.pixel_sha256, signature, profile)

            with ResultCache(root / "cache.sqlite3") as cache:
                cache.put_result(
                    key,
                    fingerprint.pixel_sha256,
                    profile,
                    signature,
                    stored,
                )
                with mock.patch(
                    "tools.texture_ocr.pipeline.fingerprint_image",
                    return_value=fingerprint,
                ), mock.patch(
                    "tools.texture_ocr.pipeline.make_preview"
                ), mock.patch(
                    "tools.texture_ocr.pipeline.scan_one_image"
                ) as scan:
                    results = scan_sources(
                        discovery, config, engine, None, cache, root / "run"
                    )

            scan.assert_not_called()
            self.assertEqual(results[0]["cache"]["state"], "hit")
            self.assertEqual(results[0]["classification"], stored["classification"])


class ScanLimitTests(unittest.TestCase):
    def test_limit_stops_fingerprinting_after_requested_unique_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [
                AssetSource(
                    root / f"source-{index}.png",
                    f"source-{index}",
                    {
                        "texture_name": f"source_{index}_D",
                        "asset_type": "item",
                        "groups": [],
                    },
                )
                for index in range(5)
            ]
            fingerprints = {
                source.path: ImageFingerprint(
                    f"file-{index}", f"pixels-{index}", 16, 16, "RGBA"
                )
                for index, source in enumerate(sources)
            }
            raw_result = {
                "processing": {
                    "status": "ok",
                    "error": "",
                    "warnings": [],
                    "variant_calls": 1,
                },
                "classification": {
                    "tier": "rejected",
                    "score": 0.0,
                    "scripts": [],
                    "target_letter_count": 0,
                    "engine_count": 1,
                    "reason_codes": ["no_latin_or_cyrillic"],
                },
                "detections": [],
            }

            with ResultCache(root / "cache.sqlite3") as cache, mock.patch(
                "tools.texture_ocr.pipeline.fingerprint_image",
                side_effect=lambda path: fingerprints[path],
            ) as fingerprint, mock.patch(
                "tools.texture_ocr.pipeline.scan_one_image",
                return_value=raw_result,
            ) as scan:
                results = scan_sources(
                    Discovery(sources, []),
                    scan_config(),
                    SuccessfulEngine(),
                    None,
                    cache,
                    root / "run",
                    limit=2,
                )

            self.assertEqual(fingerprint.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in fingerprint.call_args_list],
                [sources[0].path, sources[1].path],
            )
            self.assertEqual(scan.call_count, 2)
            self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
