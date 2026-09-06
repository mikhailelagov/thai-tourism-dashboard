#!/usr/bin/env python3
"""Write a factual Russian tourism brief from committed data, using no API."""

import argparse
import calendar
import json
import math
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
}


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt(value):
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


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


def indicator_lines(indicators, now):
    lines = ["## Опубликованная статистика отелей"]
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
            month_end = period.replace(day=calendar.monthrange(period.year, period.month)[1])
            lag = (now.astimezone(BANGKOK).date() - month_end).days
            publication_lag = (published.astimezone(BANGKOK).date() - month_end).days
            source = row.get("source_url")
            source_text = (f"[BOT]({source})" if isinstance(source, str) and source.startswith(("https://", "http://"))
                           else "источник не указан")
            provisional = "; предварительные данные" if row.get("status") == "preliminary" else ""
            lines.append(f"- **{label} — {clean(SCOPES.get(scope, scope))}: {fmt(row['value'])}%**, "
                         f"период {period:%Y-%m}; {source_text}, отметка публикации/обновления {local(published)} "
                         f"(Бангкок); задержка публикации {publication_lag} дн., от конца периода до отчёта {lag} дн.{provisional}.")
    lines += ["", "Широкие регионы статистики нельзя приравнивать к отдельным городам: "
              "«Юг» не означает отдельно Пхукет, Самуи или Краби; «Центр» не означает отдельно Паттайю или Бангкок. "
              "Бронирование на 3 месяца — показатель опубликованного периода, не обещанная будущая загрузка."]
    return lines


def currency_lines(data, now):
    fx = data.get("fx") or {}
    thb = fx.get("THB")
    if not number(thb) or thb <= 0:
        return ["## Валюта", "Доступных наблюдений курса нет."]
    rates = []
    for code in ("USD", "EUR", "RUB"):
        value = fx.get(code)
        if number(value) and value > 0:
            rates.append(f"1 {code} = {fmt(thb / value)} THB")
    if not rates:
        return ["## Валюта", "Доступных наблюдений курса нет."]
    built = stamp(data.get("built_at"))
    lines = ["## Валюта", "; ".join(rates) + ". "
             "[ExchangeRate-API](https://open.er-api.com/v6/latest/USD). "
             f"Снимок сборщика: {local(built)}, Бангкок; точное время котировки в файле не сохранено."]
    if built is None or built > now or now - built > timedelta(hours=36):
        lines.append("⚠ Свежесть курсов не подтверждена; это не текущая котировка обменника.")
    return lines


def render_report(data, footfall, indicators, now, regions=None):
    regions = regions or CAMERA_REGIONS
    now = now.astimezone(BANGKOK)
    lines = [f"# Утренняя сводка по Таиланду — {now:%d.%m.%Y}",
             f"Составлено {local(now)}, Asia/Bangkok.", ""]
    sections = (camera_lines(footfall, regions, now), weather_lines(data, REGIONS, now),
                indicator_lines(indicators, now), currency_lines(data, now))
    for section in sections:
        lines += section + [""]
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
    report = render_report(read_json(root / "data.json"), read_json(root / "footfall.json"), indicators, now)
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
