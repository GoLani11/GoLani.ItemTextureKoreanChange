from __future__ import annotations

import re


def safe_bundle_name(key: str) -> str:
    value = key.replace("\\", "/").strip("/").replace("/", "@")
    return re.sub(r"[^0-9A-Za-z._@-]", "_", value)


def texture_role(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith("_n") or "nrm" in lowered or "normal" in lowered:
        return "normal"
    if lowered.endswith("_g") or "gloss" in lowered or "smooth" in lowered:
        return "gloss"
    if lowered.endswith("_d") or "diff" in lowered or "albedo" in lowered or "basecolor" in lowered:
        return "diffuse"
    return "other"


def texture_family(name: str) -> str:
    lowered = name.lower()
    for suffix in (
        "_lod0_diff",
        "_lod0_nrm",
        "_lod0_gloss",
        "_diff",
        "_nrm",
        "_gloss",
        "_d",
        "_n",
        "_g",
    ):
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered
