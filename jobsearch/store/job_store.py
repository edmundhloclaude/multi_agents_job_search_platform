"""Repository over SQLite for the job store (spec §3).

All agents read/write ONLY through this class. Dedup lives here: ``upsert_posting``
does insert-or-update-last_seen on ``dedup_key`` collision so the Crawler
check-and-skips automatically. No raw SQL is exposed to callers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import (
    ApplyStatus,
    Posting,
    ResponseStatus,
    ScreenStatus,
)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """SQLite-backed repository. Wraps a single connection."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._apply_schema()

    def _apply_schema(self) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(sql)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #
    def upsert_posting(self, posting: Posting) -> tuple[bool, str]:
        """Insert a new posting or bump ``last_seen`` on dedup collision.

        Returns ``(is_new, dedup_key)``. ``is_new=False`` means the posting was
        already known — the Crawler uses this to skip deep re-extraction.
        Lifecycle fields (screen/apply/response) of an existing row are NOT
        overwritten, so re-crawling never clobbers screening/application state.
        """
        now = _utcnow()
        row = posting.to_row()
        key = row["dedup_key"]

        cur = self._conn.execute("SELECT dedup_key FROM jobs WHERE dedup_key = ?", (key,))
        exists = cur.fetchone() is not None

        if exists:
            # Only refresh volatile discovery fields; preserve pipeline state.
            self._conn.execute(
                """UPDATE jobs
                   SET last_seen = ?, source_url = ?, comp_text = ?,
                       requirements = ?, application_method = ?, raw = ?
                   WHERE dedup_key = ?""",
                (
                    now,
                    row["source_url"],
                    row["comp_text"],
                    row["requirements"],
                    row["application_method"],
                    row["raw"],
                    key,
                ),
            )
            self._conn.commit()
            return (False, key)

        self._conn.execute(
            """INSERT INTO jobs (
                   dedup_key, source, source_url, first_seen, last_seen, raw,
                   company, title, location, comp_text, requirements,
                   application_method, screen_status, screen_score,
                   screen_rationale, apply_status, resume_path,
                   cover_letter_path, response_status
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, row["source"], row["source_url"], now, now, row["raw"],
                row["company"], row["title"], row["location"], row["comp_text"],
                row["requirements"], row["application_method"],
                row["screen_status"], row["screen_score"], row["screen_rationale"],
                row["apply_status"], row["resume_path"], row["cover_letter_path"],
                row["response_status"],
            ),
        )
        self._conn.commit()
        return (True, key)

    # ------------------------------------------------------------------ #
    # Read path
    # ------------------------------------------------------------------ #
    def get(self, dedup_key: str) -> Optional[Posting]:
        cur = self._conn.execute("SELECT * FROM jobs WHERE dedup_key = ?", (dedup_key,))
        r = cur.fetchone()
        return Posting.from_row(dict(r)) if r else None

    def get_by_status(
        self,
        *,
        screen_status: Optional[str] = None,
        apply_status: Optional[str] = None,
        response_status: Optional[str] = None,
    ) -> list[Posting]:
        clauses, params = [], []
        if screen_status is not None:
            clauses.append("screen_status = ?")
            params.append(_val(screen_status))
        if apply_status is not None:
            clauses.append("apply_status = ?")
            params.append(_val(apply_status))
        if response_status is not None:
            clauses.append("response_status = ?")
            params.append(_val(response_status))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.execute(f"SELECT * FROM jobs{where} ORDER BY first_seen", params)
        return [Posting.from_row(dict(r)) for r in cur.fetchall()]

    def all(self) -> list[Posting]:
        cur = self._conn.execute("SELECT * FROM jobs ORDER BY first_seen")
        return [Posting.from_row(dict(r)) for r in cur.fetchall()]

    def status_counts(self) -> dict[str, dict[str, int]]:
        """Aggregate counts for the ``status`` CLI command."""
        out: dict[str, dict[str, int]] = {"screen": {}, "apply": {}, "response": {}}
        for col, key in (("screen_status", "screen"),
                         ("apply_status", "apply"),
                         ("response_status", "response")):
            cur = self._conn.execute(f"SELECT {col} AS s, COUNT(*) AS n FROM jobs GROUP BY {col}")
            for r in cur.fetchall():
                out[key][r["s"]] = r["n"]
        return out

    # ------------------------------------------------------------------ #
    # Annotation / state transitions
    # ------------------------------------------------------------------ #
    def annotate_screen(
        self, dedup_key: str, *, status: str, score: int, rationale: str
    ) -> None:
        self._require(dedup_key)
        self._conn.execute(
            "UPDATE jobs SET screen_status = ?, screen_score = ?, screen_rationale = ? "
            "WHERE dedup_key = ?",
            (_val(status), int(score), rationale, dedup_key),
        )
        self._conn.commit()

    def set_docs(self, dedup_key: str, *, resume_path: str, cover_letter_path: str) -> None:
        self._require(dedup_key)
        self._conn.execute(
            "UPDATE jobs SET resume_path = ?, cover_letter_path = ? WHERE dedup_key = ?",
            (resume_path, cover_letter_path, dedup_key),
        )
        self._conn.commit()

    def set_apply_status(self, dedup_key: str, status: str) -> None:
        self._require(dedup_key)
        self._conn.execute(
            "UPDATE jobs SET apply_status = ? WHERE dedup_key = ?",
            (_val(status), dedup_key),
        )
        self._conn.commit()

    def record_response(self, dedup_key: str, status: str) -> None:
        self._require(dedup_key)
        self._conn.execute(
            "UPDATE jobs SET response_status = ? WHERE dedup_key = ?",
            (_val(status), dedup_key),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # Run log (spec §5)
    # ------------------------------------------------------------------ #
    def reset(self) -> int:
        """Clear all postings and the run log. Returns the number of postings removed.
        Keeps the schema/file so the store is immediately reusable."""
        n = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self._conn.execute("DELETE FROM jobs")
        self._conn.execute("DELETE FROM run_log")
        self._conn.commit()
        return int(n)

    def log_run(self, stage: str, tier: str = "", message: str = "") -> None:
        self._conn.execute(
            "INSERT INTO run_log (ts, stage, tier, message) VALUES (?,?,?,?)",
            (_utcnow(), stage, tier, message),
        )
        self._conn.commit()

    def recent_runs(self, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            "SELECT ts, stage, tier, message FROM run_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    def _require(self, dedup_key: str) -> None:
        if self.get(dedup_key) is None:
            raise KeyError(f"No posting with dedup_key={dedup_key!r}")


def _val(status) -> str:
    """Accept either an enum member or a raw string."""
    if isinstance(status, (ScreenStatus, ApplyStatus, ResponseStatus)):
        return status.value
    return str(status)
