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
# One box per destination: south, west, north, east.
BOXES = {
    "pattaya":   (12.74, 100.75, 13.10, 101.02),
    "phuket":    (7.79,  98.27,  8.00,  98.40),   # Patong, Bangla, Kata, Karon
    "bangkok":   (13.68, 100.47, 13.80, 100.60),
    "krabi":     (7.85,  98.62,  8.20,  99.00),   # Ao Nang, Railay
    "samui":     (9.35,  99.85,  9.65, 100.15),
    "chiangmai": (18.75, 98.94,  18.83, 99.02),
}
MAX_CAMS_PER_REGION = 4  # keeps a run inside a couple of minutes on CPU

# Toll gates and distant viewpoints show cars or empty sea, so they add noise
# rather than footfall. Prefer pedestrian places, and drop the obvious misses.
PREFER = ("beach", "walking", "street", "road", "market", "pier", "promenade",
          "bang la", "bangla", "patong", "jomtien", "ao nang", "night")
AVOID = ("toll", "tool gate", "expressway", "motorway", "airport", "viewpoint",
         "underwater", "sky", "weather")


def api(path, key, params):
    from urllib.parse import urlencode
    req = urllib.request.Request(f"{API}{path}?{urlencode(params)}",
                                 headers={"x-windy-api-key": key,
                                          "User-Agent": "thai-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def latest_image_url(cam):
    imgs = (cam.get("images") or {}).get("current") or {}
    # Prefer the largest the tier allows; free returns a preview-size image.
    for k in ("preview", "thumbnail", "icon"):
        if imgs.get(k):
            return imgs[k]
    return None


def find_webcams(key):
    """Active webcams that serve an image, across every destination box.

    Selection used to match hard-coded words in the title, which silently
    found nothing when the cameras were named differently. The box is now
    the only filter, and each camera is keyed by its own id so a rename
    never breaks the series.
    """
    found = {}
    for region, (s_, w_, n_, e_) in BOXES.items():
        try:
            data = api("", key, {"limit": 50, "include": "images,location",
                                 "bbox": f"{n_},{e_},{s_},{w_}"})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"warn: {region}: {exc}", file=sys.stderr)
            continue
        cams = data.get("webcams") or []
        usable = [c for c in cams
                  if c.get("status") in (None, "active") and latest_image_url(c)]

        def rank(c):
            t = f"{c.get('title') or ''} {(c.get('location') or {}).get('city') or ''}".lower()
            if any(a in t for a in AVOID):
                return (2, t)
            return (0 if any(k in t for k in PREFER) else 1, t)

        usable.sort(key=rank)
        print(f"{region}: {len(cams)} in box, {len(usable)} usable", file=sys.stderr)
        for c in usable[:MAX_CAMS_PER_REGION]:
            print(f"  {c.get('webcamId')}  {c.get('title')}", file=sys.stderr)
            found[str(c.get("webcamId"))] = (region, c)
    return found


def count_people(model, path):
    """Persons in one frame. COCO class 0 is 'person'."""
    res = model.predict(str(path), classes=[0], conf=0.25, imgsz=960,
                        verbose=False)
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
        print("no usable webcams found in any destination box.", file=sys.stderr)
        return 1

    stamp = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    row = {"at": stamp.isoformat(timespec="minutes"), "cams": {}}

    with tempfile.TemporaryDirectory() as tmp:
        for slot, (region, cam) in cams.items():
            url = latest_image_url(cam)
            if not url:
                continue
            try:
                frame = pathlib.Path(tmp) / f"{slot}.jpg"
                urllib.request.urlretrieve(url, frame)
                n = count_people(model, frame)
                try:
                    from PIL import Image
                    with Image.open(frame) as im:
                        size = f"{im.width}x{im.height}"
                except Exception:
                    size = None
                row["cams"][slot] = {"people": n, "region": region,
                                     "title": cam.get("title"),
                                     "cam_id": cam.get("webcamId"),
                                     "frame": size}
                print(f"{region:<10} {n:>3} people  {size or '?':>9}  "
                      f"{cam.get('title')}")
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
