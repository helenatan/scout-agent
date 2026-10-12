"""Shared fixtures: the frozen dataset (read-only) and an independent raw SQLite connection."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scout.tools.pai_tools import DEFAULT_DB, DEFAULT_SOURCES, PAIData  # noqa: E402

# Small snapshots keep the tests fast (83 and 150 pm-titled postings respectively).
WEEK = "pai-w-2026-08-24"
WEEK2 = "pai-w-2026-08-03"
MONTH = "pai-2026-08"


@pytest.fixture(scope="session")
def data() -> PAIData:
    return PAIData(DEFAULT_DB)


@pytest.fixture(scope="session")
def raw() -> sqlite3.Connection:
    """Independent read-only connection for computing expected counts with instr() rather than LIKE."""
    conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cohort_sql(start: str, end: str, pm_titled_only: bool = True) -> tuple[str, list]:
    """WHERE fragment reproducing snapshot membership independently of the tools module."""
    src = ",".join("?" * len(DEFAULT_SOURCES))
    sql = (f"from job_postings_raw r join job_postings_enriched e on e.raw_id=r.id and e.pipeline_version='v1.0' "
           f"where r.run_date between ? and ? and r.source in ({src})")
    if pm_titled_only:
        sql += " and e.normalized_title != 'Other'"
    return sql, [start, end, *DEFAULT_SOURCES]


# ── the public repository ships only a synthetic demo snapshot ──────────────────────────────────
REAL_DATA_TESTS = Path(__file__).with_name("real_data_tests.txt")


def _is_demo_snapshot() -> bool:
    """The demo snapshot has a handful of fictional postings; the original has 13,998."""
    try:
        conn = sqlite3.connect(f"file:{DEFAULT_DB}?mode=ro", uri=True)
        n = conn.execute("select count(*) from job_postings_raw").fetchone()[0]
        conn.close()
        return n < 1000
    except sqlite3.Error:
        return True


def pytest_collection_modifyitems(config, items):
    if not _is_demo_snapshot() or not REAL_DATA_TESTS.exists():
        return
    needed = {line.strip() for line in REAL_DATA_TESTS.read_text().splitlines() if line.strip() and not line.startswith("#")}
    skip = pytest.mark.skip(reason="needs the original snapshot (not distributed); see docs/reproduce.md")
    for item in items:
        if item.nodeid in needed:
            item.add_marker(skip)
