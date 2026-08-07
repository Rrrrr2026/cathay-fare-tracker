"""Google Flights fetcher built on fast-flights 3.0.2.

Two machine-verified workarounds (2026-08-07):
  1. Google serves an EEA-style consent interstitial to this egress IP, so a
     custom FetchIntegration presets CONSENT/SOCS cookies.
  2. The stock parser raises IndexError on "price unavailable" itineraries, so
     a tolerant parse_js replacement maps those to price=None.
Prices come back as bare ints in the currency requested via create_query -
ALWAYS pass currency explicitly; Google's default guesses from the egress IP.
"""
from __future__ import annotations

import json
import time

from primp import Client

import fast_flights.parser as ffparser
from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.integrations.base import FetchIntegration
from fast_flights.model import (Airline, Alliance, Airport, CarbonEmission,
                                Flights, JsMetadata, SimpleDatetime, SingleFlight)
from fast_flights.querying import Query

URL = "https://www.google.com/travel/flights"


def _tolerant_parse_js(js):
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise ffparser.FlightsNotFound("no flights found; received error")
    payload = json.loads(data)
    meta = JsMetadata(
        alliances=[Alliance(code=c, name=n) for c, n in payload[7][1][0]],
        airlines=[Airline(code=c, name=n) for c, n in payload[7][1][1]],
    )
    flights = ffparser.ResultList()
    if payload[3][0] is None:
        flights.metadata = meta
        return flights
    for k in payload[3][0]:
        flight = k[0]
        try:
            price = k[1][0][1]
        except IndexError:
            price = None  # Google returned this itinerary without a price
        legs = [SingleFlight(
                    from_airport=Airport(code=sf[3], name=sf[4]),
                    to_airport=Airport(code=sf[6], name=sf[5]),
                    departure=SimpleDatetime(date=sf[20], time=sf[8]),
                    arrival=SimpleDatetime(date=sf[21], time=sf[10]),
                    duration=sf[11], plane_type=sf[17])
                for sf in flight[2]]
        extras = flight[22]
        flights.append(Flights(type=flight[0], price=price, airlines=flight[1],
                               flights=legs,
                               carbon=CarbonEmission(typical_on_route=extras[8],
                                                     emission=extras[7])))
    flights.metadata = meta
    return flights


ffparser.parse_js = _tolerant_parse_js


class ConsentFetch(FetchIntegration):
    def fetch_html(self, q):
        client = Client(impersonate="chrome_145", impersonate_os="macos",
                        referer=True, cookie_store=True)
        client.set_cookies("https://www.google.com", {
            "CONSENT": "PENDING+987",
            "SOCS": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg",
        })
        params = q.params() if isinstance(q, Query) else {"q": q}
        return client.get(URL, params=params).text


def _fmt_time(t) -> str:
    if not t:  # Google occasionally omits fields entirely
        return "?"
    parts = list(t) + [0, 0]
    return f"{parts[0] or 0:02d}:{parts[1] or 0:02d}"


def lowest_cx_fare(origin: str, dest: str, date: str, currency: str = "HKD",
                   seat: str = "economy", max_retries: int = 2):
    """Lowest Cathay Pacific fare for a one-way itinerary on `date` (YYYY-MM-DD).

    Returns (result, error). result is None with error=None when the route had
    flights but none operated by Cathay Pacific with a price. result dict:
    price, currency, stops, dep_time, flight_info, cx_options.
    """
    q = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=dest)],
        trip="one-way",
        seat=seat,
        passengers=Passengers(adults=1),
        currency=currency,
    )
    last_err = None
    for attempt in range(max_retries + 1):
        if attempt:
            time.sleep(5 * attempt)
        try:
            result = get_flights(q, integration=ConsentFetch())
        except ffparser.FlightsNotFound:
            return None, "no flights found"
        except Exception as e:  # network hiccups, transient parse failures
            last_err = f"{type(e).__name__}: {e}"
            continue
        cx = [f for f in result
              if f.price is not None
              and any(isinstance(name, str) and "Cathay" in name
                      for name in (f.airlines or []))]
        if not cx:
            return None, None
        best = min(cx, key=lambda f: f.price)
        stops = max(len(best.flights or []) - 1, 0)
        first_leg = (best.flights or [None])[0]
        dep = _fmt_time(first_leg.departure.time) if first_leg and first_leg.departure else "?"
        info = f"dep {dep} " + ("non-stop" if stops == 0 else f"{stops} stop(s)")
        return {
            "price": float(best.price),
            "currency": currency,
            "stops": stops,
            "dep_time": dep,
            "flight_info": info,
            "cx_options": len(cx),
        }, None
    return None, last_err or "unknown error"
