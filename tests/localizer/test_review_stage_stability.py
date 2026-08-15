from __future__ import annotations

from golani_texture_localizer.review import _candidate_errors, review_stage_sha256


def test_later_material_evidence_does_not_invalidate_candidate_approval(monkeypatch, tmp_path) -> None:
    class Validator:
        STAGES = ("source", "candidate", "material", "release")
        THROUGH = {"candidate": ("source", "candidate")}

        @staticmethod
        def validate_record(record, through, project_root=None):
            return [
                "stages.material.evidence: 현재 파일 SHA가 기록과 달라요",
                "stages.release.evidence: 현재 파일 SHA가 기록과 달라요",
            ]

    monkeypatch.setattr("golani_texture_localizer.review._skill_validator", lambda paths: Validator)

    assert _candidate_errors(type("Paths", (), {"root": tmp_path})(), {}) == []


def test_material_stage_hash_ignores_later_stage_updates() -> None:
    record = {
        "stages": {
            "material_validation": {"status": "pass", "data": {"policy": "preserve"}},
            "mip_validation": {"status": "pending", "data": {}},
        }
    }
    before = review_stage_sha256(record, "material_validation")

    record["stages"]["mip_validation"] = {"status": "pass", "data": {"missing_mips": 0}}

    assert review_stage_sha256(record, "material_validation") == before


def test_material_stage_hash_changes_with_policy() -> None:
    record = {
        "stages": {
            "material_validation": {"status": "pass", "data": {"policy": "preserve"}}
        }
    }
    before = review_stage_sha256(record, "material_validation")

    record["stages"]["material_validation"]["data"]["policy"] = "neutralize_old_text"

    assert review_stage_sha256(record, "material_validation") != before
