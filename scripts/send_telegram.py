#!/usr/bin/env python3
"""Publish the briefing to Telegram.

Sends the chart first, then the post, so a channel reads image-then-text.
Falls back to a compact generated summary when post.txt is absent, so a
problem with the model never means silence.

Credentials come from the environment, never from the code:

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat, group, or channel id
    DASHBOARD_URL        optional, appends a link to the full dashboard

Standard library only.
"""

import json
import mimetypes
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
POST = ROOT / "post.txt"
CHART = ROOT / "chart.png"
POST_ERR = ROOT / "post_error.txt"

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
ORDER = ["bangkok", "pattaya", "phuket", "krabi", "samui", "chiangmai"]
NAMES = {"bangkok": "Бангкок", "pattaya": "Паттайя", "phuket": "Пхукет",
         "krabi": "Краби", "samui": "Самуи", "chiangmai": "Чиангмай"}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tier(score):
    if score < 24:
        return "🔴", "низкий"
    if score < 30:
        return "🟠", "ниже среднего"
    if score < 45:
        return "⚪", "умеренный"
    return "🟢", "хороший"


def fallback_post(payload):
    """Compact summary used when the model-written briefing is unavailable."""
    regions = payload.get("regions", {})
    lines = ["<b>Спрос на туризм в Таиланде</b>", ""]
    for rid in ORDER:
        days = regions.get(rid)
        if not days:
            continue
        d0 = days[0]
        dot, label = tier(d0["score"])
        lines.append(f"{dot} <b>{esc(NAMES[rid])}</b> — {d0['score']}/100 "
                     f"<i>({label})</i> · {d0['tmax']}° · {d0['mm']:.1f} мм")

    lead = regions.get(ORDER[0])
    if lead and len(lead) > 1:
        fwd = lead[1:8]
        best = max(fwd, key=lambda d: d["score"])
        avg = round(sum(d["score"] for d in fwd) / len(fwd))
        wd = DOW[date.fromisoformat(best["iso"]).weekday()]
        lines += ["", f"<b>Неделя вперёд</b> — {esc(NAMES[ORDER[0]])}: "
                      f"среднее {avg}, лучший день {wd} "
                      f"{best['iso'][8:]}.{best['iso'][5:7]} ({best['score']})"]

    fx = payload.get("fx") or {}
    if fx.get("THB"):
        lines += ["", f"Бат за доллар: <b>{fx['THB']:.2f}</b>"]
    note = "Развёрнутая сводка на этой неделе недоступна — показаны расчётные показатели."
    if POST_ERR.exists():
        reason = POST_ERR.read_text(encoding="utf-8").strip()
        if reason:
            note += f"\nПричина: {esc(reason)}"
    lines += ["", f"<i>{note}</i>"]
    return "\n".join(lines)


def post_form(token, method, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8"))


def post_photo(token, chat_id, photo, caption):
    """Multipart upload - Telegram needs the file body, not a path."""
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
    body = bytearray()

    def field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(f"{value}\r\n".encode())

    field("chat_id", chat_id)
    if caption:
        field("caption", caption)
        field("parse_mode", "HTML")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="photo"; filename="{photo.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    body.extend(photo.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        API.format(token=token, method="sendPhoto"), data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def chunk(text, limit):
    """Split on paragraph breaks so a long post never cuts mid-sentence."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            buf = para[:limit]
    if buf:
        out.append(buf)
    return out


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set; skipping.",
              file=sys.stderr)
        return 0

    if POST.exists() and POST.read_text(encoding="utf-8").strip():
        post = POST.read_text(encoding="utf-8").strip()
        source = "briefing"
    elif DATA.exists():
        post = fallback_post(json.loads(DATA.read_text(encoding="utf-8")))
        source = "fallback summary"
    else:
        print("neither post.txt nor data.json is available.", file=sys.stderr)
        return 1

    url = os.environ.get("DASHBOARD_URL", "").strip()
    if url:
        post += f'\n\n<a href="{esc(url)}">Полная сводка по регионам</a>'

    if os.environ.get("DRY_RUN"):
        print(post)
        return 0

    try:
        if CHART.exists():
            stamp = datetime.now().strftime("%d.%m.%Y")
            post_photo(token, chat_id, CHART,
                       f"<b>Спрос на туризм в Таиланде</b> · {stamp}")
        for part in chunk(post, MESSAGE_LIMIT):
            res = post_form(token, "sendMessage", {
                "chat_id": chat_id, "text": part, "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            })
            if not res.get("ok"):
                print(f"Telegram returned an error: {res}", file=sys.stderr)
                return 1
    except urllib.error.HTTPError as e:
        print(f"Telegram rejected the request: {e.read().decode()}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"could not reach Telegram: {e}", file=sys.stderr)
        return 1

    print(f"published ({source}, {len(post)} characters)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
