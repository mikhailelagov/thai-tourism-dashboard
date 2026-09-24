"""Verify Railway can run Chromium. Only an in-memory test page is opened."""
import asyncio
import json
import resource
import time
from datetime import datetime, timezone


async def check():
    from playwright.async_api import async_playwright
    started = time.monotonic()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            await page.set_content("<!doctype html><title>Tourism worker health check</title>"
                                   "<p>Local synthetic test only</p><canvas width=640 height=360></canvas>")
            await page.evaluate("""() => { const ctx=document.querySelector('canvas').getContext('2d');
                ctx.fillStyle='#0055aa'; ctx.fillRect(0,0,640,360); }""")
            image = await page.screenshot()
            result = {"kind": "synthetic_browser_health", "status": "ok",
                      "browser_version": browser.version, "title": await page.title(),
                      "screenshot_bytes_in_memory": len(image), "external_pages_opened": 0,
                      "camera_access": "not_attempted", "people": None, "images_saved": 0}
            del image
        finally:
            await browser.close()
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    result.update(at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  elapsed_seconds=round(time.monotonic()-started, 2),
                  browser_max_rss_kib=child.ru_maxrss,
                  browser_cpu_seconds=round(child.ru_utime+child.ru_stime, 2))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(check(), timeout=45))
