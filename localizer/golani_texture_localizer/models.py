from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any


class ProfileError(ValueError):
    """프로필이 모호하거나 안전하게 처리할 수 없을 때 발생한다."""


def _bundle_key(value: Any) -> str:
    raw = str(value).strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.as_posix() == "."
        or raw.startswith("/")
        or re.match(r"^[A-Za-z]:/", raw)
        or any(part == ".." for part in path.parts)
    ):
        raise ProfileError(f"안전하지 않은 bundle key: {raw!r}")
    return path.as_posix()


@dataclass(frozen=True)
class BundleSpec:
    key: str
    label: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BundleSpec":
        key = _bundle_key(value.get("key", ""))
        return cls(key=key, label=str(value.get("label", key)))


@dataclass(frozen=True)
class TargetSpec:
    id: str
    texture: str
    name_ko: str
    category: str
    action: str
    bundle_key: str | None
    exact_text: tuple[str, ...]
    notes: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TargetSpec":
        target_id = str(value.get("id", ""))
        texture = str(value.get("texture", ""))
        name_ko = str(value.get("name_ko", ""))
        category = str(value.get("category", ""))
        if not target_id or not texture or not name_ko:
            raise ProfileError(f"대상 필수 필드 누락: {value!r}")
        if category not in {"food", "drink"}:
            raise ProfileError(f"지원하지 않는 음식 분류: {category!r}")
        action = str(value.get("action", "localize"))
        if action not in {"localize", "preserve"}:
            raise ProfileError(f"지원하지 않는 대상 action: {action!r}")
        bundle_key = value.get("bundle_key")
        if bundle_key is not None:
            bundle_key = _bundle_key(bundle_key)
        exact = value.get("exact_text", [name_ko])
        if not isinstance(exact, list) or not all(isinstance(x, str) and x for x in exact):
            raise ProfileError(f"exact_text는 비어 있지 않은 문자열 배열이어야 해요: {target_id}")
        return cls(
            id=target_id,
            texture=texture,
            name_ko=name_ko,
            category=category,
            action=action,
            bundle_key=bundle_key,
            exact_text=tuple(exact),
            notes=str(value.get("notes", "")),
        )


@dataclass(frozen=True)
class CollectionProfile:
    schema_version: int
    id: str
    game_version: str
    bundles: tuple[BundleSpec, ...]
    targets: tuple[TargetSpec, ...]
    ignored_diffuse_tokens: tuple[str, ...]
    path: Path

    @classmethod
    def from_dict(cls, value: dict[str, Any], path: Path) -> "CollectionProfile":
        if value.get("schema_version") != 1:
            raise ProfileError("지원하는 collection schema_version은 1이에요")
        bundles = tuple(BundleSpec.from_dict(x) for x in value.get("bundles", []))
        targets = tuple(TargetSpec.from_dict(x) for x in value.get("targets", []))
        if not bundles or not targets:
            raise ProfileError("프로필에는 bundles와 targets가 모두 필요해요")
        bundle_keys = [x.key for x in bundles]
        target_ids = [x.id for x in targets]
        target_keys = [(x.bundle_key, x.texture) for x in targets]
        for label, items in (
            ("bundle key", bundle_keys),
            ("target id", target_ids),
            ("target texture", target_keys),
        ):
            if len(items) != len(set(items)):
                raise ProfileError(f"중복된 {label}가 있어요")
        known = set(bundle_keys)
        unknown = sorted({x.bundle_key for x in targets if x.bundle_key and x.bundle_key not in known})
        if unknown:
            raise ProfileError(f"대상이 알 수 없는 bundle key를 참조해요: {unknown}")
        return cls(
            schema_version=1,
            id=str(value.get("id", "food")),
            game_version=str(value.get("game_version", "")),
            bundles=bundles,
            targets=targets,
            ignored_diffuse_tokens=tuple(str(x).lower() for x in value.get("ignored_diffuse_tokens", [])),
            path=path,
        )

    def target_by_id(self, target_id: str) -> TargetSpec:
        matches = [target for target in self.targets if target.id == target_id]
        if len(matches) != 1:
            raise ProfileError(f"대상을 찾을 수 없어요: {target_id}")
        return matches[0]
