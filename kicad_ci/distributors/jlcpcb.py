# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""
JLCPCB/LCSC parts database client (F6-T6).

Downloads the weekly-updated JLCPCB parts CSV (no scraping, no unofficial
API) and indexes it into a local SQLite database with FTS5 for MPN matching.

Download URL: https://jlcpcb.com/componentSearch/uploadComponentInfo
Local cache:  $JLCPCB_DB_PATH/parts.csv.gz  (default ~/.cache/kicad-pipeline/)
SQLite index: $JLCPCB_DB_PATH/jlcpcb_parts.db

CRITICAL: SQLite connections are NOT thread-safe; use threading.local() for
per-thread connections (same pattern as ApiCache).

Gotchas:
- CSV column layout has changed historically — check headers at load time.
- File is ~150 MB uncompressed; use streaming reader (csv.reader on gzip).
- Auto-download if DB missing or parts.csv.gz older than 7 days.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Optional

from kicad_ci.distributors.base import (
    DistributorClient,
    PriceBreak,
    PriceResult,
    register_distributor,
)

_log = logging.getLogger(__name__)

_DOWNLOAD_URL = "https://jlcpcb.com/componentSearch/uploadComponentInfo"
_DB_MAX_AGE_DAYS = 7
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jlcpcb_parts (
    lcsc_pn      TEXT PRIMARY KEY,
    mpn          TEXT NOT NULL,
    manufacturer TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    stock        INTEGER NOT NULL DEFAULT 0,
    price_usd    TEXT NOT NULL DEFAULT '0',   -- stored as Decimal string
    moq          INTEGER NOT NULL DEFAULT 1,
    datasheet    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jlcpcb_mpn ON jlcpcb_parts(mpn);
CREATE VIRTUAL TABLE IF NOT EXISTS jlcpcb_fts USING fts5(
    mpn,
    content='jlcpcb_parts',
    content_rowid='rowid'
);
"""

# Known CSV column names (checked at load time)
_EXPECTED_COLS = {"LCSC Part", "MFR.Part", "Manufacturer", "Stock", "Price", "MOQ"}


def _db_dir() -> Path:
    override = os.environ.get("JLCPCB_DB_PATH")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "kicad-pipeline"


@register_distributor("jlcpcb")
class JLCPCBClient(DistributorClient):
    """
    JLCPCB/LCSC parts database client.

    Maintains a local SQLite index built from JLCPCB's weekly CSV export.
    The DB is rebuilt if the CSV is missing or older than 7 days.
    """

    display_name = "JLCPCB / LCSC"
    cache_ttl_hours = 168.0  # 7 days — matches CSV update cadence

    def __init__(self) -> None:
        self._dir = _db_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._csv_path = self._dir / "parts.csv.gz"
        self._db_path = self._dir / "jlcpcb_parts.db"
        self._local = threading.local()
        self._ready = False  # set True after first successful DB init

    # ------------------------------------------------------------------
    # Connection management (per-thread, safe for ThreadPoolExecutor)
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # DB initialisation
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> bool:
        """Download CSV if stale, build SQLite index if needed. Returns True on success."""
        if self._ready and self._db_path.exists():
            return True

        if self._csv_stale():
            _log.info("JLCPCB: downloading parts CSV from %s", _DOWNLOAD_URL)
            if not self._download_csv():
                _log.warning("JLCPCB: download failed; skipping")
                return False

        if not self._db_path.exists() or self._csv_stale():
            _log.info("JLCPCB: building SQLite index…")
            self._build_index()

        self._ready = True
        return True

    def _csv_stale(self) -> bool:
        if not self._csv_path.exists():
            return True
        age_days = (time.time() - self._csv_path.stat().st_mtime) / 86400
        return age_days > _DB_MAX_AGE_DAYS

    def _download_csv(self) -> bool:
        try:
            tmp = self._csv_path.with_suffix(".tmp")
            urllib.request.urlretrieve(_DOWNLOAD_URL, tmp)
            tmp.rename(self._csv_path)
            return True
        except Exception as exc:
            _log.error("JLCPCB download error: %s", exc)
            return False

    def _build_index(self) -> None:
        """Stream-parse the CSV and populate SQLite (handles ~150 MB file)."""
        conn = self._conn()
        conn.executescript(_SCHEMA)

        # Detect column layout on first rows
        with gzip.open(self._csv_path, "rt", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            headers = next(reader, None)
            if headers is None:
                return

            col = {h.strip(): i for i, h in enumerate(headers)}
            missing = _EXPECTED_COLS - set(col.keys())
            if missing:
                # Try alternative header names used in older CSV versions
                col = _remap_headers(col)
                missing = _EXPECTED_COLS - set(col.keys())
                if missing:
                    _log.error("JLCPCB CSV missing columns: %s", missing)
                    return

            conn.execute("DELETE FROM jlcpcb_parts")
            conn.execute("DELETE FROM jlcpcb_fts")

            batch: list[tuple] = []
            for row in reader:
                if len(row) < max(col.values()) + 1:
                    continue
                try:
                    lcsc = row[col["LCSC Part"]].strip()
                    mpn = row[col["MFR.Part"]].strip()
                    mfr = row[col["Manufacturer"]].strip()
                    stock_str = row[col["Stock"]].strip().replace(",", "")
                    price_str = row[col["Price"]].strip().lstrip("$").replace(",", "")
                    moq_str = row[col["MOQ"]].strip().replace(",", "")
                    desc = row[col.get("Description", -1)].strip() if "Description" in col else ""
                    ds = row[col.get("Datasheet", -1)].strip() if "Datasheet" in col else ""
                except (IndexError, KeyError):
                    continue

                try:
                    stock = int(float(stock_str)) if stock_str else 0
                    price = str(Decimal(price_str)) if price_str else "0"
                    moq = int(float(moq_str)) if moq_str else 1
                except Exception:
                    continue

                batch.append((lcsc, mpn, mfr, desc, stock, price, moq, ds))

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO jlcpcb_parts "
                        "(lcsc_pn,mpn,manufacturer,description,stock,price_usd,moq,datasheet) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        batch,
                    )
                    conn.commit()
                    batch.clear()

            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO jlcpcb_parts "
                    "(lcsc_pn,mpn,manufacturer,description,stock,price_usd,moq,datasheet) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    batch,
                )
                conn.commit()

            # Rebuild FTS index
            conn.execute("INSERT INTO jlcpcb_fts(jlcpcb_fts) VALUES('rebuild')")
            conn.commit()

    # ------------------------------------------------------------------
    # DistributorClient interface
    # ------------------------------------------------------------------

    def search_by_mpn(self, mpn: str) -> Optional[PriceResult]:
        if not self._ensure_ready():
            return None

        conn = self._conn()

        # Try exact match first
        row = conn.execute(
            "SELECT * FROM jlcpcb_parts WHERE mpn = ? LIMIT 1",
            (mpn,),
        ).fetchone()

        # FTS5 approximate match fallback
        if row is None:
            try:
                row = conn.execute(
                    """
                    SELECT p.* FROM jlcpcb_parts p
                    JOIN jlcpcb_fts f ON p.rowid = f.rowid
                    WHERE jlcpcb_fts MATCH ?
                    ORDER BY rank LIMIT 1
                    """,
                    (mpn,),
                ).fetchone()
            except sqlite3.OperationalError:
                pass

        if row is None:
            return None

        try:
            price = Decimal(row["price_usd"]) if row["price_usd"] else Decimal("0")
        except Exception:
            price = Decimal("0")

        breaks = [PriceBreak(min_qty=int(row["moq"] or 1), unit_price_usd=price)]

        return PriceResult(
            mpn=row["mpn"],
            manufacturer=row["manufacturer"],
            stock=int(row["stock"] or 0),
            moq=int(row["moq"] or 1),
            price_breaks=breaks,
            currency="USD",
            distributor="jlcpcb",
            product_url=f"https://www.lcsc.com/search?q={requests_quote(row['lcsc_pn'])}",
            datasheet_url=row["datasheet"],
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _remap_headers(col: dict) -> dict:
    """Map known alternate column names to canonical names."""
    aliases = {
        "LCSC#": "LCSC Part",
        "Mfr. Part #": "MFR.Part",
        "MFR Part": "MFR.Part",
        "Qty Available": "Stock",
        "Unit Price(USD)": "Price",
        "Minimum Quantity": "MOQ",
    }
    out = dict(col)
    for alias, canonical in aliases.items():
        if alias in col and canonical not in col:
            out[canonical] = col[alias]
    return out


def requests_quote(s: str) -> str:
    """URL-encode a string without importing requests."""
    import urllib.parse
    return urllib.parse.quote(s, safe="")
