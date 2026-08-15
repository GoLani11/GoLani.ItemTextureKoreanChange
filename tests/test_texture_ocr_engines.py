import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.texture_ocr.engines import (
    EasyOcrEngine,
    PaddleOcrEngine,
    model_tree_digest,
    paddle_model_directory_ready,
)


class ModelSignatureTests(unittest.TestCase):
    def test_paddle_model_readiness_rejects_partial_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "model"
            model_dir.mkdir()
            (model_dir / "README.md").write_text("partial", encoding="utf-8")
            self.assertFalse(paddle_model_directory_ready(model_dir))
            (model_dir / "inference.pdiparams").write_bytes(b"parameters")
            self.assertFalse(paddle_model_directory_ready(model_dir))
            (model_dir / "inference.json").write_text("{}", encoding="utf-8")
            self.assertTrue(paddle_model_directory_ready(model_dir))

    def test_model_digest_and_fresh_engine_signature_follow_weight_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "models"
            model_dir.mkdir()
            detector = model_dir / "detector.pth"
            recognizer = model_dir / "recognizer.pth"
            detector.write_bytes(b"detector-v1")
            recognizer.write_bytes(b"recognizer-v1")
            config = {
                "name": "easyocr",
                "model_dir": str(model_dir),
                "detector_file": detector.name,
                "recognizer_file": recognizer.name,
                "model_revision": "synthetic",
                "gpu": False,
            }

            first_digest = model_tree_digest(model_dir)
            with mock.patch(
                "tools.texture_ocr.engines.package_version",
                return_value="test-version",
            ):
                first_signature = EasyOcrEngine(config, project_root=root).signature

            # Keep the file name stable: cache identity must follow contents,
            # not only paths, sizes, or directory entries.
            recognizer.write_bytes(b"recognizer-v2")
            second_digest = model_tree_digest(model_dir)
            with mock.patch(
                "tools.texture_ocr.engines.package_version",
                return_value="test-version",
            ):
                second_signature = EasyOcrEngine(config, project_root=root).signature

            self.assertNotEqual(first_digest, second_digest)
            self.assertNotEqual(first_signature, second_signature)
            self.assertTrue(first_signature.startswith("easyocr:"))
            self.assertTrue(second_signature.startswith("easyocr:"))

    def test_explicit_paddle_models_remain_the_signature_source_when_cache_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local_detector = root / "local-detector"
            local_recognizer = root / "local-recognizer"
            local_detector.mkdir()
            local_recognizer.mkdir()
            (local_detector / "inference.pdiparams").write_bytes(b"local-detector-v1")
            (local_detector / "inference.json").write_text("{}", encoding="utf-8")
            (local_recognizer / "inference.pdiparams").write_bytes(b"local-recognizer-v1")
            (local_recognizer / "inference.json").write_text("{}", encoding="utf-8")

            home = root / "home"
            cached_detector = home / ".paddlex" / "official_models" / "detector-id"
            cached_recognizer = home / ".paddlex" / "official_models" / "recognizer-id"
            cached_detector.mkdir(parents=True)
            cached_recognizer.mkdir(parents=True)
            (cached_detector / "inference.pdiparams").write_bytes(b"cached-detector-v1")
            (cached_detector / "inference.json").write_text("{}", encoding="utf-8")
            (cached_recognizer / "inference.pdiparams").write_bytes(b"cached-recognizer-v1")
            (cached_recognizer / "inference.json").write_text("{}", encoding="utf-8")

            config = {
                "name": "paddleocr",
                "detector_model": "detector-id",
                "recognizer_model": "recognizer-id",
                "detector_dir": str(local_detector),
                "recognizer_dir": str(local_recognizer),
                "model_revision": "synthetic",
                "device": "cpu",
                "inference_engine": "paddle_static",
            }
            constructor_calls = []

            class FakePaddleOCR:
                def __init__(self, **kwargs):
                    constructor_calls.append(kwargs)

            fake_module = types.ModuleType("paddleocr")
            fake_module.PaddleOCR = FakePaddleOCR

            def prepared_signature():
                with mock.patch.dict("sys.modules", {"paddleocr": fake_module}), mock.patch(
                    "tools.texture_ocr.engines.Path.home", return_value=home
                ), mock.patch(
                    "tools.texture_ocr.engines.package_version", return_value="test-version"
                ):
                    engine = PaddleOcrEngine(
                        config, allow_model_download=True, project_root=root
                    )
                    engine.prepare()
                    self.assertEqual(engine.detector_dir, local_detector)
                    self.assertEqual(engine.recognizer_dir, local_recognizer)
                    return engine.signature

            first_signature = prepared_signature()
            (cached_detector / "inference.pdiparams").write_bytes(b"cached-detector-v2")
            second_signature = prepared_signature()
            (local_recognizer / "inference.pdiparams").write_bytes(b"local-recognizer-v2")
            third_signature = prepared_signature()

            self.assertEqual(first_signature, second_signature)
            self.assertNotEqual(second_signature, third_signature)
            self.assertEqual(len(constructor_calls), 3)
            self.assertTrue(
                all(
                    call["text_detection_model_dir"] == str(local_detector)
                    and call["text_recognition_model_dir"] == str(local_recognizer)
                    for call in constructor_calls
                )
            )


if __name__ == "__main__":
    unittest.main()
