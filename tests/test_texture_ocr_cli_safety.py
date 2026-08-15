import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.texture_ocr import cli
from tools.texture_ocr.manifest import AssetSource, Discovery
from tools.texture_ocr.scoring import file_sha256, sanitize_component


class ExecutionGuardTests(unittest.TestCase):
    def test_scan_without_execute_creates_no_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-exist"
            with mock.patch("tools.texture_ocr.cli.load_config") as load_config:
                with self.assertRaisesRegex(SystemExit, "--execute"):
                    cli.main(
                        [
                            "scan",
                            "--output",
                            str(output),
                            "--input",
                            str(Path(temporary) / "input"),
                            "--no-manifest",
                        ]
                    )

            load_config.assert_not_called()
            self.assertFalse(output.exists())

    def test_default_run_ids_include_microseconds(self):
        first = cli._new_run_id(
            None, datetime(2026, 7, 13, 12, 30, 45, 1, tzinfo=timezone.utc)
        )
        second = cli._new_run_id(
            None, datetime(2026, 7, 13, 12, 30, 45, 2, tzinfo=timezone.utc)
        )
        self.assertNotEqual(first, second)
        self.assertEqual(first, "20260713T123045000001Z")

    def test_preflight_rejects_unvalidated_ocr_package_versions(self):
        config = {
            "engines": {
                "primary": {
                    "name": "paddleocr",
                    "enabled": True,
                    "package_version": "3.5.0",
                },
                "fallback": {
                    "name": "easyocr",
                    "enabled": True,
                    "package_version": "1.7.2",
                },
            }
        }
        installed = {
            "Pillow": "12.0.0",
            "numpy": "2.0.0",
            "paddleocr": "3.4.0",
            "easyocr": "1.7.1",
            "torch": "2.8.0",
        }
        with mock.patch(
            "tools.texture_ocr.cli.package_version",
            side_effect=lambda name: installed.get(name),
        ), mock.patch(
            "tools.texture_ocr.cli.package_version_any", return_value="3.2.0"
        ):
            errors = cli._scan_preflight(config, allow_model_download=True)

        self.assertTrue(any("paddleocr 버전 불일치" in error for error in errors))
        self.assertTrue(any("easyocr 버전 불일치" in error for error in errors))

    def test_materialize_without_execute_creates_no_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output-must-not-exist"
            destination = root / "catalog-must-not-exist"
            with mock.patch("tools.texture_ocr.cli._read_run") as read_run:
                with self.assertRaisesRegex(SystemExit, "--execute"):
                    cli.main(
                        [
                            "materialize",
                            "--output",
                            str(output),
                            "--destination",
                            str(destination),
                        ]
                    )

            read_run.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(destination.exists())

    def test_scan_refuses_missing_discovery_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "must-not-exist"
            discovery = Discovery(
                sources=[
                    AssetSource(
                        root / "synthetic.png",
                        "source-id",
                        {
                            "texture_name": "synthetic_D",
                            "asset_type": "item",
                            "groups": [],
                        },
                    )
                ],
                missing=[{"reason": "image_missing", "path": "missing.png"}],
            )
            config = {"filter": {"skip_non_color": False}}

            with mock.patch(
                "tools.texture_ocr.cli.load_config", return_value=config
            ), mock.patch(
                "tools.texture_ocr.cli._resolved_sources",
                return_value=([root], None, discovery),
            ), mock.patch(
                "tools.texture_ocr.cli._scan_preflight"
            ) as preflight, mock.patch(
                "tools.texture_ocr.cli.create_configured_engines"
            ) as create_engines:
                with self.assertRaisesRegex(SystemExit, "--allow-missing"):
                    cli.main(
                        [
                            "scan",
                            "--execute",
                            "--output",
                            str(output),
                        ]
                    )

            preflight.assert_not_called()
            create_engines.assert_not_called()
            self.assertFalse(output.exists())

    def test_existing_run_is_never_reused_even_with_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            run_dir = output / "runs" / "fixed-run"
            run_dir.mkdir(parents=True)
            sentinel = run_dir / "user-data.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            discovery = Discovery(
                [
                    AssetSource(
                        root / "synthetic.png",
                        "source-id",
                        {
                            "texture_name": "synthetic_D",
                            "asset_type": "item",
                            "groups": [],
                        },
                    )
                ],
                [],
            )
            config = {"filter": {"skip_non_color": False}}

            with mock.patch(
                "tools.texture_ocr.cli.load_config", return_value=config
            ), mock.patch(
                "tools.texture_ocr.cli._resolved_sources",
                return_value=([root], None, discovery),
            ), mock.patch(
                "tools.texture_ocr.cli._scan_preflight", return_value=[]
            ), mock.patch(
                "tools.texture_ocr.cli.create_configured_engines",
                return_value=(object(), None),
            ), mock.patch("tools.texture_ocr.cli.scan_sources") as scan:
                with self.assertRaisesRegex(SystemExit, "다른 --run-id"):
                    cli.main(
                        [
                            "scan",
                            "--execute",
                            "--force",
                            "--run-id",
                            "fixed-run",
                            "--output",
                            str(output),
                        ]
                    )

            scan.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")


class MaterializeSafetyTests(unittest.TestCase):
    def materialize(self, output, destination, row, *, overwrite=False):
        arguments = [
            "materialize",
            "--execute",
            "--output",
            str(output),
            "--destination",
            str(destination),
        ]
        if overwrite:
            arguments.append("--overwrite")
        with mock.patch(
            "tools.texture_ocr.cli._read_run",
            return_value=(
                Path(output) / "run",
                {},
                row if isinstance(row, list) else [row],
            ),
        ), contextlib.redirect_stdout(io.StringIO()):
            return cli.main(arguments)

    def candidate_row(self, source, expected_hash):
        return {
            "asset_id": "tex_0123456789abcdefabcd",
            "representative_source": str(source),
            "processing": {"status": "ok", "error": "", "warnings": []},
            "classification": {
                "tier": "confirmed",
                "score": 0.9,
                "scripts": ["latin"],
                "target_letter_count": 4,
                "engine_count": 1,
                "reason_codes": ["target_script", "primary_high"],
            },
            "detections": [],
            "references": [
                {
                    "source_id": "source-id",
                    "path": str(source),
                    "file_sha256": expected_hash,
                    "metadata": {
                        "asset_type": "item",
                        "groups": ["food"],
                    },
                }
            ],
        }

    def target_path(self, destination, source, asset_id):
        safe_name = sanitize_component(source.name, stable_id=asset_id)
        return Path(destination) / "items" / "food" / f"{asset_id}__{safe_name}"

    def test_changed_source_is_refused_and_does_not_create_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            source.write_bytes(b"original synthetic bytes")
            row = self.candidate_row(source, file_sha256(source))
            source.write_bytes(b"changed synthetic bytes")
            destination = root / "catalog"

            self.assertEqual(
                self.materialize(root / "output", destination, row, overwrite=True),
                0,
            )
            self.assertFalse(destination.exists())

    def test_existing_target_is_preserved_unless_overwrite_and_stale_source_never_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            original = b"original synthetic bytes"
            source.write_bytes(original)
            row = self.candidate_row(source, file_sha256(source))
            destination = root / "catalog"
            target = self.target_path(destination, source, row["asset_id"])

            self.assertEqual(self.materialize(root / "output", destination, row), 0)
            self.assertEqual(target.read_bytes(), original)
            sidecar = target.with_suffix(target.suffix + ".json")
            self.assertTrue(sidecar.is_file())

            target.write_bytes(b"existing target must survive")
            self.assertEqual(self.materialize(root / "output", destination, row), 0)
            self.assertEqual(target.read_bytes(), b"existing target must survive")

            self.assertEqual(
                self.materialize(root / "output", destination, row, overwrite=True),
                0,
            )
            self.assertEqual(target.read_bytes(), original)

            target.write_bytes(b"existing target after source changed")
            source.write_bytes(b"source changed after scan")
            self.assertEqual(
                self.materialize(root / "output", destination, row, overwrite=True),
                0,
            )
            self.assertEqual(target.read_bytes(), b"existing target after source changed")

    def test_mixed_map_item_and_unknown_references_keep_both_known_catalogs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "shared.png"
            source.write_bytes(b"shared texture bytes")
            expected_hash = file_sha256(source)
            row = self.candidate_row(source, expected_hash)
            row["references"].extend(
                [
                    {
                        "source_id": "map-reference",
                        "path": str(source),
                        "file_sha256": expected_hash,
                        "metadata": {"asset_type": "map", "groups": ["woods"]},
                    },
                    {
                        "source_id": "unknown-reference",
                        "path": str(source),
                        "file_sha256": expected_hash,
                        "metadata": {"asset_type": "unknown", "groups": ["mystery"]},
                    },
                ]
            )
            destination = root / "catalog"

            self.assertEqual(self.materialize(root / "output", destination, row), 0)
            item_target = self.target_path(destination, source, row["asset_id"])
            map_target = (
                destination
                / "maps"
                / "woods"
                / f"{row['asset_id']}__{sanitize_component(source.name, stable_id=row['asset_id'])}"
            )
            self.assertTrue(item_target.is_file())
            self.assertTrue(map_target.is_file())
            self.assertFalse((destination / "unknown").exists())

    def test_partial_materialization_repairs_missing_half_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            source.write_bytes(b"original synthetic bytes")
            row = self.candidate_row(source, file_sha256(source))
            destination = root / "catalog"
            target = self.target_path(destination, source, row["asset_id"])
            sidecar = target.with_suffix(target.suffix + ".json")

            self.assertEqual(self.materialize(root / "output", destination, row), 0)
            sidecar.unlink()
            with mock.patch("tools.texture_ocr.cli.shutil.copy2") as copy:
                self.assertEqual(self.materialize(root / "output", destination, row), 0)
            copy.assert_not_called()
            self.assertTrue(sidecar.is_file())

            target.unlink()
            self.assertEqual(self.materialize(root / "output", destination, row), 0)
            self.assertEqual(target.read_bytes(), b"original synthetic bytes")

    def test_sanitized_group_directory_is_stable_across_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = []
            for index in range(2):
                source = root / f"source-{index}.png"
                source.write_bytes(f"texture-{index}".encode("ascii"))
                row = self.candidate_row(source, file_sha256(source))
                row["asset_id"] = f"tex_{index:020d}"
                row["references"][0]["metadata"]["groups"] = ["zone:day"]
                rows.append(row)
            destination = root / "catalog"

            self.assertEqual(self.materialize(root / "output", destination, rows), 0)
            group_directories = [path for path in (destination / "items").iterdir() if path.is_dir()]
            self.assertEqual(len(group_directories), 1)
            self.assertEqual(len(list(group_directories[0].glob("tex_*__*.png"))), 2)


if __name__ == "__main__":
    unittest.main()
