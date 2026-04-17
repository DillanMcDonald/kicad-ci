# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Dillan McDonald
"""Tests for kicad_ci.api_cache (SI-4)."""

from __future__ import annotations

import threading
import time

import pytest

from kicad_ci.api_cache import ApiCache, CacheStats


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cache(tmp_path):
    """In-memory-equivalent cache backed by a temp-dir SQLite file."""
    c = ApiCache(db_path=tmp_path / "test_api.db")
    yield c
    c.close()


# ---------------------------------------------------------------------------
# CacheStats
# ---------------------------------------------------------------------------

class TestCacheStats:
    def test_hit_rate_zero_when_no_lookups(self):
        s = CacheStats(hits=0, misses=0, total_entries=0)
        assert s.hit_rate == 0.0

    def test_hit_rate_full(self):
        s = CacheStats(hits=10, misses=0, total_entries=10)
        assert s.hit_rate == 1.0

    def test_hit_rate_half(self):
        s = CacheStats(hits=5, misses=5, total_entries=10)
        assert s.hit_rate == 0.5

    def test_repr_contains_fields(self):
        s = CacheStats(hits=3, misses=1, total_entries=5)
        r = repr(s)
        assert "hits=3" in r
        assert "misses=1" in r
        assert "75.0%" in r


# ---------------------------------------------------------------------------
# Basic get / set
# ---------------------------------------------------------------------------

class TestGetSet:
    def test_get_missing_returns_none(self, cache):
        assert cache.get("noexist") is None

    def test_set_then_get_returns_value(self, cache):
        cache.set("mouser::R1", {"price": 0.05}, ttl_hours=24)
        result = cache.get("mouser::R1")
        assert result == {"price": 0.05}

    def test_nested_dict_round_trips(self, cache):
        val = {"items": [{"mpn": "ABC", "qty": 10}], "meta": {"source": "mouser"}}
        cache.set("k1", val)
        assert cache.get("k1") == val

    def test_set_overwrites_existing(self, cache):
        cache.set("k", {"v": 1})
        cache.set("k", {"v": 2})
        assert cache.get("k") == {"v": 2}

    def test_unicode_key_and_value(self, cache):
        cache.set("digikey::电阻", {"名前": "抵抗"})
        result = cache.get("digikey::电阻")
        assert result["名前"] == "抵抗"


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_expired_entry_returns_none(self, cache):
        cache.set("k", {"v": 1}, ttl_hours=0.0)  # instant expiry
        # Small sleep to ensure fetched_at < now - 0
        time.sleep(0.01)
        assert cache.get("k") is None

    def test_fresh_entry_returned(self, cache):
        cache.set("k", {"v": 42}, ttl_hours=1.0)
        assert cache.get("k") == {"v": 42}

    def test_very_short_ttl_expires(self, cache):
        # ttl = 0.001 hours = 3.6 seconds; force via tiny value near 0
        cache.set("k", {"v": 99}, ttl_hours=1e-9)
        time.sleep(0.01)
        assert cache.get("k") is None

    def test_set_after_expiry_refreshes(self, cache):
        cache.set("k", {"v": 1}, ttl_hours=0.0)
        time.sleep(0.01)
        cache.set("k", {"v": 2}, ttl_hours=24.0)
        assert cache.get("k") == {"v": 2}


# ---------------------------------------------------------------------------
# prune()
# ---------------------------------------------------------------------------

class TestPrune:
    def test_prune_removes_expired(self, cache):
        cache.set("expired1", {"x": 1}, ttl_hours=0.0)
        cache.set("expired2", {"x": 2}, ttl_hours=0.0)
        cache.set("fresh",    {"x": 3}, ttl_hours=24.0)
        time.sleep(0.01)
        deleted = cache.prune()
        assert deleted == 2
        assert cache.get("fresh") == {"x": 3}

    def test_prune_empty_cache_returns_zero(self, cache):
        assert cache.prune() == 0

    def test_prune_no_fresh_entries_removed(self, cache):
        for i in range(5):
            cache.set(f"k{i}", {"i": i}, ttl_hours=24.0)
        assert cache.prune() == 0

    def test_prune_reduces_total_entries(self, cache):
        for i in range(10):
            cache.set(f"k{i}", {"i": i}, ttl_hours=0.0)
        time.sleep(0.01)
        cache.prune()
        assert cache.stats().total_entries == 0


# ---------------------------------------------------------------------------
# invalidate()
# ---------------------------------------------------------------------------

class TestInvalidate:
    def test_exact_key_match(self, cache):
        cache.set("mouser::R1", {"p": 1})
        cache.set("digikey::R1", {"p": 2})
        cache.invalidate("mouser::R1")
        assert cache.get("mouser::R1") is None
        assert cache.get("digikey::R1") == {"p": 2}

    def test_wildcard_prefix(self, cache):
        cache.set("mouser::R1", {"p": 1})
        cache.set("mouser::C1", {"p": 2})
        cache.set("digikey::R1", {"p": 3})
        deleted = cache.invalidate("mouser::%")
        assert deleted == 2
        assert cache.get("digikey::R1") == {"p": 3}

    def test_wildcard_substring(self, cache):
        cache.set("mouser::RC0402-100K", {"p": 1})
        cache.set("digikey::RC0402-47K", {"p": 2})
        cache.set("arrow::1206-1K", {"p": 3})
        deleted = cache.invalidate("%RC0402%")
        assert deleted == 2

    def test_invalidate_all(self, cache):
        for i in range(5):
            cache.set(f"k{i}", {"i": i})
        cache.invalidate("%")
        assert cache.stats().total_entries == 0

    def test_invalidate_no_match_returns_zero(self, cache):
        cache.set("mouser::R1", {"p": 1})
        assert cache.invalidate("digikey::%") == 0


# ---------------------------------------------------------------------------
# stats()
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_zero_initial(self, cache):
        s = cache.stats()
        assert s.hits == 0
        assert s.misses == 0
        assert s.total_entries == 0

    def test_stats_counts_hits(self, cache):
        cache.set("k", {"v": 1})
        cache.get("k")
        cache.get("k")
        assert cache.stats().hits == 2

    def test_stats_counts_misses(self, cache):
        cache.get("missing1")
        cache.get("missing2")
        assert cache.stats().misses == 2

    def test_stats_total_entries(self, cache):
        for i in range(4):
            cache.set(f"k{i}", {"i": i})
        assert cache.stats().total_entries == 4

    def test_expired_miss_counts_as_miss(self, cache):
        cache.set("k", {"v": 1}, ttl_hours=0.0)
        time.sleep(0.01)
        cache.get("k")
        assert cache.stats().misses == 1
        assert cache.stats().hits == 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_set_get_no_error(self, tmp_path):
        errors = []
        db = tmp_path / "concurrent.db"

        def worker(n):
            try:
                c = ApiCache(db_path=db)
                for i in range(20):
                    key = f"thread{n}::item{i}"
                    c.set(key, {"n": n, "i": i})
                    result = c.get(key)
                    assert result is not None
                c.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

    def test_shared_instance_multiple_threads(self, tmp_path):
        """Single ApiCache instance accessed from multiple threads."""
        errors = []
        cache = ApiCache(db_path=tmp_path / "shared.db")

        def worker(n):
            try:
                for i in range(10):
                    cache.set(f"t{n}_k{i}", {"v": n * 100 + i})
                    cache.get(f"t{n}_k{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        cache.close()

        assert errors == []


# ---------------------------------------------------------------------------
# Environment variable override
# ---------------------------------------------------------------------------

class TestEnvOverride:
    def test_kicad_cache_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_CACHE_DIR", str(tmp_path / "custom_cache"))
        c = ApiCache()
        assert str(tmp_path / "custom_cache") in str(c._db_path)
        c.close()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class TestContextManager:
    def test_context_manager_closes(self, tmp_path):
        with ApiCache(db_path=tmp_path / "ctx.db") as c:
            c.set("k", {"v": 1})
            assert c.get("k") == {"v": 1}
        # After exit, connection should be closed (re-open is fine)
        c2 = ApiCache(db_path=tmp_path / "ctx.db")
        assert c2.get("k") == {"v": 1}
        c2.close()
