import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.texture_ocr.reporting import (
    effective_tier,
    load_results,
    summary_counts,
    write_reports,
)


def result_row(
    asset_id,
    tier,
    *,
    score=0.5,
    text="",
    references=1,
    processing="ok",
    review=None,
    preview=None,
):
    row = {
        "asset_id": asset_id,
        "representative_source": f"work/{asset_id}.png",
        "processing": {"status": processing, "error": "", "warnings": []},
        "classification": {
            "tier": tier,
            "score": score,
            "scripts": ["latin"] if text else [],
            "target_letter_count": len(text),
            "engine_count": 1 if text else 0,
            "reason_codes": [],
        },
        "detections": (
            [{"text": text, "confidence": score, "engine": "fake"}] if text else []
        ),
        "references": [
            {
                "source_id": f"{asset_id}-ref-{index}",
                "path": f"source/{index}.png",
                "metadata": {
                    "asset_type": "map" if index % 2 else "item",
                    "groups": ["우드" if index % 2 else "food"],
                },
            }
            for index in range(references)
        ],
    }
    if review is not None:
        row["review"] = {"decision": review, "note": "", "reviewer": "dex"}
    if preview is not None:
        row["preview"] = preview
    return row


class SummaryTests(unittest.TestCase):
    def test_review_decision_overrides_machine_tier(self):
        row = result_row("a", "rejected", review="confirmed")
        self.assertEqual(effective_tier(row), "confirmed")
        row["review"]["decision"] = None
        self.assertEqual(effective_tier(row), "rejected")

    def test_summary_distinguishes_unique_textures_references_and_candidates(self):
        rows = [
            result_row("a", "confirmed", references=2),
            result_row("b", "probable", references=1),
            result_row("c", "rejected", references=3),
            result_row("d", "error", references=1, processing="error"),
            result_row("e", "rejected", references=1, review="needs_review"),
        ]
        summary = summary_counts(rows)
        self.assertEqual(summary["unique_textures"], 5)
        self.assertEqual(summary["source_references"], 8)
        self.assertEqual(summary["candidate_textures"], 3)
        self.assertEqual(
            summary["tiers"],
            {
                "confirmed": 1,
                "error": 1,
                "needs_review": 1,
                "probable": 1,
                "rejected": 1,
            },
        )
        self.assertEqual(summary["processing"], {"error": 1, "ok": 4})


class ReportGenerationTests(unittest.TestCase):
    def test_reports_are_deterministic_parseable_and_preserve_unicode(self):
        run = {
            "schema_version": 1,
            "run_id": "fixed-run",
            "started_at": "2026-07-13T12:00:00+00:00",
            "finished_at": "2026-07-13T12:00:01+00:00",
        }
        rows = [
            result_row("tex-z", "probable", text="ВНИМАНИЕ", references=2),
            result_row("tex-a", "confirmed", text="WARNING", references=1),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            summary = write_reports(first, run, rows)
            write_reports(second, run, list(reversed(rows)))

            self.assertEqual(summary["unique_textures"], 2)
            for name in ("run.json", "results.jsonl", "summary.csv", "report.html"):
                self.assertEqual(
                    (first / name).read_text(encoding="utf-8"),
                    (second / name).read_text(encoding="utf-8"),
                    name,
                )
                self.assertFalse((first / f".{name}.tmp").exists())

            run_payload = json.loads((first / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run_payload["summary"], summary)
            loaded = load_results(first / "results.jsonl")
            self.assertEqual([row["asset_id"] for row in loaded], ["tex-a", "tex-z"])
            self.assertEqual(loaded[1]["detections"][0]["text"], "ВНИМАНИЕ")

            with (first / "summary.csv").open(encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual([row["asset_id"] for row in csv_rows], ["tex-a", "tex-z"])
            self.assertEqual(csv_rows[1]["ocr_text"], "ВНИМАНИЕ")
            self.assertEqual(csv_rows[1]["reference_count"], "2")
            self.assertEqual(csv_rows[1]["asset_types"], "item|map")
            self.assertEqual(csv_rows[1]["groups"], "food|우드")

    def test_html_escapes_untrusted_text_and_quotes_preview_urls(self):
        run = {"run_id": "<script>alert('run')</script>"}
        row = result_row(
            "asset<&>",
            "confirmed",
            text="</td><script>alert('ocr')</script>",
            preview='previews/a b"<.png',
        )
        row["representative_source"] = "source/<img onerror=x>.png"
        row["processing"]["error"] = "bad <b>error</b>"

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            write_reports(destination, run, [row])
            report = (destination / "report.html").read_text(encoding="utf-8")

        self.assertNotIn("<script>alert", report)
        self.assertNotIn("<img onerror=x>", report)
        self.assertNotIn("<b>error</b>", report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("source/&lt;img onerror=x&gt;.png", report)
        self.assertIn("bad &lt;b&gt;error&lt;/b&gt;", report)
        self.assertIn("previews/a%20b%22%3C.png", report)
        self.assertNotIn("http://", report)
        self.assertNotIn("https://", report)

    def test_zero_candidate_report_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            summary = write_reports(destination, {"run_id": "empty"}, [])
            self.assertEqual(
                summary,
                {
                    "unique_textures": 0,
                    "source_references": 0,
                    "candidate_textures": 0,
                    "tiers": {},
                    "processing": {},
                },
            )
            self.assertEqual(load_results(destination / "results.jsonl"), [])
            self.assertIn("<tbody></tbody>", (destination / "report.html").read_text("utf-8"))

    def test_load_results_rejects_malformed_or_non_object_jsonl(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.jsonl"
            path.write_text('{}\n{bad\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "2"):
                load_results(path)

            path.write_text('[]\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "1"):
                load_results(path)


if __name__ == "__main__":
    unittest.main()
