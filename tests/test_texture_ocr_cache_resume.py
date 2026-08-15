import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.texture_ocr.cache import ResultCache
from tools.texture_ocr.manifest import AssetSource, Discovery
from tools.texture_ocr.pipeline import scan_sources
from tools.texture_ocr.preprocess import ImageFingerprint


CLASSIFICATION = {
    "confirmed_score": 0.80,
    "probable_score": 0.45,
    "review_score": 0.15,
    "agreement_score": 0.45,
    "confirmed_min_letters": 3,
    "probable_min_letters": 2,
}


def completed_result(text="STOP", confidence=0.85):
    return {
        "processing": {
            "status": "ok",
            "error": "",
            "warnings": [],
            "variant_calls": 1,
        },
        "classification": {
            "tier": "confirmed",
            "score": confidence,
            "scripts": ["latin"],
            "target_letter_count": len(text),
            "engine_count": 1,
            "reason_codes": ["target_script", "primary_high"],
        },
        "detections": [
            {
                "text": text,
                "confidence": confidence,
                "polygon": [],
                "engine": "fake",
                "variant": "synthetic",
            }
        ],
    }


class FakeEngine:
    name = "fake"

    def __init__(self, signature="fake:v1"):
        self._signature = signature

    @property
    def signature(self):
        return self._signature


def minimal_config():
    return {
        "cache_schema": 1,
        "filter": {"skip_non_color": False},
        "preprocess": {"profile": "synthetic-v1"},
        "classification": dict(CLASSIFICATION),
        "engines": {"primary": {"name": "fake", "profile": "v1"}},
        "runtime": {"preview_max_side": 64},
    }


class ResultCacheTests(unittest.TestCase):
    def test_completed_unicode_result_persists_across_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            value = completed_result("ВНИМАНИЕ", 0.91)
            with ResultCache(path) as cache:
                cache.put_result("key", "pixels", "profile", "engine", value)
                self.assertEqual(cache.result_count(), 1)

            with ResultCache(path) as cache:
                self.assertEqual(cache.get_result("key"), value)
                self.assertIsNone(cache.get_result("missing"))

    def test_error_or_incomplete_result_is_never_cached(self):
        with tempfile.TemporaryDirectory() as temporary, ResultCache(
            Path(temporary) / "cache.sqlite3"
        ) as cache:
            with self.assertRaises(ValueError):
                cache.put_result(
                    "key",
                    "pixels",
                    "profile",
                    "engine",
                    {"processing": {"status": "error"}},
                )
            self.assertEqual(cache.result_count(), 0)

    def test_corrupt_cached_json_is_treated_as_miss_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary, ResultCache(
            Path(temporary) / "cache.sqlite3"
        ) as cache:
            with cache.connection:
                cache.connection.execute(
                    """
                    INSERT INTO ocr_results(
                        cache_key, pixel_sha256, profile_digest,
                        engine_signature, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("broken", "pixels", "profile", "engine", "{bad", "now"),
                )
            self.assertIsNone(cache.get_result("broken"))
            self.assertEqual(cache.result_count(), 0)

    def test_review_round_trip_filter_and_clear(self):
        with tempfile.TemporaryDirectory() as temporary, ResultCache(
            Path(temporary) / "cache.sqlite3"
        ) as cache:
            cache.set_review("asset-b", "probable", "Проверить", "dex")
            cache.set_review("asset-a", "confirmed", "확인", "dex")

            selected = cache.get_reviews(["asset-b", "asset-b", "missing"])
            self.assertEqual(list(selected), ["asset-b"])
            self.assertEqual(selected["asset-b"]["note"], "Проверить")
            self.assertEqual(list(cache.get_reviews()), ["asset-a", "asset-b"])

            cache.set_review("asset-a", "clear")
            self.assertIsNone(cache.get_review("asset-a"))
            with self.assertRaises(ValueError):
                cache.set_review("asset-x", "unsupported")

    def test_schema_version_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', '999')"
            )
            connection.commit()
            connection.close()

            cache = object.__new__(ResultCache)
            cache.path = path.resolve()
            cache.connection = sqlite3.connect(path)
            cache.connection.row_factory = sqlite3.Row
            try:
                with self.assertRaises(RuntimeError):
                    cache._create_schema()
            finally:
                cache.close()


class ResumeAndDedupeTests(unittest.TestCase):
    def test_pixel_duplicates_are_scanned_once_and_resume_from_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = AssetSource(
                root / "a.png",
                "src-a",
                {"texture_name": "a_D", "asset_type": "item", "groups": ["food"]},
            )
            second = AssetSource(
                root / "b.png",
                "src-b",
                {"texture_name": "b_D", "asset_type": "map", "groups": ["woods"]},
            )
            discovery = Discovery([second, first], [], 0)
            fingerprints = {
                first.path: ImageFingerprint("file-a", "same-pixels", 64, 64, "RGBA"),
                second.path: ImageFingerprint("file-b", "same-pixels", 64, 64, "RGBA"),
            }
            config = minimal_config()
            engine = FakeEngine()

            with ResultCache(root / "cache.sqlite3") as cache, mock.patch(
                "tools.texture_ocr.pipeline.fingerprint_image",
                side_effect=lambda path: fingerprints[path],
            ), mock.patch(
                "tools.texture_ocr.pipeline.make_preview"
            ), mock.patch(
                "tools.texture_ocr.pipeline.scan_one_image",
                return_value=completed_result(),
            ) as recognize:
                first_run = scan_sources(
                    discovery, config, engine, None, cache, root / "run-one"
                )
                self.assertEqual(recognize.call_count, 1)
                self.assertEqual(cache.result_count(), 1)
                self.assertEqual(len(first_run), 1)
                self.assertEqual(first_run[0]["cache"]["state"], "miss")
                self.assertEqual(
                    [ref["source_id"] for ref in first_run[0]["references"]],
                    ["src-a", "src-b"],
                )

                recognize.reset_mock()
                second_run = scan_sources(
                    discovery,
                    config,
                    engine,
                    None,
                    cache,
                    root / "run-two",
                )

                recognize.assert_not_called()
                self.assertEqual(second_run[0]["cache"]["state"], "hit")
                self.assertEqual(second_run[0]["classification"]["tier"], "confirmed")

                # Classification thresholds affect adaptive early stopping and
                # therefore intentionally form part of the OCR profile cache key.
                recognize.reset_mock()
                changed_thresholds = copy.deepcopy(config)
                changed_thresholds["classification"]["confirmed_score"] = 0.90
                changed_run = scan_sources(
                    discovery,
                    changed_thresholds,
                    engine,
                    None,
                    cache,
                    root / "run-three",
                )
                self.assertEqual(recognize.call_count, 1)
                self.assertEqual(changed_run[0]["cache"]["state"], "miss")

    def test_force_bypasses_completed_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = AssetSource(
                root / "only.png",
                "src-only",
                {"texture_name": "only_D", "asset_type": "item", "groups": []},
            )
            discovery = Discovery([source], [])
            fingerprint = ImageFingerprint("file", "pixels", 32, 32, "RGBA")
            engine = FakeEngine()
            config = minimal_config()

            with ResultCache(root / "cache.sqlite3") as cache, mock.patch(
                "tools.texture_ocr.pipeline.fingerprint_image", return_value=fingerprint
            ), mock.patch("tools.texture_ocr.pipeline.make_preview"), mock.patch(
                "tools.texture_ocr.pipeline.scan_one_image",
                return_value=completed_result(),
            ) as recognize:
                scan_sources(discovery, config, engine, None, cache, root / "one")
                scan_sources(
                    discovery, config, engine, None, cache, root / "two", force=True
                )
                self.assertEqual(recognize.call_count, 2)

    def test_failed_processing_is_retried_on_next_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = AssetSource(
                root / "only.png",
                "src-only",
                {"texture_name": "only_D", "asset_type": "item", "groups": []},
            )
            discovery = Discovery([source], [])
            fingerprint = ImageFingerprint("file", "pixels", 32, 32, "RGBA")
            failure = {
                "processing": {
                    "status": "error",
                    "error": "synthetic failure",
                    "warnings": [],
                    "variant_calls": 0,
                },
                "classification": {
                    "tier": "error",
                    "score": 0.0,
                    "scripts": [],
                    "target_letter_count": 0,
                    "engine_count": 0,
                    "reason_codes": ["ocr_error"],
                },
                "detections": [],
            }

            with ResultCache(root / "cache.sqlite3") as cache, mock.patch(
                "tools.texture_ocr.pipeline.fingerprint_image", return_value=fingerprint
            ), mock.patch("tools.texture_ocr.pipeline.make_preview"), mock.patch(
                "tools.texture_ocr.pipeline.scan_one_image", return_value=failure
            ) as recognize:
                first = scan_sources(
                    discovery, minimal_config(), FakeEngine(), None, cache, root / "one"
                )
                second = scan_sources(
                    discovery, minimal_config(), FakeEngine(), None, cache, root / "two"
                )
                self.assertEqual(recognize.call_count, 2)
                self.assertEqual(cache.result_count(), 0)
                self.assertEqual(first[0]["cache"]["state"], "miss")
                self.assertEqual(second[0]["cache"]["state"], "miss")


if __name__ == "__main__":
    unittest.main()
