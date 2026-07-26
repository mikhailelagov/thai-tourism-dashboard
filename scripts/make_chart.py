#!/usr/bin/env python3
"""Render the week-ahead demand chart that accompanies the Telegram briefing.

Small multiples: one panel per region, so the panel title carries identity and
colour never has to. Each region gets two stacked lanes - the demand index as a
line, and rainfall as a sequential strip below it. Two lanes rather than a
second y-axis, so two measures of different scale never share one scale.

Reads data.json, writes chart.png.
"""

import json
import pathlib
import sys
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
OUT = ROOT / "chart.png"

# Verified >= 3:1 against a white surface.
ACCENT = "#0F7B82"
INK = "#1C282A"
MUTED = "#5B6A6C"
GRID = "#E4DED1"
EVENT = "#B07A1C"
SURFACE = "#FFFFFF"

# Sequential ramp for rainfall: one hue, light to dark.
RAIN = LinearSegmentedColormap.from_list("rain", ["#F4F1EA", "#0F7B82"])
RAIN_MAX = 30.0

# Reserved status palette - never reused as series colours.
TIERS = [(24, "#B84A31", "низкий"),
         (30, "#B07A1C", "ниже среднего"),
         (45, "#5F6C72", "умеренный"),
         (101, "#2E7D57", "хороший")]

ORDER = ["bangkok", "pattaya", "phuket", "krabi", "samui", "chiangmai"]
NAMES = {"bangkok": "Бангкок", "pattaya": "Паттайя", "phuket": "Пхукет",
         "krabi": "Краби", "samui": "Самуи", "chiangmai": "Чиангмай"}
DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def tier(score):
    for limit, colour, label in TIERS:
        if score < limit:
            return colour, label
    return TIERS[-1][1], TIERS[-1][2]


def panel(fig, cell, rid, days):
    """One region: index line on top, rainfall strip underneath."""
    inner = cell.subgridspec(2, 1, height_ratios=[5, 1], hspace=.12)
    ax = fig.add_subplot(inner[0])
    rx = fig.add_subplot(inner[1], sharex=ax)

    xs = list(range(len(days)))
    ys = [d["score"] for d in days]
    mm = [d["mm"] for d in days]
    avg = sum(ys) / len(ys)

    ax.set_facecolor(SURFACE)
    ax.plot(xs, ys, color=ACCENT, linewidth=2, solid_capstyle="round", zorder=4)
    ax.fill_between(xs, 0, ys, color=ACCENT, alpha=.10, zorder=2)
    ax.axhline(avg, color=MUTED, linewidth=1, linestyle=(0, (4, 3)),
               alpha=.55, zorder=3)

    top = max(60, max(ys) + 16)

    # event days explain why the line moves - mark them, then name them below
    for i, d in enumerate(days):
        if d.get("event"):
            ax.axvline(i, color=EVENT, linewidth=1, linestyle=(0, (2, 2)),
                       alpha=.7, zorder=1)

    colour, label = tier(ys[0])
    ax.plot([0], [ys[0]], "o", markersize=8, color=colour,
            markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)

    fwd = ys[1:]
    if fwd:
        bi = ys.index(max(fwd), 1)
        ax.plot([bi], [ys[bi]], "o", markersize=6.5, color=SURFACE,
                markeredgecolor=ACCENT, markeredgewidth=2, zorder=5)
        ax.annotate(f"{ys[bi]}", (bi, ys[bi]), textcoords="offset points",
                    xytext=(0, 9), ha="center", fontsize=8,
                    color=ACCENT, fontweight="bold", zorder=6)

    ax.set_title(NAMES.get(rid, rid), loc="left", fontsize=11.5,
                 fontweight="bold", color=INK, pad=23)
    ax.text(0, 1.10, f"{ys[0]} · {label}", transform=ax.transAxes,
            fontsize=9, color=colour, fontweight="bold", va="bottom")
    ax.text(1, 1.10, f"среднее {round(avg)}", transform=ax.transAxes,
            fontsize=8.5, color=MUTED, va="bottom", ha="right")

    ax.set_ylim(0, top)
    ax.yaxis.set_major_locator(MultipleLocator(20))
    ax.tick_params(axis="y", labelsize=8, colors=MUTED, length=0)
    ax.tick_params(axis="x", labelbottom=False, length=0)
    ax.grid(axis="y", color=GRID, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)

    # rainfall strip: darker cell = wetter day
    for i, v in enumerate(mm):
        rx.axvspan(i - .5, i + .5, color=RAIN(min(v, RAIN_MAX) / RAIN_MAX),
                   linewidth=0)
        if v >= 10:
            rx.text(i, .5, f"{v:.0f}", ha="center", va="center", fontsize=7,
                    color=SURFACE if v >= 18 else INK, fontweight="bold")

    rx.set_xlim(-.5, len(days) - .5)
    rx.set_ylim(0, 1)
    rx.set_yticks([])
    rx.set_xticks(xs)
    rx.set_xticklabels([DOW[date.fromisoformat(d["iso"]).weekday()] for d in days],
                       fontsize=8, color=MUTED)
    rx.tick_params(axis="x", length=0, pad=3)
    for side in ("top", "right", "left", "bottom"):
        rx.spines[side].set_visible(False)


def build():
    if not DATA.exists():
        print("data.json is missing - run build_data.py first.", file=sys.stderr)
        return 1

    payload = json.loads(DATA.read_text(encoding="utf-8"))
    regions = payload.get("regions", {})
    present = [r for r in ORDER if regions.get(r)]
    if not present:
        print("data.json has no regions.", file=sys.stderr)
        return 1

    fig = plt.figure(figsize=(11, 7.2), dpi=170, facecolor=SURFACE)
    gs = fig.add_gridspec(2, 3, left=.05, right=.98, top=.80, bottom=.13,
                          wspace=.20, hspace=.62)

    # collect each event once with the span it covers - a four-day festival
    # should read as one line, not four
    spans = {}
    for i, rid in enumerate(present):
        days = regions[rid][:8]
        panel(fig, gs[i // 3, i % 3], rid, days)
        for d in days:
            ev = (d.get("event") or {}).get("ru")
            if not ev:
                continue
            day = date.fromisoformat(d["iso"])
            lo, hi = spans.get(ev, (day, day))
            spans[ev] = (min(lo, day), max(hi, day))

    def fmt(d):
        return f"{DOW[d.weekday()]} {d.day:02d}.{d.month:02d}"

    events = [f"{fmt(lo)} — {ev}" if lo == hi else f"{fmt(lo)}–{fmt(hi)} — {ev}"
              for ev, (lo, hi) in sorted(spans.items(), key=lambda kv: kv[1][0])]

    fig.suptitle("Индекс спроса на неделю вперёд", x=.05, y=.965,
                 ha="left", fontsize=16, fontweight="bold", color=INK)
    fig.text(.05, .915,
             "Линия — индекс 0–100 · пунктир — среднее за неделю · "
             "полоса снизу — осадки, мм · вертикальный пунктир — событие",
             ha="left", fontsize=9, color=MUTED)
    fig.text(.05, .877,
             "Закрашенная точка — сегодня · контурная — сильнейший день",
             ha="left", fontsize=9, color=MUTED)

    built = payload.get("built_at", "")
    if built:
        fig.text(.98, .915, built[:10], ha="right", fontsize=9, color=MUTED)

    if events:
        fig.text(.05, .045, "События:  " + "   ·   ".join(events[:4]),
                 ha="left", fontsize=8.5, color=EVENT, fontweight="bold")

    fig.savefig(OUT, facecolor=SURFACE, bbox_inches="tight", pad_inches=.30)
    print(f"wrote {OUT.relative_to(ROOT)} - {len(present)} panels")
    return 0


if __name__ == "__main__":
    sys.exit(build())
