"""Safety and history tests for the metadata-only tourism collector."""

import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from zoneinfo import ZoneInfo


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "collect_tourism.py"
SPEC = importlib.util.spec_from_file_location("collect_tourism", MODULE)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
NOW = datetime(2026, 9, 6, 2, 28, 33, tzinfo=timezone.utc)  # 09:28 Bangkok
ZONE = ZoneInfo("Asia/Bangkok")


def camera(identifier="pier", provider="pattaya", **overrides):
    value = {"id": identifier, "region": "pattaya", "title": "Tourist pier",
             "provider": provider, "source_url": "https://example.org/camera",
             "hours": list(range(7, 19)), "review_status": "access_required",
             "provider_id": "public-camera-id"}
    value.update(overrides)
    return value


def config(*cameras):
    return {"timezone": "Asia/Bangkok", "cameras": list(cameras)}


def public_map(*items):
    return {"status": True, "details": {"items": list(items)}}


class CollectionTests(unittest.TestCase):
    def test_online_catalogue_never_becomes_count_or_stream_success(self):
        cams = config(camera(), camera("pier-two"))
        response = public_map({"id": "public-camera-id", "status": True,
                               "monitorState": "online"})
        with patch.object(collector, "fetch_json", return_value=response) as fetch:
            row, failed = collector.collect(cams, ZONE, NOW, None)
        self.assertFalse(failed)
        fetch.assert_called_once_with(collector.PATTAYA_MAP)
        self.assertEqual(row["at"], "2026-09-06T02:28:33+00:00")
        for record in row["cams"].values():
            self.assertEqual(record["observed_at"], row["at"])
            self.assertEqual(record["status"], "unavailable")
            self.assertEqual(record["reason"], "provider_access_required")
            self.assertIsNone(record["people"])
            self.assertIsNone(record["series_version"])

    def test_overnight_hours_use_bangkok_and_avoid_inactive_requests(self):
        night = camera(hours=[18, 19, 20, 21, 22, 23, 0, 1, 2])
        self.assertTrue(collector.is_active(night, NOW.replace(hour=17), ZONE))
        self.assertTrue(collector.is_active(night, NOW.replace(hour=19), ZONE))
        self.assertFalse(collector.is_active(night, NOW.replace(hour=20), ZONE))
        with patch.object(collector, "fetch_json") as fetch:
            row, failed = collector.collect(config(night), ZONE, NOW, None)
        fetch.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual(row["cams"]["pier"]["reason"], "outside_observation_hours")
        self.assertEqual(row["cams"]["pier"]["status"], "inactive")
        self.assertIsNone(row["cams"]["pier"]["people"])

    def test_known_offline_source_retained_even_with_no_observation_hours(self):
        with patch.object(collector, "fetch_json") as fetch:
            row, failed = collector.collect(config(camera(
                provider="pending", review_status="offline", hours=[])), ZONE, NOW, None)
        fetch.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual(row["cams"]["pier"]["reason"], "source_offline")

    def test_missing_key_is_explicit_failure_but_inactive_camera_needs_no_key(self):
        active = camera(provider="windy", review_status="not_validated")
        inactive = camera("night-beach", "windy", hours=[20])
        with patch.object(collector, "fetch_json") as fetch:
            row, failed = collector.collect(config(active, inactive), ZONE, NOW, None)
        fetch.assert_not_called()
        self.assertTrue(failed)
        self.assertEqual(row["errors"], ["pier:missing_api_key"])
        self.assertIsNone(row["cams"]["pier"]["people"])

    def test_windy_metadata_whitelist_never_stores_image_urls_or_key(self):
        response = {"webcamId": 123, "status": "active",
                    "imageUpdatedOn": "2026-09-06T02:20:00Z",
                    "images": {"current": {"preview": "https://image/?signed=SECRET"}},
                    "title": "untrusted response SECRET"}
        cam = camera(provider="windy", provider_id=123, review_status="not_validated")
        with patch.object(collector, "fetch_json", return_value=response) as fetch:
            row, failed = collector.collect(config(cam), ZONE, NOW, "SECRET")
        self.assertFalse(failed)
        self.assertEqual(fetch.call_args.args[1], {"x-windy-api-key": "SECRET"})
        self.assertIn("include=images%2Clocation", fetch.call_args.args[0])
        self.assertNotIn("SECRET", json.dumps(row))
        self.assertEqual(row["cams"]["pier"]["imageUpdatedOn"], "2026-09-06T02:20:00+00:00")
        self.assertEqual(row["cams"]["pier"]["reason"], "camera_not_validated")
        self.assertIsNone(row["cams"]["pier"]["people"])

    def test_network_and_parser_exceptions_are_sanitized(self):
        cam = camera(provider="windy", review_status="not_validated")
        exceptions = [urllib.error.HTTPError("https://secret/?key=SECRET", 403,
                                            "SECRET", {}, None),
                      urllib.error.URLError("signed=SECRET"),
                      ValueError("response includes SECRET")]
        for exc in exceptions:
            with self.subTest(exc=type(exc).__name__):
                with patch.object(collector, "fetch_json", side_effect=exc):
                    row, failed = collector.collect(config(cam), ZONE, NOW, "SECRET")
                self.assertTrue(failed)
                self.assertNotIn("SECRET", json.dumps(row))
                self.assertEqual(row["cams"]["pier"]["reason"], "source_request_failed")

    def test_invalid_windy_identity_does_not_attach_another_camera(self):
        with patch.object(collector, "fetch_json", return_value={"webcamId": "other"}):
            row, failed = collector.collect(config(camera(provider="windy")), ZONE, NOW, "key")
        self.assertTrue(failed)
        self.assertEqual(row["errors"], ["pier:invalid_response"])


class HistoryTests(unittest.TestCase):
    def test_actual_time_retention_and_v2_dedup_leave_legacy_unchanged(self):
        legacy = {"at": "2026-09-06T02:00+00:00", "cams": {"old": {"people": 0}}}
        older_v2 = {"schema_version": 2, "at": "2026-09-06T09:03:22+07:00", "cams": {}}
        earlier_hour = {"schema_version": 2, "at": "2026-09-06T01:59:59+00:00", "cams": {}}
        expired = {"at": (NOW - timedelta(days=90, seconds=1)).isoformat(), "cams": {}}
        boundary = {"at": (NOW - timedelta(days=90)).isoformat(), "cams": {}}
        unknown_date = {"at": "old unparseable date", "legacy": True}
        row, _ = collector.collect(config(camera(provider="pending", review_status="offline")), ZONE, NOW, None)
        original = {"custom": "preserved", "rows": [legacy, older_v2, earlier_hour,
                                                    expired, boundary, unknown_date]}
        before = json.loads(json.dumps(original))
        result = collector.merge_history(original, row, NOW)
        self.assertEqual(original, before)
        self.assertEqual(result["rows"], [legacy, earlier_hour, boundary, unknown_date, row])
        self.assertEqual(result["custom"], "preserved")
        self.assertEqual(result["rows"][0], legacy)

    def test_main_missing_key_saves_gap_before_nonzero_exit_and_sanitizes_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cameras.json"
            out = Path(tmp) / "footfall.json"
            cfg.write_text(json.dumps(config(camera(provider="windy", review_status="not_validated"))))
            stderr = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(stderr):
                code = collector.main(cfg, out, NOW)
            self.assertEqual(code, 1)
            saved = json.loads(out.read_text())
            self.assertEqual(saved["rows"][0]["cams"]["pier"]["reason"], "missing_api_key")
            self.assertEqual(saved["collector_status"], "partial")
            self.assertIn("no validated people counts", stderr.getvalue())
            self.assertFalse(list(Path(tmp).glob(".footfall.json.*")))

    def test_malformed_existing_history_fails_without_overwrite_or_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cameras.json"
            out = Path(tmp) / "footfall.json"
            cfg.write_text(json.dumps(config(camera())))
            original = '{"rows": [broken SECRET'
            out.write_text(original)
            stderr = io.StringIO()
            with patch.object(collector, "fetch_json") as fetch, contextlib.redirect_stderr(stderr):
                code = collector.main(cfg, out, NOW)
            self.assertEqual(code, 1)
            fetch.assert_not_called()
            self.assertEqual(out.read_text(), original)
            self.assertNotIn("SECRET", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
