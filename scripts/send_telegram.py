#!/usr/bin/env python3
"""Publish the briefing to Telegram.

Sends the chart first, then the post, so a channel reads image-then-text.
Falls back to a compact generated summary when post.txt is absent, so a
problem with the model never means silence.

Credentials come from the environment, never from the code:

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat, group, or channel id
    TELEGRAM_ADMIN_CHAT_ID  optional, your personal chat for failure alerts
    DASHBOARD_URL        optional, appends a link to the full dashboard

Standard library only.
"""

import json
import mimetypes
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
OFFICIAL = ROOT / "tourism" / "official.json"
POST = ROOT / "post.txt"
CHART = ROOT / "chart.png"
POST_ERR = ROOT / "post_error.txt"

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

BANGKOK = ZoneInfo("Asia/Bangkok")
# Telegram rejects the whole message on any other tag.
TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code",
        "pre", "blockquote", "tg-spoiler"}
LEAD = "pattaya"
DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
ORDER = ["bangkok", "pattaya", "phuket", "krabi", "samui", "chiangmai"]
NAMES = {"bangkok": "Бангкок", "pattaya": "Паттайя", "phuket": "Пхукет",
         "krabi": "Краби", "samui": "Самуи", "chiangmai": "Чиангмай"}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tier(score):
    if score < 24:
        return "🔴", "плохие"
    if score < 30:
        return "🟠", "ниже среднего"
    if score < 45:
        return "⚪", "средние"
    return "🟢", "хорошие"


def num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bangkok_day(value):
    """dd.mm.yyyy in Bangkok for an ISO timestamp, or None."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(BANGKOK).strftime("%d.%m.%Y") if parsed.tzinfo else None


def measured_lines():
    """Official monthly figures with their period and source, if collected."""
    data, lines = load(OFFICIAL) or {}, []
    arr = data.get("arrivals") or {}
    total = arr.get("total") or {}
    if num(total.get("value")):
        yoy = f" ({total['yoy_pct']:+.1f}% г/г)" if num(total.get("yoy_pct")) else ""
        link = arr.get("source_url") or arr.get("file_url")
        lines += [f"✈️ Прибытия иностранцев, {esc(arr.get('period'))}: "
                  f"<b>{total['value']:,}</b>{yoy}".replace(",", " ")
                  + (f' — <a href="{esc(link)}">MOTS</a>' if link else "")]
    row = ((data.get("provinces") or {}).get("rows") or {}).get(LEAD) or {}
    if num(row.get("occupancy_pct")):
        prov = data["provinces"]
        change = (f" ({row['occupancy_change_pp']:+.1f} п.п. г/г)"
                  if num(row.get("occupancy_change_pp")) else "")
        link = prov.get("source_url") or prov.get("file_url")
        lines += [f"🏨 Загрузка отелей, {esc(row.get('label_ru') or 'Чонбури')}, {esc(prov.get('period'))}: "
                  f"<b>{row['occupancy_pct']:.1f}%</b>{change}"
                  + (f' — <a href="{esc(link)}">MOTS</a>' if link else "")]
    return lines


def fallback_post(payload):
    """Compact summary used when the model-written briefing is unavailable."""
    regions = payload.get("regions", {})
    today = datetime.now(BANGKOK).date().isoformat()
    lines = ["<b>Туризм в Таиланде: короткая сводка</b>", ""]
    measured = measured_lines()
    if measured:
        lines += measured + [""]
    lines.append("<b>Условия для отдыха на улице сегодня</b> <i>(модель: погода, праздники, "
                 "нефть и курсы — не измерение спроса)</i>")
    for rid in ORDER:
        days = [d for d in regions.get(rid) or [] if d.get("iso", "") >= today]
        if not days or not num(days[0].get("score")):
            continue
        d0 = days[0]
        dot, label = tier(d0["score"])
        rain = f"{d0['mm']:.1f} мм" if num(d0.get("mm")) else "осадки н/д"
        lines.append(f"{dot} <b>{esc(NAMES[rid])}</b> — {label} · {d0.get('tmax')}° · {rain}")

    # "Best by weather" must rank on the weather term alone, not on oil and FX.
    lead = [d for d in regions.get(LEAD) or [] if d.get("iso", "") > today
            and num((d.get("parts") or {}).get("weather"))][:7]
    if lead:
        best = max(lead, key=lambda d: d["parts"]["weather"])
        wd = DOW[date.fromisoformat(best["iso"]).weekday()]
        lines += ["", f"<b>Неделя вперёд</b> — {esc(NAMES[LEAD])}: лучший по погоде день "
                      f"{wd} {best['iso'][8:]}.{best['iso'][5:7]}"]

    fx = payload.get("fx") or {}
    if fx.get("THB"):
        quoted = bangkok_day(payload.get("fx_time"))
        lines += ["", f"Бат за доллар: <b>{fx['THB']:.2f}</b> <i>(справочный курс"
                      + (f" на {quoted}" if quoted else "") + ")</i>"]
    built = bangkok_day(payload.get("built_at"))
    # Readers get no technical reason: it is operator business and an API
    # error in a public channel reads as a broken product.
    note = "Развёрнутая сводка на этой неделе недоступна — показаны собранные показатели."
    if built:
        note += f" Прогноз погоды от {built}."
    lines += ["", f"<i>{note}</i>"]
    return "\n".join(lines)


class TelegramHTML(HTMLParser):
    """Keep the tags Telegram accepts, escape everything else, balance tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open = [], []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")
        elif tag in TAGS:
            if tag == "a":
                href = dict(attrs).get("href") or ""
                if not href.startswith(("http://", "https://", "tg://")):
                    return
                self.out.append(f'<a href="{esc(href).replace(chr(34), "&quot;")}">')
            else:
                self.out.append(f"<{tag}>")
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in self.open:
            while self.open:
                last = self.open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break

    def handle_data(self, data):
        self.out.append(esc(data))

    def result(self):
        self.close()
        return "".join(self.out) + "".join(f"</{t}>" for t in reversed(self.open))


def sanitize(text):
    parser = TelegramHTML()
    parser.feed(text)
    return parser.result()


def plain(text):
    return re.sub(r"<[^>]+>", "", text).replace("&lt;", "<").replace("&gt;", ">") \
        .replace("&quot;", '"').replace("&amp;", "&")


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


def failure_reason():
    if POST_ERR.exists():
        return POST_ERR.read_text(encoding="utf-8").strip() or "причина не записана"
    return None


def alert_admin(token, reason, consequence="В канал ушла короткая сводка без анализа."):
    """Tell the operator, not the channel, what went wrong."""
    admin = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    run = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run = (f'\n<a href="{os.environ.get("GITHUB_SERVER_URL", "https://github.com")}/'
               f'{os.environ.get("GITHUB_REPOSITORY", "")}/actions/runs/'
               f'{os.environ["GITHUB_RUN_ID"]}">Журнал запуска</a>')
    # Without an admin chat the workflow's last step still fails the run,
    # so GitHub e-mails the owner.
    if not admin:
        return
    text = f"⚠️ <b>Еженедельная сводка</b>\nПричина: {esc(reason)}\n{consequence}{run}"
    try:
        post_form(token, "sendMessage", {"chat_id": admin, "text": text,
                                         "parse_mode": "HTML",
                                         "disable_web_page_preview": "true"})
    except (urllib.error.URLError, OSError) as e:
        print(f"could not alert the admin chat: {e}", file=sys.stderr)


def chunk(text, limit):
    """Split on paragraph breaks so a long post never cuts mid-sentence.

    Each part is sanitized on its own, which closes tags left open at a cut.
    """
    if len(sanitize(text)) <= limit:
        return [sanitize(text)]
    # Room for the closing tags sanitize() adds at each cut.
    limit -= 100
    out, buf = [], ""
    pieces = []
    for para in text.split("\n\n"):
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else para.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(para[:cut])
            para = para[cut:].lstrip()
        pieces.append(para)
    for para in pieces:
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                out.append(buf)
            buf = para
    if buf:
        out.append(buf)
    return [sanitize(part) for part in out]


def send_text(token, chat_id, part):
    """Send one message; on an HTML parse error resend it as plain text."""
    fields = {"chat_id": chat_id, "text": part, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    try:
        return post_form(token, "sendMessage", fields)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code != 400 or "parse entities" not in body:
            raise RuntimeError(f"Telegram rejected the request: {body}") from e
        print(f"HTML rejected, sending as plain text: {body}", file=sys.stderr)
        fields = {"chat_id": chat_id, "text": plain(part), "disable_web_page_preview": "true"}
        return post_form(token, "sendMessage", fields)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set; skipping.",
              file=sys.stderr)
        # In CI a missing secret means nothing was published; say so.
        return 1 if os.environ.get("GITHUB_ACTIONS") == "true" else 0

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
        print("\n---\n".join(chunk(post, MESSAGE_LIMIT)))
        return 0

    try:
        if CHART.exists():
            stamp = datetime.now(BANGKOK).strftime("%d.%m.%Y")
            post_photo(token, chat_id, CHART,
                       f"<b>Туризм в Таиланде</b> · {stamp}")
        for part in chunk(post, MESSAGE_LIMIT):
            res = send_text(token, chat_id, part)
            if not res.get("ok"):
                raise RuntimeError(f"Telegram returned an error: {res}")
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        print(f"could not publish: {e}", file=sys.stderr)
        alert_admin(token, f"публикация в Telegram не удалась: {str(e)[:200]}",
                    "Пост в канал не вышел.")
        return 1

    print(f"published ({source}, {len(post)} characters)")
    reason = failure_reason()
    if reason:
        alert_admin(token, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
