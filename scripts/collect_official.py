#!/usr/bin/env python3
"""Collect official, dated tourism statistics and exchange rates.

The post and the morning brief relied on hand-typed BOT numbers, unsourced
market shares and an unofficial FX aggregator. This collector reads the
publishers' own files instead and writes tourism/official.json:

- MOTS international arrivals by nationality (monthly XLSX);
- MOTS provincial tourism situation: occupancy, visitors, revenue (monthly XLSX);
- BOT report 875 tourism indicators (XLSX), appended to tourism/indicators.jsonl;
- ECB euro reference rates (THB cross rates) and Bank of Russia RUB per THB.

Every section names its source, publication and retrieval time and the period
it describes. Values are copied from the file, never estimated: a missing cell
stays null. A failed source keeps its previous section and is listed under
"errors"; the run exits 0 when at least one selected source succeeded.
Requests send an honest User-Agent, retry once and never try to pass a bot
check, paywall or redirect to another site.
"""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tourism" / "official.json"
INDICATORS = ROOT / "tourism" / "indicators.jsonl"
USER_AGENT = ("thai-tourism-dashboard/1.0 "
              "(+https://github.com/mikhailelagov/thai-tourism-dashboard)")
TIMEOUT = 30
RETRY_DELAY = 5
MAX_BYTES = 20 * 1024 * 1024
MAX_PART = 50 * 1024 * 1024
BANGKOK = timezone(timedelta(hours=7))  # Thailand has no daylight saving time
SOURCES = ("arrivals", "provinces", "bot", "ecb", "cbr")
FX = ("ecb", "cbr")

MOTS = "https://www.mots.go.th/news/category/%d"
MOTS_INDEX = 411  # tourist statistics: one folder per Buddhist year
MOTS_DEFAULT = {"arrivals": 817, "provinces": 820}  # folders of 2569 (2026)
MOTS_TITLE = {
    "arrivals": "สถิตินักท่องเที่ยวชาวต่างชาติที่เดินทางเข้าประเทศไทย",
    "provinces": "สถานการณ์การท่องเที่ยวในประเทศ รายจังหวัด",
}
XLSX_LINK = re.compile(r"https://www\.mots\.go\.th/images/v2022_[0-9a-f-]+\.xlsx")
CATEGORY = re.compile(r'\\?"id\\?":(\d+),\\?"name\\?":\\?"([^"\\]*)')

BOT_PAGE = ("https://app.bot.or.th/BTWS_STAT/statistics/ReportPage.aspx"
            "?language=eng&reportID=875")
BOT_FILE = ("https://app.bot.or.th/BTWS_STAT/statistics/DownloadFile.aspx"
            "?file=EC_EI_028_S2_ENG_ALL.XLSX")
BOT_MONTHS = 13  # the latest month and the same month a year earlier
BOT_NOTES = ("BOT reports MOTS as source; methodological alignment in progress. "
             "Publication timestamp is page last-updated. Large region is not a city.")
# (label words, metric, unit words the label must contain, unit stored)
BOT_METRICS = (
    ("foreign tourists visiting", "foreign_arrivals", "thousand", "thousand persons"),
    ("occupancy rate", "hotel_occupancy", "percent", "percent"),
    ("booking rate", "advanced_booking_3_months", "percent", "percent"),
    ("room rental rate", "average_room_rate", "baht", "THB per room"),
    ("share of foreign tourists", "foreign_guest_share", "percent", "percent"),
)
BOT_REGIONS = {"total": "Thailand", "central (including bangkok)": "Central including Bangkok",
               "south": "South", "north": "North"}

ECB_PAGE = ("https://www.ecb.europa.eu/stats/policy_and_exchange_rates/"
            "euro_reference_exchange_rates/html/index.en.html")
ECB_DATA = ("https://data-api.ecb.europa.eu/service/data/EXR/"
            "D.THB+USD+CNY+INR+KRW+MYR+GBP.EUR.SP00.A?lastNObservations=5&format=csvdata")
ECB_CODES = ("USD", "EUR", "CNY", "INR", "KRW", "MYR", "GBP")
CBR_PAGE = "https://www.cbr.ru/currency_base/daily/"
CBR_DATA = "https://www.cbr.ru/scripts/XML_daily.asp"

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec")
THAI_MONTHS = {
    "มค": 1, "กพ": 2, "มีค": 3, "เมย": 4, "พค": 5, "มิย": 6,
    "กค": 7, "สค": 8, "กย": 9, "ตค": 10, "พย": 11, "ธค": 12,
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5,
    "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10,
    "พฤศจิกายน": 11, "ธันวาคม": 12,
}
YEAR = re.compile(r"^(\d{4})\s*([PpRr]?)$")

TOP_MARKETS = 20
WATCHED = {"Russian Federation", "China", "India", "Kazakhstan", "Israel",
           "Korea (Republic of)", "Malaysia", "United Kingdom", "Germany",
           "The United States of America", "United States of America", "USA",
           "Japan", "Ukraine"}
# Region subtotals in the nationality table; "Other in ..." rows are residuals.
AGGREGATES = {"grand total", "asia and the pacific", "south-east asia", "north-east asia",
              "south asia", "oceania", "europe", "northern europe", "western europe",
              "central/eastern europe", "southern/medit. europe", "america",
              "middle east", "africa"}
RU_NAMES = {
    "Brunei": "Бруней", "Cambodia": "Камбоджа", "Indonesia": "Индонезия",
    "Laos": "Лаос", "Malaysia": "Малайзия", "Myanmar": "Мьянма",
    "Philippines": "Филиппины", "Singapore": "Сингапур", "Vietnam": "Вьетнам",
    "China": "Китай", "Hong Kong (China)": "Гонконг (Китай)", "Japan": "Япония",
    "North Korea": "КНДР", "Korea (Republic of)": "Южная Корея",
    "Macao (China)": "Макао (Китай)", "Mongolia": "Монголия", "Taiwan": "Тайвань",
    "Bangladesh": "Бангладеш", "Bhutan": "Бутан", "India": "Индия", "Iran": "Иран",
    "Maldives": "Мальдивы", "Nepal": "Непал", "Pakistan": "Пакистан",
    "Sri Lanka": "Шри-Ланка", "Australia": "Австралия", "New Zealand": "Новая Зеландия",
    "Denmark": "Дания", "Finland": "Финляндия", "Iceland": "Исландия",
    "Ireland": "Ирландия", "Norway": "Норвегия", "Sweden": "Швеция",
    "United Kingdom": "Великобритания", "Austria": "Австрия", "Belgium": "Бельгия",
    "France": "Франция", "Germany": "Германия", "Luxembourg": "Люксембург",
    "Netherlands": "Нидерланды", "Switzerland": "Швейцария", "Belarus": "Беларусь",
    "Bulgaria": "Болгария", "Czech Republic": "Чехия", "Estonia": "Эстония",
    "Hungary": "Венгрия", "Kazakhstan": "Казахстан", "Kyrgyzstan": "Киргизия",
    "Latvia": "Латвия", "Lithuania": "Литва", "Poland": "Польша",
    "Romania": "Румыния", "Russian Federation": "Россия", "Slovakia": "Словакия",
    "Ukraine": "Украина", "Uzbekistan": "Узбекистан", "Croatia": "Хорватия",
    "Cyprus": "Кипр", "Greece": "Греция", "Israel": "Израиль", "Italy": "Италия",
    "Portugal": "Португалия", "Serbia": "Сербия", "Slovenia": "Словения",
    "Spain": "Испания", "Turkey": "Турция", "Canada": "Канада", "Mexico": "Мексика",
    "The United States of America": "США", "United States of America": "США",
    "USA": "США", "Argentina": "Аргентина", "Brazil": "Бразилия", "Chile": "Чили",
    "Colombia": "Колумбия", "Peru": "Перу", "Uruguay": "Уругвай",
    "Bahrain": "Бахрейн", "Egypt": "Египет", "Iraq": "Ирак", "Jordan": "Иордания",
    "Kuwait": "Кувейт", "Lebanon": "Ливан", "Oman": "Оман", "Qatar": "Катар",
    "Saudi Arabia": "Саудовская Аравия", "U.A.E.": "ОАЭ", "Yemen": "Йемен",
    "Morocco": "Марокко", "South Africa": "ЮАР", "Ethiopia": "Эфиопия",
    "Kenya": "Кения", "Mauritius": "Маврикий",
}

# (region id, province names as MOTS writes them, Russian label)
PROVINCES = (
    ("bangkok", ("กรุงเทพมหานคร", "กรุงเทพฯ"), "Бангкок"),
    ("pattaya", ("ชลบุรี",), "Чонбури (включая Паттайю)"),
    ("phuket", ("ภูเก็ต",), "Пхукет"),
    ("krabi", ("กระบี่",), "Краби"),
    ("samui", ("สุราษฎร์ธานี",), "Сураттани (включая Самуи)"),
    ("chiangmai", ("เชียงใหม่",), "Чиангмай"),
    ("thailand", ("รวมทั้งหมด", "รวมทั้งประเทศ", "ทั่วประเทศ"), "Весь Таиланд"),
)
PROVINCE_GROUPS = ("occupancy", "thai", "foreign", "revenue", "revenue_foreign")
PROVINCE_NOTE = ("Провинция шире курорта; посещения провинций нельзя складывать "
                 "и сравнивать с прибытиями в страну.")

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class Refused(Exception):
    """The site answered with something other than the data: never worked around."""


# ---------------------------------------------------------------- helpers

def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def http_time(value):
    """Turn an HTTP Last-Modified header into ISO 8601, or None."""
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return iso(moment)


def text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def number(value):
    """Return a finite float; blanks, dashes and '#DIV/0!' style errors give None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value and abs(value) != float("inf") else None
    if not isinstance(value, str):
        return None
    cleaned = value.replace(",", "").replace(" ", "").strip()
    if not cleaned or cleaned.startswith("#"):
        return None
    try:
        result = float(cleaned)
    except ValueError:
        return None
    return result if result == result and abs(result) != float("inf") else None


def r2(value):
    return None if value is None else round(value, 2)


def whole(value):
    return None if value is None else int(round(value))


def pct(value, previous):
    if value is None or not previous:
        return None
    return round((value - previous) / previous * 100, 2)


def significant(value):
    """Keep six significant digits: enough for KRW (0.0252159 THB)."""
    return float("%.6g" % value)


def ce_year(year):
    return year - 543 if year > 2400 else year


def short_error(exc):
    """A short label for the errors list; never a full traceback."""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %d" % exc.code
    if isinstance(exc, Refused):
        return ("refused: %s" % exc)[:200]
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, socket.timeout) or isinstance(reason, socket.timeout):
        return "timeout"
    if isinstance(exc, OSError):
        return "network error"
    if isinstance(exc, (ValueError, KeyError, IndexError, zipfile.BadZipFile, ET.ParseError)):
        return ("invalid data: %s" % exc)[:200]
    return type(exc).__name__


# ---------------------------------------------------------------- network

def same_site(first, second):
    def bare(host):
        host = (host or "").lower()
        return host[4:] if host.startswith("www.") else host
    return bare(urlsplit(first).hostname) == bare(urlsplit(second).hostname)


def http_get(url):
    """GET with an honest User-Agent; retry once on a network or server error.

    Returns (body, Last-Modified header). A redirect to another site (bot check,
    paywall, AI-payment gate) is a refusal, not data.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                final = response.geturl()
                if not same_site(url, final):
                    raise Refused("redirected to %s" % urlsplit(final).hostname)
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ValueError("response larger than %d bytes" % MAX_BYTES)
                return body, response.headers.get("Last-Modified")
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt:
                raise
        except OSError:
            if attempt:
                raise
        time.sleep(RETRY_DELAY)


def remember(fetch):
    """Fetch each URL at most once per run, failures included."""
    seen = {}

    def cached(url):
        if url not in seen:
            try:
                seen[url] = (fetch(url), None)
            except Exception as exc:
                seen[url] = (None, exc)
        result, error = seen[url]
        if error is not None:
            raise error
        return result
    return cached


# ---------------------------------------------------------------- XLSX

def xml_part(book, name):
    if book.getinfo(name).file_size > MAX_PART:
        raise ValueError("%s is too large" % name)
    return ET.fromstring(book.read(name))


def open_xlsx(blob):
    """Return (zip, shared strings, [(sheet name, part path)]) in workbook order."""
    book = zipfile.ZipFile(io.BytesIO(blob))
    strings = []
    if "xl/sharedStrings.xml" in book.namelist():
        for item in xml_part(book, "xl/sharedStrings.xml").iter(NS + "si"):
            runs = item.findall(NS + "t") + item.findall(NS + "r/" + NS + "t")
            strings.append("".join(run.text or "" for run in runs))
    targets = {rel.get("Id"): rel.get("Target")
               for rel in xml_part(book, "xl/_rels/workbook.xml.rels")}
    sheets = []
    for sheet in xml_part(book, "xl/workbook.xml").iter(NS + "sheet"):
        target = targets[sheet.get(REL_ID)]
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        sheets.append((sheet.get("name") or "", path))
    return book, strings, sheets


def column_index(ref):
    index = 0
    for letter in re.match(r"[A-Z]*", ref.upper()).group():
        index = index * 26 + ord(letter) - 64
    return index - 1


def cell_value(cell, strings):
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(NS + "is")
        return None if node is None else "".join(t.text or "" for t in node.iter(NS + "t"))
    raw = cell.findtext(NS + "v")
    if raw is None:
        return None
    if kind == "s":
        return strings[int(raw)]
    if kind in ("str", "e"):
        return raw  # formula text, or an error such as '#DIV/0!'
    value = number(raw)
    return raw if value is None else value


def read_sheet(book, strings, path):
    """Return {row number: {0-based column: value}} from one worksheet."""
    rows = {}
    number_row = 0
    for row in xml_part(book, path).iter(NS + "row"):
        number_row = int(row.get("r")) if row.get("r") else number_row + 1
        cells = rows.setdefault(number_row, {})
        column = 0
        for cell in row.iter(NS + "c"):
            if cell.get("r"):
                column = column_index(cell.get("r"))
            value = cell_value(cell, strings)
            if value is not None and value != "":
                cells[column] = value
            column += 1
    return rows


def first_row(rows, matches, what):
    for row in sorted(rows):
        if any(matches(value) for value in rows[row].values()):
            return row
    raise ValueError("no %s row" % what)


def year_labels(cells):
    """{column: (Gregorian year, flag)} for labels such as '2026p' or '2569P'."""
    found = {}
    for column, value in cells.items():
        match = YEAR.match(text(value))
        if match:
            found[column] = (ce_year(int(match.group(1))), match.group(2).lower())
    return found


# ---------------------------------------------------------------- MOTS

def categories(html):
    return [(int(ident), name) for ident, name in CATEGORY.findall(html)]


def discover_category(kind, fetch):
    """This year's MOTS folder, so the collector follows the January rollover."""
    index = fetch(MOTS % MOTS_INDEX)[0].decode("utf-8", "replace")
    years = []
    for ident, name in categories(index):
        match = re.search(r"สถิติด้านการท่องเที่ยว ปี (\d{4})", name)
        if match:
            years.append((int(match.group(1)), ident))
    if not years:
        return None
    folder = fetch(MOTS % max(years)[1])[0].decode("utf-8", "replace")
    found = [ident for ident, name in categories(folder) if name.startswith(MOTS_TITLE[kind])]
    return MOTS % max(found) if found else None


def mots_file_link(kind, previous, fetch):
    """Return (category page, XLSX link). The file name is a new UUID each month."""
    candidates = []
    try:
        found = discover_category(kind, fetch)
        problem = None if found else "no matching folder"
    except Exception as exc:
        found, problem = None, short_error(exc)
    if problem:
        # Best effort: the known folders below still work, but after the
        # January rollover they hold last year's file.
        print("%s: MOTS folder discovery failed (%s)" % (kind, problem), file=sys.stderr)
    else:
        candidates.append(found)
    for url in (previous.get("source_url"), MOTS % MOTS_DEFAULT[kind]):
        if url and url not in candidates:
            candidates.append(url)
    failure = None
    for url in candidates:
        try:
            html = fetch(url)[0].decode("utf-8", "replace")
        except Exception as exc:
            failure = failure or exc
            continue
        match = XLSX_LINK.search(html)
        if match:
            return url, match.group(0)
    raise failure or ValueError("no v2022_*.xlsx link on the MOTS category page")


def month_column(label):
    """'Sep' -> 9; 'YTD (Jan-Sep)' -> ('ytd', 1, 9); anything else -> None."""
    label = text(label).lower()
    ytd = re.match(r"^ytd\s*\(\s*([a-z]{3})[a-z.]*\s*-\s*([a-z]{3})[a-z.]*\s*\)$", label)
    if ytd and ytd.group(1) in MONTHS and ytd.group(2) in MONTHS:
        return ("ytd", MONTHS.index(ytd.group(1)) + 1, MONTHS.index(ytd.group(2)) + 1)
    month = re.match(r"^([a-z]{3})[a-z]*\.?$", label)
    if month and month.group(1) in MONTHS:
        return MONTHS.index(month.group(1)) + 1
    return None


def arrival_blocks(rows, head, name_col):
    """{year: (flag, {month: column}, (column, from, to) or None)} from the header rows."""
    year_row = next((row for row in range(head - 1, head - 4, -1)
                     if year_labels(rows.get(row, {}))), None)
    if year_row is None:
        raise ValueError("no year labels above the Nationality row")
    labels = year_labels(rows[year_row])
    stops = {column for column in rows[year_row] if column > name_col}
    stops |= {column for column, value in rows[head].items()
              if column > name_col and text(value).lower() == "nationality"}
    blocks = {}
    for start, (year, flag) in labels.items():
        end = min([stop for stop in stops if stop > start] + [10 ** 6])
        months, ytd = {}, None
        for column, value in rows[head].items():
            if start <= column < end:
                kind = month_column(value)
                if isinstance(kind, tuple):
                    ytd = (column, kind[1], kind[2])
                elif kind:
                    months[kind] = column
        blocks[year] = (flag, months, ytd)
    return blocks


def parse_arrivals(blob):
    """Latest month of the MOTS arrivals-by-nationality table."""
    book, strings, sheets = open_xlsx(blob)
    if not sheets:
        raise ValueError("workbook has no sheets")
    rows = read_sheet(book, strings, sheets[0][1])
    head = first_row(rows, lambda value: text(value).lower() == "nationality", "Nationality")
    name_col = min(column for column, value in rows[head].items()
                   if text(value).lower() == "nationality")
    blocks = arrival_blocks(rows, head, name_col)
    year = max(blocks)
    if year - 1 not in blocks:
        raise ValueError("no %d block next to %d" % (year - 1, year))
    flag, months, ytd = blocks[year]
    _, prev_months, prev_ytd = blocks[year - 1]

    entries, total = [], None
    for row in sorted(rows):
        name = text(rows[row].get(name_col)) if row > head else ""
        if name.lower() == "grand total":
            total = rows[row]
            break
        if name:
            entries.append((name, rows[row]))
    if total is None:
        raise ValueError("no Grand Total row")
    filled = [month for month, column in months.items() if number(total.get(column))]
    if not filled:
        raise ValueError("no month with a Grand Total value")
    month = max(filled)
    column, prev_column = months[month], prev_months.get(month)

    leaves = [(name, cells) for name, cells in entries if name.lower() not in AGGREGATES]
    if round(sum(number(cells.get(column)) or 0 for _, cells in leaves)) != round(
            number(total[column])):
        raise ValueError("nationality rows do not add up to Grand Total; layout changed")

    def figures(cells, this_col, prev_col):
        value = whole(number(cells.get(this_col)))
        previous = whole(number(cells.get(prev_col))) if prev_col is not None else None
        return value, previous, pct(value, previous)

    markets = []
    for name, cells in leaves:
        value, previous, change = figures(cells, column, prev_column)
        if value is None or name.lower().startswith("other"):
            continue
        markets.append({"name": name, "ru": RU_NAMES.get(name), "value": value,
                        "prev_year": previous, "yoy_pct": change})
    markets.sort(key=lambda market: (-market["value"], market["name"]))
    markets = [market for rank, market in enumerate(markets)
               if rank < TOP_MARKETS or market["name"] in WATCHED]

    value, previous, change = figures(total, column, prev_column)
    result = {"period": "%04d-%02d" % (year, month),
              "status": "preliminary" if flag == "p" else "revised",
              "total": {"value": value, "prev_year": previous, "yoy_pct": change},
              "ytd": None, "markets": markets}
    if ytd:
        # Compare like with like: last year's YTD must cover the same months.
        same_span = prev_ytd is not None and prev_ytd[1:] == ytd[1:]
        ytd_value, ytd_prev, ytd_change = figures(
            total, ytd[0], prev_ytd[0] if same_span else None)
        result["ytd"] = {"from": "%04d-%02d" % (year, ytd[1]), "to": "%04d-%02d" % (year, ytd[2]),
                         "value": ytd_value, "prev_year": ytd_prev, "yoy_pct": ytd_change}
    return result


def thai_period(name):
    """'ส.ค.2569p' -> ('2026-08', 'p'); the cumulative sheet gives None."""
    match = re.match(r"^\s*([฀-๿.\s]+?)\s*(\d{4})\s*([PpRr]?)\s*$", name)
    if not match:
        return None
    month = THAI_MONTHS.get(re.sub(r"[.\s]", "", match.group(1)))
    if month is None:
        return None
    return "%04d-%02d" % (ce_year(int(match.group(2))), month), match.group(3).lower()


def province_group(label):
    label = "".join(label.split())
    revenue = label.startswith("รายได้")
    if "อัตราการเข้าพัก" in label:
        return "occupancy"
    if "คนไทย" in label or "ชาวไทย" in label:
        return "revenue_thai" if revenue else "thai"
    if "ชาวต่างชาติ" in label:
        return "revenue_foreign" if revenue else "foreign"
    return "revenue" if revenue else None


def province_columns(rows, year):
    """{group: (this year, last year, change)} validated against the year labels."""
    group_row = first_row(rows, lambda value: province_group(text(value)) == "occupancy",
                          "occupancy header")
    sub_row = next((row for row in sorted(rows)
                    if row > group_row and len(year_labels(rows[row])) >= 2), None)
    if sub_row is None:
        raise ValueError("no year labels under the province headers")
    labels = year_labels(rows[sub_row])
    columns = {}
    for column, value in rows[group_row].items():
        group = province_group(text(value))
        if group is None:
            continue
        this, last = labels.get(column), labels.get(column + 1)
        change = text(rows[sub_row].get(column + 2))
        if not this or not last or this[0] != year or last[0] != year - 1 \
                or "เปลี่ยนแปลง" not in change:
            raise ValueError("unexpected columns under %s" % text(value))
        columns[group] = (column, column + 1, column + 2)
    missing = [group for group in PROVINCE_GROUPS if group not in columns]
    if missing:
        raise ValueError("missing province columns: %s" % ", ".join(missing))
    return columns, sub_row


def parse_provinces(blob):
    """Latest monthly sheet of the MOTS provincial tourism situation."""
    book, strings, sheets = open_xlsx(blob)
    monthly = [(thai_period(name), name, path) for name, path in sheets
               if not name.strip().startswith("สะสม")]
    monthly = [item for item in monthly if item[0]]
    if not monthly:
        raise ValueError("no monthly sheet such as 'ส.ค.2569p'")
    (period, flag), sheet, path = max(monthly, key=lambda item: item[0][0])
    rows = read_sheet(book, strings, path)
    columns, sub_row = province_columns(rows, int(period[:4]))
    if not flag:
        flag = year_labels(rows[sub_row])[columns["occupancy"][0]][1]

    names = {name: (region, label) for region, aliases, label in PROVINCES for name in aliases}
    found = {}
    for row in sorted(rows):
        if row <= sub_row:
            continue
        for value in rows[row].values():
            if text(value) in names and names[text(value)][0] not in found:
                region, label = names[text(value)]
                found[region] = record_province(rows[row], columns, text(value), label)
                break
    missing = [region for region, _, _ in PROVINCES if region not in found]
    if missing:
        raise ValueError("province rows not found: %s" % ", ".join(missing))
    national = found["thailand"]
    if national["occupancy_pct"] is None or national["foreign_visitors"] is None:
        # An empty placeholder sheet must not replace last month's figures.
        raise ValueError("sheet %s has no national totals yet" % sheet)
    return {"period": period, "status": "preliminary" if flag == "p" else "revised",
            "sheet": sheet, "rows": {region: found[region] for region, _, _ in PROVINCES}}


def record_province(cells, columns, name, label):
    def trio(group):
        return [number(cells.get(column)) for column in columns[group]]
    occupancy, thai, foreign = trio("occupancy"), trio("thai"), trio("foreign")
    revenue, revenue_foreign = trio("revenue"), trio("revenue_foreign")
    if occupancy[0] is not None and not 0 <= occupancy[0] <= 100:
        raise ValueError("occupancy %s for %s is not a percentage" % (occupancy[0], name))
    return {"province_th": name, "label_ru": label,
            "occupancy_pct": r2(occupancy[0]), "occupancy_prev_pct": r2(occupancy[1]),
            "occupancy_change_pp": r2(occupancy[2]),
            "foreign_visitors": whole(foreign[0]), "foreign_yoy_pct": r2(foreign[2]),
            "thai_visitors": whole(thai[0]), "thai_yoy_pct": r2(thai[2]),
            "revenue_mb": r2(revenue[0]), "revenue_yoy_pct": r2(revenue[2]),
            "revenue_foreign_mb": r2(revenue_foreign[0]),
            "revenue_foreign_yoy_pct": r2(revenue_foreign[2])}


def collect_mots(kind, previous, fetch, now):
    """Return (section, changed). An unchanged file (same sha256) is not re-parsed."""
    page, link = mots_file_link(kind, previous, fetch)
    body, modified = fetch(link)
    digest = hashlib.sha256(body).hexdigest()
    published = http_time(modified)
    complete = previous.get("total" if kind == "arrivals" else "rows")
    if previous.get("sha256") == digest and previous.get("period") and complete:
        section = dict(previous)
        section.update(source_url=page, file_url=link, retrieved_at=iso(now))
        if published:
            section["published_at"] = published
        return section, False
    parsed = (parse_arrivals if kind == "arrivals" else parse_provinces)(body)
    if previous.get("period") and parsed["period"] < previous["period"]:
        raise ValueError("file covers %s, older than kept %s" % (parsed["period"], previous["period"]))
    section = {"source": "MOTS", "source_url": page, "file_url": link,
               "published_at": published, "retrieved_at": iso(now)}
    section.update(parsed)
    if kind == "provinces":
        section["note_ru"] = PROVINCE_NOTE
    section["sha256"] = digest
    return section, True


# ---------------------------------------------------------------- BOT

def bot_period(label):
    """'AUG 2026 p' -> ('2026-08', 'preliminary'); None for other labels."""
    match = re.match(r"^([A-Za-z]{3})\s+(\d{4})(?:\s+([PpRr]))?$", text(label))
    if not match or match.group(1).lower() not in MONTHS:
        return None
    status = {"p": "preliminary", "r": "revised"}.get((match.group(3) or "").lower(), "final")
    return "%s-%02d" % (match.group(2), MONTHS.index(match.group(1).lower()) + 1), status


def bot_last_updated(html):
    """'Last Updated : 05 Oct 2026 15:34' (Bangkok time) as ISO 8601."""
    plain = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    match = re.search(r"Last Updated\s*:\s*(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{1,2}):(\d{2})", plain)
    if not match or match.group(2).lower() not in MONTHS:
        raise ValueError("no 'Last Updated' on the BOT report page")
    moment = datetime(int(match.group(3)), MONTHS.index(match.group(2).lower()) + 1,
                      int(match.group(1)), int(match.group(4)), int(match.group(5)),
                      tzinfo=BANGKOK)
    return moment.isoformat(timespec="seconds")


def bot_metric(label):
    lowered = label.lower()
    for words, metric, unit_words, unit in BOT_METRICS:
        if words in lowered:
            if unit_words not in lowered:
                raise ValueError("unexpected unit in BOT row '%s'" % label)
            return metric, unit
    return None


def parse_bot(blob):
    """Return (observations, latest period) from the 'Monthly' sheet of report 875."""
    book, strings, sheets = open_xlsx(blob)
    path = dict(sheets).get("Monthly")
    if path is None:
        raise ValueError("no 'Monthly' sheet")
    rows = read_sheet(book, strings, path)
    header = next((row for row in sorted(rows)
                   if sum(1 for value in rows[row].values() if bot_period(value)) >= 3), None)
    if header is None:
        raise ValueError("no period header such as 'AUG 2026 p'")
    periods = {column: bot_period(value) for column, value in rows[header].items()
               if bot_period(value)}
    first = min(periods)
    found, current = [], None
    for row in sorted(rows):
        if row <= header:
            continue
        cells = rows[row]
        labels = [(column, text(value)) for column, value in cells.items()
                  if column < first and isinstance(value, str) and number(value) is None]
        if not labels:
            continue
        label = max(labels)[1]
        section = re.match(r"^\d+\.\s*(.+)$", label)
        if section:
            current = bot_metric(section.group(1))
            region = "Thailand"
        else:
            region = BOT_REGIONS.get(label.lower())
        if current is None or region is None:
            continue
        for column, (period, status) in periods.items():
            value = number(cells.get(column))
            if value is not None:
                found.append({"metric": current[0], "region": region, "period": period,
                              "value": round(value, 2), "unit": current[1], "status": status})
    missing = {metric for _, metric, _, _ in BOT_METRICS} - {item["metric"] for item in found}
    if missing:
        raise ValueError("BOT rows not found: %s" % ", ".join(sorted(missing)))
    recent = sorted({item["period"] for item in found})[-BOT_MONTHS:]
    return [item for item in found if item["period"] in recent], recent[-1]


def read_indicators(path):
    """Return (raw lines, parsed rows); a malformed line stops the BOT update."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rows = []
    for number_line, line in enumerate(lines, 1):
        row = json.loads(line) if line.strip() else None
        if line.strip() and not isinstance(row, dict):
            raise ValueError("indicators.jsonl line %d is not an object" % number_line)
        rows.append(row)
    return lines, rows


def merge_indicators(path, observations, published, retrieved):
    """Append new (metric, region, period) rows, refresh revised ones, delete nothing.

    Only rows citing the BOT report are matched: a row from another source is
    never overwritten with BOT values under its own attribution.
    """
    lines, rows = read_indicators(path)
    index = {}
    for position, row in enumerate(rows):
        if row is not None and row.get("source_url") == BOT_PAGE:
            index.setdefault((row.get("metric"), row.get("region"), row.get("period")),
                             []).append(position)
    written = 0
    for item in observations:
        key = (item["metric"], item["region"], item["period"])
        if key not in index:
            row = {"metric": item["metric"], "region": item["region"], "period": item["period"],
                   "value": item["value"], "unit": item["unit"], "source_url": BOT_PAGE,
                   "published_at": published, "retrieved_at": retrieved,
                   "status": item["status"], "notes": BOT_NOTES}
            lines.append(json.dumps(row, ensure_ascii=False))
            rows.append(row)
            index[key] = [len(lines) - 1]
            written += 1
            continue
        for position in index[key]:
            row = rows[position]
            if row.get("value") == item["value"] and row.get("status") == item["status"]:
                continue
            row.update(value=item["value"], status=item["status"],
                       published_at=published, retrieved_at=retrieved)
            lines[position] = json.dumps(row, ensure_ascii=False)
            written += 1
    if written:
        atomic_write_text(path, "\n".join(lines) + "\n")
    return written


def has_period(path, period):
    try:
        _, rows = read_indicators(path)
    except (OSError, ValueError):
        return False
    return any(row and row.get("period") == period and row.get("source_url") == BOT_PAGE
               for row in rows)


def collect_bot(previous, fetch, now, indicators):
    body, _ = fetch(BOT_FILE)
    digest = hashlib.sha256(body).hexdigest()
    if previous.get("sha256") == digest and has_period(indicators, previous.get("latest_period")):
        section = dict(previous)
        section.update(retrieved_at=iso(now), rows_written=0)
        return section, False
    observations, latest = parse_bot(body)
    if previous.get("latest_period") and latest < previous["latest_period"]:
        raise ValueError("file ends at %s, older than kept %s" % (latest, previous["latest_period"]))
    published = bot_last_updated(fetch(BOT_PAGE)[0].decode("utf-8", "replace"))
    retrieved = now.astimezone(BANGKOK).isoformat(timespec="seconds")
    written = merge_indicators(indicators, observations, published, retrieved)
    return {"source": "BOT", "source_url": BOT_PAGE, "file_url": BOT_FILE,
            "retrieved_at": iso(now), "published_at": published, "latest_period": latest,
            "rows_written": written, "sha256": digest}, True


# ---------------------------------------------------------------- FX

def parse_ecb(blob):
    """THB per unit of each currency, crossed through the same day's EUR rates."""
    by_day = {}
    for row in csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))):
        day, code, value = row.get("TIME_PERIOD"), row.get("CURRENCY"), number(row.get("OBS_VALUE"))
        if (row.get("CURRENCY_DENOM") == "EUR" and value and code
                and re.match(r"^\d{4}-\d{2}-\d{2}$", day or "")):
            by_day.setdefault(day, {})[code] = value
    days = sorted(day for day, rates in by_day.items() if "THB" in rates)
    if not days:
        raise ValueError("no THB rate in the ECB response")
    rates = by_day[days[-1]]
    thb_per = {}
    for code in ECB_CODES:
        if code == "EUR":
            thb_per[code] = significant(rates["THB"])
        else:
            thb_per[code] = significant(rates["THB"] / rates[code]) if rates.get(code) else None
    return days[-1], thb_per


def parse_cbr(blob):
    """RUB per 1 THB from the Bank of Russia daily XML (windows-1251, comma decimals)."""
    root = ET.fromstring(blob)
    day = datetime.strptime(root.get("Date") or "", "%d.%m.%Y").date().isoformat()
    for valute in root.iter("Valute"):
        if valute.findtext("CharCode") == "THB":
            nominal = int(valute.findtext("Nominal"))
            value = number((valute.findtext("Value") or "").replace(",", "."))
            if not nominal or value is None:
                break
            return day, significant(value / nominal)
    raise ValueError("no THB rate in the Bank of Russia file")


def collect_ecb(previous, fetch, now):
    day, rates = parse_ecb(fetch(ECB_DATA)[0])
    return {"source": "ECB", "source_url": ECB_PAGE, "data_url": ECB_DATA,
            "retrieved_at": iso(now), "date": day, "thb_per": rates}, True


def collect_cbr(previous, fetch, now):
    day, rate = parse_cbr(fetch(CBR_DATA)[0])
    return {"source": "Bank of Russia", "source_url": CBR_PAGE, "data_url": CBR_DATA,
            "retrieved_at": iso(now), "date": day, "rub_per_thb": rate}, True


# ---------------------------------------------------------------- output

def atomic_write_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as out:
            temporary = Path(out.name)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o644)  # the temporary file is 0600; these files are public data
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_previous(path):
    path = Path(path)
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("official.json is not an object")
    return document


def summary(name, section, changed):
    same = "" if changed else " (same file as last run)"
    if name == "arrivals":
        total = section["total"]
        russia = next((m for m in section["markets"] if m["name"] == "Russian Federation"), None)
        extra = "; Russia %s (%s%% YoY)" % (format(russia["value"], ","), russia["yoy_pct"]) \
            if russia else ""
        return "arrivals: MOTS %s %s: %s arrivals, %s%% YoY%s; file %s%s" % (
            section["period"], section["status"], format(total["value"], ","),
            total["yoy_pct"], extra, section["published_at"], same)
    if name == "provinces":
        row = section["rows"]["pattaya"]
        return ("provinces: MOTS %s %s: %s occupancy %s%%, foreign visitors %s (%s%% YoY); "
                "file %s%s" % (section["period"], section["status"], row["label_ru"],
                               row["occupancy_pct"], format(row["foreign_visitors"] or 0, ","),
                               row["foreign_yoy_pct"], section["published_at"], same))
    if name == "bot":
        return "bot: BOT report 875 up to %s, %d indicator rows written, updated %s%s" % (
            section["latest_period"], section["rows_written"], section["published_at"], same)
    if name == "ecb":
        rates = section["thb_per"]
        return "ecb: ECB %s: 1 USD = %s THB, 1 EUR = %s THB, 1 CNY = %s THB" % (
            section["date"], rates["USD"], rates["EUR"], rates["CNY"])
    return "cbr: Bank of Russia %s: 1 THB = %s RUB" % (section["date"], section["rub_per_thb"])


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Collect official tourism statistics and FX.")
    parser.add_argument("--only", default=",".join(SOURCES),
                        help="comma-separated subset of: " + ",".join(SOURCES))
    names = [name.strip() for name in parser.parse_args(argv).only.split(",") if name.strip()]
    unknown = [name for name in names if name not in SOURCES]
    if unknown or not names:
        parser.error("unknown source: %s" % ", ".join(unknown or ["(none)"]))
    return [name for name in SOURCES if name in names]


def main(argv=None, output=OUT, indicators=INDICATORS, fetch=None, now=None):
    selected = parse_args(argv)
    try:
        previous = load_previous(output)
    except (OSError, ValueError) as exc:
        # A broken file is left for a human; overwriting it could hide good data.
        print("official: cannot read %s (%s); nothing written" % (output, exc), file=sys.stderr)
        return 1
    fetch = remember(fetch or http_get)
    now = now or datetime.now(timezone.utc)
    fx = dict(previous.get("fx") or {})
    sections = {name: previous.get(name) for name in ("arrivals", "provinces", "bot")}
    errors, succeeded = [], 0
    for name in selected:
        store = fx if name in FX else sections
        old = store.get(name) if isinstance(store.get(name), dict) else {}
        try:
            if name in ("arrivals", "provinces"):
                section, changed = collect_mots(name, old, fetch, now)
            elif name == "bot":
                section, changed = collect_bot(old, fetch, now, indicators)
            else:
                section, changed = (collect_ecb if name == "ecb" else collect_cbr)(old, fetch, now)
        except Exception as exc:
            errors.append({"source": name, "at": iso(now), "error": short_error(exc)})
            print("%s: FAILED (%s); previous data kept" % (name, short_error(exc)), file=sys.stderr)
            continue
        store[name] = section
        succeeded += 1
        print(summary(name, section, changed))
    document = {"built_at": iso(now)}
    document.update(sections)
    document["fx"] = {name: fx.get(name) for name in FX}
    document["errors"] = errors
    atomic_write_text(output, json.dumps(document, ensure_ascii=False, indent=1,
                                         allow_nan=False) + "\n")
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
