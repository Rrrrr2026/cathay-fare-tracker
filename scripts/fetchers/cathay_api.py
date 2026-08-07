"""Official Cathay Pacific instant-search fare endpoints (no auth required).

Verified 2026-08-07:
  - open-search: cheapest cached round-trip from ORIGIN to every cached
    destination in one call (~78 rows for HKG), origin-market currency.
  - histogram TYPE=MTH: which months have cached fares for a route.
  - histogram TYPE=DAY: per-day lowest fare calendar for one month
    (TRIP_TYPE=O gives one-way fares).
Routes absent from Cathay's fare cache return HTTP 400 - that is a normal
"not cached" signal, not a failure.
"""
from __future__ import annotations

import requests

BASE = "https://book.cathaypacific.com/CathayPacificV3/dyn/air/api/instant"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
COMMON = {"SITE": "CBEUCBEU", "LANGUAGE": "GB"}
TIMEOUT = 20


def _get(url: str, params: dict):
    """Return (json, None) on success, (None, 'not_cached') on 400, (None, err) otherwise."""
    try:
        r = requests.get(url, params={**COMMON, **params}, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        return None, f"request error: {e}"
    if r.status_code == 400:
        return None, "not_cached"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        return r.json(), None
    except ValueError:
        return None, "non-JSON response"


def _iso(yyyymmdd: str) -> str:
    s = str(yyyymmdd)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def open_search(origin: str = "HKG", cabin: str = "Y"):
    """Cheapest cached round-trip fare from origin to every cached destination.

    Returns (rows, error). Each row: origin, destination, depart_date,
    return_date, price (total incl. tax), base_fare, tax, currency.
    """
    data, err = _get(f"{BASE}/open-search", {"ORIGIN": origin, "CABIN": cabin})
    if err:
        return None, err
    rows = []
    for item in data or []:
        try:
            rows.append({
                "origin": item.get("origin", origin),
                "destination": item["destination"],
                "depart_date": _iso(item["date_departure"]),
                "return_date": _iso(item["date_return"]) if item.get("date_return") else None,
                "price": float(item["total_fare"]),
                "base_fare": float(item.get("base_fare") or 0),
                "tax": float(item.get("tax") or 0),
                "currency": item.get("currency", "HKD"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows, None


def month_probe(origin: str, dest: str):
    """Months (bare numbers) with cached fares for a route, or (None, 'not_cached')."""
    data, err = _get(f"{BASE}/histogram", {"ORIGIN": origin, "DESTINATION": dest, "TYPE": "MTH"})
    if err:
        return None, err
    months = []
    for item in data or []:
        m = item.get("month")
        if isinstance(m, int) and m not in months:
            months.append(m)
    return months, None


def day_calendar(origin: str, dest: str, month: int, cabin: str = "Y", trip_type: str = "O"):
    """Per-day lowest fares for one month. TRIP_TYPE=O -> one-way fares.

    Returns (rows, error). Each row: depart_date, price (total incl. tax),
    base_fare, tax, currency.
    """
    data, err = _get(
        f"{BASE}/histogram",
        {
            "ORIGIN": origin,
            "DESTINATION": dest,
            "TYPE": "DAY",
            "MONTH": month,
            "CABIN": cabin,
            "TRIP_TYPE": trip_type,
        },
    )
    if err:
        return None, err
    rows = []
    for item in data or []:
        try:
            rows.append({
                "depart_date": _iso(item["date_departure"]),
                "price": float(item["total_fare"]),
                "base_fare": float(item.get("base_fare") or 0),
                "tax": float(item.get("tax") or 0),
                "currency": item.get("currency", "HKD"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return rows, None
