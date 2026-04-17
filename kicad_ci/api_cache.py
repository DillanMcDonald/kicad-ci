# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
SQLite TTL cache for distributor API responses (SI-4).

Zero external dependencies — pure Python stdlib.  Designed for concurrent
CI jobs: one connection per thread (threading.local), WAL journal mode,
and a unique-suffix DB path when needed.

Public API
----------
    ApiCache(db_path=None)
        Open (or create) a cache database.

    cache.get(key)            -> dict | None
    cache.set(key, value, ttl_hours=24)
    cache.prune()             -> int   (rows deleted)
    cache.invalidate(pattern) -> int   (rows deleted; SQL LIKE wildcards)
    cache.stats()             -> CacheStats

    CacheStats(hits, misses, hit_rate, total_entries)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Default cache location
# ---------------------------------------------------------------------------

_DEFAULT_DB: Optional[Path] = None  # resolved lazily


def _default_db_path() -> Path:
    import os

    override = os.environ.get("KICAD_CACHE_DIR")
    if override:
        base = Path(override)
    else:
        base = Path.home() / ".cache" / "kicad-pipeline"
    return base / "api.db"


# ---------------------------------------------------------------------------
# Stats dataclass
# ---------------------------------------------------------------------------

@dataclass
class CacheStats:
    """Hit/miss statistics for a :class:`ApiCache` instance."""
    hits: int
    misses: int
    total_entries: int

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups served from cache (0.0–1.0)."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def __repr__(self) -> str:
        return (
            f"CacheStats(hits={self.hits}, misses={self.misses}, "
            f"hit_rate={self.hit_rate:.1%}, total_entries={self.total_entries})"
        )


# ---------------------------------------------------------------------------
# Cache implementation
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    fetched_at REAL    NOT NULL,
    ttl_hours  REAL    NOT NULL DEFAULT 24
);
"""


class ApiCache:
    """
    TTL-aware SQLite cache for distributor API responses.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Created (including parent dirs)
        if it does not exist.  Defaults to ``~/.cache/kicad-pipeline/api.db``
        or ``$KICAD_CACHE_DIR/api.db``.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is None:
            self._db_path = _default_db_path()
        else:
            self._db_path = Path(db_path)

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialise schema on first open
        conn = self._conn()
        conn.execute(_DDL)
        conn.commit()

        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()  # protects _hits/_misses counters

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a per-thread SQLite connection, creating it if needed."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Core read/write
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[dict]:
        """
        Retrieve a cached value.

        Returns the decoded dict if the entry exists and has not expired;
        ``None`` otherwise.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT value, fetched_at, ttl_hours FROM api_cache WHERE key = ?",
            (key,),
        ).fetchone()

        if row is None:
            with self._lock:
                self._misses += 1
            return None

        age = abs(time.time() - row["fetched_at"])
        if age < row["ttl_hours"] * 3600:
            with self._lock:
                self._hits += 1
            return json.loads(row["value"])

        with self._lock:
            self._misses += 1
        return None

    def set(self, key: str, value: dict, ttl_hours: float = 24.0) -> None:
        """
        Store a value in the cache.

        Overwrites any existing entry for *key* (expired or not).

        Parameters
        ----------
        key:
            Cache key, e.g. ``"mouser::RC0402FR-07100KL"``.
        value:
            JSON-serialisable dict (distributor API response).
        ttl_hours:
            Time-to-live in hours (default 24).
        """
        conn = self._conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO api_cache (key, value, fetched_at, ttl_hours)
            VALUES (?, ?, ?, ?)
            """,
            (key, json.dumps(value, ensure_ascii=False), time.time(), ttl_hours),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def prune(self) -> int:
        """
        Delete all expired entries.

        Returns the number of rows deleted.
        """
        conn = self._conn()
        now = time.time()
        cursor = conn.execute(
            """
            DELETE FROM api_cache
            WHERE (? - fetched_at) > ttl_hours * 3600
            """,
            (now,),
        )
        conn.commit()
        return cursor.rowcount

    def invalidate(self, pattern: str) -> int:
        """
        Delete cache entries whose key matches a SQL LIKE *pattern*.

        Use ``%`` as the wildcard (not ``*``).  E.g.::

            cache.invalidate("mouser::%")   # all Mouser entries
            cache.invalidate("%RC0402%")    # any key containing RC0402

        Returns the number of rows deleted.
        """
        conn = self._conn()
        cursor = conn.execute(
            "DELETE FROM api_cache WHERE key LIKE ?",
            (pattern,),
        )
        conn.commit()
        return cursor.rowcount

    def stats(self) -> CacheStats:
        """Return hit/miss statistics and total entry count."""
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
        with self._lock:
            hits, misses = self._hits, self._misses
        return CacheStats(hits=hits, misses=misses, total_entries=total)

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "ApiCache":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        """Close the per-thread connection if open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
