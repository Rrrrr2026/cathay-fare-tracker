# Cathay Pacific Fare Tracker 国泰航空全航线票价追踪

Daily recorder of the lowest Cathay Pacific (CX) fares on **all 82 current CX
passenger routes** (every route touches the HKG hub; no fifth-freedom sectors
exist as of Aug 2026), with a local dashboard showing continent-level averages,
per-route drill-down, and a manual refresh button.

All prices are **USD, taxes included, lowest CX economy itinerary**. Google
Flights is queried in USD natively; Cathay's official API quotes HKD, which is
converted at a daily rate (open.er-api.com → frankfurter.app → cached rate →
7.8-peg fallback; the HKD original and rate are kept in `flight_info`).
Continent groups: **Africa / Americas / Asia / Australia / Europe / Middle
East**.

**Fully local, no AI in the loop**: the daily pipeline is Windows Task
Scheduler → `run_daily.bat` → plain Python → `data/fares.sqlite` + CSVs on this
machine. The dashboard (vendored ECharts, stdlib HTTP server) also runs
entirely locally. Internet is needed only to reach the two fare sources and
the FX rate.

**Shared online copy**: https://rrrrr2026.github.io/cathay-fare-tracker/ — a
static export of the same dashboard (public URL, works anytime, no local
server needed). Each successful collection regenerates `docs/`
(`scripts/export_static.py`) and pushes, so the site refreshes itself every
morning. The ⟳ refresh button exists only on the local copy; the online page
is a read-only daily snapshot.

## Data sources (verified 2026-08-07)

| Source | What it provides | Coverage |
|---|---|---|
| **Google Flights** (via `fast-flights` 3.0.2) | Systematic series: lowest CX one-way fare per route at booking horizons **+7/30/60/90 days** | All 82 destinations, both directions |
| **Cathay Pacific official instant-search API** (`book.cathaypacific.com/.../instant/*`, no auth) | `open-search`: cheapest cached round-trip HKG→each destination in one call; `histogram`: official per-day one-way fare calendar (~90-day window) | Routes present in Cathay's fare cache (~78 destinations for open-search; calendar varies) |

Where Google lists no priced CX itinerary for a route (e.g. Changsha), the
dashboard falls back to the official calendar fare for the same departure date,
tagged `cal`. Fares are never mixed silently — every record carries its
`source` column.

## Layout

```
cathay-fare-tracker/
├─ config.json              horizons, delays, currency, server port
├─ routes.json              82 destinations w/ IATA + region (regenerable)
├─ run_daily.bat            headless daily run (Task Scheduler entry point)
├─ serve_dashboard.bat      start dashboard server + open browser
├─ scripts/
│  ├─ run_daily.py          collection orchestrator (progress → data/progress.json)
│  ├─ api_server.py         dashboard + JSON API server (stdlib only)
│  ├─ store.py              SQLite schema/upsert/CSV export
│  └─ fetchers/
│     ├─ google_flights.py  fast-flights wrapper (consent cookie + tolerant parser)
│     └─ cathay_api.py      official instant-search endpoints
├─ data/
│  ├─ fares.sqlite          all records (PK dedupes re-runs within a day)
│  ├─ daily/YYYY-MM-DD.csv  per-day export
│  └─ progress.json         live progress of the current run
└─ logs/run_YYYY-MM-DD.log
```

## Usage

Dashboard (starts server, opens browser):

```bash
serve_dashboard.bat
```

Manual collection from the command line (the dashboard's ⟳ button does the same
via `POST /api/refresh`):

```bash
run_daily.bat
```

Useful dev flags: `--limit 5` (first N destinations), `--horizons 30`,
`--sources google` or `--sources cathay`.

The scheduled task **CathayFareTracker** runs `run_daily.bat` daily at 07:30.
A full run makes ~660 Google queries (2–3.5 s apart) + ~250 Cathay API calls
and takes ≈ 45 minutes. Re-running the same day is safe: the primary key
`(collected_date, origin, destination, depart_date, cabin, trip_type, source)`
makes runs idempotent (official-fare snapshots are replaced transactionally),
so if a run stalls, just hit ⟳ again.

Reliability behavior:
- Task has `StartWhenAvailable` + `WakeToRun`: a 07:30 missed to sleep/power-off
  catches up as soon as the machine is next available. Limitation: it runs with
  an interactive token, so the machine must be **logged in** (lock screen is
  fine). Granting S4U ("run whether user is logged on or not") needs an
  elevated `Set-ScheduledTask` — see review notes.
- `run_daily.bat` retries a failed run up to 3×, 10 min apart; `run_daily.py`
  exits immediately if another collection is already active (10-min staleness
  rule on `data/progress.json`; `--force` overrides).
- 12 consecutive Google fetch failures abort the Google phase and mark the run
  **degraded** (shown on the dashboard meta line) instead of grinding retries.
- A crashed run still exports its partial CSV; the dashboard's ⟳ button is the
  manual recovery path.

## JSON API (stable interface, CORS enabled)

| Endpoint | Returns |
|---|---|
| `GET /api/meta` | routes, config, collection dates, row counts |
| `GET /api/summary?horizon=30&direction=out` | continent averages/min/max + prev-day trend |
| `GET /api/trends?horizon=30&direction=out` | per-continent + overall average per collection day (fare index) |
| `GET /api/movers?horizon=30&direction=out&limit=8` | biggest day-over-day route moves (needs ≥2 days) |
| `GET /api/continent/Asia?horizon=30` | per-route fares (both directions, official RT, Δ) |
| `GET /api/route/HKG/NRT` | full fare history + official forward calendar |
| `GET /api/refresh/status` | live progress of a running collection |
| `POST /api/refresh` | start a manual run (`409` if one is already running) |

`direction=out` is HKG→destination, `direction=in` the reverse.

## Maintenance notes

- **Route list**: `routes.json` was generated 2026-08-07 from Wikipedia's CX
  destination list cross-checked against cathaypacific.com and
  FlightConnections. When CX adds/cuts routes, edit it (or regenerate) — the
  scraper and dashboard adapt automatically.
- **fast-flights pin**: 3.0.2. The wrapper monkey-patches its parser (upstream
  crashes on "price unavailable" itineraries) and presets Google consent
  cookies. If an upgrade breaks either, re-check `fetchers/google_flights.py`.
- **Currency**: Google fares are requested with `currency=HKD` explicitly —
  never trust the geo-IP default. Cathay API returns origin-market currency
  (HKD for HKG departures; that's why official calendar collection is
  outbound-only).
- Google Flights shows the same public fares as an anonymous search
  (no login/cookies are carried), so no personalized-pricing distortion.
