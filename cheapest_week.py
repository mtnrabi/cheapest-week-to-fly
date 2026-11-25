#!/usr/bin/env python3
"""cheapest-week-to-fly: find the cheapest dates for a round trip.

Scans every departure date in a range, at one or more trip lengths, and prints
the round trips ranked by total price, with Google's own verdict on each fare
(low / typical / high) and its usual price range for the route.

Each (departure date, trip length) is ONE request to the Google Flights Live API
round-trip endpoint: both legs are priced together and come back as one total
with one booking link, so there is no second call for the return flight.

Standard library only. Python 3.9+.

    export RAPIDAPI_KEY=your-key
    python cheapest_week.py --from JFK --to LIS --start 2027-02-01 --end 2027-02-07 --nights 7
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

API_HOST = "google-flights-live-api.p.rapidapi.com"
ROUNDTRIP_URL = f"https://{API_HOST}/api/google_flights/roundtrip/v1"
SIGNUP_URL = "https://rapidapi.com/mtnrabi/api/google-flights-live-api"
USER_AGENT = "cheapest-week-to-fly/1.0 (+https://github.com/mtnrabi/cheapest-week-to-fly)"

# Optional: lets FlightPowers count requests that come from this example.
# The API drops these two fields before it reads the search. Delete them if you prefer.
ATTRIBUTION = {"_fp_source": "gh-example", "_fp_tool": "cheapest-week-to-fly"}


class SearchFailed(Exception):
    """The search did not complete (5xx). Says nothing about availability; safe to retry."""


@dataclass
class Trip:
    departure_date: str
    return_date: str
    nights: int
    status: str  # "ok" | "no_flights" | "failed"
    price: Optional[float] = None
    price_text: str = ""
    airline: str = ""
    stops: Optional[int] = None
    verdict: str = ""  # Google: low | typical | high
    range_low: Optional[float] = None
    range_high: Optional[float] = None
    buy_link: str = ""
    error: str = ""
    raw: list = field(default_factory=list, repr=False)


def build_body(origin: str, dest: str, depart: str, ret: str, currency: str,
               max_stops: Optional[int], limit: int = 10) -> dict:
    """The JSON body of one round-trip request (field names as in the API docs)."""
    body = {
        "from_airport": origin,
        "to_airport": dest,
        "departure_date": depart,
        "return_date": ret,
        "currency": currency,
        "limit": limit,
    }
    # No "sort_type" on purpose: Google gives no usual range and no low/typical/high verdict on a
    # price-sorted page, so the last two columns would be empty. cheapest_trip() picks the row.
    if max_stops is not None:
        body["max_departure_stops"] = max_stops
        body["max_return_stops"] = max_stops
    body.update(ATTRIBUTION)
    return body


def http_search(body: dict, api_key: str, timeout: float = 90) -> list:
    """POST one round-trip search. Returns the JSON array of itineraries."""
    req = urllib.request.Request(
        ROUNDTRIP_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-rapidapi-key": api_key,
            "x-rapidapi-host": API_HOST,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code >= 500:
            raise SearchFailed(f"HTTP {e.code}: {detail}") from None
        if e.code in (401, 403):
            raise SystemExit(f"The API refused the key (HTTP {e.code}). Is your key subscribed to {SIGNUP_URL} ?")
        if e.code == 429:
            raise SystemExit("Rate limit or monthly quota reached (HTTP 429). See your plan on RapidAPI.")
        raise SystemExit(f"Request rejected (HTTP {e.code}): {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SearchFailed(str(e)) from None


def replay_search(folder: Path) -> Callable[[dict], list]:
    """A search function that answers from recorded responses (no key, no network)."""
    def search(body: dict) -> list:
        f = folder / f"{body['from_airport']}-{body['to_airport']}_{body['departure_date']}_{body['return_date']}.json"
        if not f.exists():
            raise SearchFailed(f"no recorded response {f.name}")
        return json.loads(f.read_text())
    return search


def cheapest_trip(rows: list, depart: str, ret: str, nights: int) -> Trip:
    """Reduce one response to its cheapest itinerary."""
    priced = [r for r in rows if isinstance(r.get("total_price_as_number"), (int, float))]
    if not priced:
        return Trip(depart, ret, nights, status="no_flights", raw=rows)
    best = min(priced, key=lambda r: r["total_price_as_number"])
    return Trip(
        departure_date=depart,
        return_date=ret,
        nights=nights,
        status="ok",
        price=best["total_price_as_number"],
        price_text=best.get("total_price", ""),
        airline=best.get("departure_flight_airline", ""),
        stops=best.get("total_stops"),
        verdict=best.get("price_range_in_relation_to_other_periods") or "",
        range_low=best.get("price_insights_low"),
        range_high=best.get("price_insights_high"),
        buy_link=best.get("buy_link", ""),
        raw=rows,
    )


def date_range(start: str, end: str) -> list[str]:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if b < a:
        raise SystemExit("--end is before --start")
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def plan(start: str, end: str, nights: list[int]) -> list[tuple[str, str, int]]:
    out = []
    for d in date_range(start, end):
        for n in nights:
            out.append((d, (date.fromisoformat(d) + timedelta(days=n)).isoformat(), n))
    return out


def scan(origin: str, dest: str, combos: list[tuple[str, str, int]], search: Callable[[dict], list],
         currency: str = "usd", max_stops: Optional[int] = None, workers: int = 4,
         retries: int = 1) -> list[Trip]:
    """Run one search per (date, nights) combination and keep the cheapest trip of each."""
    def one(combo):
        depart, ret, n = combo
        body = build_body(origin, dest, depart, ret, currency, max_stops)
        last = ""
        for attempt in range(retries + 1):
            try:
                return cheapest_trip(search(body), depart, ret, n)
            except SearchFailed as e:
                last = str(e)
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        return Trip(depart, ret, n, status="failed", error=last)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, combos))


def rank(trips: list[Trip]) -> list[Trip]:
    ok = sorted((t for t in trips if t.status == "ok"), key=lambda t: (t.price, t.departure_date, t.nights))
    rest = sorted((t for t in trips if t.status != "ok"), key=lambda t: (t.departure_date, t.nights))
    return ok + rest


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def table(trips: list[Trip], currency: str, top: Optional[int] = None) -> str:
    head = ["#", "depart", "return", "nights", f"total {currency.upper()}", "stops", "airline (outbound)", "Google says", "usual range"]
    rows = []
    shown = trips if top is None else trips[:top]
    for i, t in enumerate(shown, 1):
        if t.status == "ok":
            rng = f"{_fmt_money(t.range_low)}-{_fmt_money(t.range_high)}" if t.range_low is not None else "-"
            airline = t.airline if len(t.airline) <= 32 else t.airline[:31] + "…"
            rows.append([str(i), t.departure_date, t.return_date, str(t.nights), _fmt_money(t.price),
                         "-" if t.stops is None else str(t.stops), airline, t.verdict or "-", rng])
        else:
            label = "no flights found" if t.status == "no_flights" else "search failed, retry"
            rows.append(["-", t.departure_date, t.return_date, str(t.nights), "-", "-", label, "-", "-"])
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(head)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
    return "\n".join([line(head), line(["-" * w for w in widths]), *(line(r) for r in rows)])


def write_csv(trips: list[Trip], path: str, fetched_at: str) -> None:
    cols = ["departure_date", "return_date", "nights", "status", "price", "price_text", "airline", "stops",
            "verdict", "range_low", "range_high", "buy_link", "fetched_at"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in trips:
            w.writerow([t.departure_date, t.return_date, t.nights, t.status, t.price, t.price_text, t.airline,
                        t.stops, t.verdict, t.range_low, t.range_high, t.buy_link, fetched_at])


def parse_nights(s: str) -> list[int]:
    try:
        vals = sorted({int(x) for x in s.split(",") if x.strip()})
    except ValueError:
        raise argparse.ArgumentTypeError("--nights takes numbers like 7 or 5,7,10")
    if not vals or min(vals) < 1:
        raise argparse.ArgumentTypeError("--nights must be 1 or more")
    return vals


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Find the cheapest dates for a round trip (live Google Flights fares).")
    p.add_argument("--from", dest="origin", required=True, help="origin airport IATA code, e.g. JFK")
    p.add_argument("--to", dest="dest", required=True, help="destination airport IATA code, e.g. LIS")
    p.add_argument("--start", required=True, help="first departure date, YYYY-MM-DD")
    p.add_argument("--end", required=True, help="last departure date, YYYY-MM-DD")
    p.add_argument("--nights", type=parse_nights, default=[7], help="trip length(s) in nights, e.g. 7 or 5,7,10 (default 7)")
    p.add_argument("--currency", default="usd")
    p.add_argument("--max-stops", type=int, default=None, help="max stops per leg (0 = nonstop only)")
    p.add_argument("--top", type=int, default=None, help="show only the N cheapest")
    p.add_argument("--csv", default=None, help="also write every result to this CSV file")
    p.add_argument("--workers", type=int, default=4, help="parallel requests (default 4)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation for scans over 20 requests")
    p.add_argument("--replay", default=None, help="answer from recorded responses in this folder (no key needed)")
    a = p.parse_args(argv)

    origin, dest = a.origin.strip().upper(), a.dest.strip().upper()
    combos = plan(a.start, a.end, a.nights)

    if a.replay:
        search = replay_search(Path(a.replay))
        source = f"recorded responses in {a.replay}"
    else:
        key = os.environ.get("RAPIDAPI_KEY", "").strip()
        if not key:
            print(f"Set RAPIDAPI_KEY first. Get a free key at {SIGNUP_URL}", file=sys.stderr)
            return 2
        if len(combos) > 20 and not a.yes:
            ans = input(f"This scan makes {len(combos)} requests on your RapidAPI plan. Continue? [y/N] ")
            if ans.strip().lower() not in ("y", "yes"):
                return 1
        search = lambda body: http_search(body, key)
        source = "live"

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stamp = Path(a.replay or ".") / "RECORDED_AT.txt"
    if a.replay and stamp.exists():
        fetched_at = stamp.read_text().strip()  # when the recorded responses were fetched
    trips = rank(scan(origin, dest, combos, search, a.currency, a.max_stops, a.workers))

    print(f"{origin} -> {dest}, round trip, departures {a.start} to {a.end}, "
          f"{'/'.join(map(str, a.nights))} nights ({source}, fetched {fetched_at})\n")
    print(table(trips, a.currency, a.top))
    ok = [t for t in trips if t.status == "ok"]
    if ok:
        best = ok[0]
        print(f"\nCheapest: depart {best.departure_date}, back {best.return_date}, "
              f"{_fmt_money(best.price)} {a.currency.upper()} total (Google: {best.verdict or 'n/a'}).")
        print(f"Book: {best.buy_link}")
    failed = sum(t.status == "failed" for t in trips)
    print(f"\n{len(combos)} requests, {len(ok)} priced, {failed} failed"
          + (" (a failed search is not 'no flights': run it again)" if failed else "") + ".")
    if a.csv:
        write_csv(trips, a.csv, fetched_at)
        print(f"Wrote {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
