# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
SQLite TTL cache for distributor API responses.

Zero external dependencies - pure Python stdlib.  Designed for concurrent
CI jobs: one connection per thread (threading.local), WAL journal mode.

Public API
----------
    ApiCache(db_path=None)
    cache.get(key)            -> dict | None
    cache.set(key, value, ttl_hours=24)
    cache.prune()             -> int
    cache.invalidate(pattern) -> int
    cache.stats()             -> CacheStats
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _default_db_path() -> Path:
    import os
    override = os.environ.get("KICAD_CACHE_DIR")
    base = Path(override) if override else Path.home() / ".cache" / "kicad-pipeline"
    return base / "api.db"


@dataclass
class CacheStats:
    hits: int
    misses: int
    total_entries: int

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


_DDL = """
CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    ttl_hours  REAL NOT NULL DEFAULT 24
);
"""


class ApiCache:
    """TTL-aware SQLite cache for distributor API responses."""

    def __init__(self, db_path=None):
        self._db_path = Path(db_path) if db_path else _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._conn()
        conn.execute(_DDL)
        conn.commit()
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def get(self, key: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT value, fetched_at, ttl_hours FROM api_cache WHERE key = ?", (key,)
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
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, value, fetched_at, ttl_hours) VALUES (?,?,?,?)",
            (key, json.dumps(value, ensure_ascii=False), time.time(), ttl_hours),
        )
        conn.commit()

    def prune(self) -> int:
        conn = self._conn()
        now = time.time()
        cur = conn.execute(
            "DELETE FROM api_cache WHERE (? - fetched_at) > ttl_hours * 3600", (now,)
        )
        conn.commit()
        return cur.rowcount

    def invalidate(self, pattern: str) -> int:
        conn = self._conn()
        cur = conn.execute("DELETE FROM api_cache WHERE key LIKE ?", (pattern,))
        conn.commit()
        return cur.rowcount

    def stats(self) -> CacheStats:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
        with self._lock:
            hits, misses = self._hits, self._misses
        return CacheStats(hits=hits, misses=misses, total_entries=total)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
