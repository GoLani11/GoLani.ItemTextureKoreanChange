from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .config import PROJECT_ROOT


DEFAULT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class AssetSource:
    path: Path
    source_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
        try:
            display_path = self.path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            display_path = str(self.path.resolve())
        return {
            "source_id": self.source_id,
            "path": display_path,
            "metadata": self.metadata,
        }


@dataclass
class Discovery:
    sources: list[AssetSource]
    missing: list[dict[str, Any]]
    duplicates_ignored: int = 0


def _load_manifest_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        records = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: JSON 객체가 아닙니다")
            records.append(value)
        return records

    value = json.loads(text)
    if isinstance(value, list):
        records = value
    elif isinstance(value, dict):
        records = None
        for key in ("assets", "entries", "records"):
            if isinstance(value.get(key), list):
                records = value[key]
                break
        if records is None:
            raise ValueError(
                f"지원하지 않는 manifest 구조입니다: {path} (assets/entries/records 배열 필요)"
            )
    else:
        raise ValueError(f"manifest 루트가 배열 또는 객체가 아닙니다: {path}")

    if not all(isinstance(record, dict) for record in records):
        raise ValueError(f"manifest 항목은 모두 JSON 객체여야 합니다: {path}")
    return list(records)


def infer_asset_type(metadata: Mapping[str, Any]) -> str:
    explicit = metadata.get("asset_type", metadata.get("assetType"))
    if explicit in {"item", "map"}:
        return str(explicit)
    key = str(
        metadata.get("bundle_key", metadata.get("key", metadata.get("source", "")))
    ).lower()
    if "/items/" in key or "/usable_items/" in key:
        return "item"
    if metadata.get("scene") or metadata.get("scene_path") or metadata.get("map"):
        return "map"
    return "unknown"


def infer_groups(metadata: Mapping[str, Any]) -> list[str]:
    raw = metadata.get("groups")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Sequence):
        values = [str(value) for value in raw if str(value).strip()]
    else:
        values = []
    for key in ("map", "category"):
        value = metadata.get(key)
        if value and str(value) not in values:
            values.append(str(value))
    return values


def _normalize_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(record)
    if "bundle_key" not in metadata and "key" in metadata:
        metadata["bundle_key"] = metadata["key"]
    if "texture_name" not in metadata and "texture" in metadata:
        metadata["texture_name"] = metadata["texture"]
    metadata["asset_type"] = infer_asset_type(metadata)
    metadata["groups"] = infer_groups(metadata)
    return metadata


def _entry_image(record: Mapping[str, Any]) -> str | None:
    for key in ("source", "image", "png", "path"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _resolve_entry_path(
    raw: str,
    roots: Sequence[Path],
    manifest_path: Path,
    project_root: Path,
) -> Path:
    windows_drive = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if windows_drive and os.name != "nt":
        remainder = windows_drive.group(2).replace("\\", "/")
        return Path(f"/mnt/{windows_drive.group(1).lower()}/{remainder}").resolve()
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path.resolve()
    candidates = [root / path for root in roots]
    candidates.extend((manifest_path.parent / path, project_root / path))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve() if candidates else (manifest_path.parent / path).resolve()


def _source_id(path: Path, metadata: Mapping[str, Any]) -> str:
    explicit = metadata.get("asset_id")
    if explicit:
        return str(explicit)
    metadata_key = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    seed = "\0".join((str(path.resolve()), metadata_key))
    return f"src_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def discover_sources(
    input_roots: Iterable[str | Path],
    manifest_path: str | Path | None = None,
    *,
    include_unmanifested: bool = False,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    project_root: Path = PROJECT_ROOT,
) -> Discovery:
    roots = [Path(root).expanduser().resolve() for root in input_roots]
    ext_set = {
        value.lower() if str(value).startswith(".") else f".{str(value).lower()}"
        for value in extensions
    }
    sources: list[AssetSource] = []
    missing: list[dict[str, Any]] = []
    manifest_paths: set[str] = set()
    seen_records: set[str] = set()
    duplicates_ignored = 0

    if manifest_path:
        manifest = Path(manifest_path).expanduser().resolve()
        for record in _load_manifest_records(manifest):
            raw = _entry_image(record)
            if not raw:
                missing.append({"reason": "manifest_image_field_missing", "record": record})
                continue
            path = _resolve_entry_path(raw, roots, manifest, project_root)
            metadata = _normalize_metadata(record)
            if not path.is_file():
                missing.append(
                    {"reason": "image_missing", "path": str(path), "metadata": metadata}
                )
                continue
            path_key = str(path).casefold()
            record_key = hashlib.sha256(
                (
                    path_key
                    + "\0"
                    + json.dumps(
                        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                ).encode("utf-8")
            ).hexdigest()
            if record_key in seen_records:
                duplicates_ignored += 1
                continue
            seen_records.add(record_key)
            manifest_paths.add(path_key)
            sources.append(AssetSource(path, _source_id(path, metadata), metadata))

    if not manifest_path or include_unmanifested:
        for root in roots:
            if root.is_file() and root.suffix.lower() in ext_set:
                paths = [root]
            elif root.is_dir():
                paths = sorted(
                    path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in ext_set
                )
            else:
                missing.append({"reason": "input_missing", "path": str(root)})
                continue
            for path in paths:
                key = str(path.resolve()).casefold()
                if key in manifest_paths:
                    continue
                manifest_paths.add(key)
                metadata = _normalize_metadata({"source": str(path.resolve())})
                sources.append(AssetSource(path.resolve(), _source_id(path, metadata), metadata))

    sources.sort(key=lambda source: (str(source.path).casefold(), source.source_id))
    return Discovery(sources=sources, missing=missing, duplicates_ignored=duplicates_ignored)


def color_filter_reason(source: AssetSource, filter_config: Mapping[str, Any]) -> str | None:
    if not filter_config.get("skip_non_color", True):
        return None
    name = str(
        source.metadata.get("texture_name")
        or source.metadata.get("texture")
        or source.path.stem
    ).lower()
    tokens = [token for token in _TOKEN_SPLIT.split(name) if token]
    if not tokens:
        return None

    color_tokens = {str(value).lower() for value in filter_config.get("color_tokens", [])}
    non_color_tokens = {
        str(value).lower() for value in filter_config.get("non_color_tokens", [])
    }
    last = tokens[-1]
    if last in color_tokens:
        return None
    if last in non_color_tokens:
        return f"non_color_suffix:{last}"
    if any(token in non_color_tokens for token in tokens[-2:]):
        return "non_color_token"
    return None
