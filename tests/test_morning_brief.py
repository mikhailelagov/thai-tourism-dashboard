"""Regression checks for evidence boundaries and Bangkok report dates."""

import tempfile
import unittest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from morning_brief import camera_lines, indicator_lines, render_report, write_reports


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


if __name__ == "__main__":
    unittest.main()
