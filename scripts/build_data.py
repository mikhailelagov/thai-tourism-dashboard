#!/usr/bin/env python3
"""Build data.json for the Thailand tourism demand dashboard.

Runs daily. Pulls everything it can from open APIs that need no key,
computes the demand index per region per day, and writes data.json,
which both the website and the Telegram bot read.

Standard library only - no pip install needed.
"""

import json
import pathlib
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data.json"
EVENTS_FILE = ROOT / "events.json"

# base = structural demand position, wsens = how much weather moves it
REGIONS = [
    {"id": "bangkok",   "lat": 13.69, "lon": 100.75, "base": 48, "wsens": 0.15,
     "en": "Bangkok",    "ru": "Бангкок"},
    {"id": "pattaya",   "lat": 12.93, "lon": 100.88, "base": 30, "wsens": 0.45,
     "en": "Pattaya",    "ru": "Паттайя"},
    {"id": "phuket",    "lat": 7.88,  "lon": 98.39,  "base": 42, "wsens": 0.42,
     "en": "Phuket",     "ru": "Пхукет"},
    {"id": "krabi",     "lat": 8.09,  "lon": 98.91,  "base": 44, "wsens": 0.44,
     "en": "Krabi",      "ru": "Краби"},
    {"id": "samui",     "lat": 9.51,  "lon": 100.06, "base": 45, "wsens": 0.42,
     "en": "Samui",      "ru": "Самуи"},
    {"id": "chiangmai", "lat": 18.79, "lon": 98.98,  "base": 38, "wsens": 0.30,
     "en": "Chiang Mai", "ru": "Чиангмай"},
]

# Public holiday calendars, as Google's public iCal feeds. These need no key
# and, unlike date.nager.at, they actually cover Thailand, India and Malaysia.
# TH drives domestic demand; the rest are the markets that send tourists here.
HOLIDAY_FEEDS = {
    "TH": "en.th",
    "CN": "en.china",
    "RU": "en.russian",
    "IN": "en.indian",
    "MY": "en.malaysia",
    "KR": "en.south_korea",
    "GB": "en.uk",
    "DE": "en.german",
    "AU": "en.australian",
}

TIMEOUT = 25


def get_text(url, required=True):
    """Fetch a URL as text. Returns None on failure unless required."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "thai-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status == 204:
                raise ValueError("empty response")
            return r.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as e:
        msg = f"warn: could not fetch {url.split('?')[0]}: {e}"
        print(msg, file=sys.stderr)
        if required:
            raise RuntimeError(msg) from e
        return None


def get_json(url, required=True):
    """Fetch JSON. Returns None on failure unless required."""
    raw = get_text(url, required=required)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as e:
        print(f"warn: bad JSON from {url.split('?')[0]}: {e}", file=sys.stderr)
        if required:
            raise
        return None


def fetch_weather():
    lat = ",".join(str(r["lat"]) for r in REGIONS)
    lon = ",".join(str(r["lon"]) for r in REGIONS)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,precipitation_sum,precipitation_probability_max"
        "&timezone=Asia%2FBangkok&forecast_days=8"
    )
    data = get_json(url)
    return data if isinstance(data, list) else [data]


def fetch_fx():
    data = get_json("https://open.er-api.com/v6/latest/USD", required=False)
    if not data or "rates" not in data:
        return None
    keep = ("THB", "USD", "EUR", "GBP", "RUB", "CNY", "INR", "MYR", "KRW", "AUD")
    return {k: v for k, v in data["rates"].items() if k in keep}


# The iCal feeds only carry English names, so Thai holidays need translating
# before they reach the Russian chart and briefing. Matched on a substring, so
# feed wording like "(substitute day)" still resolves.
HOLIDAY_RU = [
    ("vajiralongkorn", "День рождения короля"),
    ("king's birthday", "День рождения короля"),
    ("queen's birthday", "День рождения королевы"),
    ("queen suthida", "День рождения королевы Сутиды"),
    ("queen mother", "День рождения королевы-матери"),
    ("asalha", "Асалха Буча"),
    ("visakha", "Висакха Буча"),
    ("makha", "Макха Буча"),
    ("khao phansa", "Начало буддийского поста"),
    ("buddhist lent", "Буддийский пост"),
    ("songkran", "Сонгкран"),
    ("chakri", "День династии Чакри"),
    ("coronation", "День коронации"),
    ("labour day", "День труда"),
    ("labor day", "День труда"),
    ("chulalongkorn", "День Чулалонгкорна"),
    ("constitution day", "День конституции"),
    ("new year", "Новый год"),
    ("mother's day", "День матери"),
    ("father's day", "День отца"),
    ("national day", "Национальный день"),
    ("royal ploughing", "Праздник первой борозды"),
]


def holiday_ru(name):
    low = name.lower()
    for key, ru in HOLIDAY_RU:
        if key in low:
            return ru
    return name


def parse_ics(raw):
    """Yield (iso_date, summary) from an iCal feed. Enough for all-day holidays."""  # noqa: E501
    # unfold continuation lines, which iCal wraps at 75 octets
    raw = raw.replace("\r\n ", "").replace("\n ", "").replace("\r\n", "\n")
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        m_date = re.search(r"^DTSTART[^:]*:(\d{8})", block, re.M)
        m_name = re.search(r"^SUMMARY:(.*)$", block, re.M)
        if not (m_date and m_name):
            continue
        d = m_date.group(1)
        yield f"{d[:4]}-{d[4:6]}-{d[6:]}", m_name.group(1).strip()


def fetch_holidays(window):
    """Public holidays in Thailand and the main source markets, for the dates we care about."""
    out = {}
    for code, feed in HOLIDAY_FEEDS.items():
        raw = get_text(
            f"https://calendar.google.com/calendar/ical/"
            f"{feed}%23holiday%40group.v.calendar.google.com/public/basic.ics",
            required=False,
        )
        if not raw:
            continue
        for iso, name in parse_ics(raw):
            if iso in window:
                out.setdefault(iso, []).append({"country": code, "name": name})
    return out


def load_events():
    """Optional festival/cruise calendar. Public holidays come in automatically."""
    if not EVENTS_FILE.exists():
        return []
    try:
        return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"warn: events.json is not valid JSON: {e}", file=sys.stderr)
        return []


def events_for(events, region_id, iso):
    return [
        e for e in events
        if e.get("from", "") <= iso <= e.get("to", e.get("from", ""))
        and e.get("scope") in ("all", region_id)
    ]


def build():
    weather = fetch_weather()
    fx = fetch_fx()
    events = load_events()

    # only the dates in the forecast window matter
    window = set(weather[0]["daily"]["time"])
    holidays = fetch_holidays(window)

    regions = {}
    for i, r in enumerate(REGIONS):
        daily = weather[i]["daily"]
        days = []
        for j, iso in enumerate(daily["time"]):
            mm = daily["precipitation_sum"][j] or 0.0
            pop = daily["precipitation_probability_max"][j] or 0
            tmax = daily["temperature_2m_max"][j]

            # rain penalty: volume dominates, probability nudges
            rain = min(1.0, (mm / 28.0) * 0.75 + (pop / 100.0) * 0.25)
            score = r["base"] * (1 - rain * r["wsens"])

            event = None
            for e in events_for(events, r["id"], iso):
                score += e.get("boost", 0)
                if event is None:
                    event = {"en": e.get("en", ""), "ru": e.get("ru", "")}

            # a Thai public holiday lifts domestic demand
            th_holiday = [h for h in holidays.get(iso, []) if h["country"] == "TH"]
            if th_holiday:
                score += 6
                if event is None:
                    name = th_holiday[0]["name"]
                    event = {"en": name, "ru": holiday_ru(name)}

            # holidays abroad matter less per country but add up
            abroad = [h for h in holidays.get(iso, []) if h["country"] != "TH"]
            score += min(4, len(abroad) * 1.5)

            days.append({
                "iso": iso,
                "tmax": round(tmax) if tmax is not None else None,
                "mm": round(mm, 1),
                "pop": pop,
                "score": max(0, min(100, round(score))),
                "event": event,
            })
        regions[r["id"]] = days

    payload = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "regions": regions,
        "fx": fx,
        "holidays": holidays,
    }

    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote {OUT.relative_to(ROOT)} - {len(regions)} regions, "
          f"{len(next(iter(regions.values())))} days, fx={'yes' if fx else 'no'}")


def main():
    """Keep yesterday's data rather than losing the whole run.

    The weather API is required - without it there is nothing to compute. But
    an outage upstream should not cost a chart, a briefing, and the Telegram
    post as well, so fall back to the committed data.json when one exists.
    """
    try:
        build()
        return 0
    except Exception as e:
        print(f"could not rebuild data: {e}", file=sys.stderr)
        if not OUT.exists():
            print("no previous data.json to fall back to.", file=sys.stderr)
            return 1
        try:
            stale = json.loads(OUT.read_text(encoding="utf-8"))["built_at"]
        except (ValueError, KeyError, OSError):
            stale = "unknown date"
        print(f"keeping the existing data.json (built {stale}).", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
