import hashlib
import json
from pathlib import Path

from PIL import Image

from golani_texture_localizer.paths import ProjectPaths
from golani_texture_localizer.visual import create_visual_transcription_sheet


def test_visual_sheet_hides_ocr_text_and_preserves_only_geometry(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    Image.new("RGBA", (64, 64), (240, 240, 240, 255)).save(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    ocr = root / "ocr.json"
    ocr.write_text(
        json.dumps(
            {
                "image_sha256": source_sha,
                "status": "completed",
                "errors": [],
                "detections": [
                    {
                        "text": "SECRET SOURCE TEXT",
                        "confidence": 0.9,
                        "bbox": [8, 10, 50, 24],
                        "rotation_deg": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = create_visual_transcription_sheet(paths, "sample", source, ocr)
    index = json.loads(Path(report["index"]).read_text(encoding="utf-8"))

    assert index["ocr_text_hidden_from_sheet"] is True
    assert len(index["regions"]) == 1
    assert index["regions"][0]["bbox"] == [8, 10, 50, 24]
    assert index["regions"][0]["rotation_deg"] == 0
    assert index["regions"][0]["visual_region_id"] == "visual-001"
    assert Path(index["regions"][0]["crop"]).is_file()
    assert "SECRET" not in Path(report["index"]).read_text(encoding="utf-8")


def test_no_ocr_detection_still_creates_whole_image_visual_review(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    Image.new("RGBA", (40, 24), (120, 130, 140, 255)).save(source)
    ocr = root / "ocr.json"
    ocr.write_text(
        json.dumps(
            {
                "image_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "status": "completed",
                "errors": [],
                "detections": [],
            }
        ),
        encoding="utf-8",
    )

    report = create_visual_transcription_sheet(paths, "empty", source, ocr)

    assert report["whole_image_fallback"] is True
    assert report["regions"][0]["bbox"] == [0, 0, 40, 24]
