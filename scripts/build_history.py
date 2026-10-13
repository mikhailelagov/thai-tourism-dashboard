#!/usr/bin/env python3
"""Rebuild the same calendar week in past years, for comparison.

Answers "was the weather like this last year?" from the free Open-Meteo
archive, which reaches back to 1940. mm_total, wet_days and tmax_avg are
measured. avg_score is only the weather term of the build_data model, with
rain probability approximated from rainfall because the archive has none;
holidays and events are left out because they move around the calendar.

Each past year gets real dates: the window starts on the same month and day
(29 February becomes 28 February in a non-leap year) and runs WINDOW_DAYS from
there, so a week that crosses New Year ends in the following year. Days the
archive leaves empty stay out of the sums and are counted, never read as dry.

A year that fails keeps its previous figures when history.json already holds
the same window. Exits 1, leaving history.json as it was, only when no year
could be fetched.

Writes history.json.
"""

import json
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build_data import BANGKOK, REGIONS, error_entry, get_bytes, write_json  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "history.json"

YEARS_BACK = 4
WINDOW_DAYS = 7
WET_MM = 15
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
NOTE = ("weather-only index; events and holidays excluded so the years compare "
        "conditions rather than calendars; rain probability approximated from "
        "rainfall")


def weather_index(region, mm, pop):
    """The weather term of the build_data model, with no event bonus."""
    rain = min(1.0, (mm / 28.0) * 0.75 + (pop / 100.0) * 0.25)
    return max(0, min(100, round(region["base"] * (1 - rain * region["wsens"]))))


def past_window(today, year, days=WINDOW_DAYS):
    """(start, end) dates in `year` for the window that starts on today's day."""
    day = today.day
    if today.month == 2 and day == 29:
        try:
            date(year, 2, 29)
        except ValueError:
            day = 28
    start = date(year, today.month, day)
    return start, start + timedelta(days=days - 1)


def archive_url(start, end):
    """One request covers every region - the API takes comma-separated coords."""
    lat = ",".join(str(r["lat"]) for r in REGIONS)
    lon = ",".join(str(r["lon"]) for r in REGIONS)
    return (f"{ARCHIVE}?latitude={lat}&longitude={lon}"
            f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
            "&daily=temperature_2m_max,precipitation_sum&timezone=Asia%2FBangkok")


def parse_archive(raw):
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(data.get("reason") or "archive API error")
    blocks = data if isinstance(data, list) else [data]
    if len(blocks) != len(REGIONS):
        raise ValueError("expected %d locations, got %d" % (len(REGIONS), len(blocks)))
    return blocks


def summarize(region, daily):
    """Measured week figures for one region, or None when no day has rain data."""
    def values(key):
        return [x if isinstance(x, (int, float)) else None for x in daily.get(key) or []]

    rain = [mm for mm in values("precipitation_sum") if mm is not None]
    temp = [t for t in values("temperature_2m_max") if t is not None]
    if not rain:
        return None
    # No archived probability field, so approximate it from rainfall.
    scores = [weather_index(region, mm, 100 if mm >= 5 else mm * 20) for mm in rain]
    return {
        "avg_score": round(sum(scores) / len(scores)),
        "mm_total": round(sum(rain), 1),
        "wet_days": sum(1 for mm in rain if mm >= WET_MM),
        "tmax_avg": round(sum(temp) / len(temp), 1) if temp else None,
        "days_with_data": len(rain),
    }


def load_previous(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def build(now=None, fetch=get_bytes, out=OUT):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    at = now.isoformat(timespec="seconds")
    today = now.astimezone(BANGKOK).date()
    end = today + timedelta(days=WINDOW_DAYS - 1)
    previous = load_previous(out)
    same_window = (previous.get("window") or {}).get("start_date") == today.isoformat()
    years, windows, errors, fetched = {}, {}, [], 0

    for back in range(1, YEARS_BACK + 1):
        start, stop = past_window(today, today.year - back)
        label = str(start.year)
        try:
            blocks = parse_archive(fetch(archive_url(start, stop)))
        except Exception as e:
            errors.append(error_entry("archive:" + label, e, at))
            if same_window and label in (previous.get("years") or {}):
                years[label] = previous["years"][label]
                windows[label] = (previous.get("year_windows") or {}).get(label)
            continue

        regions = {}
        for region, block in zip(REGIONS, blocks):
            summary = summarize(region, block.get("daily") or {})
            if summary:
                regions[region["id"]] = summary
        if regions:
            years[label] = regions
            windows[label] = {"from": start.isoformat(), "to": stop.isoformat()}
            fetched += 1

    for e in errors:
        print(f"warn: {e['source']}: {e['error']}", file=sys.stderr)
    if not fetched:
        print("no archive data retrieved; history.json left as it was.", file=sys.stderr)
        return 1

    write_json(out, {
        "built_at": at,
        "window": {"from": today.strftime("%m-%d"), "to": end.strftime("%m-%d"),
                   "days": WINDOW_DAYS, "start_date": today.isoformat(),
                   "end_date": end.isoformat()},
        "year_windows": windows,
        "score_kind": "weather_only_model",
        "note": NOTE,
        "source": "Open-Meteo historical weather API (CC BY 4.0)",
        "source_url": "https://open-meteo.com/en/docs/historical-weather-api",
        "years": years,
        "errors": errors,
    })
    print(f"wrote {out.name} - {len(years)} years ({fetched} fetched), "
          f"{len(next(iter(years.values())))} regions")
    return 0


if __name__ == "__main__":
    sys.exit(build())
