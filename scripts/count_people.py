#!/usr/bin/env python3
"""Count people in public webcam frames. Real footfall, not a proxy.

Pulls the latest frame from each curated webcam through the Windy Webcams
API, runs a person detector on it, and appends the counts to footfall.json.
Frames are never kept: only a number per camera per hour is stored.

Needs WINDY_API_KEY in the environment (free tier is enough - the low
resolution it returns is fine for counting people).
"""

import json
import os
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "footfall.json"
API = "https://api.windy.com/webcams/api/v3/webcams"
TIMEOUT = 30
KEEP_DAYS = 90

# Pattaya bounding box - the API is asked for every webcam inside it, then
# the curated list below picks the ones that matter. Names are matched on
# a substring so a renamed webcam still resolves.
BBOX = (12.87, 100.83, 12.99, 100.93)  # south, west, north, east
WATCH = [
    ("walking_street", "walking street"),
    ("beach_road", "beach road"),
    ("jomtien", "jomtien"),
    ("na_kluea", "na kluea"),
]


def api(path, key, params):
    from urllib.parse import urlencode
    req = urllib.request.Request(f"{API}{path}?{urlencode(params)}",
                                 headers={"x-windy-api-key": key,
                                          "User-Agent": "thai-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def find_webcams(key):
    """Every webcam in the box, tagged with the watch-list slot it fills."""
    s, w, n, e = BBOX
    data = api("", key, {"limit": 50, "include": "images,location",
                         "bbox": f"{n},{e},{s},{w}"})
    hits = {}
    for cam in data.get("webcams", []):
        title = (cam.get("title") or "").lower()
        for slot, needle in WATCH:
            if needle in title and slot not in hits:
                hits[slot] = cam
    return hits


def latest_image_url(cam):
    imgs = (cam.get("images") or {}).get("current") or {}
    # Prefer the largest the tier allows; free returns a preview-size image.
    for k in ("preview", "thumbnail", "icon"):
        if imgs.get(k):
            return imgs[k]
    return None


def count_people(model, path):
    """Persons in one frame. COCO class 0 is 'person'."""
    res = model.predict(str(path), classes=[0], conf=0.35, verbose=False)
    return sum(len(r.boxes) for r in res)


def main():
    key = os.environ.get("WINDY_API_KEY")
    if not key:
        print("WINDY_API_KEY is not set - nothing to count.", file=sys.stderr)
        return 0

    from ultralytics import YOLO
    model = YOLO("yolov8n.pt")

    try:
        cams = find_webcams(key)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"could not list webcams: {e}", file=sys.stderr)
        return 1
    if not cams:
        print("no watched webcams found inside the Pattaya box.", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    row = {"at": stamp.isoformat(timespec="minutes"), "cams": {}}

    with tempfile.TemporaryDirectory() as tmp:
        for slot, cam in cams.items():
            url = latest_image_url(cam)
            if not url:
                continue
            try:
                frame = pathlib.Path(tmp) / f"{slot}.jpg"
                urllib.request.urlretrieve(url, frame)
                n = count_people(model, frame)
                row["cams"][slot] = {"people": n, "title": cam.get("title"),
                                     "cam_id": cam.get("webcamId")}
                print(f"{slot:<16} {n:>3} people   {cam.get('title')}")
            except (urllib.error.URLError, OSError) as e:
                print(f"warn: {slot}: {e}", file=sys.stderr)
    # frames are gone with the temp dir - only counts leave this function

    hist = []
    if OUT.exists():
        try:
            hist = json.loads(OUT.read_text(encoding="utf-8")).get("rows", [])
        except ValueError:
            hist = []
    hist = [r for r in hist if r.get("at") != row["at"]] + [row]
    hist = hist[-(KEEP_DAYS * 24):]

    OUT.write_text(json.dumps({"note": "people counted in public webcam frames; "
                                       "frames are not stored",
                               "rows": hist}, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    print(f"wrote {OUT.name} - {len(hist)} hourly rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
