"""One bounded, public-browser connectivity test. No images or counts are saved."""
import asyncio
import json
import os
import resource
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

SOURCE = "https://livestream.pattaya.go.th/"
CAMERA_LABEL = "ท่าเทียบเรือแหลมบาลีฮาย - SC-088"
VIDEO_STATE = """() => [...document.querySelectorAll('video')].map(v => ({
    width:v.videoWidth, height:v.videoHeight, readyState:v.readyState,
    currentTime:v.currentTime, paused:v.paused, error:v.error?.code??null
}))"""


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(event, **fields):
    print(json.dumps({"event": event, "at": stamp(), **fields}, ensure_ascii=False), flush=True)


def video_progressed(samples):
    """A playback clock is evidence of decoding, not absolute camera freshness."""
    if len(samples) != 2 or any(len(s["videos"]) != 1 for s in samples):
        return False
    first, last = (s["videos"][0] for s in samples)
    return (all(v["width"] > 0 and v["height"] > 0 and v["readyState"] >= 2
                and not v["paused"] and not v["error"] for v in (first, last))
            and (first["width"], first["height"]) == (last["width"], last["height"])
            and last["currentTime"] > first["currentTime"] + 1)


async def probe(result):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            page.set_default_timeout(15000)

            def response_seen(response):
                parts = urlsplit(response.url)
                if parts.hostname == "livestream.pattaya.go.th" and response.status >= 400:
                    # Never log query strings, tokens, response bodies or browser storage.
                    result["http_errors"].append({"path": parts.path, "status": response.status})

            page.on("response", response_seen)
            response = await page.goto(SOURCE, wait_until="domcontentloaded", timeout=30000)
            result["page_status"] = response.status if response else None
            result["page_title"] = await page.title()
            emit("page_loaded", status=result["page_status"], title=result["page_title"])
            # Normal public UI only: no CAPTCHA clicks, stealth, cookie import or API fallback.
            await page.get_by_role("textbox").fill("SC-088")
            await page.get_by_text(CAMERA_LABEL, exact=True).click()
            emit("camera_selected", camera="SC-088")
            await page.wait_for_function(
                """() => [...document.querySelectorAll('video')].some(v =>
                  v.readyState >= 2 && v.videoWidth > 0 && !v.paused)""", timeout=45000)
            for i in range(2):
                if i:
                    await asyncio.sleep(20)
                sample = {"at": stamp(), "videos": await page.evaluate(VIDEO_STATE)}
                result["samples"].append(sample)
                emit("video_sample", **sample)
            result["status"] = "playback_verified" if video_progressed(result["samples"]) else "playback_unverified"
            result["reason"] = None if result["status"] == "playback_verified" else "video_clock_not_advancing"
        finally:
            await browser.close()


async def main():
    started = time.monotonic()
    result = {"schema_version": 1, "kind": "connectivity_probe", "camera_id": "pattaya-balihai-sc088",
              "source_url": SOURCE, "started_at": stamp(), "status": "unavailable",
              "people": None, "samples": [], "http_errors": [], "reason": None,
              "region": os.environ.get("RAILWAY_REPLICA_REGION"), "images_saved": 0}
    emit("probe_started", camera=result["camera_id"], limit_seconds=120)
    try:
        await asyncio.wait_for(probe(result), timeout=120)
    except Exception as exc:
        # Exception strings can contain page URLs; keep only the safe type.
        result["reason"] = type(exc).__name__
        if any(e["status"] in (401, 403) for e in result["http_errors"]):
            result["reason"] = "provider_access_denied"
    result["finished_at"] = stamp()
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    result["browser_max_rss_kib"] = child.ru_maxrss
    result["browser_cpu_seconds"] = round(child.ru_utime + child.ru_stime, 2)
    result["freshness"] = "unverified"
    emit("probe_result", **result)
    # A blocked source is a completed diagnostic, not a crash to retry indefinitely.


if __name__ == "__main__":
    asyncio.run(main())
