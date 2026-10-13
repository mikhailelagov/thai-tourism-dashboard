"""Offline checks for the data.json model: holidays, missing rain, FX and fallbacks."""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_data as bd  # noqa: E402

NOW = datetime(2026, 10, 13, 1, 30, tzinfo=timezone.utc)  # 08:30 Bangkok
DAYS = [(date(2026, 10, 13) + timedelta(days=i)).isoformat() for i in range(8)]


def vevent(day, summary, description):
    return ("BEGIN:VEVENT\r\nDTSTART;VALUE=DATE:%s\r\nDESCRIPTION:%s\r\n"
            "SUMMARY:%s\r\nEND:VEVENT\r\n" % (day, description, summary))


TH_ICS = "BEGIN:VCALENDAR\r\n" + "".join([
    vevent("20261013", "Anniversary of the Death of King Bhumibol", "Public holiday"),
    # folded DESCRIPTION line, as Google wraps it at 75 octets
    vevent("20261014", "Valentine's Day",
           "Observance\\nTo hide observances\\, go to Google Calendar Setting\r\n s > Holidays in Thailand"),
    vevent("20261015", "Chinese New Year's Day", "Observance\\nTo hide observances"),
    vevent("20261016", "Makha Bucha", "Public holiday"),
    vevent("20261017", "Day off for Visakha Bucha", "Public holiday"),
    vevent("20260729", "Asalha Bucha", "Public holiday"),
    vevent("20261205", "King Bhumibol's Birthday", "Public holiday"),
]) + "END:VCALENDAR\r\n"

IN_ICS = "BEGIN:VCALENDAR\r\n" + "".join([
    vevent("20261013", "Maha Saptami", "Observance\\nTo hide observances"),
    vevent("20261014", "Dussehra", "Public holiday"),
    vevent("20261014", "Dussehra (second)", "Public holiday"),
    vevent("20261015", "State Day", "Public holiday in Kerala\\, Goa"),
]) + "END:VCALENDAR\r\n"

OTHER_ICS = ("BEGIN:VCALENDAR\r\n" + vevent("20260101", "New Year's Day", "Public holiday")
             + "END:VCALENDAR\r\n")

ECB_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
    "EXR.D.THB.EUR.SP00.A,D,THB,EUR,SP00,A,2026-10-08,38.0\n"
    "EXR.D.THB.EUR.SP00.A,D,THB,EUR,SP00,A,2026-10-09,37.8\n"
    "EXR.D.THB.EUR.SP00.A,D,THB,EUR,SP00,A,2026-10-12,37.585\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-08,1.12\n"
    # no USD fixing on 10-09: the cross for that day must not borrow another day
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-12,1.1181\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-10-13,1.2\n"
)

CBR_XML = (
    '<?xml version="1.0" encoding="windows-1251"?>'
    '<ValCurs ID="R01675" name="Foreign Currency Market Dynamic">'
    '<Record Date="10.10.2026" Id="R01675"><Nominal>10</Nominal><Value>25,3009</Value></Record>'
    '<Record Date="13.10.2026" Id="R01675"><Nominal>10</Nominal><Value>25,0337</Value></Record>'
    '</ValCurs>').encode("cp1251")

ER_JSON = json.dumps({
    "result": "success", "time_last_update_utc": "Mon, 12 Oct 2026 23:57:31 +0000",
    "rates": {"USD": 1, "THB": 33.53, "RUB": 85.0, "EUR": 0.89, "XAU": 0.0004}})


def ecb_series(start, n):
    """n business days of THB, USD and CNY per EUR, with the baht slowly weakening."""
    rows, day, i = [], start, 0
    while i < n:
        if day.weekday() < 5:
            iso = day.isoformat()
            rows.append("x,D,THB,EUR,SP00,A,%s,%s" % (iso, 37.0 + i * 0.02))
            rows.append("x,D,USD,EUR,SP00,A,%s,1.10" % iso)
            rows.append("x,D,CNY,EUR,SP00,A,%s,7.50" % iso)
            i += 1
        day += timedelta(days=1)
    return "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n" + "\n".join(rows)


def fred_csv(start, n, value=100.0, last=None):
    rows, day = ["observation_date,DCOILBRENTEU"], start
    for i in range(n):
        rows.append("%s,%s" % (day.isoformat(), last if (last and i == n - 1) else value))
        rows.append("%s," % (day + timedelta(days=1)).isoformat())  # holiday gap
        day += timedelta(days=2)
    return "\n".join(rows) + "\n"


def weather_json(mm=5.0, pop=50, none_day=None):
    blocks = []
    for _ in bd.REGIONS:
        mms = [mm] * 8
        pops = [pop] * 8
        if none_day is not None:
            mms[none_day] = None
        blocks.append({"daily": {"time": DAYS, "temperature_2m_max": [31.0] * 8,
                                 "precipitation_sum": mms,
                                 "precipitation_probability_max": pops}})
    return json.dumps(blocks)


class FakeFetch:
    """Answers by URL prefix; anything listed in `fail` raises like a dead source."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.responses = {
            "weather": weather_json(none_day=2).encode(),
            "fx": ER_JSON.encode(),
            "ecb": ecb_series(date(2026, 8, 3), 50).encode(),
            "cbr": CBR_XML,
            "brent": fred_csv(date(2026, 9, 1), 20, 100.0, last=110.0).encode(),
        }

    def __call__(self, url):
        key = self.key(url)
        if key in self.fail or key.split(":")[0] in self.fail:
            raise RuntimeError("%s: HTTP 503" % url.split("?")[0])
        if key.startswith("holidays:"):
            code = key.split(":")[1]
            return {"TH": TH_ICS, "IN": IN_ICS}.get(code, OTHER_ICS).encode()
        return self.responses[key]

    @staticmethod
    def key(url):
        if "open-meteo" in url:
            return "weather"
        if "er-api" in url:
            return "fx"
        if "ecb.europa" in url:
            return "ecb"
        if "cbr.ru" in url:
            return "cbr"
        if "fred.stlouisfed" in url:
            return "brent"
        for code, feed in bd.HOLIDAY_FEEDS.items():
            if feed + "%23" in url:
                return "holidays:" + code
        raise AssertionError(url)


class HolidayTests(unittest.TestCase):
    def test_ics_keeps_type_and_unfolds_description(self):
        events = {e["name"]: e for e in bd.parse_ics(TH_ICS)}
        self.assertEqual(events["Anniversary of the Death of King Bhumibol"]["type"], "public_holiday")
        self.assertEqual(events["Valentine's Day"]["type"], "observance")
        regional = list(bd.parse_ics(IN_ICS))[-1]
        self.assertEqual((regional["type"], regional["where"]), ("regional_holiday", "Kerala, Goa"))

    def test_russian_names(self):
        self.assertEqual(bd.holiday_ru("Anniversary of the Death of King Bhumibol"),
                         "День памяти короля Пхумипона")
        self.assertEqual(bd.holiday_ru("King Bhumibol's Birthday"),
                         "День рождения короля Пхумипона (День отца)")
        self.assertEqual(bd.holiday_ru("Chinese New Year's Day"), "Китайский Новый год")
        self.assertEqual(bd.holiday_ru("Third Day of Chinese New Year"), "Китайский Новый год")
        self.assertEqual(bd.holiday_ru("New Year's Day"), "Новый год")
        self.assertEqual(bd.holiday_ru("Day off for Visakha Bucha"),
                         "Висакха Буча (перенесённый выходной)")
        self.assertEqual(bd.holiday_ru("Bridge Public Holiday"), "Дополнительный выходной")

    def test_alcohol_ban_days(self):
        self.assertTrue(bd.alcohol_ban_day("Makha Bucha"))
        self.assertTrue(bd.alcohol_ban_day("Ok Phansa (End of Buddhist Lent)"))
        self.assertFalse(bd.alcohol_ban_day("Day off for Visakha Bucha"))
        self.assertFalse(bd.alcohol_ban_day("Asalha Bucha observed"))
        self.assertFalse(bd.alcohol_ban_day("Chulalongkorn Day"))

    def test_lent_days_derived_from_asalha_bucha(self):
        derived = {e["iso"]: e for e in bd.derived_lent_days(list(bd.parse_ics(TH_ICS)))}
        self.assertEqual(sorted(derived), ["2026-07-30", "2026-10-26"])
        self.assertTrue(all(bd.alcohol_ban_day(e["name"]) for e in derived.values()))
        self.assertEqual(derived["2026-10-26"]["derived_from"], "Asalha Bucha 2026-07-29")

    def test_scoring_weights_only_nationwide_public_holidays(self):
        errors = []
        fetch = FakeFetch()
        holidays, ok = bd.fetch_holidays(set(DAYS), fetch, {}, errors, "t")
        region = bd.REGIONS[1]

        def day(iso):
            return bd.score_day(region, iso, 0.0, 0, 30, holidays.get(iso, []), [], 0.0, 0.0)

        memorial, valentine, cny, makha, day_off = (day(DAYS[i]) for i in range(5))
        self.assertEqual(memorial["parts"]["holiday_th"], 6.0)
        self.assertEqual(memorial["event"]["ru"], "День памяти короля Пхумипона")
        self.assertEqual(valentine["parts"]["holiday_th"], 0.0)
        self.assertIsNone(valentine["event"])
        self.assertEqual(cny["parts"]["holiday_th"], 0.0)
        self.assertEqual([h["type"] for h in holidays[DAYS[1]] if h["country"] == "TH"],
                         ["observance"])
        # a ban day is named and flagged, but never adds points
        self.assertTrue(makha["alcohol_ban"])
        self.assertEqual(makha["parts"]["holiday_th"], 0.0)
        self.assertTrue(makha["event"]["alcohol_ban"])
        self.assertIn("алкогол", makha["alcohol_note_ru"])
        self.assertFalse(day_off["alcohol_ban"])
        self.assertEqual(day_off["parts"]["holiday_th"], 6.0)
        # IN: observance 0, two public holidays the same day count as one country,
        # a regional holiday counts as nothing
        self.assertEqual(memorial["parts"]["holiday_abroad"], 0.0)
        self.assertEqual(valentine["parts"]["holiday_abroad"], 1.5)
        self.assertEqual(cny["parts"]["holiday_abroad"], 0.0)


class MissingWeatherTests(unittest.TestCase):
    def test_missing_rain_is_null_not_dry(self):
        region = bd.REGIONS[1]
        day = bd.score_day(region, DAYS[0], None, 80, 31.0, [], [], 0.0, 0.0)
        self.assertIsNone(day["mm"])
        self.assertTrue(day["missing"])
        self.assertEqual(day["missing_fields"], ["mm"])
        self.assertIsNone(day["score"])
        self.assertIsNone(day["parts"]["weather"])
        dry = bd.score_day(region, DAYS[0], 0.0, 0, 31.0, [], [], 0.0, 0.0)
        self.assertFalse(dry["missing"])
        self.assertEqual(dry["score"], 30)

    def test_value_at_never_defaults_to_zero(self):
        daily = {"precipitation_sum": [1.5, None]}
        self.assertEqual(bd.value_at(daily, "precipitation_sum", 0), 1.5)
        self.assertIsNone(bd.value_at(daily, "precipitation_sum", 1))
        self.assertIsNone(bd.value_at(daily, "precipitation_sum", 5))
        self.assertIsNone(bd.value_at(daily, "precipitation_probability_max", 0))


class MarketTests(unittest.TestCase):
    def test_er_api_keeps_provider_time(self):
        rates, when = bd.parse_er_api(ER_JSON)
        self.assertEqual(when, "2026-10-12T23:57:31+00:00")
        self.assertEqual(rates["THB"], 33.53)
        self.assertNotIn("XAU", rates)

    def test_provider_time_always_has_an_offset(self):
        self.assertEqual(bd.provider_time({"time_last_update_utc": "Mon, 12 Oct 2026 23:57:31 -0000"}),
                         "2026-10-12T23:57:31+00:00")
        self.assertEqual(bd.provider_time({"time_last_update_utc": "garbage",
                                           "time_last_update_unix": 1791849451}),
                         "2026-10-12T23:57:31+00:00")
        self.assertIsNone(bd.provider_time({}))

    def test_ecb_crosses_align_by_date(self):
        crosses = bd.thb_crosses(bd.parse_ecb_csv(ECB_CSV))
        self.assertEqual(sorted(crosses["USD"]), ["2026-10-08", "2026-10-12"])
        self.assertAlmostEqual(crosses["USD"]["2026-10-12"], 37.585 / 1.1181)
        self.assertEqual(crosses["EUR"]["2026-10-09"], 37.8)

    def test_cbr_parses_comma_decimals_and_nominal(self):
        series = bd.parse_cbr_dynamic(CBR_XML)
        self.assertAlmostEqual(series["2026-10-13"], 1 / 2.50337)
        self.assertEqual(sorted(series), ["2026-10-10", "2026-10-13"])

    def test_fred_skips_holidays_and_baseline_uses_dates(self):
        series = bd.parse_fred_csv(fred_csv(date(2026, 9, 1), 6, 100.0, last=110.0),
                                   "DCOILBRENTEU")
        self.assertEqual(len(series), 6)
        # last point 2026-09-11; the 7 days before hold 09-05, 09-07, 09-09
        self.assertAlmostEqual(bd.baseline_change(series, 7, 3), 0.10)
        self.assertIsNone(bd.baseline_change(series, 7, 4))
        with self.assertRaises(ValueError):
            bd.parse_fred_csv("DATE,OTHER\n2026-01-01,1\n", "DCOILBRENTEU")

    def test_weights_from_official_arrivals(self):
        official = {"arrivals": {
            "source": "MOTS", "source_url": "https://example.org/817", "period": "2026-09",
            "status": "preliminary", "total": {"value": 2000000},
            "markets": [{"name": "China", "value": 349182},
                        {"name": "Russian Federation", "value": 81744},
                        {"name": "Korea (Republic of)", "value": 71000},
                        {"name": "Germany", "value": 42454},
                        {"name": "France", "value": 20000}]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "official.json"
            path.write_text(json.dumps(official), encoding="utf-8")
            weights, meta = bd.market_weights(path)
            missing_weights, missing_meta = bd.market_weights(Path(tmp) / "none.json")
        self.assertEqual(weights["CNY"], 17.46)
        self.assertEqual(weights["RUB"], 4.09)
        self.assertEqual(weights["KRW"], 3.55)
        self.assertEqual(weights["EUR"], 3.12)
        self.assertIsNone(weights["INR"])
        self.assertEqual((meta["kind"], meta["period"], meta["source"]),
                         ("arrivals_share_pct", "2026-09", "MOTS"))
        self.assertEqual(missing_meta["kind"], "fixed_constants")
        self.assertEqual(missing_weights["CNY"], 2.23)

    def test_malformed_official_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "official.json"
            path.write_text(json.dumps({"arrivals": {"total": {"value": 10}, "markets": [None]}}),
                            encoding="utf-8")
            self.assertEqual(bd.market_weights(path)[1]["kind"], "fixed_constants")

    def test_composite_skips_markets_without_weight(self):
        pp = {"CNY": {"delta": 0.02, "share": 3.0}, "INR": {"delta": 0.5, "share": None},
              "RUB": {"delta": -0.01, "share": 1.0}}
        self.assertEqual(bd.composite(pp), 0.0125)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "data.json"
        self.events = self.dir / "events.json"
        self.events.write_text("[]", encoding="utf-8")
        self.official = self.dir / "official.json"  # absent: fallback weights

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, fetch, now=NOW):
        with patch("sys.stdout"), patch("sys.stderr"):
            return bd.build(now=now, fetch=fetch, out=self.out,
                            official=self.official, events_file=self.events)

    def test_full_build_shape(self):
        ok = self.run_build(FakeFetch())
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(ok, 5 + len(bd.HOLIDAY_FEEDS))
        self.assertEqual(data["score_kind"], "weather_calendar_model")
        self.assertEqual(data["score_note_ru"],
                         "Модель условий из погоды, праздников, цены нефти и курсов валют, а не измерение спроса")
        for key in ("built_at", "regions", "fx", "markets", "holidays"):
            self.assertIn(key, data)
        self.assertEqual(data["fx_time"], "2026-10-12T23:57:31+00:00")
        self.assertEqual(data["fx_source"], "ExchangeRate-API (open.er-api.com)")
        self.assertEqual(data["built_at"], "2026-10-13T01:30:00+00:00")
        self.assertFalse(data["stale"])
        self.assertEqual(data["errors"], [])

        pattaya = data["regions"]["pattaya"]
        self.assertEqual(len(pattaya), 8)
        self.assertTrue(pattaya[2]["missing"])
        self.assertIsNone(pattaya[2]["mm"])
        self.assertIsNone(pattaya[2]["score"])
        self.assertTrue(set(pattaya[0]) >= {"iso", "tmax", "mm", "pop", "score", "parts", "event"})

        m = data["markets"]
        self.assertEqual(m["brent"], 110.0)
        self.assertEqual(m["brent_date"], "2026-10-09")
        self.assertAlmostEqual(m["brent_delta"], 0.1)
        self.assertEqual(m["usdthb_date"], max(bd.thb_crosses(bd.parse_ecb_csv(
            FakeFetch().responses["ecb"].decode()))["USD"]))
        self.assertEqual(m["purchasing_power"]["CNY"]["source"], "ECB")
        self.assertEqual(m["weights"]["kind"], "fixed_constants")
        self.assertIn("pp_composite", m)
        # the two-day CBR fixture is too thin for a 30-day baseline: no RUB, not zero
        self.assertNotIn("RUB", m["purchasing_power"])

    def test_failed_sources_keep_previous_sections(self):
        self.run_build(FakeFetch())
        first = json.loads(self.out.read_text(encoding="utf-8"))
        later = NOW + timedelta(days=1)
        ok = self.run_build(FakeFetch(fail={"weather", "fx", "brent", "holidays"}), now=later)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(ok, 2)  # ecb + cbr
        self.assertEqual(data["regions"], first["regions"])
        self.assertEqual(data["built_at"], first["built_at"])
        self.assertTrue(data["stale"])
        self.assertEqual(data["fx"], first["fx"])
        self.assertEqual(data["fx_time"], first["fx_time"])
        self.assertEqual(data["markets"]["brent"], first["markets"]["brent"])
        self.assertEqual(data["markets"]["brent_date"], first["markets"]["brent_date"])
        self.assertEqual(data["holidays"][DAYS[3]], first["holidays"][DAYS[3]])
        failed = {e["source"] for e in data["errors"]}
        self.assertTrue({"weather", "fx", "brent", "holidays:TH"} <= failed)
        self.assertTrue(all(e["at"].endswith("+00:00") for e in data["errors"]))
        self.assertEqual(data["sources"]["weather"]["retrieved_at"], first["built_at"])
        self.assertFalse(data["sources"]["weather"]["ok"])

    def test_all_sources_failed_exits_1_and_keeps_data(self):
        self.run_build(FakeFetch())
        first = json.loads(self.out.read_text(encoding="utf-8"))
        everything = {"weather", "fx", "ecb", "cbr", "brent", "holidays"}
        real_build = bd.build

        def failing_build():
            return real_build(now=NOW + timedelta(days=1), fetch=FakeFetch(fail=everything),
                              out=self.out, official=self.official, events_file=self.events)

        with patch.object(bd, "build", failing_build), patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(bd.main(), 1)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["errors"]), 5 + len(bd.HOLIDAY_FEEDS))
        self.assertEqual(data["regions"], first["regions"])
        self.assertEqual(data["markets"]["purchasing_power"],
                         first["markets"]["purchasing_power"])

    def test_undated_yahoo_markets_are_not_carried(self):
        self.out.write_text(json.dumps({"markets": {"brent": 104.59, "brent_delta": 0.02,
                                                    "usdthb": 33.58}}), encoding="utf-8")
        self.run_build(FakeFetch(fail={"brent", "ecb", "cbr"}))
        markets = json.loads(self.out.read_text(encoding="utf-8"))["markets"]
        self.assertNotIn("brent", markets)
        self.assertNotIn("usdthb", markets)

    def test_undated_fx_snapshot_is_not_carried(self):
        self.out.write_text(json.dumps({"fx": {"THB": 33.0, "USD": 1}}), encoding="utf-8")
        self.run_build(FakeFetch(fail={"fx"}))
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertNotIn("fx", data)
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o644")


if __name__ == "__main__":
    unittest.main()
