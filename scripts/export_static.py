"""Export the dashboard as a static site into docs/ for GitHub Pages.

Reuses api_server's aggregation functions verbatim, pre-computing every
endpoint response the dashboard can request into a JSON file:

  api/meta.json
  api/summary-{h}-{dir}.json | trends-{h}-{dir}.json | movers-{h}-{dir}.json
  api/continent-{Name_With_Underscores}-{h}.json
  api/route-{ORIG}-{DEST}.json          (both directions, all destinations)
  api/refresh-status.json               (static stub)

docs/index.html is dashboard/index.html with window.STATIC_MODE injected,
which switches the page to file-based fetches and hides the refresh button.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import api_server as srv

DOCS = PROJECT_ROOT / "docs"
API = DOCS / "api"


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    if not srv.DB_PATH.exists():
        print("no database yet - nothing to export")
        return 1
    # clean slate so renamed/dropped files do not live on as orphans in git/Pages
    shutil.rmtree(API, ignore_errors=True)
    conn = srv._db()
    try:
        dump(API / "meta.json", srv.api_meta(conn))
        dump(API / "drift.json", srv.api_drift(conn, {}))
        dump(API / "departure-watch.json", srv.api_departure_watch(conn, {}))
        for d in ("out", "in"):
            dump(API / f"window-compare-{d}.json",
                 srv.api_window_compare(conn, {"direction": [d]}))
            dump(API / f"forward-structure-{d}.json",
                 srv.api_forward_structure(conn, {"direction": [d]}))
        for w in (1, 7, 14, 30):
            dump(API / f"flight-movers-{w}.json",
                 srv.api_flight_movers(conn, {"window": [str(w)], "limit": ["10"]}))
        horizons = srv.CONFIG["horizons_days"]
        continents = sorted({m["continent"] for m in srv.DEST_META.values()})
        n = 1
        for h in horizons:
            for d in ("out", "in"):
                qs = {"horizon": [str(h)], "direction": [d]}
                dump(API / f"summary-{h}-{d}.json", srv.api_summary(conn, qs))
                dump(API / f"trends-{h}-{d}.json", srv.api_trends(conn, qs))
                n += 2
        for name in continents:
            for d in ("out", "in"):
                dump(API / f"continent-{name.replace(' ', '_')}-{d}.json",
                     srv.api_continent(conn, name, {"direction": [d]}))
                n += 1
        for iata in srv.DEST_META:
            dump(API / f"route-HKG-{iata}.json", srv.api_route(conn, "HKG", iata))
            dump(API / f"route-{iata}-HKG.json", srv.api_route(conn, iata, "HKG"))
            n += 2
        dump(API / "refresh-status.json", {"running": False, "static": True})
    finally:
        conn.close()

    html = (PROJECT_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    marker = '<script src="echarts.min.js"></script>'
    assert marker in html, "dashboard/index.html changed - update export marker"
    html = html.replace(marker,
                        "<script>window.STATIC_MODE=true</script>\n" + marker)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    shutil.copy2(PROJECT_ROOT / "dashboard" / "echarts.min.js", DOCS / "echarts.min.js")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"exported {n} api files + index.html to {DOCS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
