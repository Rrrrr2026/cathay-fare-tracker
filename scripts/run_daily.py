"""Daily Cathay Pacific fare collection.

Sources:
  google_flights  - lowest CX one-way fare per route/direction at each booking
                    horizon (7/30/60/90 days ahead), all 82 destinations, HKD.
  cathay_api      - official open-search snapshot (cheapest cached round-trip
                    HKG -> every cached destination) plus the official per-day
                    one-way fare calendar for routes Cathay caches (HKD).

Progress is mirrored to data/progress.json so the dashboard's manual-refresh
button can show live status regardless of who started the run.

Usage:
  run_daily.py [--sources google,cathay] [--limit N] [--horizons 7,30]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store
from fetchers import cathay_api

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
ROUTES_PATH = PROJECT_ROOT / "routes.json"
PROGRESS_PATH = PROJECT_ROOT / "data" / "progress.json"
LOG_DIR = PROJECT_ROOT / "logs"

log = logging.getLogger("run_daily")


def setup_logging(collected_date: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"run_{collected_date}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


class Progress:
    """Mirrors collection progress to data/progress.json for the dashboard."""

    def __init__(self, collected_date: str, total: int):
        self.state = {
            "running": True,
            "pid": os.getpid(),
            "collected_date": collected_date,
            "phase": "starting",
            "done": 0,
            "total": total,
            "current": "",
            "started_ts": datetime.now().isoformat(timespec="seconds"),
            "updated_ts": None,
            "finished_ts": None,
            "errors": 0,
            "last_error": None,
        }
        self._write()

    def _write(self) -> None:
        self.state["updated_ts"] = datetime.now().isoformat(timespec="seconds")
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, PROGRESS_PATH)

    def step(self, phase: str, current: str, error: str | None = None) -> None:
        self.state["phase"] = phase
        self.state["current"] = current
        self.state["done"] += 1
        if error:
            self.state["errors"] += 1
            self.state["last_error"] = f"{current}: {error}"
        self._write()

    def degrade(self, reason: str) -> None:
        self.state["degraded"] = True
        self.state["degraded_reason"] = reason
        self._write()

    def finish(self, summary: dict) -> None:
        self.state.update(running=False, phase="finished", current="",
                          finished_ts=datetime.now().isoformat(timespec="seconds"),
                          summary=summary)
        self._write()

    def fail(self, err: str) -> None:
        self.state.update(running=False, phase="crashed", last_error=err,
                          finished_ts=datetime.now().isoformat(timespec="seconds"))
        self._write()


def collect_google(conn, progress: Progress, destinations: list, cfg: dict,
                   today: date, horizons: list) -> None:
    from fetchers import google_flights  # deferred: import cost + primp only when used

    gcfg = cfg["google_flights"]
    dmin, dmax = gcfg["delay_seconds_min"], gcfg["delay_seconds_max"]
    directions = []
    for d in destinations:
        directions.append(("HKG", d["iata"]))
        if cfg.get("directions", "both") == "both":
            directions.append((d["iata"], "HKG"))

    # Consecutive transport/parse failures signal a Google block; successful
    # parses (even with no CX itinerary) prove there is none.
    consec_failures = 0
    ABORT_AFTER = 12
    for origin, dest in directions:
        for horizon in horizons:
            depart = today + timedelta(days=horizon)
            label = f"{origin}->{dest} +{horizon}d"
            try:
                res, err = google_flights.lowest_cx_fare(
                    origin, dest, depart.isoformat(),
                    currency=cfg["currency"], seat=cfg["cabin"],
                    max_retries=gcfg["max_retries"],
                )
            except Exception as e:
                res, err = None, f"{type(e).__name__}: {e}"
                log.exception("%s unexpected fetcher error", label)
            if res is None and err not in (None, "no flights found"):
                consec_failures += 1
            else:
                consec_failures = 0
            record = {
                "collected_date": today.isoformat(),
                "collected_ts": datetime.now().isoformat(timespec="seconds"),
                "origin": origin,
                "destination": dest,
                "depart_date": depart.isoformat(),
                "horizon_days": horizon,
                "cabin": cfg["cabin"],
                "trip_type": "one-way",
                "source": "google_flights",
            }
            if res:
                record.update(price=res["price"], currency=res["currency"],
                              stops=res["stops"], flight_info=res["flight_info"])
                log.info("%s %s %.0f %s", label, res["flight_info"], res["price"], res["currency"])
            else:
                record["error"] = err or "no CX fare in results"
                log.warning("%s -> %s", label, record["error"])
            store.upsert_fare(conn, record)
            progress.step("google_flights", label, err)
            if consec_failures >= ABORT_AFTER:
                reason = (f"aborted google phase after {consec_failures} consecutive "
                          f"fetch failures (likely blocked); last: {err}")
                log.error(reason)
                progress.degrade(reason)
                return
            time.sleep(random.uniform(dmin, dmax))


def collect_cathay(conn, progress: Progress, destinations: list, cfg: dict,
                   today: date) -> None:
    from fetchers import fx

    ccfg = cfg["cathay_api"]
    delay = ccfg["delay_seconds"]
    ts = datetime.now().isoformat(timespec="seconds")
    base = {
        "collected_date": today.isoformat(),
        "collected_ts": ts,
        "cabin": cfg["cabin"],
    }
    # Cathay's API quotes origin-market currency (HKD for HKG departures);
    # everything is stored in USD, so convert at today's rate.
    rate, rate_src = fx.hkd_to_usd()
    log.info("FX HKD->USD %.5f (%s)", rate, rate_src)

    def usd(hkd: float) -> float:
        return round(hkd * rate, 2)

    if ccfg.get("open_search", True):
        rows, err = cathay_api.open_search("HKG", cabin="Y")
        if err:
            log.warning("cathay open-search failed: %s", err)
        else:
            log.info("cathay open-search: %d destinations", len(rows))
            # The cheapest cached departure date shifts intraday, so the PK
            # would not dedupe an afternoon re-run: replace the whole day's
            # snapshot in one transaction - only after a successful fetch.
            store.delete_day_source(conn, base["collected_date"], "cathay_api",
                                    origin="HKG")
            for r in rows:
                depart = date.fromisoformat(r["depart_date"])
                store.upsert_fare(conn, {
                    **base,
                    "origin": "HKG",
                    "destination": r["destination"],
                    "depart_date": r["depart_date"],
                    "horizon_days": (depart - today).days,
                    "trip_type": "round-trip",
                    "price": usd(r["price"]),
                    "currency": "USD",
                    "flight_info": f"official lowest RT, return {r['return_date']}, "
                                   f"{r['currency']} {r['price']:.0f} @ {rate:.4f}",
                    "source": "cathay_api",
                }, commit=False)
            conn.commit()
        progress.step("cathay_open_search", "HKG open-search", err)
        time.sleep(delay)

    if not ccfg.get("calendar", True):
        return
    for d in destinations:
        dest = d["iata"]
        months, err = cathay_api.month_probe("HKG", dest)
        time.sleep(delay)
        if err:
            note = "not in Cathay fare cache" if err == "not_cached" else err
            progress.step("cathay_calendar", f"HKG->{dest}", None if err == "not_cached" else err)
            log.info("calendar HKG->%s skipped (%s)", dest, note)
            continue
        fresh = []
        any_month_ok = False
        for month in months:
            rows, err = cathay_api.day_calendar("HKG", dest, month, cabin="Y", trip_type="O")
            time.sleep(delay)
            if err:
                continue
            any_month_ok = True
            fresh.extend(rows)
        if any_month_ok:
            # replace this destination's day-fares in one transaction (a date
            # dropping out of Cathay's cache must not survive as a stale row)
            store.delete_day_source(conn, base["collected_date"], "cathay_calendar",
                                    origin="HKG", destination=dest)
            for r in fresh:
                depart = date.fromisoformat(r["depart_date"])
                store.upsert_fare(conn, {
                    **base,
                    "origin": "HKG",
                    "destination": dest,
                    "depart_date": r["depart_date"],
                    "horizon_days": (depart - today).days,
                    "trip_type": "one-way",
                    "price": usd(r["price"]),
                    "currency": "USD",
                    "flight_info": f"official calendar, {r['currency']} {r['price']:.0f} @ {rate:.4f}",
                    "source": "cathay_calendar",
                }, commit=False)
            conn.commit()
        progress.step("cathay_calendar", f"HKG->{dest}")
        log.info("calendar HKG->%s: %d day-fares over months %s", dest, len(fresh), months)


def publish_site(today_iso: str) -> None:
    """Regenerate docs/ and push to GitHub so the Pages site stays current.
    Failures are logged but never fail the collection - data is safe locally."""
    import subprocess
    py = sys.executable
    subprocess.run([py, str(Path(__file__).resolve().parent / "export_static.py")],
                   check=True, cwd=str(PROJECT_ROOT), capture_output=True, timeout=300)
    def git(*args, ok_codes=(0,)):
        r = subprocess.run(["git", "-C", str(PROJECT_ROOT), *args],
                           capture_output=True, text=True, timeout=300)
        if r.returncode not in ok_codes:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
        return r
    git("add", "docs")
    # commit exits 1 when there is nothing to commit - that is fine
    git("commit", "-m", f"data: {today_iso} collection\n\n"
        "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>", ok_codes=(0, 1))
    git("push", "origin", "main")
    log.info("published static site to GitHub Pages")


def another_run_active(max_silence_s: int = 600) -> bool:
    """Same staleness rule as api_server._is_running: a live run touches
    progress.json on every step, so 10 min of silence means it is dead."""
    try:
        p = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not p.get("running"):
        return False
    try:
        updated = datetime.fromisoformat(p["updated_ts"])
    except (KeyError, ValueError, TypeError):
        return False
    return (datetime.now() - updated).total_seconds() < max_silence_s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="google,cathay",
                    help="comma list: google,cathay")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N destinations (testing)")
    ap.add_argument("--horizons", default="",
                    help="comma list of day-horizons, overrides config")
    ap.add_argument("--force", action="store_true",
                    help="run even if another collection appears active")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    routes = json.loads(ROUTES_PATH.read_text(encoding="utf-8"))
    destinations = routes["destinations"]
    if args.limit:
        destinations = destinations[: args.limit]
    horizons = ([int(h) for h in args.horizons.split(",") if h]
                if args.horizons else cfg["horizons_days"])
    sources = {s.strip() for s in args.sources.split(",") if s.strip()}

    today = date.today()
    setup_logging(today.isoformat())

    if not args.force and another_run_active():
        # exit 0 so Task Scheduler records success and the retry loop stops
        log.info("another collection run is active - exiting (use --force to override)")
        return 0

    n_dir = 2 if cfg.get("directions", "both") == "both" else 1
    total = 0
    if "google" in sources and cfg["google_flights"]["enabled"]:
        total += len(destinations) * n_dir * len(horizons)
    if "cathay" in sources and cfg["cathay_api"]["enabled"]:
        total += (1 if cfg["cathay_api"].get("open_search", True) else 0)
        total += len(destinations) if cfg["cathay_api"].get("calendar", True) else 0

    log.info("=== collection start: %d destinations, horizons %s, sources %s, %d steps ===",
             len(destinations), horizons, sorted(sources), total)
    # connect BEFORE Progress marks the run as running: a failed connect must
    # not leave progress.json stuck at running=true
    conn = store.connect()
    progress = Progress(today.isoformat(), total)
    try:
        if "cathay" in sources and cfg["cathay_api"]["enabled"]:
            collect_cathay(conn, progress, destinations, cfg, today)
        if "google" in sources and cfg["google_flights"]["enabled"]:
            collect_google(conn, progress, destinations, cfg, today, horizons)
        csv_path = store.export_daily_csv(conn, today.isoformat())
        s = store.summary(conn, today.isoformat())
        progress.finish(s)
        log.info("=== done: %(rows)d rows, %(with_price)d priced, %(errors)d errors ===", s)
        log.info("csv: %s", csv_path)
        if cfg.get("publish", {}).get("enabled"):
            try:
                publish_site(today.isoformat())
            except Exception:
                log.exception("static-site publish failed (local data unaffected)")
        return 0
    except Exception as e:
        log.exception("collection crashed")
        try:  # export whatever was collected; must not mask the original crash
            store.export_daily_csv(conn, today.isoformat())
        except Exception:
            log.exception("csv export after crash failed")
        progress.fail(f"{type(e).__name__}: {e}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
