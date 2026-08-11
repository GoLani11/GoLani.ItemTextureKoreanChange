from __future__ import annotations

import json
from pathlib import Path

from .models import CollectionProfile


def load_profile(path: Path) -> CollectionProfile:
    resolved = path.expanduser().resolve()
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"프로필 루트는 JSON object여야 해요: {resolved}")
    return CollectionProfile.from_dict(data, resolved)
