"""Offline checks for the past-years window: real dates, gaps and fallbacks."""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_history as bh  # noqa: E402


def archive(url, rain=None):
    """Fake archive answer covering exactly the requested dates."""
    q = parse_qs(urlparse(url).query)
    start = date.fromisoformat(q["start_date"][0])
    end = date.fromisoformat(q["end_date"][0])
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    rain = rain or [2.0] * len(days)
    return json.dumps([{"daily": {"time": days, "precipitation_sum": rain,
                                  "temperature_2m_max": [30.0] * len(days)}}
                       for _ in bh.REGIONS]).encode()


class WindowTests(unittest.TestCase):
    def test_week_crossing_new_year(self):
        start, end = bh.past_window(date(2026, 12, 28), 2025)
        self.assertEqual((start, end), (date(2025, 12, 28), date(2026, 1, 3)))

    def test_leap_day_in_a_non_leap_year(self):
        self.assertEqual(bh.past_window(date(2028, 2, 29), 2027),
                         (date(2027, 2, 28), date(2027, 3, 6)))
        self.assertEqual(bh.past_window(date(2028, 2, 29), 2024),
                         (date(2024, 2, 29), date(2024, 3, 6)))

    def test_bangkok_date_not_utc(self):
        # 31 Dec 18:00 UTC is already 1 January in Bangkok
        now = datetime(2026, 12, 31, 18, 0, tzinfo=timezone.utc)
        urls = []

        def fetch(url):
            urls.append(url)
            return archive(url)

        with tempfile.TemporaryDirectory() as tmp, patch("sys.stdout"), patch("sys.stderr"):
            out = Path(tmp) / "history.json"
            self.assertEqual(bh.build(now=now, fetch=fetch, out=out), 0)
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("start_date=2026-01-01&end_date=2026-01-07", urls[0])
        self.assertEqual(data["window"]["start_date"], "2027-01-01")
        self.assertEqual(data["year_windows"]["2026"], {"from": "2026-01-01", "to": "2026-01-07"})


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "history.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, fetch, now):
        with patch("sys.stdout"), patch("sys.stderr"):
            return bh.build(now=now, fetch=fetch, out=self.out)

    def test_year_boundary_requests_real_dates(self):
        urls = []

        def fetch(url):
            urls.append(url)
            return archive(url)

        now = datetime(2026, 12, 28, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(self.run_build(fetch, now), 0)
        self.assertIn("start_date=2025-12-28&end_date=2026-01-03", urls[0])
        self.assertIn("start_date=2022-12-28&end_date=2023-01-03", urls[-1])
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(sorted(data["years"]), ["2022", "2023", "2024", "2025"])
        self.assertEqual(data["window"]["from"], "12-28")
        self.assertEqual(data["window"]["to"], "01-03")
        self.assertEqual(data["years"]["2025"]["pattaya"]["days_with_data"], 7)

    def test_missing_rain_is_not_dry(self):
        rain = [None, None, None, None, None, 20.0, 1.0]
        now = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
        self.run_build(lambda url: archive(url, rain), now)
        row = json.loads(self.out.read_text(encoding="utf-8"))["years"]["2025"]["pattaya"]
        self.assertEqual(row["days_with_data"], 2)
        self.assertEqual(row["mm_total"], 21.0)
        self.assertEqual(row["wet_days"], 1)
        # all-dry days would score the full base of 30; two observed days cannot
        self.assertLess(row["avg_score"], 30)

    def test_failed_year_keeps_previous_only_for_same_window(self):
        now = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
        self.run_build(archive, now)
        first = json.loads(self.out.read_text(encoding="utf-8"))

        def flaky(url):
            if "start_date=2024-" in url:
                raise RuntimeError("archive: HTTP 503")
            return archive(url, [9.0] * 7)

        self.assertEqual(self.run_build(flaky, now + timedelta(hours=3)), 0)
        same = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(same["years"]["2024"], first["years"]["2024"])
        self.assertNotEqual(same["years"]["2025"], first["years"]["2025"])
        self.assertEqual(same["errors"][0]["source"], "archive:2024")

        self.assertEqual(self.run_build(flaky, now + timedelta(days=1)), 0)
        moved = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertNotIn("2024", moved["years"])

    def test_all_years_failed_exits_1_and_keeps_file(self):
        now = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
        self.run_build(archive, now)
        before = self.out.read_text(encoding="utf-8")

        def dead(url):
            raise RuntimeError("archive: timed out")

        self.assertEqual(self.run_build(dead, now + timedelta(days=1)), 1)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
