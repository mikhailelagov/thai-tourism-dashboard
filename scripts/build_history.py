#!/usr/bin/env python3
"""Rebuild the same week in past years, for comparison.

Answers "was it like this last year?" using the archive weather API, which is
free and reaches back to 1940. Only the weather-driven part of the index is
comparable across years - holidays move around the calendar and arrivals
statistics are not published at this granularity - so this deliberately drops
the event bonus and says so in the output.

Writes history.json.
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_data import REGIONS  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "history.json"

YEARS_BACK = 4
WINDOW_DAYS = 7
TIMEOUT = 45
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def weather_index(region, mm, pop):
    """The weather half of the demand index, with no event bonus.

    Mirrors build_data so the numbers line up; events are excluded on purpose
    because a holiday that fell midweek in one year falls on a weekend in the
    next, which would compare calendars rather than conditions.
    """
    rain = min(1.0, (mm / 28.0) * 0.75 + (pop / 100.0) * 0.25)
    return max(0, min(100, round(region["base"] * (1 - rain * region["wsens"]))))


def fetch_year(year, start, end):
    """One request covers every region - the API takes comma-separated coords."""
    lat = ",".join(str(r["lat"]) for r in REGIONS)
    lon = ",".join(str(r["lon"]) for r in REGIONS)
    url = (f"{ARCHIVE}?latitude={lat}&longitude={lon}"
           f"&start_date={year}-{start}&end_date={year}-{end}"
           f"&daily=temperature_2m_max,precipitation_sum"
           f"&timezone=Asia%2FBangkok")
    req = urllib.request.Request(url, headers={"User-Agent": "thai-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data if isinstance(data, list) else [data]


def build():
    today = date.today()
    end = today + timedelta(days=WINDOW_DAYS - 1)
    years = {}

    for back in range(1, YEARS_BACK + 1):
        year = today.year - back
        try:
            blocks = fetch_year(year, today.strftime("%m-%d"), end.strftime("%m-%d"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"warn: no archive for {year}: {e}", file=sys.stderr)
            continue

        regions = {}
        for region, block in zip(REGIONS, blocks):
            daily = block.get("daily") or {}
            rain = [x or 0.0 for x in daily.get("precipitation_sum") or []]
            temp = [x for x in daily.get("temperature_2m_max") or [] if x is not None]
            if not rain:
                continue
            # No archived probability field, so approximate it from rainfall.
            scores = [weather_index(region, mm, 100 if mm >= 5 else mm * 20)
                      for mm in rain]
            regions[region["id"]] = {
                "avg_score": round(sum(scores) / len(scores)),
                "mm_total": round(sum(rain), 1),
                "wet_days": sum(1 for mm in rain if mm >= 15),
                "tmax_avg": round(sum(temp) / len(temp), 1) if temp else None,
            }
        if regions:
            years[str(year)] = regions

    if not years:
        print("no archive data retrieved.", file=sys.stderr)
        return 1

    OUT.write_text(json.dumps({
        "window": {"from": today.strftime("%m-%d"), "to": end.strftime("%m-%d"),
                   "days": WINDOW_DAYS},
        "note": "weather-only index; events and holidays excluded so the years "
                "compare conditions rather than calendars",
        "years": years,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"wrote {OUT.name} - {len(years)} years, {len(next(iter(years.values())))} regions")
    return 0


if __name__ == "__main__":
    sys.exit(build())
