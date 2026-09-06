#!/usr/bin/env python3
"""Record honest availability gaps for the explicitly reviewed tourist cameras.

This collector downloads JSON metadata only. It never fetches image URLs, calls
Pattaya's challenged stream endpoint, runs a detector, or reports a person count.
Catalogue availability does not validate a stream, image freshness, a pedestrian
ROI, or person detail. ``observed_at`` is the actual metadata-check time, not the
time an image was captured. Counts and series versions stay null until a separate
camera-validation workflow establishes those requirements.

Known access/validation gaps complete with exit 0 and a partial status. Missing
WINDY_API_KEY for an active Windy camera, network failures, and malformed responses
are recorded before exit 1. A malformed existing history is never overwritten.
"""

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "tourism" / "cameras.json"
OUT = ROOT / "footfall.json"
PATTAYA_MAP = "https://livestream.pattaya.go.th/kapi/live/map"
WINDY_API = "https://api.windy.com/webcams/api/v3/webcams"
TIMEOUT = 30
KEEP_DAYS = 90
SCHEMA_VERSION = 2


def parse_time(value):
    """Accept only timezone-aware ISO timestamps; never guess a local timezone."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return stamp.astimezone(timezone.utc)


def safe_error(exc):
    """Use only controlled labels: exception text can contain secrets and URLs."""
    if isinstance(exc, urllib.error.HTTPError):
        return "http_%d" % exc.code
    if isinstance(exc, (TimeoutError,)):
        return "request_timeout"
    if isinstance(exc, (urllib.error.URLError, OSError)):
        return "network_error"
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return "invalid_response"
    return "unexpected_error"


def fetch_json(url, headers=None):
    request_headers = {"User-Agent": "thai-tourism-dashboard/2.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("cameras"), list):
        raise ValueError("invalid camera config")
    zone = ZoneInfo(config["timezone"])
    seen = set()
    for camera in config["cameras"]:
        if not isinstance(camera, dict):
            raise ValueError("invalid camera config")
        for field in ("id", "region", "title", "source_url"):
            if not isinstance(camera.get(field), str) or not camera[field]:
                raise ValueError("missing camera field")
        if camera["id"] in seen:
            raise ValueError("duplicate camera id")
        seen.add(camera["id"])
        if camera.get("provider") not in ("pattaya", "windy", "pending"):
            raise ValueError("unknown camera provider")
        if camera.get("review_status") not in (
                "access_required", "not_validated", "offline"):
            raise ValueError("unknown camera review status")
        hours = camera.get("hours")
        if not isinstance(hours, list) or any(
                type(hour) is not int or not 0 <= hour <= 23 for hour in hours):
            raise ValueError("invalid observation hours")
        if camera["provider"] != "pending" and not camera.get("provider_id"):
            raise ValueError("missing provider id")
    return config, zone


def load_history(path):
    path = Path(path)
    if not path.exists():
        return {"rows": []}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list):
        raise ValueError("invalid history document")
    if any(not isinstance(row, dict) for row in document["rows"]):
        raise ValueError("invalid history row")
    return document


def is_active(camera, now, zone):
    return now.astimezone(zone).hour in camera["hours"]


def catalog_state(value):
    # Provider values are not copied verbatim: unexpected strings may contain URLs.
    if isinstance(value, str) and value.lower() in (
            "active", "inactive", "online", "offline", "maintenance"):
        return value.lower()
    return "unknown"


def image_update_time(value):
    """Store a supplied image timestamp only, never an image or signed URL."""
    if isinstance(value, str):
        try:
            return parse_time(value).isoformat(timespec="seconds")
        except ValueError:
            return None
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return value
    return None


def pattaya_catalog():
    data = fetch_json(PATTAYA_MAP)
    if not isinstance(data, dict) or data.get("status") is not True:
        raise ValueError("invalid Pattaya response")
    details = data.get("details")
    items = details.get("items") if isinstance(details, dict) else None
    if not isinstance(items, list) or any(
            not isinstance(item, dict) or not item.get("id") for item in items):
        raise ValueError("invalid Pattaya catalogue")
    return {str(item["id"]): item for item in items}


def collect(config, zone, now, windy_key):
    """Return an availability-only row and whether a configuration/request failed."""
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    row = {"schema_version": SCHEMA_VERSION, "at": stamp,
           "measurement_kind": "availability_only", "collector_status": "partial",
           "errors": [], "cams": {}}
    active_pattaya = any(
        camera["provider"] == "pattaya" and camera["review_status"] != "offline"
        and is_active(camera, now, zone) for camera in config["cameras"])
    catalog = None
    catalog_error = None
    if active_pattaya:
        try:
            catalog = pattaya_catalog()
        except Exception as exc:
            catalog_error = safe_error(exc)
            row["errors"].append("pattaya:" + catalog_error)

    for camera in config["cameras"]:
        record = {"status": "unavailable", "people": None, "observed_at": stamp,
                  "region": camera["region"], "title": camera["title"],
                  "source_url": camera["source_url"], "series_version": None}
        row["cams"][camera["id"]] = record
        if camera["review_status"] == "offline":
            record["reason"] = "source_offline"
            continue
        if not is_active(camera, now, zone):
            record.update(status="inactive", reason="outside_observation_hours")
            continue
        if camera["provider"] == "pattaya":
            if catalog_error:
                record["reason"] = "source_request_failed"
                continue
            item = catalog.get(str(camera["provider_id"]))
            if item is None:
                record["reason"] = "source_request_failed"
                row["errors"].append(camera["id"] + ":camera_not_in_catalog")
                continue
            state = catalog_state(item.get("monitorState"))
            record["catalog_status"] = state
            record["reason"] = (
                "source_offline" if state in ("offline", "maintenance")
                or item.get("status") is False else "provider_access_required")
        elif camera["provider"] == "windy":
            if not windy_key:
                record["reason"] = "missing_api_key"
                row["errors"].append(camera["id"] + ":missing_api_key")
                continue
            try:
                url = WINDY_API + "/" + quote(str(camera["provider_id"]), safe="")
                data = fetch_json(url + "?" + urlencode({"include": "images,location"}),
                                  {"x-windy-api-key": windy_key})
                if not isinstance(data, dict) or str(data.get("webcamId")) != str(
                        camera["provider_id"]):
                    raise ValueError("invalid Windy response")
                state = catalog_state(data.get("status"))
                record["catalog_status"] = state
                updated = image_update_time(data.get("imageUpdatedOn"))
                if updated is not None:
                    record["imageUpdatedOn"] = updated
                record["reason"] = (
                    "source_offline" if state in ("inactive", "offline", "maintenance")
                    else "camera_not_validated")
            except Exception as exc:
                record["reason"] = "source_request_failed"
                row["errors"].append(camera["id"] + ":" + safe_error(exc))
        else:
            record["reason"] = "camera_not_validated"
    return row, bool(row["errors"])


def merge_history(document, row, now):
    """Expire by actual time; dedup only valid v2 rows in the same UTC hour.

    Legacy rows are never rewritten or conflated with this metadata-only series.
    An unparseable historical timestamp is retained instead of silently discarded.
    """
    cutoff = now.astimezone(timezone.utc) - timedelta(days=KEEP_DAYS)
    current_hour = parse_time(row["at"]).replace(minute=0, second=0, microsecond=0)
    retained = []
    for previous in document["rows"]:
        try:
            previous_time = parse_time(previous.get("at"))
        except (ValueError, TypeError, OverflowError):
            retained.append(previous)
            continue
        if previous_time < cutoff:
            continue
        valid_v2 = (previous.get("schema_version") == SCHEMA_VERSION
                    and isinstance(previous.get("cams"), dict))
        if valid_v2 and previous_time.replace(
                minute=0, second=0, microsecond=0) == current_hour:
            continue
        retained.append(previous)
    result = dict(document)
    result.update(
        note="Schema 2 records camera availability only; people=null means no measurement. "
             "Legacy rows are retained without revalidation.",
        collector_status=row["collector_status"], collector_errors=row["errors"],
        collected_at=row["at"], rows=retained + [row])
    return result


def atomic_write(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as out:
            temporary = Path(out.name)
            json.dump(document, out, ensure_ascii=False, indent=1, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main(config_path=CONFIG, output_path=OUT, now=None):
    try:
        # Check history before network access. A failed parse must not reset it.
        document = load_history(output_path)
        config, zone = load_config(config_path)
        actual_now = now if now is not None else datetime.now(timezone.utc)
        if actual_now.tzinfo is None or actual_now.utcoffset() is None:
            raise ValueError("observation time requires timezone")
        row, failed = collect(config, zone, actual_now, os.environ.get("WINDY_API_KEY"))
        atomic_write(output_path, merge_history(document, row, actual_now))
    except Exception as exc:
        print("Tourism metadata collection failed: " + safe_error(exc), file=sys.stderr)
        return 1
    print("Tourism collection is partial: no validated people counts; "
          "%d configured camera statuses saved." % len(row["cams"]), file=sys.stderr)
    for error in row["errors"]:
        print("Tourism collector gap: " + error, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
