import csv
import json
from pathlib import Path

import pytest

import cheapest_week as cw

REC = Path(__file__).resolve().parent.parent / "recorded" / "jfk-lis-2027-02"


def test_body_uses_documented_field_names():
    body = cw.build_body("JFK", "LIS", "2027-02-01", "2027-02-08", "usd", max_stops=0)
    for k in ("from_airport", "to_airport", "departure_date", "return_date", "currency", "limit"):
        assert k in body
    assert body["max_departure_stops"] == 0 and body["max_return_stops"] == 0
    # either one drops Google's range and verdict from the answer
    assert "sort_type" not in body and "max_price" not in body
    # nothing camelCased: the API rejects unknown fields with a 422
    assert all(k == k.lower() for k in body)


def test_plan_expands_dates_times_nights():
    combos = cw.plan("2027-02-01", "2027-02-03", [5, 7])
    assert len(combos) == 6
    assert combos[0] == ("2027-02-01", "2027-02-06", 5)
    assert combos[-1] == ("2027-02-03", "2027-02-10", 7)


def test_scan_on_recorded_responses_ranks_cheapest_first():
    combos = cw.plan("2027-02-01", "2027-02-07", [7])
    trips = cw.rank(cw.scan("JFK", "LIS", combos, cw.replay_search(REC), workers=2))
    assert [t.status for t in trips] == ["ok"] * 7
    prices = [t.price for t in trips]
    assert prices == sorted(prices)
    assert trips[0].price == 628 and trips[0].departure_date == "2027-02-01"
    assert trips[-1].price == 713 and trips[-1].departure_date == "2027-02-06"
    assert trips[0].verdict in {"low", "typical", "high"}
    assert trips[0].buy_link.startswith("https://www.google.com/travel/flights")


def test_cheapest_row_wins_when_it_is_not_first():
    # default order: fewest stops first, so the cheapest row can come later
    band = {"price_insights_low": 100, "price_insights_high": 200, "price_range_in_relation_to_other_periods": "low"}
    rows = [dict(band, total_price_as_number=140, total_stops=0), dict(band, total_price_as_number=90, total_stops=2)]
    t = cw.cheapest_trip(rows, "2027-02-01", "2027-02-08", 7)
    assert (t.price, t.stops, t.verdict, t.range_low, t.range_high) == (90, 2, "low", 100, 200)


def test_failed_search_is_reported_not_dropped(monkeypatch):
    monkeypatch.setattr(cw.time, "sleep", lambda s: None)
    calls = []

    def flaky(body):
        calls.append(body["departure_date"])
        raise cw.SearchFailed("HTTP 503")

    trips = cw.scan("JFK", "LIS", [("2027-02-01", "2027-02-08", 7)], flaky, retries=1)
    assert trips[0].status == "failed"
    assert len(calls) == 2  # one retry
    assert "search failed" in cw.table(cw.rank(trips), "usd")


def test_empty_array_means_no_flights():
    t = cw.cheapest_trip([], "2027-02-01", "2027-02-08", 7)
    assert t.status == "no_flights"


def test_csv_and_cli_replay(tmp_path, capsys):
    out = tmp_path / "trips.csv"
    rc = cw.main(["--from", "jfk", "--to", "lis", "--start", "2027-02-01", "--end", "2027-02-07",
                  "--replay", str(REC), "--csv", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "Cheapest: depart 2027-02-01" in text
    assert "2026-11-11 21:55 UTC" in text
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 7 and rows[0]["price"] == "628"


def test_missing_key_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    assert cw.main(["--from", "JFK", "--to", "LIS", "--start", "2027-02-01", "--end", "2027-02-01"]) == 2
    assert "rapidapi.com/mtnrabi/api/google-flights-live-api" in capsys.readouterr().err
