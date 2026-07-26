#!/usr/bin/env python3
"""Send the daily demand summary to Telegram.

Reads data.json (built by build_data.py) and posts one message.
Credentials come from the environment, never from the code:

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat or channel id
    DASHBOARD_URL        optional, adds a link to the full site
    SUMMARY_LANG         optional, "ru" (default) or "en"

Standard library only.
"""

import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"

NAMES = {
    "bangkok":   ("Bangkok", "Бангкок"),
    "pattaya":   ("Pattaya", "Паттайя"),
    "phuket":    ("Phuket", "Пхукет"),
    "krabi":     ("Krabi", "Краби"),
    "samui":     ("Samui", "Самуи"),
    "chiangmai": ("Chiang Mai", "Чиангмай"),
}
ORDER = ["bangkok", "pattaya", "phuket", "krabi", "samui", "chiangmai"]

DOW = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
}
TXT = {
    "en": {
        "title": "Thailand tourism demand",
        "today": "Today",
        "week": "Week ahead",
        "best": "best",
        "worst": "weakest",
        "avg": "average",
        "events": "Events",
        "rate": "Baht per dollar",
        "full": "Full dashboard",
        "tones": ["low", "below average", "moderate", "good"],
        "norain": "no heavy rain expected",
        "rainy": "heavy rain on {n} of 7 days",
    },
    "ru": {
        "title": "Спрос на туризм в Таиланде",
        "today": "Сегодня",
        "week": "Неделя вперёд",
        "best": "лучший",
        "worst": "слабейший",
        "avg": "среднее",
        "events": "События",
        "rate": "Бат за доллар",
        "full": "Полная сводка",
        "tones": ["низкий", "ниже среднего", "умеренный", "хороший"],
        "norain": "сильного дождя не ожидается",
        "rainy": "сильный дождь в {n} из 7 дней",
    },
}


def tone(score, lang):
    t = TXT[lang]["tones"]
    if score < 24:
        return "🔴", t[0]
    if score < 30:
        return "🟠", t[1]
    if score < 45:
        return "⚪", t[2]
    return "🟢", t[3]


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def dow(iso, lang):
    d = date.fromisoformat(iso)
    return DOW[lang][d.weekday()]


def build_message(data, lang, url):
    t = TXT[lang]
    regions = data.get("regions", {})
    lines = [f"<b>{esc(t['title'])}</b>"]

    built = data.get("built_at")
    if built:
        try:
            stamp = datetime.fromisoformat(built).strftime("%d.%m %H:%M UTC")
            lines.append(f"<i>{esc(stamp)}</i>")
        except ValueError:
            pass
    lines.append("")

    # today across all regions, ordered
    lines.append(f"<b>{esc(t['today'])}</b>")
    for rid in ORDER:
        days = regions.get(rid)
        if not days:
            continue
        d0 = days[0]
        dot, label = tone(d0["score"], lang)
        name = NAMES[rid][0 if lang == "en" else 1]
        rain = f"{d0['mm']:.1f} мм" if lang == "ru" else f"{d0['mm']:.1f} mm"
        lines.append(
            f"{dot} <b>{esc(name)}</b> — {d0['score']}/100 "
            f"<i>({esc(label)})</i> · {d0['tmax']}° · {esc(rain)}"
        )

    # week ahead for the lead region
    lead = regions.get(ORDER[0]) or next(iter(regions.values()), None)
    if lead and len(lead) > 1:
        fwd = lead[1:8]
        best = max(fwd, key=lambda x: x["score"])
        worst = min(fwd, key=lambda x: x["score"])
        avg = round(sum(x["score"] for x in fwd) / len(fwd))
        wet = sum(1 for x in fwd if x["mm"] >= 15)
        name = NAMES[ORDER[0]][0 if lang == "en" else 1]

        lines.append("")
        lines.append(f"<b>{esc(t['week'])}</b> — {esc(name)}")
        lines.append(
            f"{t['avg']} {avg} · {t['best']} {dow(best['iso'], lang)} "
            f"{best['iso'][8:]}.{best['iso'][5:7]} ({best['score']}) · "
            f"{t['worst']} {dow(worst['iso'], lang)} "
            f"{worst['iso'][8:]}.{worst['iso'][5:7]} ({worst['score']})"
        )
        lines.append(t["norain"] if wet == 0 else t["rainy"].format(n=wet))

    # upcoming events across all regions
    seen, evs = set(), []
    for rid in ORDER:
        for d in regions.get(rid, [])[:8]:
            ev = d.get("event")
            if not ev:
                continue
            label = ev.get(lang) or ev.get("en") or ""
            key = (d["iso"], label)
            if label and key not in seen:
                seen.add(key)
                evs.append(
                    f"{dow(d['iso'], lang)} {d['iso'][8:]}.{d['iso'][5:7]} — {esc(label)}"
                )
    if evs:
        lines.append("")
        lines.append(f"<b>{esc(t['events'])}</b>")
        lines.extend(evs[:6])

    fx = data.get("fx") or {}
    if fx.get("THB"):
        lines.append("")
        lines.append(f"{esc(t['rate'])}: <b>{fx['THB']:.2f}</b>")

    if url:
        lines.append("")
        lines.append(f'<a href="{esc(url)}">{esc(t["full"])}</a>')

    return "\n".join(lines)


def send(token, chat_id, text):
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set; skipping send.",
            file=sys.stderr,
        )
        return 0

    if not DATA.exists():
        print("data.json is missing - run build_data.py first.", file=sys.stderr)
        return 1

    data = json.loads(DATA.read_text(encoding="utf-8"))
    lang = os.environ.get("SUMMARY_LANG", "ru").lower()
    if lang not in TXT:
        lang = "ru"

    text = build_message(data, lang, os.environ.get("DASHBOARD_URL", ""))

    if os.environ.get("DRY_RUN"):
        print(text)
        return 0

    try:
        res = send(token, chat_id, text)
    except urllib.error.HTTPError as e:
        print(f"Telegram rejected the message: {e.read().decode()}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"Could not reach Telegram: {e}", file=sys.stderr)
        return 1

    if not res.get("ok"):
        print(f"Telegram returned an error: {res}", file=sys.stderr)
        return 1
    print("summary sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
