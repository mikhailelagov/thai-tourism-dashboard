"""Regression checks for evidence boundaries and Bangkok report dates."""

import json
import tempfile
import unittest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from morning_brief import (arrivals_lines, camera_lines, currency_lines, indicator_lines, news_lines,
                           provinces_lines, render_report, write_reports)

NBSP = "\u00a0"
# 13 October 2026, 10:00 in Bangkok: the September arrivals file is out, the
# September provincial file is not yet.
OCTOBER = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
MOTS = "https://www.mots.go.th/news/category/"


def official_fixture():
    """Shape of tourism/official.json with values from the real MOTS/ECB/CBR files."""
    return {
        "built_at": "2026-10-13T02:00:00+00:00",
        "arrivals": {
            "source": "MOTS", "source_url": MOTS + "817",
            "file_url": "https://www.mots.go.th/images/v2022_cf32d908-111e-4269-802d-18807cd65884.xlsx",
            "published_at": "2026-10-01T03:40:30+00:00", "retrieved_at": "2026-10-13T02:00:00+00:00",
            "period": "2026-09", "status": "preliminary",
            "total": {"value": 2000339, "prev_year": 2235852, "yoy_pct": -10.53},
            "ytd": {"from": "2026-01", "to": "2026-09", "value": 23199306, "prev_year": 24115325, "yoy_pct": -3.8},
            "markets": [
                {"name": "China", "ru": "Китай", "value": 349182, "prev_year": 321309, "yoy_pct": 8.67},
                {"name": "Russian Federation", "ru": "Россия", "value": 81744, "prev_year": 76907, "yoy_pct": 6.29},
                {"name": "Korea (Republic of)", "ru": "Южная Корея", "value": 70997, "prev_year": 101672,
                 "yoy_pct": -30.17},
                {"name": "Kazakhstan ", "ru": "Казахстан", "value": 6371, "prev_year": None, "yoy_pct": None},
            ],
        },
        "provinces": {
            "source": "MOTS", "source_url": MOTS + "820", "file_url": "https://www.mots.go.th/images/v2022_x.xlsx",
            "published_at": "2026-09-14T08:46:04+00:00", "retrieved_at": "2026-10-13T02:00:00+00:00",
            "period": "2026-08", "status": "preliminary",
            "rows": {
                "pattaya": {"province_th": "ชลบุรี", "label_ru": "Чонбури (включая Паттайю)",
                            "occupancy_pct": 74.99, "occupancy_prev_pct": 77.7, "occupancy_change_pp": -2.71,
                            "foreign_visitors": 804858, "foreign_yoy_pct": -4.64, "thai_visitors": 1500000,
                            "thai_yoy_pct": 1.2, "revenue_mb": 25276.67, "revenue_yoy_pct": -4.47},
                "chiangmai": {"label_ru": "Чиангмай", "occupancy_pct": 70.02, "occupancy_prev_pct": 68.53,
                              "foreign_visitors": 252928, "foreign_yoy_pct": 7.49},
                "thailand": {"label_ru": "Весь Таиланд", "occupancy_pct": 68.5},
            },
            "note_ru": "Провинция шире курорта; посещения провинций нельзя складывать и сравнивать с прибытиями в страну.",
        },
        "bot": {"source": "BOT", "source_url": "https://app.bot.or.th/BTWS_STAT/statistics/ReportPage.aspx?reportID=875",
                "retrieved_at": "2026-10-13T02:00:00+00:00", "published_at": "2026-09-30T07:30:00+00:00",
                "latest_period": "2026-08", "rows_written": 4},
        "fx": {
            "ecb": {"source": "ECB", "source_url": "https://data-api.ecb.europa.eu/service/data/EXR/", "date": "2026-10-12",
                    "thb_per": {"USD": 33.6151, "EUR": 37.585, "CNY": 5.0226, "INR": 0.35097,
                                "KRW": 0.025216, "MYR": 8.231, "GBP": 44.46}},
            "cbr": {"source": "Bank of Russia", "source_url": "https://www.cbr.ru/scripts/XML_daily.asp",
                    "date": "2026-10-14", "rub_per_thb": 2.50337},
        },
        "errors": [],
    }


def news_item(title, topics, hours_ago, **extra):
    published = (OCTOBER - timedelta(hours=hours_ago)).isoformat()
    item = {"source": "The Pattaya News", "title": title, "url": "https://thepattayanews.com/" + str(hours_ago),
            "published_at": published, "lang": "en", "topics": topics, "official": False}
    item.update(extra)
    return item


class MorningBriefTests(unittest.TestCase):
    def setUp(self):
        # Already 6 September in Bangkok, still 5 September UTC.
        self.now = datetime(2026, 9, 5, 18, 30, tzinfo=timezone.utc)

    def test_missing_and_experimental_are_not_zero_but_valid_zero_is_reported(self):
        common = {"observed_at": "2026-09-05T18:00:00Z", "series_version": "v1"}
        footfall = {"rows": [{"at": "2026-09-05T18:05:00Z", "cams": {
            "valid": dict(common, region="pattaya", title="Проверенная", status="ok", people=0),
            "old": dict(common, region="phuket", title="Старая", people=0),
            "preview": dict(common, region="phuket", title="Превью", status="experimental", people=0),
            "blocked": dict(common, region="bangkok", status="unavailable", people=None,
                            reason="HTTP 403: требуется доступ"),
        }}]}
        text = "\n".join(camera_lines(footfall, {"pattaya": "Паттайя", "phuket": "Пхукет", "bangkok": "Бангкок"}, self.now))
        self.assertIn("Проверенная: 0 чел. в кадре", text)
        self.assertIn("Камер: 1; часов с хотя бы одним проверенным кадром: 1", text)
        self.assertNotIn("Старая: 0", text)
        self.assertNotIn("Превью: 0", text)
        self.assertIn("HTTP 403: требуется доступ", text)
        self.assertEqual(text.count("Нет проверенных наблюдений"), 2)

    def test_stale_future_and_unversioned_camera_frames_excluded(self):
        common = {"region": "pattaya", "status": "ok", "people": 9, "series_version": "v1"}
        footfall = {"rows": [{"at": "2026-09-05T18:05:00Z", "cams": {
            "stale": dict(common, observed_at="2026-09-04T18:00:00Z"),
            "future": dict(common, observed_at="2026-09-06T18:00:00Z"),
            "no_version": dict(common, observed_at="2026-09-05T18:00:00Z", series_version=None),
            "bool": dict(common, observed_at="2026-09-05T18:00:00Z", people=True),
        }}]}
        text = "\n".join(camera_lines(footfall, {"pattaya": "Паттайя"}, self.now))
        self.assertIn("Нет проверенных наблюдений", text)
        self.assertNotIn("9 чел.", text)
        self.assertIn("замер исключён", text)

    def test_rolling_day_coverage_never_exceeds_24_intervals(self):
        rows = []
        for hour in range(25):
            when = (self.now - timedelta(hours=hour)).isoformat()
            rows.append({"at": when, "cams": {"camera": {
                "region": "pattaya", "observed_at": when, "status": "ok",
                "people": 0, "series_version": "v1",
            }}})
        text = "\n".join(camera_lines({"rows": rows}, {"pattaya": "Паттайя"}, self.now))
        self.assertIn("часов с хотя бы одним проверенным кадром: 24", text)
        self.assertNotIn("из 24", text)

    def test_modern_rows_hide_legacy_cameras_and_explain_current_sources(self):
        rows = [
            {"at": "2026-09-05T17:00:00Z", "cams": {
                "old-windy": {"region": "pattaya", "title": "Старая камера", "people": 0}}},
            {"schema_version": 2, "at": "2026-09-05T18:00:00Z", "cams": {
                "street": {"region": "pattaya", "title": "Walking Street", "status": "unavailable",
                           "people": None, "reason": "provider_access_required"},
                "pier": {"region": "pattaya", "title": "Пирс", "status": "experimental",
                         "people": None, "reason": "camera_not_validated"},
            }},
        ]
        text = "\n".join(camera_lines({"rows": rows}, {"pattaya": "Паттайя"}, self.now))
        self.assertNotIn("Старая камера", text)
        self.assertNotIn("old-windy", text)
        self.assertNotIn("provider_access_required", text)
        self.assertNotIn("camera_not_validated", text)
        self.assertIn("Walking Street — недоступен: источник требует доступ через браузер", text)
        self.assertIn("Пирс — экспериментальный: детализация кадра и область подсчёта ещё не проверены", text)

    def test_weather_window_uses_bangkok_today_and_reports_staleness(self):
        data = {"built_at": "2026-09-03T12:00:00Z", "regions": {"pattaya": [
            {"iso": "2026-09-05", "tmax": 99, "mm": 0, "pop": 0},
            {"iso": "2026-09-06", "tmax": 31, "mm": 0, "pop": 20, "score": 999},
            {"iso": "2026-09-07", "tmax": 32, "mm": 10, "pop": 70},
            {"iso": "2026-09-09", "tmax": 88, "mm": 0, "pop": 0},
        ]}, "fx": {"THB": 32, "USD": 1}}
        text = render_report(data, {}, [], self.now)
        self.assertIn("06.09.2026", text)
        self.assertIn("Период 06.09–08.09", text)
        self.assertIn("Погода устарела", text)
        self.assertIn("06.09: осадки мало ограничивают прогулки, до 31 °C", text)
        self.assertIn("08.09: данных нет", text)
        self.assertNotIn("99 °C", text)
        self.assertNotIn("88 °C", text)
        self.assertNotIn("999", text)
        self.assertIn("Свежесть курсов не подтверждена", text)

    def test_indicators_choose_latest_published_period_and_keep_scope(self):
        base = {"metric": "hotel_occupancy", "region": "South", "unit": "percent",
                "source_url": "https://app.bot.or.th/example", "status": "preliminary"}
        rows = [
            dict(base, period="2026-06", value=60, published_at="2026-07-31T14:30:00+07:00"),
            dict(base, period="2026-07", value=71.65, published_at="2026-08-31T14:30:00+07:00"),
            dict(base, period="2026-08", value=99, published_at="2026-09-30T14:30:00+07:00"),
        ]
        text = "\n".join(indicator_lines(rows, self.now))
        self.assertIn("Южный регион: 71,65%", text)
        self.assertIn("период 2026-07", text)
        self.assertNotIn("период 2026-06", text)
        self.assertNotIn("99%", text)
        self.assertIn("задержка публикации 31 дн.", text)
        self.assertIn("от конца периода до отчёта 37 дн.", text)
        self.assertIn("Предварительное бронирование на 3 месяца:** данных нет", text)
        north = "\n".join(indicator_lines([dict(base, region="North", period="2026-08", value=60.25,
                                                  published_at="2026-09-01T08:34:00+00:00")], self.now))
        self.assertIn("Северный регион: 60,25%", north)
        self.assertIn("«Север» — отдельно Чиангмай", north)

    def test_missing_files_write_both_reports_with_actual_bangkok_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            latest, dated = write_reports(root, self.now)
            self.assertEqual(dated.name, "2026-09-06-tourism.md")
            self.assertEqual(latest.name, "tourism-latest.md")
            self.assertEqual(latest.read_text(), dated.read_text())
            text = latest.read_text()
            self.assertIn("Доступных наблюдений курса нет", text)
            self.assertIn("Нет доступных опубликованных значений", text)
            self.assertIn("Свежесть погоды не подтверждена", text)
            self.assertNotIn("0 чел. в кадре", text)
            self.assertIn("Нет доступных данных MOTS о прибытиях", text)
            self.assertIn("Нет доступных данных MOTS по провинциям", text)
            self.assertIn("Новости не загружены", text)


class OfficialDataTests(unittest.TestCase):
    def test_arrivals_show_period_yoy_ytd_key_markets_and_publication_age(self):
        text = "\n".join(arrivals_lines(official_fixture(), OCTOBER))
        self.assertIn(f"[MOTS]({MOTS}817)", text)
        self.assertIn("период сентябрь 2026 (2026-09), предварительные данные", text)
        self.assertIn("опубликовано 01.10.2026 10:40 (Бангкок), 12 дн. назад; задержка публикации 1 дн.", text)
        self.assertIn(f"**Сентябрь 2026: 2{NBSP}000{NBSP}339** прибытий, −10,5% г/г (2025: 2{NBSP}235{NBSP}852)", text)
        self.assertIn(f"**С начала года (01–09.2026): 23{NBSP}199{NBSP}306**, −3,8% г/г", text)
        self.assertIn(f"| Россия | 81{NBSP}744 | +6,3% |", text)
        self.assertIn(f"| Южная Корея | 70{NBSP}997 | −30,2% |", text)
        self.assertIn(f"| Казахстан | 6{NBSP}371 | — |", text)
        self.assertIn("| Израиль | нет данных | — |", text)
        self.assertLess(text.index("Россия"), text.index("Китай"))
        self.assertNotIn("⚠", text)

    def test_overdue_month_and_failed_refresh_keep_previous_data_with_warning(self):
        official = official_fixture()
        official["errors"] = [{"source": "arrivals", "at": "2026-11-20T02:00:00+00:00", "error": "http_403 <b>"}]
        text = "\n".join(arrivals_lines(official, OCTOBER + timedelta(days=38)))
        self.assertIn(f"2{NBSP}000{NBSP}339", text)
        self.assertIn("От конца периода прошло 51 дн.", text)
        self.assertIn("обновление источника MOTS (прибытия) не удалось (20.11.2026 09:00, Бангкок): http\\_403 \\<b\\>", text)

    def test_missing_or_malformed_official_sections_are_not_guessed(self):
        broken = {"arrivals": {"period": "сентябрь", "total": {"value": "2000339"}},
                  "provinces": {"period": "2026-08", "rows": []}, "fx": {"ecb": {"thb_per": {"USD": -1}}},
                  "errors": [{"source": "provinces", "at": None, "error": None}]}
        text = render_report({}, {}, [], OCTOBER, official=broken, news={"items": "x"})
        self.assertIn("Нет доступных данных MOTS о прибытиях", text)
        self.assertIn("Нет доступных данных MOTS по провинциям", text)
        self.assertIn("обновление источника MOTS (провинции) не удалось (неизвестно, Бангкок): причина не указана", text)
        self.assertIn("Доступных наблюдений курса нет", text)
        self.assertNotIn("2000339", text)

    def test_provinces_table_keeps_province_scope_and_missing_cells(self):
        text = "\n".join(provinces_lines(official_fixture(), OCTOBER))
        self.assertIn("период август 2026 (2026-08)", text)
        self.assertIn("29 дн. назад", text)
        self.assertNotIn("⚠", text)
        self.assertIn(f"| Чонбури (включая Паттайю) | 74,99% (−2,71 п.п. г/г) | 804{NBSP}858 (−4,6% г/г) | "
                      f"−4,5% г/г (25{NBSP}277 млн бат) |", text)
        self.assertIn(f"| Чиангмай | 70,02% (+1,49 п.п. г/г) | 252{NBSP}928 (+7,5% г/г) | нет данных |", text)
        self.assertIn("| Сураттани (включая Самуи) | нет данных | нет данных | нет данных |", text)
        self.assertIn("| Весь Таиланд | 68,5% | нет данных | нет данных |", text)
        self.assertIn("Провинция шире курорта", text)
        self.assertLess(text.index("Чонбури"), text.index("Весь Таиланд"))

    def test_bot_check_time_and_error_are_shown_with_indicators(self):
        official = official_fixture()
        official["errors"] = [{"source": "bot", "at": "2026-10-13T02:00:00+00:00", "error": "network_error"}]
        text = "\n".join(indicator_lines([], OCTOBER, official))
        self.assertIn("Последняя проверка [BOT](https://app.bot.or.th/", text)
        self.assertIn("13.10.2026 09:00 (Бангкок); последний период в отчёте: 2026-08", text)
        self.assertIn("network\\_error", text)
        self.assertIn("Нет доступных опубликованных значений", text)

    def test_official_fx_replaces_aggregator_and_keeps_precision(self):
        data = {"built_at": "2026-10-12T08:28:45+00:00", "fx": {"THB": 33.5, "USD": 1, "RUB": 85}}
        text = "\n".join(currency_lines(data, OCTOBER, official_fixture()))
        self.assertIn("## Курсы", text)
        self.assertIn("справочные курсы на 12.10.2026", text)
        self.assertIn("1 USD = 33,62 THB", text)
        self.assertIn("1 INR = 0,351 THB", text)
        self.assertIn("100 KRW = 2,52 THB", text)
        self.assertIn("официальный курс на 14.10.2026: 1 THB = 2,5034 RUB (1 RUB = 0,3995 THB)", text)
        self.assertNotIn("ExchangeRate-API", text)
        self.assertNotIn("⚠", text)

    def test_stale_or_partial_official_fx_is_flagged_and_aggregator_is_fallback(self):
        official = official_fixture()
        official["fx"]["ecb"]["date"] = "2026-10-01"
        del official["fx"]["cbr"]
        official["errors"] = [{"source": "cbr", "at": "2026-10-13T02:00:00+00:00", "error": "http_403"}]
        text = "\n".join(currency_lines({}, OCTOBER, official))
        self.assertIn("ЕЦБ: курсу 12 дн.", text)
        self.assertIn("Курс Банка России недоступен", text)
        self.assertIn("источника Банк России не удалось", text)
        fallback = "\n".join(currency_lines({"built_at": "2026-10-13T02:00:00+00:00",
                                               "fx": {"THB": 33.5, "USD": 1, "RUB": 85}}, OCTOBER, {}))
        self.assertIn("ExchangeRate-API", fallback)
        self.assertIn("1 USD = 33,5 THB", fallback)
        self.assertNotIn("⚠", fallback)


class NewsTests(unittest.TestCase):
    def test_news_window_priority_caps_and_escaping(self):
        items = [news_item(f"Cannabis clinic rule {n}", ["cannabis", "pattaya"], n) for n in range(1, 5)]
        items += [
            news_item("Visa exemption cut to 30 days", ["entry_rules"], 30, source="TAT Newsroom", official=True),
            news_item("Old flights story", ["flights"], 24 * 8),
            news_item("Future story", ["flights"], -2),
            news_item("Kratom raid", ["safety"], 5),
            news_item("Road works on Beach Road", ["pattaya"], 6, source="Pattaya City Hall", lang="th"),
            news_item("Phuket only story", ["phuket"], 7),
            news_item("Bad link", ["cannabis"], 1, url="javascript:alert(1)"),
            news_item("Evil [x](https://bad.example) <script>*", ["statistics"], 8),
        ]
        news = {"built_at": "2026-10-13T02:30:00+00:00", "window_days": 10, "items": items,
                "sources": [{"name": "Khaosod English", "endpoint": "x", "ok": False, "count": 0, "error": "http_403"},
                            {"name": "TAT Newsroom", "endpoint": "y", "ok": True, "count": 1, "error": None}]}
        text = "\n".join(news_lines(news, OCTOBER))
        self.assertIn("Окно: 06.10.2026 10:00 — 13.10.2026 10:00, Бангкок", text)
        self.assertIn("Не обновились: Khaosod English (http\\_403).", text)
        self.assertEqual(text.count("Cannabis clinic rule"), 3)
        self.assertNotIn("Cannabis clinic rule 4", text)
        self.assertNotIn("Old flights story", text)
        self.assertNotIn("Future story", text)
        self.assertNotIn("Phuket only story", text)
        self.assertNotIn("javascript:", text)
        self.assertIn("- [Visa exemption cut to 30 days](https://thepattayanews.com/30) — TAT Newsroom, "
                      "12.10.2026, официальный источник", text)
        self.assertIn("Road works on Beach Road](https://thepattayanews.com/6) — Pattaya City Hall, 13.10.2026, "
                      "на тайском", text)
        self.assertIn("Evil \\[x\\](https://bad.example) \\<script\\>\\*", text)
        order = [text.index(label) for label in ("**Каннабис**", "**Въезд и визы**", "**Статистика**",
                                                  "**Безопасность**", "**Официально и Паттайя**")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Показано 7 из 8 новостей", text)
        self.assertNotIn("⚠", text)

    def test_official_items_lead_their_topic(self):
        items = [news_item(f"Flight theft arrest {n}", ["flights"], n) for n in range(1, 5)]
        items.append(news_item("TAT welcomes new Warsaw-Bangkok route", ["flights", "bangkok"], 100,
                               source="TAT Newsroom", official=True))
        text = "\n".join(news_lines({"built_at": "2026-10-13T02:30:00+00:00", "items": items}, OCTOBER))
        self.assertLess(text.index("TAT welcomes new Warsaw-Bangkok route"), text.index("Flight theft arrest 1"))
        self.assertIn("Flight theft arrest 2", text)
        self.assertNotIn("Flight theft arrest 3", text)

    def test_news_total_cap_and_staleness(self):
        topics = ["cannabis", "entry_rules", "flights", "statistics", "alcohol", "events", "safety"]
        items = [news_item(f"{topic} {n}", [topic], n + 1) for topic in topics for n in range(3)]
        news = {"built_at": "2026-10-10T00:00:00+00:00", "items": items, "sources": []}
        text = "\n".join(news_lines(news, OCTOBER))
        self.assertEqual(text.count("\n- ["), 12)
        self.assertNotIn("**Безопасность**", text)
        self.assertIn("Свежесть новостей не подтверждена", text)
        empty = "\n".join(news_lines({"built_at": "2026-10-13T02:00:00+00:00", "items": []}, OCTOBER))
        self.assertIn("За 7 дней нет новостей по отслеживаемым темам", empty)

    def test_write_reports_reads_collected_files_and_survives_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tourism").mkdir()
            (root / "tourism" / "official.json").write_text(json.dumps(official_fixture()), encoding="utf-8")
            (root / "tourism" / "news.json").write_text("{not json", encoding="utf-8")
            latest, _ = write_reports(root, OCTOBER)
            text = latest.read_text(encoding="utf-8")
            self.assertIn("## Прибытия иностранцев (MOTS)", text)
            self.assertIn("## Провинции (MOTS)", text)
            self.assertIn("1 THB = 2,5034 RUB", text)
            self.assertIn("Новости не загружены", text)
            sections = [line for line in text.splitlines() if line.startswith("## ")]
            self.assertEqual(sections[2:], ["## Прибытия иностранцев (MOTS)", "## Провинции (MOTS)",
                                            "## Опубликованная статистика отелей", "## Новости за 7 дней", "## Курсы"])


if __name__ == "__main__":
    unittest.main()
