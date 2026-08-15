import math
import unittest

from tools.texture_ocr.scoring import (
    Detection,
    classify_detections,
    detect_scripts,
    make_asset_id,
    make_cache_key,
    normalize_confidence,
)


THRESHOLDS = {
    "confirmed_score": 0.80,
    "probable_score": 0.45,
    "review_score": 0.15,
    "agreement_score": 0.45,
    "agreement_text_similarity": 0.80,
    "confirmed_min_letters": 3,
    "probable_min_letters": 2,
}


class ScriptDetectionTests(unittest.TestCase):
    def test_detects_latin_cyrillic_and_mixed_confusables(self):
        latin = detect_scripts("WARNING")
        cyrillic = detect_scripts("АВС")  # Cyrillic letters that resemble ABC.
        mixed = detect_scripts("PАY")  # Middle character is Cyrillic A.

        self.assertEqual((latin.latin, latin.cyrillic), (7, 0))
        self.assertEqual((cyrillic.latin, cyrillic.cyrillic), (0, 3))
        self.assertEqual((mixed.latin, mixed.cyrillic), (2, 1))
        self.assertEqual(mixed.scripts, ("latin", "cyrillic"))

    def test_ignores_hangul_numbers_punctuation_and_combining_marks(self):
        evidence = detect_scripts("한국어 123 !? ́")
        self.assertEqual(evidence.total, 0)
        self.assertEqual(evidence.scripts, ())

    def test_normalizes_decomposed_latin_and_counts_extended_cyrillic(self):
        evidence = detect_scripts("Cafe\u0301 Ёж")
        self.assertEqual(evidence.latin, 4)
        self.assertEqual(evidence.cyrillic, 2)


class ConfidenceTests(unittest.TestCase):
    def test_confidence_is_clamped_and_non_finite_values_are_zero(self):
        values = [
            (None, 0.0),
            ("bad", 0.0),
            (-0.5, 0.0),
            (1.5, 1.0),
            ("0.75", 0.75),
            (math.nan, 0.0),
            (math.inf, 0.0),
        ]
        for raw, expected in values:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_confidence(raw), expected)

    def test_detection_mapping_normalizes_text_confidence_and_polygon(self):
        detection = Detection.from_mapping(
            {
                "text": "Cafe\u0301",
                "score": "0.7",
                "box": ((1, 2), (3, 4)),
                "engine": "fake",
            }
        )
        self.assertEqual(detection.text, "Café")
        self.assertEqual(detection.confidence, 0.7)
        self.assertEqual(detection.polygon, [[1, 2], [3, 4]])


class ClassificationTests(unittest.TestCase):
    def classify(self, *detections):
        return classify_detections(detections, THRESHOLDS)

    def test_threshold_boundaries_and_minimum_letters(self):
        confirmed = self.classify(
            Detection("STOP", 0.80, engine="primary")
        )
        probable = self.classify(
            Detection("СТ", 0.45, engine="primary")
        )
        weak = self.classify(
            Detection("AB", 0.4499, engine="primary")
        )
        too_short = self.classify(
            Detection("A", 0.99, engine="primary")
        )

        self.assertEqual(confirmed.tier, "confirmed")
        self.assertEqual(probable.tier, "probable")
        self.assertEqual(weak.tier, "needs_review")
        self.assertEqual(too_short.tier, "needs_review")

    def test_two_engines_can_confirm_medium_confidence_text(self):
        result = self.classify(
            Detection("СТОП", 0.45, engine="primary"),
            Detection("СТОП", 0.45, engine="fallback"),
        )
        self.assertEqual(result.tier, "confirmed")
        self.assertIn("engine_agreement", result.reason_codes)
        self.assertEqual(result.engine_count, 2)
        self.assertEqual(result.scripts, ("cyrillic",))

    def test_two_engines_with_unrelated_text_do_not_claim_agreement(self):
        result = self.classify(
            Detection("AB", 0.45, engine="primary"),
            Detection("АБ", 0.45, engine="fallback"),
        )
        self.assertEqual(result.tier, "probable")
        self.assertNotIn("engine_agreement", result.reason_codes)

    def test_non_target_high_confidence_does_not_override_target_score(self):
        result = self.classify(
            Detection("12345", 0.99, engine="primary"),
            Detection("СТОП", 0.50, engine="fallback"),
        )
        self.assertEqual(result.tier, "probable")
        self.assertEqual(result.score, 0.50)

    def test_no_target_text_is_rejected_but_detector_only_is_reviewed(self):
        rejected = self.classify(Detection("한국어 123", 0.99, engine="primary"))
        detector_only = self.classify(Detection("", 0.35, polygon=[[0, 0]], engine="primary"))

        self.assertEqual(rejected.tier, "rejected")
        self.assertEqual(detector_only.tier, "needs_review")
        self.assertEqual(detector_only.reason_codes, ("detector_only",))

    def test_empty_detection_list_is_rejected(self):
        result = self.classify()
        self.assertEqual(result.tier, "rejected")
        self.assertEqual(result.reason_codes, ("no_latin_or_cyrillic",))


class StableIdentifierTests(unittest.TestCase):
    def test_cache_key_is_stable_and_includes_every_resume_dimension(self):
        base = make_cache_key("pixels", "engine-v1", "preprocess-v1")
        self.assertEqual(base, make_cache_key("pixels", "engine-v1", "preprocess-v1"))
        self.assertNotEqual(base, make_cache_key("pixels-2", "engine-v1", "preprocess-v1"))
        self.assertNotEqual(base, make_cache_key("pixels", "engine-v2", "preprocess-v1"))
        self.assertNotEqual(base, make_cache_key("pixels", "engine-v1", "preprocess-v2"))

    def test_asset_id_uses_content_hash_not_source_path(self):
        content_hash = "a" * 64
        self.assertEqual(make_asset_id(content_hash), "tex_" + "a" * 20)


if __name__ == "__main__":
    unittest.main()
