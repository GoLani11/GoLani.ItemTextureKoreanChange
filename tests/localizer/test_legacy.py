import hashlib
import json
from pathlib import Path

from PIL import Image

from golani_texture_localizer.legacy import create_legacy_layout_sheet
from golani_texture_localizer.paths import ProjectPaths


def test_legacy_layout_is_marked_reference_only(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    legacy = root / "legacy.png"
    Image.new("RGBA", (64, 64), (220, 210, 190, 255)).save(source)
    Image.new("RGBA", (64, 64), (215, 205, 185, 255)).save(legacy)
    ocr = root / "legacy-ocr.json"
    ocr.write_text(
        json.dumps(
            {
                "status": "completed",
                "errors": [],
                "image_sha256": hashlib.sha256(legacy.read_bytes()).hexdigest(),
                "detections": [
                    {
                        "region_id": "ocr-001",
                        "text": "한글",
                        "script": "korean",
                        "confidence": 0.99,
                        "bbox": [10, 15, 50, 35],
                        "rotation_deg": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = create_legacy_layout_sheet(paths, "sample", source, legacy, ocr)

    assert report["regions"][0]["reference_only"] is True
    assert report["regions"][0]["text_ko_suggestion"] == "한글"
    assert "최종 픽셀로 재사용하지 않고" in report["warning"]
    assert Path(report["sheet"]).is_file()
