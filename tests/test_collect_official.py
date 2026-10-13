"""Offline tests for the official statistics collector.

The XLSX fixtures are built here with zipfile and mirror the publishers' real
layouts: MOTS year blocks and region subtotals, Thai sheet names in Buddhist
years, BOT period labels such as 'AUG 2026 p'. No test touches the network.
"""

import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from xml.sax.saxutils import escape
import zipfile


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "collect_official.py"
SPEC = importlib.util.spec_from_file_location("collect_official", MODULE)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
NOW = datetime(2026, 10, 13, 3, 0, 0, tzinfo=timezone.utc)  # 10:00 Bangkok
LATER = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)


class Inline(str):
    """A cell written as an inline string instead of a shared string."""


class Error(str):
    """A cell holding an Excel error value such as '#DIV/0!'."""


def column_name(index):
    name, index = "", index + 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def make_xlsx(sheets):
    """[(sheet name, {row: [values from column A]})] -> XLSX bytes."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    strings, parts, entries, links = [], {}, [], []
    for number, (name, rows) in enumerate(sheets, 1):
        xml_rows = []
        for row in sorted(rows):
            cells = []
            for column, value in enumerate(rows[row]):
                ref = column_name(column) + str(row)
                if value is None:
                    continue
                if isinstance(value, Inline):
                    cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, escape(value)))
                elif isinstance(value, Error):
                    cells.append('<c r="%s" t="e"><v>%s</v></c>' % (ref, escape(value)))
                elif isinstance(value, str):
                    strings.append(value)
                    cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, len(strings) - 1))
                else:
                    cells.append('<c r="%s"><v>%r</v></c>' % (ref, value))
            xml_rows.append('<row r="%d">%s</row>' % (row, "".join(cells)))
        parts["xl/worksheets/sheet%d.xml" % number] = (
            '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>%s'
            '</sheetData></worksheet>' % (main, "".join(xml_rows)))
        entries.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(name), number, number))
        links.append('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
                     % (number, rel, number))
    parts["xl/workbook.xml"] = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="%s" '
                                'xmlns:r="%s"><sheets>%s</sheets></workbook>'
                                % (main, rel, "".join(entries)))
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships">%s</Relationships>' % "".join(links))
    parts["xl/sharedStrings.xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?><sst xmlns="%s">%s</sst>'
        % (main, "".join("<si><t>%s</t></si>" % escape(text) for text in strings)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        for name, content in parts.items():
            book.writestr(name, content.encode("utf-8"))
    return buffer.getvalue()


# ---------------------------------------------------------------- fixtures

def arrivals_rows(extra=None):
    """Two months of 2026p next to 2025p, plus the %change block, as MOTS lays it out."""
    rows = {
        2: ["จำนวนนักท่องเที่ยว ปี 2569P"],
        4: ["Country of", "2026p", None, None, "2025p", None, None, "Country of", "%CHANG"],
        5: ["Nationality", "Jan", "Feb", "YTD (Jan-Feb)", "Jan", "Feb", "YTD (Jan-Feb)",
            "Nationality", "Jan", "Feb", "YTD (Jan-Feb)"],
    }
    body = [
        ["Asia and the Pacific", 3060, 3410, 6470, 2489, 1910, 4399],
        ["China", 1800, 2000, 3800, 1400, 1500, 2900],
        ["India", 800, 900, 1700, 700, None, 700],
        ["Laos", 450, 500, 950, 380, 400, 780],
        ["  Other in Asia", 10, 10, 20, 9, 10, 19],
        ["Europe", 490, 1550, 2040, 470, 1400, 1870],
        ["Russian Federation", 100, "1,200", 1300, 90, 1000, 1090],
        [Inline("Germany"), 350, 300, 650, 380, 400, 780],
        ["Kazakhstan", 40, 50, 90, 0, 0, 0],
    ] + (extra or []) + [
        ["Grand Total", 3550, 4960, 8510, 2959, 3310, 6269],
    ]
    for offset, values in enumerate(body):
        rows[6 + offset] = values + [values[0], 1.0, Error("#DIV/0!"), 2.0]
    rows[6 + len(body)] = ["หมายเหตุ : P หมายถึง ข้อมูลเบื้องต้น"]
    return rows


def arrivals_file(extra=None):
    return make_xlsx([("จำนวน 69", arrivals_rows(extra))])


PROVINCE_GROUPS = ["อัตราการเข้าพัก", "จำนวนผู้เข้าพัก (คน)", "จำนวนผู้เยี่ยมเยือนทั้งหมด (คน)",
                   "จำนวนผู้เยี่ยมเยือนคนไทย (คน)", "จำนวนผู้เยี่ยมเยือนชาวต่างชาติ (คน)",
                   "รายได้จากผู้เยี่ยมเยือน (ล้านบาท)", "รายได้จากผู้เยี่ยมเยือนคนไทย (ล้านบาท)",
                   "รายได้จากผู้เยี่ยมเยือนชาวต่างชาติ (ล้านบาท)"]
CHONBURI = [74.989999999999995, 77.7, -2.71, 1486365, 1541994, -3.61, 2225797, 2345613, -5.11,
            1420939, 1501590, -5.37, "804858", 844023, -4.6399999999999997,
            25276.670000000002, 26458.99, -4.47, 8946.48, 9300.84, -3.81,
            16330.19, 17158.150000000001, -4.83]


def generic(occupancy):
    return [occupancy, occupancy - 1, 1.0] + [1000, 900, 11.11] * 7


def province_sheet(this_label, chonburi_occupancy, drop=None):
    rows = {1: [None, "รายงานเบื้องต้นเดือนกุมภาพันธ์ ปี 2569P"],
            2: [None, None] + sum([[group, None, None] for group in PROVINCE_GROUPS], []),
            4: [None, None, this_label, "2568R", "การเปลี่ยนแปลง"]
            + [this_label, "2568R", "%การเปลี่ยนแปลง"] * 7}
    chonburi = list(CHONBURI)
    chonburi[0] = chonburi_occupancy
    krabi = generic(61.85)
    krabi[14] = Error("#DIV/0!")
    body = [("กรุงเทพมหานคร", generic(76.38)), ("กาญจนบุรี", generic(65.0)),
            ("ภาคกลาง+ตว.ตก", generic(62.0)), ("ชลบุรี", chonburi), ("กระบี่", krabi),
            ("ภูเก็ต", generic(70.01)), ("สุราษฎร์ธานี", generic(76.7)), ("ภาคใต้", generic(70.3)),
            ("เชียงใหม่", generic(70.02)), ("รวมทั้งหมด", generic(68.5))]
    for offset, (name, values) in enumerate(item for item in body if item[0] != drop):
        rows[5 + offset] = [None, name] + values
    rows[30] = [None, "ที่มา : กองเศรษฐกิจการท่องเที่ยวและกีฬา กระทรวงการท่องเที่ยวและกีฬา"]
    return rows


def provinces_file(drop=None):
    return make_xlsx([("ม.ค.2569R", province_sheet("2569R", 80.0)),
                      ("ก.พ.2569p", province_sheet("2569P", 74.989999999999995, drop)),
                      ("สะสมม.ค.-ก.พ. 2569p", {1: [None, "สะสม"]})])


BOT_SECTIONS = [
    ("1.  Number of foreign tourists visiting Thailand (in thousands) ", None),
    ("2.  Accommodation occupancy rate (percent) ", 60.0),
    ("3.  3-month advanced booking rate (percent)", 20.0),
    ("4.  Average room rental rate  (baht : room) ", 1500.0),
    ("5.  Share of revenue from conference/seminar package to total revenue  (percent) ", 40.0),
    ("6.  Share of foreign tourists to total guests (percent) ", 30.0),
]


def bot_file():
    rows = {1: ["Bank of Thailand"], 2: ["EC_EI_028_S2 : Tourism Indicators"],
            4: ["Last Updated : "],
            6: [None, None, "MAY 2026 r", "JUN 2026 p", "JUL 2026 p", "AUG 2026 p"]}
    row, counter = 7, 1
    for label, base in BOT_SECTIONS:
        if base is None:
            rows[row] = [str(counter), label, 2346.85, 2105.38, 2545.62, 2515.32]
            row, counter = row + 1, counter + 1
            continue
        rows[row] = [str(counter), label]
        row, counter = row + 1, counter + 1
        for step, region in enumerate(["Total", "Central (including Bangkok)", "South",
                                       "North", "Northeast"]):
            values = [base + step, base + step + 1, base + step + 2, base + step + 3]
            if label.startswith("2.") and region == "Total":
                values = [64.1, 65.7, 70.05, 68.5]
            if label.startswith("2.") and region == "North":
                values[3] = None
            rows[row] = [str(counter), "   " + region] + values
            row, counter = row + 1, counter + 1
    rows[row + 1] = ["Source:"]
    rows[row + 2] = ["Economic Tourism and Sports Division, Ministry of Tourism and Sports"]
    return make_xlsx([("Monthly", rows), ("Quarterly", {1: ["Bank of Thailand"]})])


BOT_PAGE_HTML = (b'<html><span id="lbRange">( JAN 2015 - AUG 2026 )</span>'
                 b'<span id="lbUpdate">Last Updated : <b>05 Oct 2026 15:34</b></span></html>')
EXISTING = [
    '{"metric": "foreign_arrivals", "region": "Thailand", "period": "2026-07", "value": 2545.62, '
    '"unit": "thousand persons", "source_url": "%s", "published_at": "2026-08-31T14:30:00+07:00", '
    '"retrieved_at": "2026-09-06T00:10:52+07:00", "status": "preliminary", "notes": "%s"}'
    % (collector.BOT_PAGE, collector.BOT_NOTES),
    '{"metric": "foreign_arrivals", "region": "Thailand", "period": "2026-06", "value": 1841.55, '
    '"unit": "thousand persons", "source_url": "%s", "published_at": "2026-08-31T14:30:00+07:00", '
    '"retrieved_at": "2026-09-06T00:10:52+07:00", "status": "preliminary", "notes": "%s"}'
    % (collector.BOT_PAGE, collector.BOT_NOTES),
    '{"metric": "hotel_occupancy", "region": "Thailand", "period": "2026-01", "value": 70.0, '
    '"unit": "percent", "source_url": "%s", "published_at": "2026-03-31T14:30:00+07:00", '
    '"retrieved_at": "2026-09-06T00:10:52+07:00", "status": "preliminary", "notes": "%s"}'
    % (collector.BOT_PAGE, collector.BOT_NOTES),
]

ECB_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
    + "".join("EXR.D.%s.EUR.SP00.A,D,%s,EUR,SP00,A,%s,%s\n" % (code, code, day, value)
              for day, rates in (("2026-10-09", {"THB": 37.9, "USD": 1.12, "GBP": 0.85}),
                                 ("2026-10-12", {"THB": 37.585, "USD": 1.1181, "CNY": 7.4832,
                                                 "INR": 107.086, "KRW": 1490.53, "MYR": 4.5663}))
              for code, value in rates.items())).encode("utf-8")
CBR_XML = ('<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="13.10.2026" '
           'name="Foreign Currency Market"><Valute ID="R01235"><NumCode>840</NumCode>'
           '<CharCode>USD</CharCode><Nominal>1</Nominal><Name>Доллар США</Name>'
           '<Value>83,9153</Value></Valute><Valute ID="R01675"><NumCode>764</NumCode>'
           '<CharCode>THB</CharCode><Nominal>10</Nominal><Name>Батов</Name>'
           '<Value>25,0337</Value><VunitRate>2,50337</VunitRate></Valute></ValCurs>'
           ).encode("cp1251")

ARRIVALS_XLSX = "https://www.mots.go.th/images/v2022_cf32d908-111e-4269-802d-18807cd65884.xlsx"
PROVINCES_XLSX = "https://www.mots.go.th/images/v2022_84449624-a42a-4e8b-8c2f-eb983373f91b.xlsx"
INDEX_HTML = (r'<script>self.__next_f.push([1,"[{\"id\":816,\"name\":\"สถิติด้านการท่องเที่ยว ปี 2569 '
              r'(Tourism Statistics 2026)\"},{\"id\":758,\"name\":\"สถิติด้านการท่องเที่ยว ปี 2567 '
              r'(Tourism Statistics 2024)\"}]"])</script>').encode("utf-8")
YEAR_HTML = (r'<script>self.__next_f.push([1,"[{\"id\":820,\"name\":\"สถานการณ์การท่องเที่ยวในประเทศ '
             r'รายจังหวัด ปี 2569 (Domestic Tourism Statistics)\"},{\"id\":817,\"name\":\"'
             r'สถิตินักท่องเที่ยวชาวต่างชาติที่เดินทางเข้าประเทศไทย ปี 2569 (เบื้องต้น)\"}]"])</script>'
             ).encode("utf-8")


def pages():
    """URL -> (body, Last-Modified) as the real sites answer."""
    return {
        collector.MOTS % 411: (INDEX_HTML, None),
        collector.MOTS % 816: (YEAR_HTML, None),
        collector.MOTS % 817: (('<a href="%s">xlsx</a>' % ARRIVALS_XLSX).encode(), None),
        collector.MOTS % 820: (('<a href="%s">xlsx</a>' % PROVINCES_XLSX).encode(), None),
        ARRIVALS_XLSX: (arrivals_file(), "Thu, 01 Oct 2026 03:40:30 GMT"),
        PROVINCES_XLSX: (provinces_file(), "Mon, 14 Sep 2026 08:46:04 GMT"),
        collector.BOT_FILE: (bot_file(), None),
        collector.BOT_PAGE: (BOT_PAGE_HTML, None),
        collector.ECB_DATA: (ECB_CSV, None),
        collector.CBR_DATA: (CBR_XML, None),
    }


def site(**overrides):
    """A fake fetch over pages(); an override may be an exception to raise."""
    answers = pages()
    answers.update(overrides)
    calls = []

    def fetch(url):
        calls.append(url)
        result = answers[url]
        if isinstance(result, Exception):
            raise result
        return result
    return fetch, calls


def forbidden(url):
    return urllib.error.HTTPError(url, 403, "Forbidden", {}, None)


# ---------------------------------------------------------------- parsers

class ArrivalsTests(unittest.TestCase):
    def test_latest_month_totals_ytd_and_markets(self):
        with patch.object(collector, "TOP_MARKETS", 2):
            result = collector.parse_arrivals(arrivals_file())
        self.assertEqual(result["period"], "2026-02")
        self.assertEqual(result["status"], "preliminary")
        self.assertEqual(result["total"], {"value": 4960, "prev_year": 3310, "yoy_pct": 49.85})
        self.assertEqual(result["ytd"], {"from": "2026-01", "to": "2026-02", "value": 8510,
                                         "prev_year": 6269, "yoy_pct": 35.75})
        # Top 2 by value plus every watched market; subtotals and "Other" rows never appear.
        self.assertEqual([m["name"] for m in result["markets"]],
                         ["China", "Russian Federation", "India", "Germany", "Kazakhstan"])
        russia = result["markets"][1]
        self.assertEqual(russia, {"name": "Russian Federation", "ru": "Россия", "value": 1200,
                                  "prev_year": 1000, "yoy_pct": 20.0})
        india, germany, kazakhstan = result["markets"][2:]
        self.assertIsNone(india["prev_year"])
        self.assertIsNone(india["yoy_pct"])
        self.assertEqual(germany["yoy_pct"], -25.0)
        self.assertEqual(kazakhstan["prev_year"], 0)
        self.assertIsNone(kazakhstan["yoy_pct"])

    def test_unknown_subtotal_breaks_the_balance_check(self):
        with self.assertRaisesRegex(ValueError, "do not add up"):
            collector.parse_arrivals(arrivals_file(extra=[["Caucasus (new group)", 5, 5, 10, 5, 5, 10]]))

    def test_ytd_without_matching_months_last_year_has_no_comparison(self):
        rows = arrivals_rows()
        rows[5][6] = "YTD (Jan-Dec)"
        result = collector.parse_arrivals(make_xlsx([("จำนวน 69", rows)]))
        self.assertEqual(result["ytd"], {"from": "2026-01", "to": "2026-02", "value": 8510,
                                         "prev_year": None, "yoy_pct": None})
        self.assertEqual(result["total"]["prev_year"], 3310)

    def test_month_labels(self):
        self.assertEqual(collector.month_column("Sep"), 9)
        self.assertEqual(collector.month_column("YTD (Jan-Sep)"), ("ytd", 1, 9))
        self.assertIsNone(collector.month_column("Nationality"))


class ProvinceTests(unittest.TestCase):
    def test_latest_month_sheet_and_region_rows(self):
        result = collector.parse_provinces(provinces_file())
        self.assertEqual((result["period"], result["status"], result["sheet"]),
                         ("2026-02", "preliminary", "ก.พ.2569p"))
        self.assertEqual(list(result["rows"]), ["bangkok", "pattaya", "phuket", "krabi",
                                                "samui", "chiangmai", "thailand"])
        self.assertEqual(result["rows"]["pattaya"], {
            "province_th": "ชลบุรี", "label_ru": "Чонбури (включая Паттайю)",
            "occupancy_pct": 74.99, "occupancy_prev_pct": 77.7, "occupancy_change_pp": -2.71,
            "foreign_visitors": 804858, "foreign_yoy_pct": -4.64,
            "thai_visitors": 1420939, "thai_yoy_pct": -5.37,
            "revenue_mb": 25276.67, "revenue_yoy_pct": -4.47,
            "revenue_foreign_mb": 16330.19, "revenue_foreign_yoy_pct": -4.83})
        self.assertIsNone(result["rows"]["krabi"]["foreign_yoy_pct"])
        self.assertEqual(result["rows"]["thailand"]["province_th"], "รวมทั้งหมด")
        self.assertEqual(result["rows"]["thailand"]["label_ru"], "Весь Таиланд")

    def test_missing_province_is_an_error_not_a_gap(self):
        with self.assertRaisesRegex(ValueError, "samui"):
            collector.parse_provinces(provinces_file(drop="สุราษฎร์ธานี"))

    def test_empty_placeholder_sheet_is_an_error(self):
        empty = province_sheet("2569P", None)
        for row in empty:
            if row > 4 and len(empty[row]) > 2:
                empty[row] = empty[row][:2]
        blob = make_xlsx([("ม.ค.2569R", province_sheet("2569R", 80.0)), ("ก.พ.2569p", empty)])
        with self.assertRaisesRegex(ValueError, "no national totals"):
            collector.parse_provinces(blob)

    def test_thai_sheet_names_use_buddhist_years(self):
        self.assertEqual(collector.thai_period("ส.ค.2569p"), ("2026-08", "p"))
        self.assertEqual(collector.thai_period("มี.ค.2569R"), ("2026-03", "r"))
        self.assertEqual(collector.thai_period("ม.ค.2570"), ("2027-01", ""))
        self.assertIsNone(collector.thai_period("สะสมม.ค.-ส.ค 2569p"))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "indicators.jsonl"
        self.path.write_text("\n".join(EXISTING) + "\n", encoding="utf-8")

    def tearDown(self):
        self.folder.cleanup()

    def test_parse_reads_sections_regions_and_flags(self):
        with patch.object(collector, "BOT_MONTHS", 3):
            observations, latest = collector.parse_bot(bot_file())
        self.assertEqual(latest, "2026-08")
        self.assertEqual({item["period"] for item in observations}, {"2026-06", "2026-07", "2026-08"})
        self.assertNotIn("Northeast", {item["region"] for item in observations})
        self.assertEqual({item["metric"] for item in observations},
                         {"foreign_arrivals", "hotel_occupancy", "advanced_booking_3_months",
                          "average_room_rate", "foreign_guest_share"})
        self.assertEqual(len(observations), 17 * 3 - 1)  # the blank North cell is not a zero
        rate = [item for item in observations if item["metric"] == "average_room_rate"
                and item["region"] == "Central including Bangkok" and item["period"] == "2026-08"]
        self.assertEqual(rate, [{"metric": "average_room_rate", "region": "Central including Bangkok",
                                 "period": "2026-08", "value": 1504.0, "unit": "THB per room",
                                 "status": "preliminary"}])

    def test_last_updated_is_bangkok_time(self):
        self.assertEqual(collector.bot_last_updated(BOT_PAGE_HTML.decode()),
                         "2026-10-05T15:34:00+07:00")
        with self.assertRaises(ValueError):
            collector.bot_last_updated("<html>maintenance</html>")

    def test_merge_appends_refreshes_revisions_and_keeps_everything(self):
        fetch, calls = site()
        with patch.object(collector, "BOT_MONTHS", 3):
            section, changed = collector.collect_bot({}, fetch, NOW, self.path)
        self.assertTrue(changed)
        self.assertEqual(section["latest_period"], "2026-08")
        self.assertEqual(section["published_at"], "2026-10-05T15:34:00+07:00")
        self.assertEqual(section["rows_written"], 48 + 1)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3 + 48)
        self.assertEqual(lines[0], EXISTING[0])  # same value and status: untouched
        self.assertEqual(lines[2], EXISTING[2])  # outside the window: never deleted
        revised = json.loads(lines[1])
        self.assertEqual(list(revised), list(json.loads(EXISTING[1])))
        self.assertEqual((revised["value"], revised["published_at"], revised["retrieved_at"]),
                         (2105.38, "2026-10-05T15:34:00+07:00", "2026-10-13T10:00:00+07:00"))
        self.assertIn(
            '{"metric": "hotel_occupancy", "region": "Thailand", "period": "2026-08", '
            '"value": 68.5, "unit": "percent", "source_url": "%s", '
            '"published_at": "2026-10-05T15:34:00+07:00", "retrieved_at": "2026-10-13T10:00:00+07:00", '
            '"status": "preliminary", "notes": "%s"}' % (collector.BOT_PAGE, collector.BOT_NOTES),
            lines)

        # Same file again: not re-parsed, nothing written, report page not requested.
        before = self.path.read_text(encoding="utf-8")
        fetch, calls = site()
        with patch.object(collector, "parse_bot", side_effect=AssertionError("re-parsed")):
            again, changed = collector.collect_bot(section, fetch, LATER, self.path)
        self.assertFalse(changed)
        self.assertEqual(again["rows_written"], 0)
        self.assertEqual(again["retrieved_at"], "2026-10-14T03:00:00+00:00")
        self.assertEqual(calls, [collector.BOT_FILE])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_rows_from_other_sources_keep_their_values(self):
        other = json.loads(EXISTING[0])
        other.update(source_url="https://example.org/manual-entry", value=2500.0)
        line = json.dumps(other, ensure_ascii=False)
        self.path.write_text(line + "\n", encoding="utf-8")
        fetch, _ = site()
        with patch.object(collector, "BOT_MONTHS", 2):
            collector.collect_bot({}, fetch, NOW, self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], line)  # same key, other source: untouched
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o644")
        bot = [(row["metric"], row["period"], row["value"], row["source_url"])
               for row in map(json.loads, lines[1:])]
        self.assertIn(("foreign_arrivals", "2026-07", 2545.62, collector.BOT_PAGE), bot)

    def test_malformed_indicator_file_is_not_rewritten(self):
        self.path.write_text(EXISTING[0] + "\n{broken\n", encoding="utf-8")
        fetch, _ = site()
        with self.assertRaises(ValueError):
            collector.collect_bot({}, fetch, NOW, self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), EXISTING[0] + "\n{broken\n")


class FxTests(unittest.TestCase):
    def test_ecb_cross_rates_use_one_reference_day(self):
        day, rates = collector.parse_ecb(ECB_CSV)
        self.assertEqual(day, "2026-10-12")
        self.assertEqual(rates["EUR"], 37.585)
        self.assertEqual(rates["USD"], 33.6151)
        self.assertEqual(rates["CNY"], 5.02258)
        self.assertEqual(rates["KRW"], 0.0252159)
        self.assertIsNone(rates["GBP"])  # not published that day: no carry-over

    def test_cbr_windows_1251_and_nominal(self):
        self.assertEqual(collector.parse_cbr(CBR_XML), ("2026-10-13", 2.50337))


# ---------------------------------------------------------------- network

class FakeResponse:
    def __init__(self, url, body=b"ok", headers=None):
        self.url, self.body, self.headers = url, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *details):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body


class HttpTests(unittest.TestCase):
    def test_honest_user_agent_timeout_and_last_modified(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.get_header("User-agent"), timeout))
            return FakeResponse(request.full_url, b"data", {"Last-Modified": "x"})
        with patch.object(collector.urllib.request, "urlopen", opener):
            self.assertEqual(collector.http_get(collector.MOTS % 817), (b"data", "x"))
        self.assertEqual(seen, [(collector.USER_AGENT, 30)])

    def test_one_retry_for_network_errors_none_for_refusals(self):
        attempts = []

        def flaky(request, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.URLError(socket.timeout("timed out"))
            return FakeResponse(request.full_url)
        with patch.object(collector.time, "sleep"), \
                patch.object(collector.urllib.request, "urlopen", flaky):
            self.assertEqual(collector.http_get(collector.ECB_DATA)[0], b"ok")
        self.assertEqual(len(attempts), 2)

        for code, expected in ((403, 1), (503, 2)):
            attempts.clear()

            def failing(request, timeout, code=code):
                attempts.append(1)
                raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)
            with patch.object(collector.time, "sleep"), \
                    patch.object(collector.urllib.request, "urlopen", failing):
                with self.assertRaises(urllib.error.HTTPError):
                    collector.http_get(collector.CBR_DATA)
            self.assertEqual(len(attempts), expected)

    def test_redirect_to_another_site_is_a_refusal(self):
        def paywall(request, timeout):
            return FakeResponse("https://paywall.example.com/pay?bot=1")
        with patch.object(collector.urllib.request, "urlopen", paywall):
            with self.assertRaises(collector.Refused) as caught:
                collector.http_get(collector.MOTS % 817)
        self.assertEqual(collector.short_error(caught.exception),
                         "refused: redirected to paywall.example.com")

    def test_short_errors(self):
        self.assertEqual(collector.short_error(forbidden("u")), "HTTP 403")
        self.assertEqual(collector.short_error(urllib.error.URLError(socket.timeout())), "timeout")
        self.assertEqual(collector.short_error(urllib.error.URLError("dns")), "network error")
        self.assertEqual(collector.http_time("Thu, 01 Oct 2026 03:40:30 GMT"),
                         "2026-10-01T03:40:30+00:00")
        self.assertIsNone(collector.http_time(None))


class DiscoveryTests(unittest.TestCase):
    def test_new_year_folder_is_preferred(self):
        index = INDEX_HTML.replace(b"816", b"900").replace(b"2569 (Tourism", b"2570 (Tourism")
        year = YEAR_HTML.replace(b"817", b"901")
        link = b'<a href="https://www.mots.go.th/images/v2022_aaaa-1111.xlsx">'
        fetch, _ = site(**{collector.MOTS % 411: (index, None), collector.MOTS % 900: (year, None),
                           collector.MOTS % 901: (link, None)})
        self.assertEqual(collector.mots_file_link("arrivals", {}, fetch),
                         (collector.MOTS % 901, "https://www.mots.go.th/images/v2022_aaaa-1111.xlsx"))

    def test_empty_new_folder_or_failed_index_fall_back(self):
        index = INDEX_HTML.replace(b"816", b"900").replace(b"2569 (Tourism", b"2570 (Tourism")
        year = YEAR_HTML.replace(b"817", b"901")
        fetch, _ = site(**{collector.MOTS % 411: (index, None), collector.MOTS % 900: (year, None),
                           collector.MOTS % 901: (b"<html>no file yet</html>", None)})
        previous = {"source_url": collector.MOTS % 817}
        self.assertEqual(collector.mots_file_link("arrivals", previous, fetch),
                         (collector.MOTS % 817, ARRIVALS_XLSX))
        fetch, _ = site(**{collector.MOTS % 411: forbidden("index")})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(collector.mots_file_link("provinces", {}, fetch),
                             (collector.MOTS % 820, PROVINCES_XLSX))
        self.assertIn("provinces: MOTS folder discovery failed (HTTP 403)", err.getvalue())


# ---------------------------------------------------------------- end to end

class MainTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.out = Path(self.folder.name) / "official.json"
        self.indicators = Path(self.folder.name) / "indicators.jsonl"
        self.indicators.write_text("\n".join(EXISTING) + "\n", encoding="utf-8")

    def tearDown(self):
        self.folder.cleanup()

    def run_main(self, fetch, argv=(), now=NOW):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = collector.main(list(argv), self.out, self.indicators, fetch, now)
        return code, out.getvalue(), err.getvalue()

    def document(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    def test_full_run_then_failures_keep_previous_sections(self):
        fetch, _ = site()
        code, out, _ = self.run_main(fetch)
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 5)
        first = self.document()
        self.assertEqual(list(first), ["built_at", "arrivals", "provinces", "bot", "fx", "errors"])
        self.assertEqual(first["built_at"], "2026-10-13T03:00:00+00:00")
        self.assertEqual(first["errors"], [])
        arrivals = first["arrivals"]
        self.assertEqual((arrivals["source"], arrivals["source_url"], arrivals["file_url"]),
                         ("MOTS", collector.MOTS % 817, ARRIVALS_XLSX))
        self.assertEqual(arrivals["published_at"], "2026-10-01T03:40:30+00:00")
        self.assertEqual(arrivals["retrieved_at"], "2026-10-13T03:00:00+00:00")
        self.assertEqual(len(arrivals["sha256"]), 64)
        self.assertEqual(first["provinces"]["note_ru"], collector.PROVINCE_NOTE)
        self.assertEqual(first["provinces"]["published_at"], "2026-09-14T08:46:04+00:00")
        self.assertEqual(first["bot"]["latest_period"], "2026-08")
        self.assertEqual(first["fx"]["ecb"]["thb_per"]["USD"], 33.6151)
        self.assertEqual(first["fx"]["cbr"]["rub_per_thb"], 2.50337)

        # MOTS refuses the arrivals file and ECB is down: both keep their old sections.
        fetch, _ = site(**{ARRIVALS_XLSX: forbidden(ARRIVALS_XLSX),
                           collector.ECB_DATA: urllib.error.URLError("down")})
        with patch.object(collector, "parse_provinces", side_effect=AssertionError("re-parsed")):
            code, _, err = self.run_main(fetch, now=LATER)
        self.assertEqual(code, 0)
        self.assertIn("arrivals: FAILED (HTTP 403)", err)
        second = self.document()
        self.assertEqual(second["arrivals"], arrivals)
        self.assertEqual(second["fx"]["ecb"], first["fx"]["ecb"])
        self.assertEqual(second["errors"], [
            {"source": "arrivals", "at": "2026-10-14T03:00:00+00:00", "error": "HTTP 403"},
            {"source": "ecb", "at": "2026-10-14T03:00:00+00:00", "error": "network error"}])
        provinces = dict(second["provinces"])
        self.assertEqual(provinces.pop("retrieved_at"), "2026-10-14T03:00:00+00:00")
        expected = dict(first["provinces"])
        expected.pop("retrieved_at")
        self.assertEqual(provinces, expected)

        # Everything down: exit 1, data still there, only this run's errors listed.
        down = urllib.error.URLError("down")
        fetch, _ = site(**{url: down for url in pages()})
        code, _, _ = self.run_main(fetch, now=LATER)
        self.assertEqual(code, 1)
        third = self.document()
        self.assertEqual([error["source"] for error in third["errors"]], list(collector.SOURCES))
        for name in ("arrivals", "provinces", "bot"):
            self.assertEqual(third[name], second[name])
        self.assertEqual(third["fx"], second["fx"])

    def test_only_selected_sources_are_fetched(self):
        fetch, calls = site()
        code, out, _ = self.run_main(fetch, ["--only", "ecb,cbr"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [collector.ECB_DATA, collector.CBR_DATA])
        document = self.document()
        self.assertIsNone(document["arrivals"])
        self.assertEqual(document["fx"]["ecb"]["date"], "2026-10-12")
        self.assertTrue(out.startswith("ecb: ECB 2026-10-12"))

    def test_older_file_never_replaces_newer_data(self):
        self.out.write_text(json.dumps({"arrivals": {"period": "2026-10", "sha256": "old"}}),
                            encoding="utf-8")
        fetch, _ = site()
        code, _, _ = self.run_main(fetch, ["--only", "arrivals"])
        self.assertEqual(code, 1)
        document = self.document()
        self.assertEqual(document["arrivals"], {"period": "2026-10", "sha256": "old"})
        self.assertIn("older than kept 2026-10", document["errors"][0]["error"])

    def test_incomplete_cached_section_is_parsed_again(self):
        fetch, _ = site()
        digest = collector.hashlib.sha256(fetch(ARRIVALS_XLSX)[0]).hexdigest()
        self.out.write_text(json.dumps({"arrivals": {"period": "2026-02", "sha256": digest}}),
                            encoding="utf-8")
        code, out, _ = self.run_main(fetch, ["--only", "arrivals"])
        self.assertEqual(code, 0)
        self.assertEqual(self.document()["arrivals"]["total"]["value"], 4960)
        self.assertNotIn("same file", out)

    def test_unreadable_output_is_left_alone(self):
        self.out.write_text("{not json", encoding="utf-8")
        fetch, calls = site()
        code, _, _ = self.run_main(fetch)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(self.out.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
