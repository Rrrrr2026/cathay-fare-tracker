"""Local dashboard + JSON API server for the Cathay Pacific fare tracker.

Stdlib only. Serves dashboard/index.html at / and a stable JSON API:

  GET  /api/meta                      routes, config, collection dates, db stats
  GET  /api/summary?horizon&direction continent-level averages (+ prev-day trend)
  GET  /api/continent/<name>?...      per-route detail for one continent
  GET  /api/route/<orig>/<dest>       full fare history + official forward curve
  GET  /api/refresh/status            live progress of a running collection
  POST /api/refresh                   start a manual collection run

The refresh run is a detached subprocess of run_daily.py, so it survives a
server restart; liveness is judged by the child handle OR the freshness of
data/progress.json (updated on every step by the runner).

Fares served: lowest Cathay Pacific fares in HKD. direction=out is HKG->dest,
direction=in is dest->HKG. Google Flights is the systematic series; where a
route has no priced CX itinerary on Google, the official Cathay calendar fare
for the same departure date fills in (marked src="cathay_calendar").
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "fares.sqlite"
DASH_DIR = PROJECT_ROOT / "dashboard"
PROGRESS_PATH = PROJECT_ROOT / "data" / "progress.json"
LOG_DIR = PROJECT_ROOT / "logs"

CONFIG = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
ROUTES = json.loads((PROJECT_ROOT / "routes.json").read_text(encoding="utf-8"))

# Display grouping requested by the user: Africa / Americas / Asia /
# Australia / Europe / Middle East
CONTINENT_OF_REGION = {
    "East Asia": "Asia",
    "Southeast Asia": "Asia",
    "South Asia": "Asia",
    "Central Asia": "Asia",
    "Middle East": "Middle East",
    "Europe": "Europe",
    "North America": "Americas",
    "Oceania": "Australia",
    "Africa": "Africa",
}
DEST_META = {
    d["iata"]: {**d, "continent": CONTINENT_OF_REGION.get(d["region"], d["region"])}
    for d in ROUTES["destinations"]
}

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}

_proc_lock = threading.Lock()
_refresh_proc: subprocess.Popen | None = None


# ---------------------------------------------------------------- data access

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collection_dates(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT collected_date FROM fares ORDER BY collected_date DESC LIMIT 400")]


def _google_fares(conn, cdate: str, horizon: int) -> dict:
    """(origin, destination) -> row for one collection day and horizon."""
    rows = conn.execute(
        """SELECT origin, destination, price, stops, flight_info, error
           FROM fares WHERE collected_date=? AND source='google_flights' AND horizon_days=?""",
        (cdate, horizon)).fetchall()
    return {(r["origin"], r["destination"]): r for r in rows}


def _calendar_fallback(conn, cdate: str, horizon: int) -> dict:
    """destination -> official one-way calendar price for depart = cdate + horizon."""
    depart = (date.fromisoformat(cdate) + timedelta(days=horizon)).isoformat()
    rows = conn.execute(
        """SELECT destination, price FROM fares
           WHERE collected_date=? AND source='cathay_calendar' AND origin='HKG'
             AND depart_date=? AND price IS NOT NULL""",
        (cdate, depart)).fetchall()
    return {r["destination"]: r["price"] for r in rows}


def _route_fare(google: dict, calendar: dict, iata: str, direction: str):
    """Effective one-way fare + provenance for one destination/direction."""
    key = ("HKG", iata) if direction == "out" else (iata, "HKG")
    row = google.get(key)
    if row is not None and row["price"] is not None:
        return row["price"], "google", row
    if direction == "out" and iata in calendar:
        return calendar[iata], "cathay_calendar", row
    return None, None, row


def api_summary(conn, qs) -> dict:
    horizon = int(qs.get("horizon", ["30"])[0])
    direction = qs.get("direction", ["out"])[0]
    dates = collection_dates(conn)
    if not dates:
        return {"empty": True, "continents": [], "dates": []}
    latest, prev = dates[0], (dates[1] if len(dates) > 1 else None)

    def snapshot(cdate):
        google = _google_fares(conn, cdate, horizon)
        cal = _calendar_fallback(conn, cdate, horizon)
        out = {}
        for iata in DEST_META:
            price, src, _ = _route_fare(google, cal, iata, direction)
            if price is not None:
                out[iata] = (price, src)
        return out

    latest_fares = snapshot(latest)
    prev_fares = snapshot(prev) if prev else {}

    continents: dict[str, dict] = {}
    for iata, meta in DEST_META.items():
        c = continents.setdefault(meta["continent"], {
            "name": meta["continent"], "routes_total": 0, "prices": [], "prev": [],
            "pair_deltas": []})
        c["routes_total"] += 1
        if iata in latest_fares:
            c["prices"].append(latest_fares[iata][0])
        if iata in prev_fares:
            c["prev"].append(prev_fares[iata][0])
        if iata in latest_fares and iata in prev_fares:
            c["pair_deltas"].append(latest_fares[iata][0] - prev_fares[iata][0])

    result = []
    for c in continents.values():
        p = c.pop("prices")
        pv = c.pop("prev")
        pd = c.pop("pair_deltas")
        c.update(
            routes_priced=len(p),
            avg=round(sum(p) / len(p)) if p else None,
            min=min(p) if p else None,
            max=max(p) if p else None,
            prev_avg=round(sum(pv) / len(pv)) if pv else None,
            # day-over-day movement over routes priced on BOTH days - a route
            # entering/leaving coverage must not masquerade as a fare move
            trend_delta=round(sum(pd) / len(pd)) if pd else None,
            trend_routes=len(pd),
        )
        result.append(c)
    result.sort(key=lambda c: -(c["avg"] or 0))
    return {"collected_date": latest, "prev_date": prev, "horizon": horizon,
            "direction": direction, "currency": CONFIG["currency"],
            "continents": result, "dates": dates}


def _snapshot(conn, cdate: str, horizon: int, direction: str) -> dict:
    """iata -> (price, src) for one collection day, with calendar fallback."""
    google = _google_fares(conn, cdate, horizon)
    cal = _calendar_fallback(conn, cdate, horizon)
    out = {}
    for iata in DEST_META:
        price, src, _ = _route_fare(google, cal, iata, direction)
        if price is not None:
            out[iata] = (price, src)
    return out


def api_trends(conn, qs) -> dict:
    """Per-continent and overall average fare for every collection day -
    the time-series behind the fare-index chart and card sparklines."""
    horizon = int(qs.get("horizon", ["30"])[0])
    direction = qs.get("direction", ["out"])[0]
    dates = sorted(collection_dates(conn))[-120:]
    continents: dict[str, list] = {}
    overall = []
    for cdate in dates:
        snap = _snapshot(conn, cdate, horizon, direction)
        by_cont: dict[str, list] = {}
        for iata, (price, _) in snap.items():
            by_cont.setdefault(DEST_META[iata]["continent"], []).append(price)
        for cont, prices in by_cont.items():
            continents.setdefault(cont, []).append(
                {"date": cdate, "avg": round(sum(prices) / len(prices)), "n": len(prices)})
        allp = [p for prices in by_cont.values() for p in prices]
        if allp:
            overall.append({"date": cdate,
                            "avg": round(sum(allp) / len(allp)), "n": len(allp)})
    return {"dates": dates, "horizon": horizon, "direction": direction,
            "currency": CONFIG["currency"], "continents": continents,
            "overall": overall}


def api_movers(conn, qs) -> dict:
    """Biggest route-level day-over-day fare moves (routes priced both days)."""
    horizon = int(qs.get("horizon", ["30"])[0])
    direction = qs.get("direction", ["out"])[0]
    limit = int(qs.get("limit", ["8"])[0])
    dates = collection_dates(conn)
    if len(dates) < 2:
        return {"available": False, "reason": "need at least two collection days",
                "fallers": [], "risers": []}
    latest, prev = dates[0], dates[1]
    snap_now = _snapshot(conn, latest, horizon, direction)
    snap_prev = _snapshot(conn, prev, horizon, direction)
    moves = []
    for iata in snap_now:
        if iata not in snap_prev:
            continue
        now_p, prev_p = snap_now[iata][0], snap_prev[iata][0]
        if prev_p <= 0:
            continue
        delta = now_p - prev_p
        meta = DEST_META[iata]
        moves.append({"iata": iata, "city": meta["city"], "country": meta["country"],
                      "continent": meta["continent"], "price": now_p, "prev": prev_p,
                      "delta": round(delta, 2),
                      "pct": round(100 * delta / prev_p, 1)})
    moves.sort(key=lambda m: m["delta"])
    fallers = [m for m in moves if m["delta"] < 0][:limit]
    risers = [m for m in moves if m["delta"] > 0][-limit:][::-1]
    return {"available": True, "collected_date": latest, "prev_date": prev,
            "horizon": horizon, "direction": direction,
            "fallers": fallers, "risers": risers}


# --------------------------- same-flight analytics ---------------------------
# "Same flight" = identical (origin, destination, departure date, one-way,
# economy). The official calendar re-observes every future departure daily,
# so each flight accumulates a booking curve of observations.

def _matched_pairs(conn, day_a: str, day_b: str) -> list:
    """Calendar fares observed on BOTH days for the same flight (outbound)."""
    return conn.execute(
        """SELECT a.destination, a.depart_date, a.price AS now_p, b.price AS then_p
           FROM fares a JOIN fares b
             ON a.destination=b.destination AND a.depart_date=b.depart_date
            AND a.origin=b.origin AND a.trip_type=b.trip_type AND a.cabin=b.cabin
            AND a.source=b.source
           WHERE a.source='cathay_calendar' AND a.origin='HKG'
             AND a.collected_date=? AND b.collected_date=?
             AND a.price IS NOT NULL AND b.price IS NOT NULL""",
        (day_a, day_b)).fetchall()


def _closest_date(dates: list[str], target: str, tolerance_days: int) -> str | None:
    best, best_gap = None, None
    tgt = date.fromisoformat(target)
    for d in dates:
        gap = abs((date.fromisoformat(d) - tgt).days)
        if gap <= tolerance_days and (best_gap is None or gap < best_gap):
            best, best_gap = d, gap
    return best


def api_drift(conn, qs) -> dict:
    """Matched same-flight fare drift per continent, plus a cumulative index.

    Each continent chains against its own last calendar-bearing collection day
    (not blindly the previous date): a day whose calendar scrape failed leaves
    the cursor in place, so the next good day produces a correct multi-day
    catch-up step instead of silently dropping the move (review finding)."""
    dates = sorted(collection_dates(conn))[-120:]
    conts = sorted({m["continent"] for m in DEST_META.values()})
    drift: dict[str, list] = {}
    index: dict[str, list] = {c: [] for c in conts}
    level: dict[str, float] = {c: 100.0 for c in conts}
    last_good: dict[str, str | None] = {c: None for c in conts}
    for cur_d in dates:
        has_cal = {DEST_META[r[0]]["continent"] for r in conn.execute(
            """SELECT DISTINCT destination FROM fares
               WHERE collected_date=? AND source='cathay_calendar'
                 AND origin='HKG' AND price IS NOT NULL""", (cur_d,))
            if r[0] in DEST_META}
        pairs_by_base: dict[str, list] = {}
        for c in conts:
            base_d = last_good[c]
            if base_d is None:
                if c in has_cal:
                    last_good[c] = cur_d  # index starts (=100) at first calendar day
                index[c].append({"date": cur_d, "idx": round(level[c], 2)})
                continue
            if base_d not in pairs_by_base:
                pairs_by_base[base_d] = _matched_pairs(conn, cur_d, base_d)
            pcts = [100 * (p["now_p"] - p["then_p"]) / p["then_p"]
                    for p in pairs_by_base[base_d]
                    if p["then_p"] > 0
                    and DEST_META.get(p["destination"], {}).get("continent") == c]
            if pcts:
                pct = sum(pcts) / len(pcts)
                level[c] *= (1 + pct / 100)
                drift.setdefault(c, []).append(
                    {"date": cur_d, "pct": round(pct, 2), "n": len(pcts),
                     "base": base_d,
                     "span_days": (date.fromisoformat(cur_d)
                                   - date.fromisoformat(base_d)).days})
                last_good[c] = cur_d
            index[c].append({"date": cur_d, "idx": round(level[c], 2)})
    return {"dates": dates, "currency": CONFIG["currency"],
            "note": "matched same-flight pairs (identical route + departure date), outbound HKG official calendar",
            "drift": drift, "index": index}


def api_flight_movers(conn, qs) -> dict:
    """Biggest same-flight moves: identical (route, departure) priced on the
    latest collection day and ~window days earlier."""
    window = int(qs.get("window", ["1"])[0])
    limit = int(qs.get("limit", ["10"])[0])
    dates = sorted(collection_dates(conn))
    if len(dates) < 2:
        return {"available": False, "reason": "need at least two collection days",
                "fallers": [], "risers": []}
    latest = dates[-1]
    target = (date.fromisoformat(latest) - timedelta(days=window)).isoformat()
    base = _closest_date(dates[:-1], target, tolerance_days=min(3, max(1, window // 3)))
    if not base:
        return {"available": False,
                "reason": f"no collection ~{window} days back yet (first day was {dates[0]})",
                "fallers": [], "risers": []}
    moves = []
    for p in _matched_pairs(conn, latest, base):
        meta = DEST_META.get(p["destination"])
        if not meta or p["then_p"] <= 0:
            continue
        delta = p["now_p"] - p["then_p"]
        moves.append({"iata": p["destination"], "city": meta["city"],
                      "continent": meta["continent"], "depart_date": p["depart_date"],
                      "price": p["now_p"], "prev": p["then_p"],
                      "delta": round(delta, 2),
                      "pct": round(100 * delta / p["then_p"], 1)})
    # one representative flight per route: its biggest absolute % move
    best_per_route: dict[str, dict] = {}
    for m in moves:
        cur = best_per_route.get(m["iata"])
        if cur is None or abs(m["pct"]) > abs(cur["pct"]):
            best_per_route[m["iata"]] = m
    ranked = sorted(best_per_route.values(), key=lambda m: m["pct"])
    return {"available": True, "collected_date": latest, "base_date": base,
            "window_requested": window,
            "window_actual": (date.fromisoformat(latest) - date.fromisoformat(base)).days,
            "flights_compared": len(moves),
            "fallers": [m for m in ranked if m["pct"] < 0][:limit],
            "risers": [m for m in ranked if m["pct"] > 0][-limit:][::-1]}


def api_departure_watch(conn, qs) -> dict:
    """For each route: the specific flights departing +7, +14 and +30 days from
    the latest collection day - today's price for that exact departure, its
    change since yesterday's observation and since first observation."""
    windows = [7, 14, 30]
    dates = sorted(collection_dates(conn))
    if not dates:
        return {"windows": windows, "rows": []}
    latest = dates[-1]
    latest_d = date.fromisoformat(latest)
    prev = dates[-2] if len(dates) > 1 else None

    def flight_obs(dest: str, depart: str) -> list:
        return conn.execute(
            """SELECT collected_date, price, source FROM fares
               WHERE origin='HKG' AND destination=? AND depart_date=?
                 AND trip_type='one-way' AND price IS NOT NULL
                 AND source IN ('cathay_calendar', 'google_flights')
               ORDER BY collected_date""", (dest, depart)).fetchall()

    prev_gap_days = ((latest_d - date.fromisoformat(prev)).days if prev else None)

    rows = []
    for iata, meta in DEST_META.items():
        cells = {}
        for w in windows:
            depart = (latest_d + timedelta(days=w)).isoformat()
            obs = flight_obs(iata, depart)
            today_obs = [o for o in obs if o["collected_date"] == latest]
            if not today_obs:
                cells[f"w{w}"] = None
                continue
            # displayed price: cheapest observation today, correctly attributed
            cheapest = min(today_obs, key=lambda o: o["price"])
            price = cheapest["price"]
            # Deltas compare CALENDAR vs CALENDAR only: the two sources quote
            # the same flight up to ~290% apart, so a cross-source comparison
            # would fabricate fare moves (review finding, verified on live DB).
            cal = [o for o in obs if o["source"] == "cathay_calendar"]
            def cal_min(day):
                p = [o["price"] for o in cal if o["collected_date"] == day]
                return min(p) if p else None
            cal_today = cal_min(latest)
            cal_prev = cal_min(prev) if prev else None
            d1 = (100 * (cal_today - cal_prev) / cal_prev
                  if cal_today is not None and cal_prev else None)
            cal_days = sorted({o["collected_date"] for o in cal})
            first_day = cal_days[0] if cal_days else None
            base = cal_min(first_day) if first_day else None
            since = (100 * (cal_today - base) / base
                     if cal_today is not None and base and first_day != latest else None)
            cells[f"w{w}"] = {"depart": depart, "price": price,
                              "src": "cal" if cheapest["source"] == "cathay_calendar" else "g",
                              "d1_pct": round(d1, 1) if d1 is not None else None,
                              "since_pct": round(since, 1) if since is not None else None,
                              "since_date": first_day,
                              "n_obs": len({o["collected_date"] for o in obs})}
        if any(cells.values()):
            rows.append({"iata": iata, "city": meta["city"], "country": meta["country"],
                         "continent": meta["continent"], "region": meta["region"],
                         "airport": meta.get("airport"), **cells})
    rows.sort(key=lambda r: (r["continent"], r["city"]))
    return {"windows": windows, "collected_date": latest, "prev_date": prev,
            "prev_gap_days": prev_gap_days,
            "currency": CONFIG["currency"], "rows": rows}


def api_continent(conn, name: str, qs) -> dict:
    horizon = int(qs.get("horizon", ["30"])[0])
    dates = collection_dates(conn)
    if not dates:
        return {"empty": True, "rows": []}
    latest, prev = dates[0], (dates[1] if len(dates) > 1 else None)

    google = _google_fares(conn, latest, horizon)
    cal = _calendar_fallback(conn, latest, horizon)
    google_prev = _google_fares(conn, prev, horizon) if prev else {}
    cal_prev = _calendar_fallback(conn, prev, horizon) if prev else {}

    official = {r["destination"]: r for r in conn.execute(
        """SELECT destination, price, depart_date, flight_info FROM fares
           WHERE collected_date=? AND source='cathay_api' AND origin='HKG'
           ORDER BY collected_ts""", (latest,))}  # newest same-day snapshot wins

    rows = []
    for iata, meta in DEST_META.items():
        if meta["continent"] != name:
            continue
        out_price, out_src, out_row = _route_fare(google, cal, iata, "out")
        in_price, in_src, in_row = _route_fare(google, cal, iata, "in")
        prev_out, _, _ = _route_fare(google_prev, cal_prev, iata, "out")
        prev_in, _, _ = _route_fare(google_prev, cal_prev, iata, "in")
        off = official.get(iata)
        rows.append({
            "iata": iata, "city": meta["city"], "country": meta["country"],
            "region": meta["region"], "airport": meta.get("airport"),
            "out_price": out_price, "out_src": out_src,
            "out_info": out_row["flight_info"] if out_row else None,
            "in_price": in_price, "in_src": in_src,
            "in_info": in_row["flight_info"] if in_row else None,
            "prev_out_price": prev_out,
            "prev_in_price": prev_in,
            "official_rt": off["price"] if off else None,
            "official_rt_info": off["flight_info"] if off else None,
        })
    rows.sort(key=lambda r: (r["out_price"] is None, r["out_price"] or 0))
    return {"collected_date": latest, "prev_date": prev, "horizon": horizon,
            "currency": CONFIG["currency"], "continent": name, "rows": rows}


def api_route(conn, origin: str, dest: str) -> dict:
    origin, dest = origin.upper(), dest.upper()
    history = [dict(r) for r in conn.execute(
        """SELECT collected_date, horizon_days, price, stops, flight_info, error
           FROM fares WHERE source='google_flights' AND origin=? AND destination=?
           ORDER BY collected_date, horizon_days""", (origin, dest))]
    official_rt = [dict(r) for r in conn.execute(
        """SELECT collected_date, depart_date, price, flight_info
           FROM fares WHERE source='cathay_api' AND origin=? AND destination=?
           ORDER BY collected_date""", (origin, dest))]
    cal_dates = [r[0] for r in conn.execute(
        """SELECT DISTINCT collected_date FROM fares
           WHERE source='cathay_calendar' AND origin=? AND destination=?
           ORDER BY collected_date DESC LIMIT 2""", (origin, dest))]
    calendar = {}
    for cd in cal_dates:
        calendar[cd] = [dict(r) for r in conn.execute(
            """SELECT depart_date, price FROM fares
               WHERE source='cathay_calendar' AND origin=? AND destination=?
                 AND collected_date=? ORDER BY depart_date""", (origin, dest, cd))]
    latest = collection_dates(conn)
    google_points = []
    if latest:
        google_points = [dict(r) for r in conn.execute(
            """SELECT depart_date, price, horizon_days FROM fares
               WHERE source='google_flights' AND origin=? AND destination=?
                 AND collected_date=? AND price IS NOT NULL
               ORDER BY depart_date""", (origin, dest, latest[0]))]
    # booking curves: every observation of each future departure of this route,
    # so the modal can chart "same flight, price vs days-to-departure"
    curves: dict[str, list] = {}
    if latest:
        cutoff = latest[0]  # only departures still in the future
        for r in conn.execute(
            """SELECT depart_date, collected_date, price, source FROM fares
               WHERE origin=? AND destination=? AND trip_type='one-way'
                 AND price IS NOT NULL AND depart_date >= ?
                 AND source IN ('cathay_calendar', 'google_flights')
               ORDER BY depart_date, collected_date, source""",
                (origin, dest, cutoff)):
            curves.setdefault(r["depart_date"], []).append(
                {"obs": r["collected_date"], "price": r["price"],
                 "src": "cal" if r["source"] == "cathay_calendar" else "g"})
    other = dest if origin == "HKG" else origin
    meta = DEST_META.get(other, {})
    return {"origin": origin, "destination": dest, "meta": meta,
            "currency": CONFIG["currency"], "google_history": history,
            "official_rt_history": official_rt, "calendar": calendar,
            "latest_google_points": google_points, "booking_curves": curves}


def api_meta(conn) -> dict:
    # conn may be None before the first collection - the dashboard still needs
    # the full meta shape (horizons etc.) to render its empty state
    dates = collection_dates(conn) if conn else []
    total = conn.execute("SELECT COUNT(*) FROM fares").fetchone()[0] if conn else 0
    by_continent: dict[str, int] = {}
    for meta in DEST_META.values():
        by_continent[meta["continent"]] = by_continent.get(meta["continent"], 0) + 1
    progress = _read_progress()
    return {
        "hub": "HKG",
        "destinations": len(DEST_META),
        "directional_routes": len(DEST_META) * 2,
        "continents": by_continent,
        "currency": CONFIG["currency"],
        "cabin": CONFIG["cabin"],
        "horizons": CONFIG["horizons_days"],
        "dates": dates,
        "total_rows": total,
        "last_run": {k: progress.get(k) for k in
                     ("collected_date", "finished_ts", "phase", "errors", "summary",
                      "degraded", "degraded_reason")}
                    if progress else None,
        "routes": [{"iata": i, **m} for i, m in DEST_META.items()],
    }


# ------------------------------------------------------------------- refresh

def _read_progress() -> dict | None:
    try:
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_running() -> bool:
    if _refresh_proc is not None and _refresh_proc.poll() is None:
        return True
    p = _read_progress()
    if not p or not p.get("running"):
        return False
    try:
        updated = datetime.fromisoformat(p["updated_ts"])
    except (KeyError, ValueError):
        return False
    # The runner touches progress.json on every step; a long Google retry chain
    # stays under ~1 min, so 10 min of silence means the run is dead.
    return (datetime.now() - updated).total_seconds() < 600


def start_refresh(sources: str) -> tuple[bool, str]:
    global _refresh_proc
    with _proc_lock:
        if _is_running():
            return False, "a collection run is already in progress"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        spawn_log = open(LOG_DIR / "refresh_spawn.log", "ab")
        _refresh_proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "run_daily.py"),
             "--sources", sources],
            cwd=str(PROJECT_ROOT), stdout=spawn_log, stderr=subprocess.STDOUT,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        return True, "started"


def refresh_status() -> dict:
    p = _read_progress() or {}
    # computed liveness must override the raw stored flag: a hard kill or
    # reboot leaves running=true in progress.json forever
    return {**p, "running": _is_running()}


# -------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    server_version = "CathayFareTracker/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != "/api/refresh":
            return self._json({"error": "not found"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        sources = "google,cathay"
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                sources = body.get("sources") or sources
            except ValueError:
                pass
        ok, msg = start_refresh(sources)
        self._json({"started": ok, "message": msg, "status": refresh_status()},
                   200 if ok else 409)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api(path, qs)
            return self._static(path)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api(self, path: str, qs) -> None:
        if path == "/api/refresh/status":
            return self._json(refresh_status())
        if path == "/api/meta":
            conn = _db() if DB_PATH.exists() else None
            try:
                return self._json(api_meta(conn))
            finally:
                if conn:
                    conn.close()
        if not DB_PATH.exists():
            return self._json({"empty": True, "error": "no data collected yet",
                               "continents": [], "rows": [], "dates": []})
        conn = _db()
        try:
            if path == "/api/summary":
                return self._json(api_summary(conn, qs))
            if path == "/api/trends":
                return self._json(api_trends(conn, qs))
            if path == "/api/movers":
                return self._json(api_movers(conn, qs))
            if path == "/api/drift":
                return self._json(api_drift(conn, qs))
            if path == "/api/flight-movers":
                return self._json(api_flight_movers(conn, qs))
            if path == "/api/departure-watch":
                return self._json(api_departure_watch(conn, qs))
            if path.startswith("/api/continent/"):
                name = path.split("/api/continent/", 1)[1].replace("%20", " ")
                return self._json(api_continent(conn, name, qs))
            if path.startswith("/api/route/"):
                parts = path.split("/api/route/", 1)[1].strip("/").split("/")
                if len(parts) == 2:
                    return self._json(api_route(conn, parts[0], parts[1]))
            self._json({"error": "not found"}, 404)
        finally:
            conn.close()

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (DASH_DIR / rel).resolve()
        if not str(target).startswith(str(DASH_DIR.resolve())) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix.lower(), "application/octet-stream"))


def main() -> None:
    host = CONFIG["server"]["host"]
    port = CONFIG["server"]["port"]
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Cathay fare dashboard: http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
