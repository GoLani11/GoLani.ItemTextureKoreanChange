from __future__ import annotations

from golani_texture_localizer.review import _candidate_errors


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
