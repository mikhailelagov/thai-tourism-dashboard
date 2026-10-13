#!/usr/bin/env python3
"""Write a factual Russian tourism brief from committed data, using no API.

Every number keeps its period, source link and publication age, because the
readers act on it. Collected files can be missing or partly stale (a collector
keeps the last good section when a source fails), so each section degrades to
an explicit "no data" line instead of a guess, and repeats the collector's
error next to the data it could not refresh.
"""

import argparse
import calendar
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BANGKOK = ZoneInfo("Asia/Bangkok")
ROOT = Path(__file__).resolve().parent.parent
REGIONS = {
    "pattaya": "Паттайя", "phuket": "Пхукет", "bangkok": "Бангкок",
    "krabi": "Краби", "samui": "Самуи", "chiangmai": "Чиангмай",
}
CAMERA_REGIONS = {rid: REGIONS[rid] for rid in ("pattaya", "phuket", "bangkok")}
SCOPES = {
    "Thailand": "Весь Таиланд",
    "Central including Bangkok": "Центральный регион, включая Бангкок",
    "South": "Южный регион",
    "North": "Северный регион",
}
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
KEY_MARKETS = (
    ("Russian Federation", "Россия"), ("China", "Китай"), ("India", "Индия"),
    ("Kazakhstan", "Казахстан"), ("Israel", "Израиль"), ("Korea (Republic of)", "Южная Корея"),
    ("Malaysia", "Малайзия"), ("United Kingdom", "Великобритания"), ("Germany", "Германия"),
)
PROVINCES = {
    "pattaya": "Чонбури (включая Паттайю)", "phuket": "Пхукет", "bangkok": "Бангкок",
    "krabi": "Краби", "samui": "Сураттани (включая Самуи)", "chiangmai": "Чиангмай",
    "thailand": "Весь Таиланд",
}
PROVINCE_NOTE = ("Провинция шире курорта; посещения провинций нельзя складывать "
                 "и сравнивать с прибытиями в страну.")
NEWS_TOPICS = (
    ("cannabis", "Каннабис"), ("entry_rules", "Въезд и визы"), ("flights", "Авиасообщение"),
    ("statistics", "Статистика"), ("alcohol", "Алкоголь"), ("events", "События"),
    ("safety", "Безопасность"), ("other", "Официально и Паттайя"),
)
# Days from the end of a month after which the next month is overdue.
OVERDUE_DAYS = {"arrivals": 40, "provinces": 50}
SOURCE_NAMES = {"arrivals": "MOTS (прибытия)", "provinces": "MOTS (провинции)", "bot": "BOT",
                "ecb": "ЕЦБ", "cbr": "Банк России"}


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value, digits=2):
    text = f"{value:.{digits}f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text).replace(".", ",")


def count(value):
    return f"{round(value):,}".replace(",", " ")


def change(value, digits=1, unit="%"):
    if not number(value):
        return None
    value = round(value, digits)
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{fmt(abs(value), digits)}{unit}"


def section(value, key):
    item = value.get(key) if isinstance(value, dict) else None
    return item if isinstance(item, dict) else {}


def month(value):
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except (TypeError, ValueError):
        return None


def month_name(period):
    return f"{MONTHS[period.month - 1]} {period.year}"


def month_end(period):
    return period.replace(day=calendar.monthrange(period.year, period.month)[1])


def safe_url(value):
    if not isinstance(value, str) or not value.startswith(("https://", "http://")) or re.search(r"\s", value):
        return None
    return value.replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def link(text, url):
    url = safe_url(url)
    return f"[{text}]({url})" if url else text


def md(value):
    """Untrusted text (news titles, collector errors) must not inject Markdown."""
    text = " ".join(clean(value).split())
    for char in "\\`*_[]<>":
        text = text.replace(char, "\\" + char)
    return text


def local(value):
    return value.astimezone(BANGKOK).strftime("%d.%m.%Y %H:%M") if value else "неизвестно"


def clean(value):
    return str(value).replace("|", "/").replace("\n", " ")


def region_id(value):
    return str(value or "").lower().replace(" ", "").replace("kohsamui", "samui")


def camera_status(cam, valid=True):
    status = {"ok": "проверен", "experimental": "экспериментальный",
              "unavailable": "недоступен", "inactive": "вне времени наблюдения"}.get(
                  cam.get("status"), "старый замер без проверки качества")
    if cam.get("status") == "ok" and not valid:
        status = "замер исключён: неверное время, число или версия проверки"
    reason = cam.get("reason")
    if reason:
        reasons = {
            "provider_access_required": "источник требует доступ через браузер или разрешение владельца",
            "camera_not_validated": "детализация кадра и область подсчёта ещё не проверены",
            "missing_api_key": "не настроен ключ доступа к источнику",
            "source_offline": "камера не передаёт изображение",
            "source_request_failed": "не удалось получить ответ источника",
            "outside_observation_hours": "сейчас вне установленного времени наблюдения",
            "camera_not_found": "камера отсутствует в каталоге источника",
            "missing_frame": "источник не предоставил кадр",
            "stale_frame": "кадр устарел",
            "low_resolution": "недостаточно деталей для надёжного подсчёта",
            "source_not_configured": "источник ещё не настроен",
            "not_configured": "источник ещё не настроен",
            "access_denied": "источник ограничил автоматический доступ",
            "catalog_unavailable": "каталог камер временно недоступен",
        }
        reason = str(reason)
        explanation = reasons.get(reason.split(":", 1)[0].strip())
        if explanation is None:
            explanation = reason if any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in reason) else "причина недоступности требует проверки источника"
        status += ": " + clean(explanation)
    return status


def camera_lines(footfall, regions, now):
    start = now - timedelta(hours=24)
    found = {rid: [] for rid in regions}
    rows = [row for row in footfall.get("rows", []) if isinstance(row, dict)]
    modern_rows = [row for row in rows if row.get("schema_version") == 2]
    for row in modern_rows or rows:
        attempted = stamp(row.get("at"))
        if not attempted or not start <= attempted <= now:
            continue
        for cid, cam in (row.get("cams") or {}).items():
            if not isinstance(cam, dict):
                continue
            rid = region_id(cam.get("region"))
            if rid in found:
                observed = stamp(cam.get("observed_at"))
                valid = (cam.get("status") == "ok" and number(cam.get("people"))
                         and cam["people"] >= 0 and observed is not None
                         and start <= observed <= now and bool(cam.get("series_version")))
                found[rid].append((cid, cam, observed, valid, attempted))
    lines = [
        "## Камеры за последние 24 часа",
        f"Окно: {local(start)} — {local(now)}, Бангкок. Учитываются только проверенные кадры; отсутствие замера не равно нулю.",
        "", "| Направление | Покрытие проверенными кадрами | Последний проверенный кадр | Состояние камер |",
        "|---|---|---|---|",
    ]
    for rid, name in regions.items():
        observations = found[rid]
        valid = [item for item in observations if item[3]]
        cameras = {item[0] for item in valid}
        hours = {min(23, int((item[2] - start).total_seconds() // 3600)) for item in valid}
        if valid:
            _, cam, observed, _, _ = max(valid, key=lambda item: item[2])
            title = clean(cam.get("title") or "Камера")
            url = cam.get("source_url")
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                title = f"[{title}]({url})"
            latest = f"{title}: {fmt(cam['people'])} чел. в кадре, {local(observed)}"
            coverage = f"Камер: {len(cameras)}; часов с хотя бы одним проверенным кадром: {len(hours)}"
        else:
            coverage = "Нет проверенных наблюдений"
            latest = ("Есть экспериментальные или непригодные замеры; численность неизвестна"
                      if observations else "Данных за окно нет")
        current = {}
        for cid, cam, _, valid_frame, attempted in observations:
            if cid not in current or attempted > current[cid][0]:
                current[cid] = (attempted, cam, valid_frame)
        statuses = [f"{clean(cam.get('title') or cid)} — {camera_status(cam, valid_frame)}"
                    for cid, (_, cam, valid_frame) in sorted(current.items())]
        state = "<br>".join(statuses) if statuses else "За последние 24 часа попыток сбора нет"
        lines.append(f"| {clean(name)} | {coverage} | {latest} | {state} |")
    lines += ["", "Число людей в отдельном кадре не показывает количество туристов в городе. "
              "Прогноз численности и проценты изменения не рассчитываются: нужна сопоставимая история проверенных камер."]
    return lines


def weather_condition(day):
    mm, pop = day.get("mm"), day.get("pop")
    if not number(mm) and not number(pop):
        return "недостаточно данных об осадках"
    if (number(mm) and mm >= 10) or (number(pop) and pop >= 70):
        text = "дождь может мешать прогулкам; нужен вариант под крышей"
    elif (number(mm) and mm >= 2) or (number(pop) and pop >= 40):
        text = "возможны дожди; планировать укрытие"
    else:
        text = "осадки мало ограничивают прогулки"
    if number(day.get("tmax")) and day["tmax"] >= 35:
        text += "; жарко, прогулки удобнее утром или вечером"
    return text


def weather_lines(data, regions, now):
    today = now.astimezone(BANGKOK).date()
    end = today + timedelta(days=2)
    built = stamp(data.get("built_at"))
    lines = ["## Погода: сегодня и следующие два дня",
             f"Период {today:%d.%m}–{end:%d.%m}. [Open-Meteo](https://open-meteo.com/): "
             f"сбор данных {local(built)}, Бангкок; время выпуска модели в файле не сохранено."]
    if built is None or built > now:
        lines.append("⚠ Свежесть погоды не подтверждена: корректного времени сбора нет.")
    elif now - built > timedelta(hours=36):
        lines.append("⚠ Погода устарела: данным более 36 часов; перед планированием нужен свежий прогноз.")
    lines.append("")
    for rid, name in regions.items():
        days = {}
        for item in (data.get("regions") or {}).get(rid, []):
            try:
                when = date.fromisoformat(item.get("iso", ""))
            except (TypeError, ValueError):
                continue
            if today <= when <= end:
                days[when] = item
        outlook = []
        for offset in range(3):
            when = today + timedelta(days=offset)
            item = days.get(when)
            if item is None:
                outlook.append(f"{when:%d.%m}: данных нет")
                continue
            temp = f", до {fmt(item['tmax'])} °C" if number(item.get("tmax")) else ""
            outlook.append(f"{when:%d.%m}: {weather_condition(item)}{temp}")
        lines.append(f"- **{clean(name)}:** " + "; ".join(outlook) + ".")
    lines += ["", "Это условия для активности на улице, а не прогноз туристического спроса."]
    return lines


def official_errors(official, *sources):
    errors = official.get("errors") if isinstance(official, dict) else None
    lines = []
    for item in errors if isinstance(errors, list) else []:
        if isinstance(item, dict) and item.get("source") in sources:
            reason = md(item.get("error") or "причина не указана")
            lines.append(f"⚠ Последнее обновление источника {SOURCE_NAMES[item['source']]} не удалось "
                         f"({local(stamp(item.get('at')))}, Бангкок): {reason}. "
                         "Показаны последние успешно полученные данные, если они есть.")
    return lines


def freshness(period, published, now):
    today, end = now.astimezone(BANGKOK).date(), month_end(period)
    if published is None:
        text = "дата публикации не указана"
    elif published > now:
        text = "дата публикации в будущем, не подтверждена"
    else:
        day = published.astimezone(BANGKOK).date()
        text = (f"опубликовано {local(published)} (Бангкок), {(today - day).days} дн. назад; "
                f"задержка публикации {(day - end).days} дн.")
    return f"{text}; от конца периода до отчёта {(today - end).days} дн."


def mots_header(block, period, now, kind):
    status = {"preliminary": "предварительные данные", "revised": "уточнённые данные"}.get(
        block.get("status"), "статус данных не указан")
    sources = [link("MOTS", block.get("source_url"))]
    if safe_url(block.get("file_url")):
        sources.append(link("файл XLSX", block["file_url"]))
    lines = [f"{', '.join(sources)}: период {month_name(period)} ({period:%Y-%m}), {status}; "
             f"{freshness(period, stamp(block.get('published_at')), now)} "
             f"Получено {local(stamp(block.get('retrieved_at')))} (Бангкок)."]
    overdue = (now.astimezone(BANGKOK).date() - month_end(period)).days
    if overdue > OVERDUE_DAYS[kind]:
        lines.append(f"⚠ От конца периода прошло {overdue} дн. — обычно MOTS к этому сроку "
                     "уже публикует следующий месяц; более свежие данные не получены.")
    return lines


def compared(block, year):
    text = f", {change(block.get('yoy_pct'))} г/г" if number(block.get("yoy_pct")) else ""
    if number(block.get("prev_year")):
        text += f" ({year}: {count(block['prev_year'])})"
    return text


def arrivals_lines(official, now):
    lines = ["## Прибытия иностранцев (MOTS)"]
    block = section(official, "arrivals")
    period, total = month(block.get("period")), section(block, "total")
    errors = official_errors(official, "arrivals")
    if period is None or not number(total.get("value")):
        return lines + ["Нет доступных данных MOTS о прибытиях."] + errors
    lines += mots_header(block, period, now, "arrivals") + errors + [""]
    lines.append(f"- **{month_name(period).capitalize()}: {count(total['value'])}** прибытий"
                 f"{compared(total, period.year - 1)}.")
    ytd = section(block, "ytd")
    start, end = month(ytd.get("from")), month(ytd.get("to"))
    if start and end and number(ytd.get("value")):
        lines.append(f"- **С начала года ({start:%m}–{end:%m.%Y}): {count(ytd['value'])}**"
                     f"{compared(ytd, end.year - 1)}.")
    markets = {}
    for item in block.get("markets") if isinstance(block.get("markets"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            markets.setdefault(re.sub(r"[^a-z]", "", item["name"].lower()), item)
    lines += ["", f"Ключевые для Паттайи рынки, {period:%m.%Y}:", "",
              "| Рынок | Прибытия | Изменение г/г |", "|---|---|---|"]
    for name, label in KEY_MARKETS:
        item = markets.get(re.sub(r"[^a-z]", "", name.lower()), {})
        if number(item.get("value")):
            lines.append(f"| {label} | {count(item['value'])} | {change(item.get('yoy_pct')) or '—'} |")
        else:
            lines.append(f"| {label} | нет данных | — |")
    lines += ["", "Прибытия — въезды иностранцев в Таиланд в целом, а не в Паттайю; "
              "один человек может въехать несколько раз."]
    return lines


def province_cells(row):
    occupancy = foreign = revenue = "нет данных"
    if number(row.get("occupancy_pct")):
        delta = row.get("occupancy_change_pp")
        if not number(delta) and number(row.get("occupancy_prev_pct")):
            delta = row["occupancy_pct"] - row["occupancy_prev_pct"]
        occupancy = f"{fmt(row['occupancy_pct'])}%"
        if number(delta):
            occupancy += f" ({change(delta, 2, ' п.п.')} г/г)"
    if number(row.get("foreign_visitors")):
        foreign = count(row["foreign_visitors"])
        if number(row.get("foreign_yoy_pct")):
            foreign += f" ({change(row['foreign_yoy_pct'])} г/г)"
    if number(row.get("revenue_yoy_pct")):
        revenue = f"{change(row['revenue_yoy_pct'])} г/г"
        if number(row.get("revenue_mb")):
            revenue += f" ({count(row['revenue_mb'])} млн бат)"
    return occupancy, foreign, revenue


def provinces_lines(official, now):
    lines = ["## Провинции (MOTS)"]
    block = section(official, "provinces")
    period, rows = month(block.get("period")), section(block, "rows")
    errors = official_errors(official, "provinces")
    if period is None or not rows:
        return lines + ["Нет доступных данных MOTS по провинциям."] + errors
    lines += mots_header(block, period, now, "provinces") + errors + [
        "", "| Провинция | Загрузка отелей | Иностранные посещения | Доход от посетителей |",
        "|---|---|---|---|"]
    for key, label in PROVINCES.items():
        row = section(rows, key)
        name = md(row.get("label_ru") or label)
        lines.append(f"| {name} | " + " | ".join(province_cells(row)) + " |")
    note = block.get("note_ru") if isinstance(block.get("note_ru"), str) and block["note_ru"].strip() else PROVINCE_NOTE
    lines += ["", f"{md(note)} Посещения и доход — оценки MOTS по данным размещения, а не кассовые данные."]
    return lines


def news_lines(news, now, days=7, limit=12, per_topic=3):
    lines = ["## Новости за 7 дней"]
    built = stamp(news.get("built_at"))
    items = news.get("items")
    if built is None and not isinstance(items, list):
        return lines + ["Новости не загружены."]
    start = now - timedelta(days=days)
    groups = {key: [] for key, _ in NEWS_TOPICS}
    # Official items lead their topic: keyword tags also catch off-topic media stories.
    for item in sorted((i for i in items if isinstance(i, dict)) if isinstance(items, list) else (),
                       key=lambda i: (i.get("official") is True, stamp(i.get("published_at")) or start),
                       reverse=True):
        published, url = stamp(item.get("published_at")), safe_url(item.get("url"))
        title = md(item.get("title") or "")
        title = title if len(title) <= 220 else title[:217].rstrip("\\") + "…"
        if published is None or not start <= published <= now or not url or not title:
            continue
        topics = item.get("topics") if isinstance(item.get("topics"), list) else []
        topic = next((key for key, _ in NEWS_TOPICS[:-1] if key in topics), None)
        if topic is None and (item.get("official") is True or "pattaya" in topics):
            topic = "other"
        if topic is not None:
            groups[topic].append((item, published, url, title))
    lines.append(f"Окно: {local(start)} — {local(now)}, Бангкок. Сбор новостей: {local(built)}.")
    if built is None or built > now or now - built > timedelta(hours=36):
        lines.append("⚠ Свежесть новостей не подтверждена: последний сбор старше 36 часов или без времени.")
    failed = [source for source in news.get("sources") or [] if isinstance(source, dict) and source.get("ok") is False]
    if failed:
        lines.append("Не обновились: " + "; ".join(
            f"{md(s.get('name') or 'источник')} ({md(s.get('error') or 'причина не указана')})" for s in failed) + ".")
    shown, total = 0, sum(len(items) for items in groups.values())
    for key, label in NEWS_TOPICS:
        picked = groups[key][:min(per_topic, limit - shown)]
        if not picked:
            continue
        lines += ["", f"**{label}**"]
        for item, published, url, title in picked:
            notes = [md(item.get("source") or "источник не указан"), f"{published.astimezone(BANGKOK):%d.%m.%Y}"]
            if item.get("official") is True:
                notes.append("официальный источник")
            if item.get("lang") == "th":
                notes.append("на тайском")
            lines.append(f"- [{title}]({url}) — {', '.join(notes)}")
        shown += len(picked)
    if not shown:
        return lines + ["", "За 7 дней нет новостей по отслеживаемым темам."]
    return lines + ["", f"Показано {shown} из {total} новостей по темам; заголовки — как у источника, без проверки фактов."]


def indicator_lines(indicators, now, official=None):
    lines = ["## Опубликованная статистика отелей"]
    bot = section(official, "bot")
    checked = stamp(bot.get("retrieved_at"))
    if checked:
        period = month(bot.get("latest_period"))
        period = period.strftime("%Y-%m") if period else "не указан"
        lines.append(f"Последняя проверка {link('BOT', bot.get('source_url'))}: {local(checked)} (Бангкок); "
                     f"последний период в отчёте: {period}.")
    lines += official_errors(official, "bot")
    labels = {"hotel_occupancy": "Фактическая загрузка отелей",
              "advanced_booking_3_months": "Предварительное бронирование на 3 месяца"}
    latest = {}
    for row in indicators:
        if (row.get("metric") not in labels or not number(row.get("value"))
                or row.get("unit") != "percent" or not 0 <= row["value"] <= 100
                or row.get("status") in {"unavailable", "invalid", "missing"}):
            continue
        try:
            period = datetime.strptime(row.get("period", ""), "%Y-%m").date()
        except (TypeError, ValueError):
            continue
        published = stamp(row.get("published_at"))
        if published is None or published > now or period > now.astimezone(BANGKOK).date():
            continue
        key = row["metric"], row.get("region", "неизвестный охват")
        rank = (period, published)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, row)
    if not latest:
        return lines + ["Нет доступных опубликованных значений загрузки и предварительного бронирования."]
    lines += ["Месячные данные BOT/MOTS; это опубликованные наблюдения, а не оценка сегодняшнего дня.", ""]
    for metric, label in labels.items():
        matches = [(scope, value) for (kind, scope), value in latest.items() if kind == metric]
        if not matches:
            lines.append(f"- **{label}:** данных нет.")
        for scope, ((period, published), row) in sorted(matches):
            end = month_end(period)
            lag = (now.astimezone(BANGKOK).date() - end).days
            publication_lag = (published.astimezone(BANGKOK).date() - end).days
            source = row.get("source_url")
            source_text = (f"[BOT]({source})" if isinstance(source, str) and source.startswith(("https://", "http://"))
                           else "источник не указан")
            provisional = "; предварительные данные" if row.get("status") == "preliminary" else ""
            lines.append(f"- **{label} — {clean(SCOPES.get(scope, scope))}: {fmt(row['value'])}%**, "
                         f"период {period:%Y-%m}; {source_text}, отметка публикации/обновления {local(published)} "
                         f"(Бангкок); задержка публикации {publication_lag} дн., от конца периода до отчёта {lag} дн.{provisional}.")
    lines += ["", "Широкие регионы статистики нельзя приравнивать к отдельным городам: "
              "«Юг» не означает отдельно Пхукет, Самуи или Краби; «Север» — отдельно Чиангмай; "
              "«Центр» — отдельно Паттайю или Бангкок. "
              "Бронирование на 3 месяца — показатель опубликованного периода, не обещанная будущая загрузка."]
    return lines


def rate(value):
    return fmt(value, 2 if value >= 1 else 4)


def fx_date(value, now, ahead=0):
    """Return (date, warning); the Bank of Russia sets tomorrow's rate today."""
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None, "дата курса не указана"
    age = (now.astimezone(BANGKOK).date() - day).days
    if age < -ahead:
        return day, "дата курса в будущем, не подтверждена"
    if age > 4:
        return day, f"курсу {age} дн.; свежее значение не получено"
    return day, None


def official_fx_lines(official, now):
    fx = section(official, "fx")
    ecb, cbr = section(fx, "ecb"), section(fx, "cbr")
    rates = section(ecb, "thb_per")
    lines = []
    quoted = []
    for code in ("USD", "EUR", "CNY", "INR", "KRW", "MYR", "GBP"):
        value = rates.get(code)
        if number(value) and value > 0:
            unit = 100 if code == "KRW" else 1
            quoted.append(f"{unit} {code} = {rate(value * unit)} THB")
    if quoted:
        day, warning = fx_date(ecb.get("date"), now)
        when = f"{day:%d.%m.%Y}" if day else "неизвестную дату"
        lines.append(f"- {link('ЕЦБ', ecb.get('source_url'))}, справочные курсы на {when} (пересчёт через евро): "
                     + "; ".join(quoted) + ".")
        if warning:
            lines.append(f"  ⚠ ЕЦБ: {warning}.")
    value = cbr.get("rub_per_thb")
    if number(value) and value > 0:
        day, warning = fx_date(cbr.get("date"), now, ahead=3)
        when = f"{day:%d.%m.%Y}" if day else "неизвестную дату"
        lines.append(f"- {link('Банк России', cbr.get('source_url'))}, официальный курс на {when}: "
                     f"1 THB = {fmt(value, 4)} RUB (1 RUB = {fmt(1 / value, 4)} THB).")
        if warning:
            lines.append(f"  ⚠ Банк России: {warning}.")
    if lines:
        if not quoted:
            lines.append("- Курсы ЕЦБ недоступны.")
        if not (number(value) and value > 0):
            lines.append("- Курс Банка России недоступен.")
        lines += ["", "Это официальные справочные курсы, а не котировки обменников."]
    return lines


def currency_lines(data, now, official=None):
    errors = official_errors(official, "ecb", "cbr")
    lines = official_fx_lines(official, now)
    if lines:
        return ["## Курсы"] + lines + errors
    fx = data.get("fx") or {}
    thb = fx.get("THB")
    rates = []
    for code in ("USD", "EUR", "RUB"):
        value = fx.get(code)
        if number(thb) and thb > 0 and number(value) and value > 0:
            rates.append(f"1 {code} = {fmt(thb / value)} THB")
    if not rates:
        return ["## Курсы", "Доступных наблюдений курса нет."] + errors
    # The provider's own update time; build_data carries an old snapshot
    # forward when the provider fails, so built_at would date it wrongly.
    quoted = stamp(data.get("fx_time"))
    when = (f"Котировка поставщика: {local(quoted)}, Бангкок." if quoted
            else "Время котировки в файле не сохранено.")
    lines = ["## Курсы", "; ".join(rates) + ". "
             "[ExchangeRate-API](https://open.er-api.com/v6/latest/USD), неофициальный агрегатор: "
             f"официальные курсы ЕЦБ и Банка России недоступны. {when}"]
    if quoted is None or quoted > now or now - quoted > timedelta(hours=36):
        lines.append("⚠ Свежесть курсов не подтверждена; это не текущая котировка обменника.")
    return lines + errors


def render_report(data, footfall, indicators, now, regions=None, official=None, news=None):
    regions = regions or CAMERA_REGIONS
    official = official if isinstance(official, dict) else {}
    news = news if isinstance(news, dict) else {}
    now = now.astimezone(BANGKOK)
    lines = [f"# Утренняя сводка по Таиланду — {now:%d.%m.%Y}",
             f"Составлено {local(now)}, Asia/Bangkok.", ""]
    parts = (camera_lines(footfall, regions, now), weather_lines(data, REGIONS, now),
             arrivals_lines(official, now), provinces_lines(official, now),
             indicator_lines(indicators, now, official), news_lines(news, now),
             currency_lines(data, now, official))
    for part in parts:
        lines += part + [""]
    return "\n".join(lines)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_reports(root, now=None, indicators_path=None):
    now = now or datetime.now(timezone.utc)
    indicators = []
    path = indicators_path or root / "tourism" / "indicators.jsonl"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    indicators.append(row)
            except ValueError:
                continue
    except OSError:
        pass
    report = render_report(read_json(root / "data.json"), read_json(root / "footfall.json"), indicators, now,
                           official=read_json(root / "tourism" / "official.json"),
                           news=read_json(root / "tourism" / "news.json"))
    directory = root / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    paths = (directory / "tourism-latest.md",
             directory / f"{now.astimezone(BANGKOK):%Y-%m-%d}-tourism.md")
    for output in paths:
        output.write_text(report, encoding="utf-8")
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--indicators", type=Path)
    args = parser.parse_args()
    for path in write_reports(args.root, indicators_path=args.indicators):
        print(path)


if __name__ == "__main__":
    main()
