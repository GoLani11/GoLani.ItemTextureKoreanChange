from __future__ import annotations

import csv
import html
import json
import os
from collections import Counter
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


CANDIDATE_TIERS = {"confirmed", "probable", "needs_review"}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def effective_tier(result: Mapping[str, Any]) -> str:
    review = result.get("review")
    if isinstance(review, Mapping) and review.get("decision"):
        return str(review["decision"])
    classification = result.get("classification")
    if isinstance(classification, Mapping):
        return str(classification.get("tier", "unknown"))
    return "unknown"


def summary_counts(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(results)
    tier_counts = Counter(effective_tier(row) for row in rows)
    processing_counts = Counter(
        str(row.get("processing", {}).get("status", "unknown")) for row in rows
    )
    reference_count = sum(len(row.get("references", [])) for row in rows)
    return {
        "unique_textures": len(rows),
        "source_references": reference_count,
        "candidate_textures": sum(tier_counts[tier] for tier in CANDIDATE_TIERS),
        "tiers": dict(sorted(tier_counts.items())),
        "processing": dict(sorted(processing_counts.items())),
    }


def _sorted_results(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in results),
        key=lambda row: (str(row.get("asset_id", "")), str(row.get("representative_source", ""))),
    )


def _jsonl(results: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in results
    )


def _csv(results: list[dict[str, Any]]) -> str:
    stream = StringIO(newline="")
    fields = [
        "asset_id",
        "processing_status",
        "tier",
        "effective_tier",
        "score",
        "scripts",
        "ocr_text",
        "representative_source",
        "reference_count",
        "asset_types",
        "groups",
        "error",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in results:
        references = row.get("references", [])
        metadata_rows = [ref.get("metadata", {}) for ref in references if isinstance(ref, Mapping)]
        detections = row.get("detections", [])
        writer.writerow(
            {
                "asset_id": row.get("asset_id", ""),
                "processing_status": row.get("processing", {}).get("status", ""),
                "tier": row.get("classification", {}).get("tier", ""),
                "effective_tier": effective_tier(row),
                "score": row.get("classification", {}).get("score", ""),
                "scripts": "|".join(row.get("classification", {}).get("scripts", [])),
                "ocr_text": " | ".join(
                    str(item.get("text", "")) for item in detections if item.get("text")
                ),
                "representative_source": row.get("representative_source", ""),
                "reference_count": len(references),
                "asset_types": "|".join(
                    sorted({str(meta.get("asset_type", "unknown")) for meta in metadata_rows})
                ),
                "groups": "|".join(
                    sorted(
                        {
                            str(group)
                            for meta in metadata_rows
                            for group in meta.get("groups", [])
                        }
                    )
                ),
                "error": row.get("processing", {}).get("error", ""),
            }
        )
    return stream.getvalue()


def _short_text(value: str, limit: int = 220) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _html(run: Mapping[str, Any], results: list[dict[str, Any]]) -> str:
    summary = summary_counts(results)
    rows: list[str] = []
    order = {"confirmed": 0, "probable": 1, "needs_review": 2, "rejected": 3}
    report_rows = [
        row
        for row in results
        if effective_tier(row) in CANDIDATE_TIERS
        or row.get("processing", {}).get("status") == "error"
    ]
    visible = sorted(
        report_rows,
        key=lambda row: (
            order.get(effective_tier(row), 9),
            -float(row.get("classification", {}).get("score", 0.0) or 0.0),
            str(row.get("asset_id", "")),
        ),
    )
    for row in visible:
        tier = effective_tier(row)
        classification = row.get("classification", {})
        detections = row.get("detections", [])
        texts = " | ".join(
            _short_text(str(item.get("text", "")))
            for item in detections
            if item.get("text")
        )
        preview = row.get("preview")
        if preview:
            preview_html = (
                f'<a href="{html.escape(quote(str(preview)))}">'
                f'<img loading="lazy" src="{html.escape(quote(str(preview)))}" alt="preview"></a>'
            )
        else:
            preview_html = '<span class="muted">미리보기 없음</span>'
        references = row.get("references", [])
        source = html.escape(str(row.get("representative_source", "")))
        error = html.escape(str(row.get("processing", {}).get("error", "")))
        texts_html = html.escape(texts) if texts else '<span class="muted">—</span>'
        error_html = error or '<span class="muted">—</span>'
        rows.append(
            "<tr>"
            f'<td class="preview">{preview_html}</td>'
            f'<td><span class="tier {html.escape(tier)}">{html.escape(tier)}</span><br>'
            f'{float(classification.get("score", 0.0) or 0.0):.3f}</td>'
            f"<td><code>{html.escape(str(row.get('asset_id', '')))}</code><br>"
            f'<span class="source">{source}</span><br>'
            f'<span class="muted">참조 {len(references)}개</span></td>'
            f"<td>{texts_html}</td>"
            f"<td>{error_html}</td>"
            "</tr>"
        )

    summary_json = html.escape(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    title = html.escape(str(run.get("run_id", "OCR selection")))
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1500px; margin: 2rem auto; padding: 0 1rem; background:#111; color:#eee; }}
h1 {{ margin-bottom:.25rem; }}
.summary {{ color:#bbb; margin-bottom:1.5rem; word-break:break-all; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border-bottom:1px solid #333; padding:.65rem; text-align:left; vertical-align:top; }}
th {{ position:sticky; top:0; background:#181818; }}
.preview {{ width:180px; }}
img {{ width:170px; max-height:170px; object-fit:contain; background:#282828; }}
.tier {{ display:inline-block; padding:.15rem .45rem; border-radius:.35rem; font-weight:700; }}
.confirmed {{ background:#155d35; }} .probable {{ background:#745b08; }}
.needs_review {{ background:#70411d; }} .rejected {{ background:#444; }}
.error {{ background:#7a1f2a; }} .skipped {{ background:#3d4752; }}
code,.source {{ word-break:break-all; }} .muted {{ color:#888; }}
</style>
</head>
<body>
<h1>텍스처 문자 OCR 선별</h1>
<div class="summary">실행: {title}<br>{summary_json}</div>
<table>
<thead><tr><th>미리보기</th><th>등급</th><th>에셋</th><th>OCR</th><th>오류</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
</body>
</html>
"""


def write_reports(
    run_dir: str | Path,
    run: Mapping[str, Any],
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    destination = Path(run_dir)
    destination.mkdir(parents=True, exist_ok=True)
    rows = _sorted_results(results)
    summary = summary_counts(rows)
    run_payload = dict(run)
    run_payload["summary"] = summary
    _atomic_write(
        destination / "run.json",
        json.dumps(run_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write(destination / "results.jsonl", _jsonl(rows))
    _atomic_write(destination / "summary.csv", _csv(rows))
    _atomic_write(destination / "report.html", _html(run_payload, rows))
    return summary


def load_results(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{line_number}: 손상된 JSONL입니다: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{source}:{line_number}: JSON 객체가 아닙니다")
        results.append(value)
    return results
