from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CACHE_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ResultCache:
    """Transactional OCR detection cache and review store."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ocr_results (
                cache_key TEXT PRIMARY KEY,
                pixel_sha256 TEXT NOT NULL,
                profile_digest TEXT NOT NULL,
                engine_signature TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ocr_results_pixel
                ON ocr_results(pixel_sha256);

            CREATE TABLE IF NOT EXISTS reviews (
                asset_id TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                reviewer TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL
            );
            """
        )
        existing = self.connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if existing and int(existing["value"]) != CACHE_SCHEMA_VERSION:
            raise RuntimeError(
                f"지원하지 않는 OCR 캐시 스키마입니다: {existing['value']} "
                f"(도구: {CACHE_SCHEMA_VERSION})"
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(CACHE_SCHEMA_VERSION),),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ResultCache":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def get_result(self, cache_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT result_json FROM ocr_results WHERE cache_key=?", (cache_key,)
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["result_json"])
        except json.JSONDecodeError:
            self.connection.execute("DELETE FROM ocr_results WHERE cache_key=?", (cache_key,))
            self.connection.commit()
            return None
        return value if isinstance(value, dict) else None

    def put_result(
        self,
        cache_key: str,
        pixel_sha256: str,
        profile_digest: str,
        engine_signature: str,
        result: dict[str, Any],
    ) -> None:
        processing = result.get("processing", {})
        if processing.get("status") != "ok":
            raise ValueError("정상 완료(ok) 결과만 OCR 캐시에 저장할 수 있습니다")
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO ocr_results(
                    cache_key, pixel_sha256, profile_digest,
                    engine_signature, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    pixel_sha256,
                    profile_digest,
                    engine_signature,
                    payload,
                    utc_now_iso(),
                ),
            )

    def result_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM ocr_results").fetchone()
        return int(row["count"])

    def set_review(
        self,
        asset_id: str,
        decision: str,
        note: str = "",
        reviewer: str = "",
    ) -> None:
        allowed = {"confirmed", "probable", "needs_review", "rejected", "clear"}
        if decision not in allowed:
            raise ValueError(f"지원하지 않는 검수 결정입니다: {decision}")
        with self.connection:
            if decision == "clear":
                self.connection.execute("DELETE FROM reviews WHERE asset_id=?", (asset_id,))
            else:
                self.connection.execute(
                    """
                    INSERT OR REPLACE INTO reviews(
                        asset_id, decision, note, reviewer, reviewed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (asset_id, decision, note, reviewer, utc_now_iso()),
                )

    def get_review(self, asset_id: str) -> dict[str, str] | None:
        row = self.connection.execute(
            "SELECT * FROM reviews WHERE asset_id=?", (asset_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_reviews(self, asset_ids: Iterable[str] | None = None) -> dict[str, dict[str, str]]:
        if asset_ids is None:
            rows = self.connection.execute("SELECT * FROM reviews ORDER BY asset_id")
        else:
            ids = sorted(set(asset_ids))
            if not ids:
                return {}
            placeholders = ",".join("?" for _ in ids)
            rows = self.connection.execute(
                f"SELECT * FROM reviews WHERE asset_id IN ({placeholders}) ORDER BY asset_id",
                ids,
            )
        return {str(row["asset_id"]): dict(row) for row in rows}
