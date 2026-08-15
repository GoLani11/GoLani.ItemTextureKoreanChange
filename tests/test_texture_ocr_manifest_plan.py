import json
import tempfile
import unittest
from pathlib import Path

from tools.texture_ocr.manifest import (
    AssetSource,
    Discovery,
    color_filter_reason,
    discover_sources,
    infer_asset_type,
    infer_groups,
)
from tools.texture_ocr.pipeline import build_plan


FILTER = {
    "skip_non_color": True,
    "color_tokens": ["d", "diff", "diffuse", "albedo", "basecolor"],
    "non_color_tokens": ["n", "nrm", "normal", "g", "gloss", "mask", "roughness"],
}


class MetadataInferenceTests(unittest.TestCase):
    def test_infers_item_map_and_unknown_types(self):
        self.assertEqual(
            infer_asset_type({"key": "assets/content/items/food/mayo.bundle"}),
            "item",
        )
        self.assertEqual(
            infer_asset_type({"key": "assets/content/weapons/usable_items/med.bundle"}),
            "item",
        )
        self.assertEqual(infer_asset_type({"scene": "woods"}), "map")
        self.assertEqual(infer_asset_type({"assetType": "map", "key": "items/x"}), "map")
        self.assertEqual(infer_asset_type({}), "unknown")

    def test_groups_preserve_explicit_order_and_add_map_category_once(self):
        groups = infer_groups(
            {"groups": ["shared", "woods"], "map": "woods", "category": "posters"}
        )
        self.assertEqual(groups, ["shared", "woods", "posters"])
        self.assertEqual(infer_groups({"groups": "food"}), ["food"])


class ManifestDiscoveryTests(unittest.TestCase):
    def test_map_json_style_manifest_normalizes_metadata_and_tracks_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "mayo_D.png"
            image.write_bytes(b"synthetic-not-an-image")
            manifest = root / "manifest.json"
            first_record = {
                "png": image.name,
                "key": "assets/content/items/food/mayo.bundle",
                "texture": "mayo_D",
                "category": "food",
                "asset_id": "explicit-source",
            }
            manifest.write_text(
                json.dumps(
                    [
                        first_record,
                        {"png": image.name, "texture": "duplicate_D"},
                        first_record,
                        {"png": "missing.png", "texture": "missing_D"},
                        {"key": "no-image-field.bundle"},
                    ]
                ),
                encoding="utf-8",
            )

            discovery = discover_sources([root], manifest, project_root=root)

            # The same physical image may intentionally have multiple logical
            # bundle/texture references. Only an identical manifest record is
            # ignored here; pixel-level dedupe happens in the scan pipeline.
            self.assertEqual(len(discovery.sources), 2)
            self.assertEqual(discovery.duplicates_ignored, 1)
            self.assertEqual(
                {entry["reason"] for entry in discovery.missing},
                {"image_missing", "manifest_image_field_missing"},
            )
            source = next(row for row in discovery.sources if row.source_id == "explicit-source")
            self.assertEqual(source.source_id, "explicit-source")
            self.assertEqual(source.metadata["bundle_key"], "assets/content/items/food/mayo.bundle")
            self.assertEqual(source.metadata["texture_name"], "mayo_D")
            self.assertEqual(source.metadata["asset_type"], "item")
            self.assertEqual(source.metadata["groups"], ["food"])

    def test_wrapped_manifest_and_jsonl_are_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.png"
            second = root / "second.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            wrapped = root / "wrapped.json"
            wrapped.write_text(
                json.dumps({"assets": [{"source": first.name}]}), encoding="utf-8"
            )
            jsonl = root / "assets.jsonl"
            jsonl.write_text(
                "\n" + json.dumps({"image": second.name}) + "\n", encoding="utf-8"
            )

            wrapped_result = discover_sources([root], wrapped, project_root=root)
            jsonl_result = discover_sources([root], jsonl, project_root=root)
            self.assertEqual([row.path.name for row in wrapped_result.sources], ["first.png"])
            self.assertEqual([row.path.name for row in jsonl_result.sources], ["second.jpg"])

    def test_include_unmanifested_filters_extensions_and_sorts_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("z.PNG", "a.jpg", "ignore.txt"):
                (root / name).write_bytes(name.encode("ascii"))

            discovery = discover_sources([root], include_unmanifested=True, project_root=root)
            self.assertEqual([row.path.name for row in discovery.sources], ["a.jpg", "z.PNG"])
            self.assertEqual(discovery.missing, [])


class ColorFilterAndPlanTests(unittest.TestCase):
    def source(self, name, asset_type="item"):
        return AssetSource(
            Path("/synthetic") / f"{name}.png",
            f"src-{name}",
            {"texture_name": name, "asset_type": asset_type, "groups": []},
        )

    def test_color_filter_uses_token_suffixes_not_substrings(self):
        self.assertIsNone(color_filter_reason(self.source("poster_D"), FILTER))
        self.assertEqual(
            color_filter_reason(self.source("poster_normal"), FILTER),
            "non_color_suffix:normal",
        )
        self.assertEqual(
            color_filter_reason(self.source("poster_n_LOD0"), FILTER),
            "non_color_token",
        )
        self.assertIsNone(color_filter_reason(self.source("original_sign"), FILTER))

    def test_plan_counts_candidates_types_skips_missing_and_duplicates(self):
        discovery = Discovery(
            sources=[
                self.source("item_D", "item"),
                self.source("wall_diffuse", "map"),
                self.source("item_N", "item"),
                self.source("wall_gloss", "map"),
                self.source("mystery", "unknown"),
            ],
            missing=[{"reason": "image_missing"}],
            duplicates_ignored=2,
        )
        plan = build_plan(discovery, {"filter": FILTER})

        self.assertEqual(plan.total_sources, 5)
        self.assertEqual(plan.color_candidates, 3)
        self.assertEqual(plan.skipped_non_color, 2)
        self.assertEqual(plan.missing_sources, 1)
        self.assertEqual(plan.duplicates_ignored, 2)
        self.assertEqual(plan.asset_types, {"item": 2, "map": 2, "unknown": 1})
        self.assertEqual(
            plan.skip_reasons,
            {"non_color_suffix:gloss": 1, "non_color_suffix:n": 1},
        )
        self.assertEqual(plan.to_dict()["color_candidates"], 3)


if __name__ == "__main__":
    unittest.main()
