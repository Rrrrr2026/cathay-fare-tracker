"""SQLite storage for daily Cathay Pacific fare records.

One row = the lowest CX fare observed for (route, departure date, cabin, source)
on a given collection day. Re-running the same day overwrites instead of
duplicating, so a crashed run can simply be restarted.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "fares.sqlite"
DAILY_CSV_DIR = PROJECT_ROOT / "data" / "daily"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fares (
    collected_date TEXT NOT NULL,      -- YYYY-MM-DD the scraper ran
    collected_ts   TEXT NOT NULL,      -- full ISO timestamp
    origin         TEXT NOT NULL,      -- IATA
    destination    TEXT NOT NULL,      -- IATA
    depart_date    TEXT NOT NULL,      -- YYYY-MM-DD
    horizon_days   INTEGER NOT NULL,   -- depart_date - collected_date
    cabin          TEXT NOT NULL,
    trip_type      TEXT NOT NULL DEFAULT 'one-way',
    price          REAL,               -- NULL when no CX fare was found
    currency       TEXT,
    stops          INTEGER,
    flight_info    TEXT,               -- e.g. "CX520 dep 09:35 non-stop"
    source         TEXT NOT NULL,      -- 'google_flights' | 'cathay_api'
    error          TEXT,               -- set when the query failed
    PRIMARY KEY (collected_date, origin, destination, depart_date, cabin, trip_type, source)
);
CREATE INDEX IF NOT EXISTS idx_fares_route ON fares (origin, destination, collected_date);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # WAL keeps the read-only dashboard connection usable even if a writer
    # dies mid-commit (persistent setting; later connects are no-ops).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def delete_day_source(conn: sqlite3.Connection, collected_date: str, source: str,
                      origin: str | None = None, destination: str | None = None) -> None:
    """Remove one day's rows for a source (optionally scoped to a route leg).

    Does NOT commit - callers pair it with fresh inserts in one transaction so
    a failed refetch can never wipe data it did not replace.
    """
    sql = "DELETE FROM fares WHERE collected_date=? AND source=?"
    params: list = [collected_date, source]
    if origin:
        sql += " AND origin=?"
        params.append(origin)
    if destination:
        sql += " AND destination=?"
        params.append(destination)
    conn.execute(sql, params)


def upsert_fare(conn: sqlite3.Connection, record: dict, commit: bool = True) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO fares
            (collected_date, collected_ts, origin, destination, depart_date,
             horizon_days, cabin, trip_type, price, currency, stops,
             flight_info, source, error)
        VALUES
            (:collected_date, :collected_ts, :origin, :destination, :depart_date,
             :horizon_days, :cabin, :trip_type, :price, :currency, :stops,
             :flight_info, :source, :error)
        """,
        {
            "trip_type": "one-way",
            "price": None,
            "currency": None,
            "stops": None,
            "flight_info": None,
            "error": None,
            **record,
        },
    )
    if commit:
        conn.commit()


def export_daily_csv(conn: sqlite3.Connection, collected_date: str) -> Path:
    """Write all of one collection day's rows to data/daily/<date>.csv."""
    DAILY_CSV_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILY_CSV_DIR / f"{collected_date}.csv"
    cur = conn.execute(
        "SELECT * FROM fares WHERE collected_date = ? ORDER BY origin, destination, depart_date",
        (collected_date,),
    )
    columns = [d[0] for d in cur.description]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(cur.fetchall())
    return out_path


def summary(conn: sqlite3.Connection, collected_date: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN price IS NOT NULL THEN 1 ELSE 0 END),
               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)
        FROM fares WHERE collected_date = ?
        """,
        (collected_date,),
    ).fetchone()
    return {"rows": row[0] or 0, "with_price": row[1] or 0, "errors": row[2] or 0}


if __name__ == "__main__":
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM fares").fetchone()[0]
    print(json.dumps({"db": str(DB_PATH), "total_rows": n}))
