from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_config.json")


class ConfigError(ValueError):
    """Raised when the OCR selector configuration is invalid."""


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"설정 JSON이 올바르지 않습니다: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"설정 루트는 JSON 객체여야 합니다: {path}")
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    config = _read_json(DEFAULT_CONFIG_PATH)
    if path:
        config = _deep_merge(config, _read_json(Path(path).expanduser().resolve()))
    validate_config(config)
    return config


def validate_config(config: Mapping[str, Any]) -> None:
    try:
        classification = config["classification"]
        confirmed = float(classification["confirmed_score"])
        probable = float(classification["probable_score"])
        review = float(classification["review_score"])
        agreement = float(classification["agreement_score"])
        agreement_similarity = float(classification.get("agreement_text_similarity", 0.8))
        confirmed_letters = int(classification["confirmed_min_letters"])
        probable_letters = int(classification["probable_min_letters"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError("classification 설정이 없거나 숫자 형식이 아닙니다") from exc

    if not (0.0 <= review <= probable <= confirmed <= 1.0):
        raise ConfigError(
            "점수는 0 <= review_score <= probable_score <= confirmed_score <= 1 이어야 합니다"
        )
    if not (0.0 <= agreement <= 1.0):
        raise ConfigError("agreement_score는 0과 1 사이여야 합니다")
    if not (0.0 <= agreement_similarity <= 1.0):
        raise ConfigError("agreement_text_similarity는 0과 1 사이여야 합니다")
    if confirmed_letters < 1 or probable_letters < 1:
        raise ConfigError("최소 문자 수는 1 이상이어야 합니다")

    preprocess = config.get("preprocess")
    if not isinstance(preprocess, Mapping):
        raise ConfigError("preprocess 설정이 필요합니다")
    rotations = preprocess.get("rotations")
    if not isinstance(rotations, list) or not rotations:
        raise ConfigError("preprocess.rotations는 비어 있지 않은 배열이어야 합니다")
    if any(int(value) not in (0, 90, 180, 270) for value in rotations):
        raise ConfigError("회전값은 0, 90, 180, 270 중 하나여야 합니다")
    tile_size = int(preprocess.get("tile_size", 0))
    overlap = int(preprocess.get("tile_overlap", 0))
    if tile_size < 64 or overlap < 0 or overlap >= tile_size:
        raise ConfigError("tile_size는 64 이상이고 tile_overlap은 그보다 작아야 합니다")
    if int(preprocess.get("max_variants", 0)) < 0:
        raise ConfigError("max_variants는 0(무제한) 이상이어야 합니다")

    engines = config.get("engines")
    if not isinstance(engines, Mapping):
        raise ConfigError("engines 설정이 필요합니다")
    primary = engines.get("primary")
    if not isinstance(primary, Mapping) or not primary.get("enabled", True):
        raise ConfigError("활성화된 primary OCR 엔진이 필요합니다")
    supported = {"paddleocr", "easyocr"}
    for label in ("primary", "fallback"):
        engine = engines.get(label)
        if not engine or not engine.get("enabled", True):
            continue
        if engine.get("name") not in supported:
            raise ConfigError(f"지원하지 않는 OCR 엔진입니다: {engine.get('name')}")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def ocr_profile_digest(config: Mapping[str, Any]) -> str:
    """Digest settings that can change the adaptively collected OCR evidence.

    The pipeline may stop variants or skip the fallback after a confirmed
    classification, so thresholds are part of the cache identity.  This avoids
    treating a partial evidence set as complete after thresholds change.
    """

    return digest(
        {
            "preprocess": config["preprocess"],
            "engines": config["engines"],
            "classification": config["classification"],
            "runtime": {
                "early_stop_on_confirmed": config.get("runtime", {}).get(
                    "early_stop_on_confirmed", True
                )
            },
            "cache_schema": config.get("cache_schema", 1),
        }
    )


def classification_profile_digest(config: Mapping[str, Any]) -> str:
    return digest(config["classification"])


def resolve_project_path(
    value: str | os.PathLike[str] | None,
    project_root: Path = PROJECT_ROOT,
) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    raw = os.path.expandvars(os.path.expanduser(str(value)))
    windows_drive = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if windows_drive and os.name != "nt":
        remainder = windows_drive.group(2).replace("\\", "/")
        raw = f"/mnt/{windows_drive.group(1).lower()}/{remainder}"
    path = Path(raw)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()
