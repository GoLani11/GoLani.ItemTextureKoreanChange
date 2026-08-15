import os
import tempfile
import unittest
import unicodedata
from pathlib import Path

from tools.texture_ocr.cli import _source_path
from tools.texture_ocr.config import resolve_project_path
from tools.texture_ocr.scoring import safe_join, sanitize_component


FORBIDDEN = set('<>:"/\\|?*')


class SanitizeComponentTests(unittest.TestCase):
    def test_preserves_safe_korean_cyrillic_and_normalizes_unicode(self):
        self.assertEqual(
            sanitize_component("한글_Вывеска.png"),
            "한글_Вывеска.png",
        )
        normalized = sanitize_component("Cafe\u0301.png")
        self.assertEqual(normalized, "Café.png")
        self.assertEqual(normalized, unicodedata.normalize("NFC", normalized))

    def test_replaces_windows_forbidden_and_control_characters(self):
        # Illegal trailing control characters must not destroy a recognizable
        # image extension; Windows can surface names in this malformed form.
        result = sanitize_component('a<b>:c"d/e\\f|g?h*.png\x00\x1f')
        self.assertTrue(result.endswith(".png"))
        self.assertFalse(any(character in result for character in FORBIDDEN))
        self.assertFalse(any(ord(character) < 32 for character in result))

    def test_protects_windows_device_names_case_insensitively_with_extensions(self):
        for name in (
            "CON",
            "nul.txt",
            "PrN.png",
            "AUX",
            "COM1.log",
            "com9",
            "LPT1.jpg",
            "lpt9",
            "CLOCK$",
        ):
            with self.subTest(name=name):
                result = sanitize_component(name)
                stem = Path(result).stem.split("__", 1)[0]
                self.assertTrue(stem.startswith("_"), result)

    def test_empty_dot_and_trailing_space_names_become_safe(self):
        for name in ("", ".", "..", "   ", "label. "):
            with self.subTest(name=name):
                result = sanitize_component(name)
                self.assertNotIn(result, {"", ".", ".."})
                self.assertFalse(result.endswith((" ", ".")))

    def test_changed_name_collisions_use_stable_id(self):
        first = sanitize_component("a:b.png", stable_id="asset-one")
        second = sanitize_component("a?b.png", stable_id="asset-two")
        self.assertNotEqual(first.casefold(), second.casefold())
        self.assertEqual(first, sanitize_component("a:b.png", stable_id="asset-one"))

    def test_long_component_stays_within_limit_and_keeps_extension(self):
        result = sanitize_component("x" * 300 + ".png", stable_id="asset", max_length=48)
        self.assertLessEqual(len(result), 48)
        self.assertTrue(result.endswith(".png"))


class SafeJoinTests(unittest.TestCase):
    def test_output_is_always_below_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = safe_join(root, "maps", "우드", stable_id="asset")
            self.assertEqual(result.parent.name, "maps")
            self.assertEqual(result.name, "우드")
            self.assertEqual(os.path.commonpath((root.resolve(), result)), str(root.resolve()))

    def test_rejects_absolute_drive_unc_and_parent_traversal(self):
        unsafe = (
            "/etc/passwd",
            "../outside",
            "folder/../outside",
            "folder\\..\\outside",
            "C:\\Windows\\System32",
            "\\\\server\\share",
            "//server/share",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for value in unsafe:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        safe_join(temporary, value)

    def test_separator_inside_component_is_sanitized_not_interpreted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = safe_join(root, "nested/name", stable_id="asset")
            self.assertEqual(result.parent, root)
            self.assertNotIn("/", result.name)
            self.assertNotIn("\\", result.name)


class ProjectPathTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "WSL/POSIX conversion only")
    def test_windows_drive_path_converts_to_wsl_mount(self):
        converted = resolve_project_path(
            r"D:\SPT\work\1_raw\sign.png",
            project_root=Path("/unused/project"),
        )
        self.assertEqual(converted, Path("/mnt/d/SPT/work/1_raw/sign.png").resolve())
        self.assertEqual(
            _source_path(r"D:\SPT\work\1_raw\sign.png"),
            Path("/mnt/d/SPT/work/1_raw/sign.png").resolve(),
        )


if __name__ == "__main__":
    unittest.main()
