#!/usr/bin/env python3
"""Compose the weekly tourism-demand briefing with Claude.

This is what removes the manual market context. The computed numbers come from
data.json; the model searches the web itself for what is happening right now -
fuel prices, route changes, arrival statistics - and writes the briefing.

Needs ANTHROPIC_API_KEY in the environment. Writes post.txt.
"""

import json
import os
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
OUT = ROOT / "post.txt"
ERR = ROOT / "post_error.txt"

MODEL = "claude-opus-5"
DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
ORDER = ["bangkok", "pattaya", "phuket", "krabi", "samui", "chiangmai"]
NAMES = {"bangkok": "Бангкок", "pattaya": "Паттайя", "phuket": "Пхукет",
         "krabi": "Краби", "samui": "Самуи", "chiangmai": "Чиангмай"}

SYSTEM = """\
Ты пишешь еженедельную аналитическую сводку о спросе на туризм в Таиланде для \
Telegram-канала. Читатели — владельцы и управляющие туристического бизнеса: \
бары, рестораны, отели, обмен валют, экскурсии, транспорт, \
каннабис-диспансари и смежные заведения.

Задача: объяснить, что происходит со спросом и почему, и что из этого следует \
на ближайшую неделю.

Как писать:
— По-русски, профессионально, без канцелярита и без рекламного тона.
— Ведёшь читателя от причины к следствию: что случилось в мире → как это дошло \
до Таиланда → что это значит для их выручки.
— Конкретика вместо общих слов. Цифра с источником весит больше прилагательного.
— Не пересказывай прогноз погоды по дням. Погода — только там, где она \
меняет решение (сухое окно, шторм на выходных).
— Если данные противоречат друг другу — скажи об этом прямо, не сглаживай.
— Если за неделю не произошло ничего заметного — так и напиши, коротко. \
Не раздувай пустую неделю до полноценного разбора.
— Регулирование каннабиса в Таиланде меняется часто и бьёт по выручке \
напрямую. Если за неделю были новости — законопроекты, статус растения, \
правила лицензирования и продления, требования к рецепту и учёту, \
ограничения для туристов, рейды и штрафы — разбери их отдельным блоком \
и скажи, что делать владельцу заведения. Если новостей нет — блок пропусти, \
не выдумывай и не пересказывай старое как свежее.

Читаемость — это пост в Telegram, его читают с телефона:
— Абзацы короткие, 2–3 предложения. Между блоками пустая строка.
— Ключевые цифры выделяй <b>жирным</b>, чтобы их было видно при беглом чтении.
— Каждый подзаголовок начинай с уместного эмодзи. Одного, по смыслу блока: \
✈️ авиасообщение, 💵 деньги и курс, 🏨 отели и брони, 🌧 погода, \
📊 цифры и статистика, 🎆 события, ⚠️ риски, 🌿 каннабис и регулирование, \
✅ что делать.
— В списках маркер — эмодзи или «— », но одинаковый внутри блока.
— Эмодзи только как ориентир для глаза. Не в середине предложения, \
не для украшения, не больше одного на строку.

Структура: заголовок, затем 3–5 смысловых блоков. Каждый блок — подзаголовок \
и один-два коротких абзаца. В конце — раздел «✅ Что делать на этой неделе» с \
2–4 конкретными пунктами.

Формат: Telegram HTML. Разрешены только <b>, <i>, <a href="">, <code>. \
Никакого Markdown, никаких заголовков через #. Подзаголовки — <b>жирным</b>. \
Строго не более 3200 символов вместе с тегами.

Обязательно: используй веб-поиск, чтобы найти свежие факты этой недели — \
цены на авиатопливо, изменения в маршрутах и провозных ёмкостях, статистику \
приездов, курс бата, события, а также изменения в регулировании каннабиса \
в Таиланде. Опирайся на найденное, а не на память. \
Не выдумывай цифры: если факт не нашёлся, не пиши его.

Верни только текст поста. Не пиши ничего до него: ни что собираешься искать, \
ни что нашёл, ни пояснений к своей работе. Первая строка ответа — заголовок \
поста. Без markdown-ограды."""


def summarize(payload):
    """Compact the computed data into a briefing-ready digest."""
    regions, lines = payload.get("regions", {}), []
    for rid in ORDER:
        days = regions.get(rid)
        if not days:
            continue
        today = days[0]
        fwd = days[1:8]
        if fwd:
            best = max(fwd, key=lambda d: d["score"])
            worst = min(fwd, key=lambda d: d["score"])
            avg = round(sum(d["score"] for d in fwd) / len(fwd))
            wet = sum(1 for d in fwd if d["mm"] >= 15)
            lines.append(
                f"{NAMES[rid]}: сегодня {today['score']}/100, среднее за неделю {avg}, "
                f"лучший день {DOW[date.fromisoformat(best['iso']).weekday()]} "
                f"{best['iso'][8:]}.{best['iso'][5:7]} ({best['score']}), "
                f"слабейший {DOW[date.fromisoformat(worst['iso']).weekday()]} "
                f"{worst['iso'][8:]}.{worst['iso'][5:7]} ({worst['score']}), "
                f"дней с сильным дождём: {wet}"
            )

    events = []
    for rid in ORDER:
        for d in regions.get(rid, [])[:8]:
            ev = d.get("event")
            if ev and ev.get("ru"):
                item = f"{d['iso']} — {ev['ru']} ({NAMES[rid]})"
                if item not in events:
                    events.append(item)

    fx = payload.get("fx") or {}
    parts = ["Расчётные показатели (индекс спроса 0–100, считается из погоды, "
             "структурного положения направления и календаря событий):", *lines]
    if events:
        parts += ["", "События и праздники в окне:", *events]
    if fx.get("THB"):
        parts += ["", f"Курс: {fx['THB']:.2f} бата за доллар."]
    return "\n".join(parts)


def extract_text(message):
    """Return the post only.

    The model narrates before it searches ("I'll look up..."), so the response
    holds that preamble, then the search blocks, then the post. Everything
    before the last non-text block is working commentary, not the briefing.
    """
    blocks = message.content
    last_tool = -1
    for i, b in enumerate(blocks):
        if b.type != "text":
            last_tool = i
    tail = blocks[last_tool + 1:]
    return "".join(b.text for b in tail if b.type == "text").strip()


def generate(digest):
    import anthropic

    client = anthropic.Anthropic()
    today = date.today().isoformat()
    prompt = (
        f"Сегодня {today}. Напиши еженедельную сводку.\n\n"
        f"Вот посчитанные данные — используй их как фактуру для выводов, "
        f"но не перечисляй механически:\n\n{digest}\n\n"
        f"Найди в вебе, что происходит прямо сейчас с туризмом в Таиланде: "
        f"авиатопливо, провозные ёмкости и маршруты, приезды, загрузка отелей, "
        f"курс бата, крупные события. Затем напиши пост."
    )

    # Thinking is on by default on this model and shares the max_tokens budget
    # with the response text, so a tight limit truncates the post mid-sentence.
    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Claude Opus 5 can decline a request; a server-side fallback re-runs it on
    # another model in the same call rather than returning an empty result.
    try:
        message = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
        create = lambda **kw: client.beta.messages.create(  # noqa: E731
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw
        )
    except TypeError:
        # SDK too old for the fallback parameter - proceed without it.
        message = client.messages.create(**kwargs)
        create = client.messages.create

    # Server-side web search runs its own loop; it pauses when it hits the
    # per-turn iteration cap and must be re-sent to continue.
    history = list(kwargs["messages"])
    for _ in range(5):
        if message.stop_reason != "pause_turn":
            break
        history.append({"role": "assistant", "content": message.content})
        message = create(**{**kwargs, "messages": history})

    if message.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request: {message.stop_details}")

    text = extract_text(message)
    if not text:
        raise RuntimeError(f"model returned no text (stop_reason={message.stop_reason})")
    return text


def fail(reason):
    """Record why the briefing is missing so Telegram can say so.

    The workflow step is non-blocking, which makes it green even when it
    fails - without this the only symptom is a short summary and no clue why.
    """
    print(reason, file=sys.stderr)
    ERR.write_text(reason, encoding="utf-8")
    return 1


def main():
    ERR.unlink(missing_ok=True)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fail("ключ ANTHROPIC_API_KEY не задан в секретах репозитория")
    if not DATA.exists():
        return fail("нет data.json — сборщик данных не отработал")

    digest = summarize(json.loads(DATA.read_text(encoding="utf-8")))
    try:
        post = generate(digest)
    except Exception as e:
        return fail(f"{type(e).__name__}: {e}"[:300])

    OUT.write_text(post, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} - {len(post)} characters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
