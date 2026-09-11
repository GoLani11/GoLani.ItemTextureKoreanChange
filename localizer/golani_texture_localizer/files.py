"""Small shared file operations; no game-directory writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def local_path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"프로젝트 내부 상대 경로가 필요해요: {value!r}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"상위 폴더를 참조할 수 없어요: {value}")
    result = (root / path).resolve()
    result.relative_to(root.resolve())
    return result


def descriptor(root: Path, path: Path) -> dict:
    return {"path": path.resolve().relative_to(root.resolve()).as_posix(), "sha256": sha256(path)}


def verified_file(root: Path, value: dict) -> Path:
    path = local_path(root, value["path"])
    if sha256(path) != value["sha256"]:
        raise ValueError(f"기준 파일이 변경됐어요: {value['path']}")
    return path
