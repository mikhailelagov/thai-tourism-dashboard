#!/usr/bin/env python3
"""Build data.json for the Thailand tourism dashboard.

Runs daily. The per-day "score" is a weather and calendar model, not a measure
of demand: a hand-set level per region, discounted for forecast rain, plus
points for listed events, nationwide public holidays and two market inputs.
data.json says so in "score_kind" and keeps every term in "parts", so the
number can be audited rather than trusted.

Every measured input carries its source and date:
- Open-Meteo forecast: rain, rain probability, temperature.
- Google's public holiday iCal feeds: Thailand and the source markets. Only
  events the feed types as a nationwide "Public holiday" score; observances
  and regional holidays are kept with weight 0. Buddhist holy days are flagged
  as alcohol-sale ban days and never add points.
- ExchangeRate-API (open.er-api.com): the mid-market "fx" snapshot, with the
  provider's own update time.
- ECB euro reference rates: THB crosses for the source-market currencies,
  matched by date. Bank of Russia: RUB/THB, which the ECB dropped in 2022.
- FRED DCOILBRENTEU (U.S. EIA): Brent spot, about a week behind.

Market weights come from MOTS arrivals by nationality when
tourism/official.json exists, and from hand-typed fallbacks otherwise.

A source that fails keeps its previous section and is listed under "errors".
The run exits 1 only when every source failed.

Standard library only - no pip install needed.
"""

import csv
import http.client
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data.json"
EVENTS_FILE = ROOT / "events.json"
OFFICIAL = ROOT / "tourism" / "official.json"

USER_AGENT = ("thai-tourism-dashboard/1.0 "
              "(+https://github.com/mikhailelagov/thai-tourism-dashboard)")
TIMEOUT = 30
RETRIES = 1
BANGKOK = timezone(timedelta(hours=7))

SCORE_KIND = "weather_calendar_model"
SCORE_NOTE_RU = ("Модель условий из погоды, праздников, цены нефти и курсов валют, "
                 "а не измерение спроса")

# base = hand-set starting level per region and wsens = how much rain moves
# it. Both are model constants, not measurements.
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
# TH drives domestic travel; the rest are the markets that send tourists here.
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
ICS_URL = ("https://calendar.google.com/calendar/ical/"
           "{feed}%23holiday%40group.v.calendar.google.com/public/basic.ics")

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
ER_API = "https://open.er-api.com/v6/latest/USD"
ECB_URL = ("https://data-api.ecb.europa.eu/service/data/EXR/D.{codes}.EUR.SP00.A"
           "?startPeriod={start}&format=csvdata")
CBR_URL = ("https://www.cbr.ru/scripts/XML_dynamic.asp"
           "?date_req1={start}&date_req2={end}&VAL_NM_RQ=R01675")  # R01675 = THB
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU&cosd={start}"

SOURCES = {
    "weather": ("Open-Meteo forecast API (CC BY 4.0)", "https://open-meteo.com/"),
    "holidays": ("Google Calendar public holiday feeds", "https://calendar.google.com/"),
    "fx": ("ExchangeRate-API (open.er-api.com)", ER_API),
    "ecb": ("ECB euro reference rates",
            "https://data.ecb.europa.eu/data/datasets/EXR"),
    "cbr": ("Bank of Russia", "https://www.cbr.ru/currency_base/dynamics/"),
    "brent": ("FRED DCOILBRENTEU (U.S. EIA, Europe Brent spot FOB)",
              "https://fred.stlouisfed.org/series/DCOILBRENTEU"),
}
FX_KEEP = ("THB", "USD", "EUR", "GBP", "RUB", "CNY", "INR", "MYR", "KRW", "AUD")
FX_NOTE_RU = "Справочный курс (mid-market), не курс обменника"


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def get_bytes(url):
    """GET with an honest User-Agent and one retry. Raises RuntimeError.

    A refusal (4xx other than 429) is not retried: a source that turns our
    User-Agent away, or wants AI crawlers to pay, is recorded and skipped.
    """
    error = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(3)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read()
            if not body:
                raise ValueError("empty response")
            return body
        except urllib.error.HTTPError as e:
            error = "HTTP %d" % e.code
            if 400 <= e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, http.client.HTTPException, ValueError, OSError) as e:
            error = str(getattr(e, "reason", "") or e) or e.__class__.__name__
    raise RuntimeError("%s: %s" % (url.split("?")[0], error))


def round4(value):
    return round(value, 4) + 0.0  # + 0.0 turns -0.0 into 0.0


def error_entry(source, exc, at):
    return {"source": source, "at": at, "error": str(exc)[:200]}


# --- weather ---------------------------------------------------------------

def weather_url():
    lat = ",".join(str(r["lat"]) for r in REGIONS)
    lon = ",".join(str(r["lon"]) for r in REGIONS)
    return (f"{WEATHER_URL}?latitude={lat}&longitude={lon}"
            "&daily=temperature_2m_max,precipitation_sum,precipitation_probability_max"
            "&timezone=Asia%2FBangkok&forecast_days=8")


def parse_weather(raw):
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(data.get("reason") or "weather API error")
    blocks = data if isinstance(data, list) else [data]
    if len(blocks) != len(REGIONS):
        raise ValueError("expected %d locations, got %d" % (len(REGIONS), len(blocks)))
    for block in blocks:
        if not (block.get("daily") or {}).get("time"):
            raise ValueError("no daily forecast")
    return blocks


def value_at(daily, key, j):
    """A forecast value, or None when the API left it out. Never 0 by default."""
    seq = daily.get(key) or []
    value = seq[j] if j < len(seq) else None
    return value if number(value) else None


# --- holidays ----------------------------------------------------------------

# The iCal feeds only carry English names, so Thai holidays need translating
# before they reach the Russian chart and briefing. Matched on a substring in
# this order, so "chinese new year" wins over "new year" and the Bhumibol
# memorial over his birthday.
HOLIDAY_RU = [
    ("death of king bhumibol", "День памяти короля Пхумипона"),
    ("bhumibol", "День рождения короля Пхумипона (День отца)"),
    ("vajiralongkorn", "День рождения короля"),
    ("king's birthday", "День рождения короля"),
    ("queen suthida", "День рождения королевы Сутиды"),
    ("queen mother", "День рождения королевы-матери"),
    ("queen's birthday", "День рождения королевы"),
    ("asalha", "Асалха Буча"),
    ("visakha", "Висакха Буча"),
    ("makha", "Макха Буча"),
    ("khao phansa", "Начало буддийского поста (Кхао Пханса)"),
    ("ok phansa", "Окончание буддийского поста (Ок Пханса)"),
    ("end of buddhist lent", "Окончание буддийского поста (Ок Пханса)"),
    ("buddhist lent", "Начало буддийского поста (Кхао Пханса)"),
    ("songkran", "Сонгкран"),
    ("chakri", "День династии Чакри"),
    ("coronation", "День коронации"),
    ("labour day", "День труда"),
    ("labor day", "День труда"),
    ("chulalongkorn", "День Чулалонгкорна"),
    ("constitution day", "День конституции"),
    ("mahidol", "День Махидола"),
    ("royal ploughing", "Праздник первой борозды"),
    ("bridge public holiday", "Дополнительный выходной"),
    ("chinese new year", "Китайский Новый год"),
    ("new year's eve", "Канун Нового года"),
    ("new year special", "Новый год (дополнительный выходной)"),
    ("new year", "Новый год"),
    ("valentine", "День святого Валентина"),
    ("christmas eve", "Сочельник"),
    ("christmas", "Рождество"),
    ("mother's day", "День матери"),
    ("father's day", "День отца"),
    ("national day", "Национальный день"),
]
SUBSTITUTE_MARKERS = ("day off for", "observed", "substitut", "bridge")

# Alcohol sales stop nationwide on these Buddhist holy days (PM's Office
# notification; the 2025 version adds exemptions). The substitute day off
# that follows a weekend holy day is not a ban day.
ALCOHOL_BAN_KEYS = ("makha", "magha", "visakha", "vesak", "asalha",
                    "khao phansa", "ok phansa", "buddhist lent")
ALCOHOL_NOTE_RU = (
    "Запрет продажи алкоголя (буддийский праздник, 00:00–24:00). Исключения "
    "по уведомлению канцелярии премьер-министра 2025 г.: международные "
    "терминалы аэропортов, заведения с лицензией по закону о местах "
    "развлечений и аналогичные в туристических зонах, отели, особые мероприятия.")
ALCOHOL_SOURCE_URL = "https://www.nationthailand.com/news/general/40069132"


def is_substitute(name):
    low = name.lower()
    return any(m in low for m in SUBSTITUTE_MARKERS)


def holiday_ru(name):
    low = name.lower()
    for key, ru in HOLIDAY_RU:
        if key in low:
            if is_substitute(low) and "выходной" not in ru:
                return ru + " (перенесённый выходной)"
            return ru
    print(f"warn: no Russian name for holiday {name!r}", file=sys.stderr)
    return name


def alcohol_ban_day(name):
    low = name.lower()
    return any(k in low for k in ALCOHOL_BAN_KEYS) and not is_substitute(low)


def ics_text(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
            .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def holiday_type(description):
    """Google opens DESCRIPTION with the event type; keep it.

    'Public holiday' -> public_holiday, 'Public holiday in X' ->
    regional_holiday, 'Observance' / 'Observance in X' -> observance.
    Returns (type, where).
    """
    first = ics_text(description or "").split("\n")[0].strip()
    low = first.lower()
    for prefix, kind in (("public holiday", "public_holiday"), ("observance", "observance")):
        if low == prefix:
            return kind, None
        if low.startswith(prefix + " in "):
            regional = "regional_holiday" if kind == "public_holiday" else kind
            return regional, first[len(prefix) + 4:]
    return (re.sub(r"\W+", "_", low).strip("_") or "unknown"), None


def parse_ics(raw):
    """Yield {iso, name, type, where} for each all-day event in an iCal feed."""
    # unfold continuation lines, which iCal wraps at 75 octets
    raw = raw.replace("\r\n ", "").replace("\n ", "").replace("\r\n", "\n")
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.S):
        m_date = re.search(r"^DTSTART[^:]*:(\d{8})", block, re.M)
        m_name = re.search(r"^SUMMARY:(.*)$", block, re.M)
        if not (m_date and m_name):
            continue
        m_desc = re.search(r"^DESCRIPTION:(.*)$", block, re.M)
        kind, where = holiday_type(m_desc.group(1) if m_desc else "")
        d = m_date.group(1)
        yield {"iso": f"{d[:4]}-{d[4:6]}-{d[6:]}", "name": ics_text(m_name.group(1)).strip(),
               "type": kind, "where": where}


def derived_lent_days(events):
    """Khao Phansa and Ok Phansa, which Google's Thai feed leaves out.

    Both sit at fixed lunar offsets from Asalha Bucha (full moon of month 8):
    Khao Phansa is the next day, Ok Phansa the full moon of month 11, 89 days
    later. That matches the official dates for 2023-2026 (Ok Phansa 2026 is
    26 October). Both are alcohol-ban days.
    """
    listed = {(e["iso"], "phansa" in e["name"].lower() or "lent" in e["name"].lower())
              for e in events}
    out = []
    for e in events:
        if "asalha" not in e["name"].lower() or is_substitute(e["name"]):
            continue
        day = date.fromisoformat(e["iso"])
        for offset, name in ((1, "Khao Phansa (Buddhist Lent Day)"),
                             (89, "Ok Phansa (End of Buddhist Lent)")):
            iso = (day + timedelta(days=offset)).isoformat()
            if (iso, True) in listed:
                continue
            out.append({"iso": iso, "name": name, "type": "observance", "where": None,
                        "derived_from": "Asalha Bucha " + e["iso"]})
    return out


def holiday_entry(country, event):
    entry = {"country": country, "name": event["name"], "type": event["type"]}
    for key in ("where", "derived_from"):
        if event.get(key):
            entry[key] = event[key]
    if country == "TH":
        entry["ru"] = holiday_ru(event["name"])
        if alcohol_ban_day(event["name"]):
            entry["alcohol_ban"] = True
            entry["alcohol_note_ru"] = ALCOHOL_NOTE_RU
            entry["alcohol_source_url"] = ALCOHOL_SOURCE_URL
    return entry


def fetch_holidays(window, fetch, previous, errors, at):
    """Holidays in the window, per feed. A failed feed keeps its previous entries."""
    out, ok = {}, []
    for code, feed in HOLIDAY_FEEDS.items():
        try:
            events = list(parse_ics(fetch(ICS_URL.format(feed=feed)).decode("utf-8")))
            if not events:
                raise ValueError("no events in feed")
        except Exception as e:
            errors.append(error_entry("holidays:" + code, e, at))
            for iso, items in (previous or {}).items():
                if iso in window:
                    out.setdefault(iso, []).extend(
                        h for h in items if h.get("country") == code and h.get("type"))
            continue
        ok.append(code)
        if code == "TH":
            events += derived_lent_days(events)
        for event in events:
            if event["iso"] in window:
                out.setdefault(event["iso"], []).append(holiday_entry(code, event))
    return out, ok


# --- exchange rates and fuel ----------------------------------------------

# Fallback relative weights per currency, typed by hand from Jan-Jul 2026
# arrival counts. Units, period and source were never recorded, so they are
# not measured shares; they are used only when tourism/official.json is
# missing. With it, each weight is that market's share of MOTS arrivals.
SOURCE_MARKETS = {
    "CNY": ("Китай", 2.23), "MYR": ("Малайзия", 1.55), "RUB": ("Россия", 1.02),
    "INR": ("Индия", 1.00), "KRW": ("Южная Корея", 0.60), "GBP": ("Британия", 0.50),
    "EUR": ("Еврозона", 0.50), "AUD": ("Австралия", 0.50),
}
EUROZONE = ("austria", "belgium", "bulgaria", "croatia", "cyprus", "estonia",
            "finland", "france", "germany", "greece", "ireland", "italy", "latvia",
            "lithuania", "luxembourg", "malta", "netherlands", "portugal",
            "slovakia", "slovenia", "spain")
MARKET_NAMES = {
    "CNY": ("china",),
    "MYR": ("malaysia",),
    "RUB": ("russian federation", "russia"),
    "INR": ("india",),
    "KRW": ("korea (republic of)", "korea, republic of", "republic of korea",
            "south korea"),
    "GBP": ("united kingdom", "uk"),
    "AUD": ("australia",),
}
ECB_CODES = ("THB", "USD", "CNY", "INR", "KRW", "MYR", "GBP", "AUD")


def fallback_weights():
    return ({code: share for code, (_, share) in SOURCE_MARKETS.items()},
            {"kind": "fixed_constants",
             "note_ru": "Относительные веса набраны вручную по прибытиям за "
                        "янв–июл 2026; источник и единицы не зафиксированы. "
                        "Это не измеренные доли."})


def market_weights(path=OFFICIAL):
    """Weight per currency: that market's % of MOTS arrivals in the latest month.

    Eurozone sums the euro countries listed in official.json, which keeps the
    top 20 markets plus a fixed set, so smaller euro markets are left out.
    """
    try:
        arrivals = json.loads(path.read_text(encoding="utf-8"))["arrivals"]
        total = arrivals["total"]["value"]
        values = {str(m["name"]).strip().lower(): m["value"]
                  for m in arrivals["markets"] if number(m.get("value"))}
        if not number(total) or total <= 0 or not values:
            raise ValueError("no arrivals")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return fallback_weights()

    weights = {}
    for code in SOURCE_MARKETS:
        if code == "EUR":
            hits = [v for name, v in values.items() if name in EUROZONE]
        else:
            hits = [values[a] for a in MARKET_NAMES[code] if a in values][:1]
        weights[code] = round(sum(hits) / total * 100, 2) if hits else None
    if not any(weights.values()):
        return fallback_weights()
    meta = {"kind": "arrivals_share_pct", "unit": "% of international arrivals"}
    for key in ("source", "source_url", "file_url", "period", "status", "published_at"):
        meta[key] = arrivals.get(key)
    return weights, meta


def parse_er_api(raw):
    data = json.loads(raw)
    if data.get("result") != "success" or not isinstance(data.get("rates"), dict):
        raise ValueError("unexpected response")
    rates = {k: v for k, v in data["rates"].items() if k in FX_KEEP and number(v)}
    if not number(rates.get("THB")):
        raise ValueError("no THB rate")
    return rates, provider_time(data)


def provider_time(data):
    """The provider's own update time as ISO with offset, or None."""
    try:
        when = parsedate_to_datetime(data.get("time_last_update_utc") or "")
    except (TypeError, ValueError, IndexError):
        when = None
    if when is None and number(data.get("time_last_update_unix")):
        when = datetime.fromtimestamp(data["time_last_update_unix"], timezone.utc)
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)  # RFC 2822 "-0000" is UTC
    return when.isoformat() if when else None


def parse_ecb_csv(text):
    """{currency: {iso_date: units per EUR}} from ECB SDMX csvdata."""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            value = float(row["OBS_VALUE"])
            date.fromisoformat(row["TIME_PERIOD"])
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(row["CURRENCY"], {})[row["TIME_PERIOD"]] = value
    if "THB" not in out:
        raise ValueError("no THB series")
    return out


def thb_crosses(per_eur):
    """THB per unit of each currency, only on dates both ECB rates exist."""
    thb = per_eur["THB"]
    out = {"EUR": dict(thb)}
    for code, series in per_eur.items():
        if code != "THB":
            out[code] = {d: thb[d] / v for d, v in series.items() if d in thb and v}
    return out


def parse_xml_safely(blob):
    """ElementTree root of downloaded XML, refusing any DTD first.

    expat honours the declared encoding, so a UTF-16 document cannot hide a
    <!DOCTYPE> from this check, and entity expansion never starts.
    """
    def refuse(*_):
        raise ValueError("XML with a DTD refused")
    scanner = xml.parsers.expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = refuse
    scanner.EntityDeclHandler = refuse
    try:
        scanner.Parse(blob, True)
    except xml.parsers.expat.ExpatError as exc:
        raise ValueError("malformed XML: %s" % exc)
    return ET.fromstring(blob)


def parse_cbr_dynamic(raw):
    """{iso_date: THB per RUB} from the Bank of Russia XML_dynamic feed.

    The bank quotes RUB per Nominal THB with a decimal comma, in cp1251; the
    XML declaration carries the encoding, so ElementTree reads the bytes.
    """
    out = {}
    for rec in parse_xml_safely(raw).iter("Record"):
        try:
            d = datetime.strptime(rec.get("Date", ""), "%d.%m.%Y").date().isoformat()
            rub = float(rec.findtext("Value").replace(",", ".")) / int(rec.findtext("Nominal"))
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            continue
        if rub > 0:
            out[d] = 1 / rub
    if not out:
        raise ValueError("no THB records")
    return out


def parse_fred_csv(text, series_id):
    """{iso_date: value} from a FRED graph CSV; holidays ('' or '.') are skipped."""
    rows = csv.reader(io.StringIO(text))
    header = next(rows, [])
    if len(header) < 2 or header[1].strip() != series_id:
        raise ValueError("unexpected header %r" % header[:2])
    out = {}
    for row in rows:
        if len(row) < 2 or row[1].strip() in ("", "."):
            continue
        try:
            date.fromisoformat(row[0])
            out[row[0]] = float(row[1])
        except ValueError:
            continue
    if not out:
        raise ValueError("no observations")
    return out


def baseline_change(series, days, min_obs):
    """Latest value against the mean of the observations in the `days` before it.

    `series` is {iso_date: value}. Dates, not positions, set the window, so a
    gap in a feed shortens the baseline instead of shifting it. Returns None
    when there are fewer than `min_obs` baseline points, so a thin feed never
    silently reads as zero.
    """
    if not series:
        return None
    last = max(series)
    start = (date.fromisoformat(last) - timedelta(days=days)).isoformat()
    base = [v for d, v in series.items() if start <= d < last]
    if len(base) < min_obs:
        return None
    mean = sum(base) / len(base)
    return (series[last] - mean) / mean if mean else None


def brent_section(text):
    series = parse_fred_csv(text, "DCOILBRENTEU")
    last = max(series)
    out = {"brent": round(series[last], 2), "brent_date": last,
           "brent_source": SOURCES["brent"][0], "brent_source_url": SOURCES["brent"][1]}
    d = baseline_change(series, 7, 3)
    if d is not None:
        out["brent_delta"] = round4(d)
    return out


def usd_section(usd):
    last = max(usd)
    out = {"usdthb": round(usd[last], 3), "usdthb_date": last,
           "usdthb_source": SOURCES["ecb"][0] + " (cross via EUR)"}
    d = baseline_change(usd, 7, 3)
    if d is not None:
        out["usdthb_delta"] = round4(d)
    return out


def purchasing_power(series, origin, weights):
    """How much a visitor's own money buys in baht, versus its 30-day average.

    A negative delta means that market's visitors got poorer here.
    """
    out = {}
    for code, (name, _) in SOURCE_MARKETS.items():
        points = series.get(code)
        delta = baseline_change(points, 30, 10) if points else None
        if delta is None:
            continue
        last = max(points)
        out[code] = {"name": name, "share": weights.get(code),
                     "thb": round(points[last], 6), "delta": round4(delta),
                     "date": last, "source": origin[code]}
    return out


def composite(pp):
    weighted = [(v["delta"], v["share"]) for v in pp.values()
                if number(v.get("share")) and v["share"] > 0 and number(v.get("delta"))]
    total = sum(s for _, s in weighted)
    return round4(sum(d * s for d, s in weighted) / total) if total else None


def carry(previous, prefix, keys):
    """Previous values of one source, only when they carry a date of their own.

    Undated values from the retired Yahoo feed are dropped, not carried on.
    """
    if not previous.get(prefix + "_date"):
        return {}
    return {k: previous[k] for k in (prefix, prefix + "_date") + keys if k in previous}


def fetch_markets(fetch, today, previous, weights, meta, errors, ok, at):
    """Brent and THB crosses. A failed source keeps its previous dated values."""
    previous = previous or {}
    markets, series, origin = {}, {}, {}

    try:
        markets.update(brent_section(fetch(FRED_URL.format(
            start=(today - timedelta(days=120)).isoformat())).decode("utf-8")))
        ok.append("brent")
    except Exception as e:
        errors.append(error_entry("brent", e, at))
        markets.update(carry(previous, "brent", ("brent_delta", "brent_source",
                                                 "brent_source_url")))

    try:
        crosses = thb_crosses(parse_ecb_csv(fetch(ECB_URL.format(
            codes="+".join(ECB_CODES),
            start=(today - timedelta(days=70)).isoformat())).decode("utf-8")))
        if not crosses.get("USD"):
            raise ValueError("no USD rate")
        markets.update(usd_section(crosses["USD"]))
        for code in SOURCE_MARKETS:
            if code in crosses:
                series[code], origin[code] = crosses[code], "ECB"
        ok.append("ecb")
    except Exception as e:
        errors.append(error_entry("ecb", e, at))
        markets.update(carry(previous, "usdthb", ("usdthb_delta", "usdthb_source")))

    try:
        series["RUB"] = parse_cbr_dynamic(fetch(CBR_URL.format(
            start=(today - timedelta(days=70)).strftime("%d/%m/%Y"),
            end=(today + timedelta(days=1)).strftime("%d/%m/%Y"))))
        origin["RUB"] = "Bank of Russia"
        ok.append("cbr")
    except Exception as e:
        errors.append(error_entry("cbr", e, at))

    pp = purchasing_power(series, origin, weights)
    # a source that failed keeps its previous dated entries, re-weighted
    for code, old in (previous.get("purchasing_power") or {}).items():
        if code not in pp and code not in series and code in SOURCE_MARKETS and old.get("date"):
            pp[code] = dict(old, share=weights.get(code))
    if pp:
        markets["purchasing_power"] = pp
        value = composite(pp)
        if value is not None:
            markets["pp_composite"] = value
    markets["weights"] = meta
    return markets


# --- events and scoring ------------------------------------------------------

def load_events(path=EVENTS_FILE):
    """Optional festival/cruise calendar. Public holidays come in automatically."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"warn: events.json is not valid JSON: {e}", file=sys.stderr)
        return []


def events_for(events, region_id, iso):
    return [
        e for e in events
        if e.get("from", "") <= iso <= e.get("to", e.get("from", ""))
        and e.get("scope") in ("all", region_id)
    ]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def score_day(region, iso, mm, pop, tmax, holidays, events, fuel_pts, fx_pts):
    """One region-day of the model. Missing rain data leaves the score null."""
    missing = [k for k, v in (("mm", mm), ("pop", pop)) if v is None]
    weather_pts = None
    if not missing:
        # rain penalty: volume dominates, probability nudges
        rain = min(1.0, (mm / 28.0) * 0.75 + (pop / 100.0) * 0.25)
        weather_pts = region["base"] * (1 - rain * region["wsens"])

    event = None
    ev_pts = 0.0
    for e in events_for(events, region["id"], iso):
        ev_pts += e.get("boost", 0)
        if event is None:
            event = {"en": e.get("en", ""), "ru": e.get("ru", "")}

    th = [h for h in holidays if h.get("country") == "TH"]
    ban = [h for h in th if h.get("alcohol_ban")]
    th_public = [h for h in th if h.get("type") == "public_holiday"]
    # A nationwide Thai public holiday lifts domestic travel. A Buddhist
    # alcohol-ban day can cost bars more than it brings, so it adds nothing.
    th_pts = 6.0 if th_public and not ban else 0.0
    named = ban or th_public
    if named and event is None:
        event = {"en": named[0]["name"], "ru": named[0].get("ru") or named[0]["name"]}
        if ban:
            event["alcohol_ban"] = True

    # nationwide holidays abroad matter less per country but add up
    abroad = {h["country"] for h in holidays
              if h.get("country") != "TH" and h.get("type") == "public_holiday"}
    ab_pts = min(4.0, len(abroad) * 1.5)

    # Every term is kept so the number can be audited rather than trusted.
    parts = {
        "weather": round(weather_pts, 1) if weather_pts is not None else None,
        "event": round(ev_pts, 1),
        "holiday_th": round(th_pts, 1),
        "holiday_abroad": round(ab_pts, 1),
        "fuel": round(fuel_pts, 1),
        "fx": round(fx_pts, 1),
    }
    score = None
    if weather_pts is not None:
        total = weather_pts + ev_pts + th_pts + ab_pts + fuel_pts + fx_pts
        score = max(0, min(100, round(total)))

    day = {
        "iso": iso,
        "tmax": round(tmax) if tmax is not None else None,
        "mm": round(mm, 1) if mm is not None else None,
        "pop": pop,
        "score": score,
        "parts": parts,
        "event": event,
        "missing": bool(missing),
        "alcohol_ban": bool(ban),
    }
    if missing:
        day["missing_fields"] = missing
    if ban:
        day["alcohol_note_ru"] = ALCOHOL_NOTE_RU
    return day


def build_regions(weather, holidays, events, fuel_pts, fx_pts):
    regions = {}
    for region, block in zip(REGIONS, weather):
        daily = block["daily"]
        regions[region["id"]] = [
            score_day(region, iso,
                      value_at(daily, "precipitation_sum", j),
                      value_at(daily, "precipitation_probability_max", j),
                      value_at(daily, "temperature_2m_max", j),
                      holidays.get(iso, []), events, fuel_pts, fx_pts)
            for j, iso in enumerate(daily["time"])
        ]
    return regions


# --- assembly ------------------------------------------------------------------

def load_previous(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, payload):
    """Write via a temp file, so a crash never leaves half a data.json."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".data-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the site reads this file
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def build(now=None, fetch=get_bytes, out=OUT, official=OFFICIAL, events_file=EVENTS_FILE):
    """Collect every source, keep the previous section for any that failed.

    Returns the number of sources that succeeded.
    """
    now = now or now_utc()
    at = now.isoformat(timespec="seconds")
    today = now.astimezone(BANGKOK).date()
    previous = load_previous(out)
    errors, ok = [], []
    prev_sources = previous.get("sources") or {}

    weather = None
    try:
        weather = parse_weather(fetch(weather_url()))
        ok.append("weather")
    except Exception as e:
        errors.append(error_entry("weather", e, at))

    # an fx snapshot without the provider's time is not carried on
    fx_fields = {k: previous[k] for k in ("fx", "fx_time", "fx_source", "fx_source_url",
                                         "fx_note_ru") if k in previous and previous.get("fx_time")}
    try:
        rates, when = parse_er_api(fetch(ER_API))
        fx_fields = {"fx": rates, "fx_time": when, "fx_source": SOURCES["fx"][0],
                     "fx_source_url": ER_API, "fx_note_ru": FX_NOTE_RU}
        ok.append("fx")
    except Exception as e:
        errors.append(error_entry("fx", e, at))

    weights, meta = market_weights(official)
    markets = fetch_markets(fetch, today, previous.get("markets"), weights, meta,
                            errors, ok, at)

    # Measured market inputs, my point weights: crude above its own recent
    # baseline means carriers pull capacity, and a weaker visitor currency
    # makes the country pricier for that market.
    fuel_pts = clamp(-(markets.get("brent_delta") or 0.0) * 40, -4.0, 4.0)
    fx_pts = clamp((markets.get("pp_composite") or 0.0) * 120, -6.0, 6.0)
    print(f"market: brent {markets.get('brent')} ({markets.get('brent_date')}) -> "
          f"{fuel_pts:+.1f} pts, composite {markets.get('pp_composite')} -> "
          f"{fx_pts:+.1f} pts", file=sys.stderr)

    if weather:
        window = set(weather[0]["daily"]["time"])
    else:
        window = {(today + timedelta(days=i)).isoformat() for i in range(8)}
    holidays, feeds_ok = fetch_holidays(window, fetch, previous.get("holidays"), errors, at)
    ok += ["holidays:" + c for c in feeds_ok]

    if weather:
        regions = build_regions(weather, holidays, load_events(events_file),
                                fuel_pts, fx_pts)
        built_at = at
    else:
        regions = previous.get("regions") or {}
        built_at = previous.get("built_at")

    sources = {}
    for key, (name, url) in SOURCES.items():
        if key == "holidays":
            fresh = bool(feeds_ok)
        else:
            fresh = key in ok
        last = at if fresh else (prev_sources.get(key) or {}).get("retrieved_at")
        sources[key] = {"name": name, "url": url, "ok": fresh, "retrieved_at": last}

    payload = {
        "built_at": built_at,
        "score_kind": SCORE_KIND,
        "score_note_ru": SCORE_NOTE_RU,
        "stale": not weather,
        "updated_at": at,
        "regions": regions,
        **fx_fields,
        "markets": markets,
        "holidays": holidays,
        "sources": sources,
        "errors": errors,
    }
    write_json(out, payload)
    days = len(next(iter(regions.values()))) if regions else 0
    print(f"wrote {out.name} - {len(regions)} regions, {days} days, "
          f"{len(ok)} sources ok, {len(errors)} errors")
    for e in errors:
        print(f"warn: {e['source']}: {e['error']}", file=sys.stderr)
    return len(ok)


def main():
    """Exit 0 when at least one source refreshed, 1 when every source failed."""
    try:
        return 0 if build() else 1
    except Exception as e:
        print(f"could not rebuild data: {e}; keeping the existing data.json",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
