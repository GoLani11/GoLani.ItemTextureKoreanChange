from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def discover_project_root(start: Path | None = None) -> Path:
    configured = os.environ.get("GOLANI_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    cursor = (start or Path.cwd()).resolve()
    for candidate in (cursor, *cursor.parents):
        if (candidate / "profiles" / "food" / "collection.json").is_file():
            return candidate
    raise FileNotFoundError("profiles/food/collection.json이 있는 프로젝트 루트를 찾지 못했어요")


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    workspace: Path

    @classmethod
    def create(cls, root: Path, workspace: Path | None = None) -> "ProjectPaths":
        root = root.expanduser().resolve()
        selected = (workspace or (root / "workspace")).expanduser().resolve()
        git_path = root / ".git"
        if selected == root or selected == git_path or git_path in selected.parents:
            raise ValueError("workspace가 프로젝트 루트나 .git일 수 없어요")
        return cls(root=root, workspace=selected)

    @property
    def inventory(self) -> Path:
        return self.workspace / "inventory.json"

    @property
    def source(self) -> Path:
        return self.workspace / "source"

    @property
    def source_overrides(self) -> Path:
        return self.workspace / "source-overrides"

    @property
    def source_override_manifest(self) -> Path:
        return self.workspace / "source-overrides.json"

    @property
    def drafts(self) -> Path:
        return self.workspace / "drafts"

    @property
    def approved(self) -> Path:
        return self.workspace / "approved"

    @property
    def reviews(self) -> Path:
        return self.workspace / "reviews"

    @property
    def ocr(self) -> Path:
        return self.workspace / "ocr"

    @property
    def derived(self) -> Path:
        return self.workspace / "derived"

    @property
    def derived_manifest(self) -> Path:
        return self.workspace / "derived.json"

    @property
    def reports(self) -> Path:
        return self.workspace / "reports"

    @property
    def bundles(self) -> Path:
        return self.workspace / "bundles"

    @property
    def releases(self) -> Path:
        return self.workspace / "releases"

    @property
    def latest_release(self) -> Path:
        return self.releases / "latest.json"


def game_bundle_root(spt_root: Path) -> Path:
    root = spt_root.expanduser().resolve()
    candidate = root / "EscapeFromTarkov_Data" / "StreamingAssets" / "Windows"
    if not candidate.is_dir():
        raise FileNotFoundError(f"게임 번들 루트를 찾지 못했어요: {candidate}")
    return candidate
