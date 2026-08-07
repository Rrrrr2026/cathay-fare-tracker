"""Daily HKD -> USD rate for converting Cathay's official (HKD) fares.

Google Flights fares are requested in USD natively; only the Cathay API rows
need conversion. Two free no-key sources with a local cache fallback, so one
bad day for an FX provider can never block fare collection.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import requests

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "fx_cache.json"
LAST_RESORT_RATE = 0.128  # HKD is pegged 7.75-7.85/USD, so this is never far off


def hkd_to_usd() -> tuple[float, str]:
    """Return (rate, source). Never raises."""
    for url, extract, name in (
        ("https://open.er-api.com/v6/latest/HKD",
         lambda j: j["rates"]["USD"], "open.er-api.com"),
        ("https://api.frankfurter.app/latest?from=HKD&to=USD",
         lambda j: j["rates"]["USD"], "frankfurter.app"),
    ):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            rate = float(extract(r.json()))
            if 0.09 < rate < 0.16:  # sanity band around the peg
                CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                CACHE_PATH.write_text(
                    json.dumps({"date": date.today().isoformat(), "hkd_usd": rate,
                                "source": name}),
                    encoding="utf-8")
                return rate, name
        except Exception:
            continue
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return float(cached["hkd_usd"]), f"cache ({cached.get('date', '?')})"
    except Exception:
        return LAST_RESORT_RATE, "hardcoded peg fallback"
