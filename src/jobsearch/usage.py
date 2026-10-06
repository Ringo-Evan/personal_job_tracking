"""state/router.db (plan-models.md §2): per-endpoint daily call counters, response cache, escalations, 429 backoffs."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from jobsearch.config import ROOT

ROUTER_DB = ROOT / "state" / "router.db"


def connect(path: Path = ROUTER_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS usage (model TEXT NOT NULL, day TEXT NOT NULL, calls INTEGER NOT NULL, "
        "PRIMARY KEY (model, day))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS cache (task TEXT NOT NULL, model TEXT NOT NULL, prompt_hash TEXT NOT NULL, "
        "response TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (task, model, prompt_hash))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS escalations (at REAL NOT NULL, task TEXT NOT NULL, tier TEXT NOT NULL, "
        "model TEXT NOT NULL, reason TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS exhausted (model TEXT PRIMARY KEY, until REAL NOT NULL)")
    return db


def used_today(db: sqlite3.Connection, model: str, today: date | None = None) -> int:
    day = (today or date.today()).isoformat()
    row = db.execute("SELECT calls FROM usage WHERE model = ? AND day = ?", (model, day)).fetchone()
    return row[0] if row else 0


def record_call(db: sqlite3.Connection, model: str, today: date | None = None) -> None:
    day = (today or date.today()).isoformat()
    db.execute(
        "INSERT INTO usage (model, day, calls) VALUES (?, ?, 1) "
        "ON CONFLICT (model, day) DO UPDATE SET calls = calls + 1",
        (model, day),
    )
    db.commit()
