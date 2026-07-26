#!/usr/bin/env python3
"""Render the week-ahead demand chart that accompanies the Telegram briefing.

Small multiples: one panel per region, so the panel title carries identity and
colour never has to. The line is a single accent; the tier colour appears only
on today's value, always next to a written tier label - never colour alone.

Reads data.json, writes chart.png.
"""

import json
import pathlib
import sys
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data.json"
OUT = ROOT / "chart.png"

# Verified >= 3:1 against a white surface.
ACCENT = "#0F7B82"
INK = "#1C282A"
MUTED = "#5B6A6C"
GRID = "#E4DED1"
SURFACE = "#FFFFFF"

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

    fig, axes = plt.subplots(2, 3, figsize=(10.5, 6.0), dpi=170,
                             facecolor=SURFACE, sharey=True)
    fig.subplots_adjust(left=.06, right=.98, top=.79, bottom=.09,
                        wspace=.18, hspace=.52)

    for ax, rid in zip(axes.flat, present):
        days = regions[rid][:8]
        xs = list(range(len(days)))
        ys = [d["score"] for d in days]

        ax.set_facecolor(SURFACE)
        ax.plot(xs, ys, color=ACCENT, linewidth=2, solid_capstyle="round",
                zorder=3)
        ax.fill_between(xs, 0, ys, color=ACCENT, alpha=.10, zorder=2)

        # Today's value sits under the panel title, not beside the dot - a
        # label anchored to the line collides with it whenever today is high.
        colour, label = tier(ys[0])
        ax.plot([0], [ys[0]], "o", markersize=8, color=colour,
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=4)
        ax.text(0, 1.06, f"{ys[0]} · {label}", transform=ax.transAxes,
                fontsize=9, color=colour, fontweight="bold", va="bottom")

        # mark the strongest day ahead so the panel answers "when"
        fwd = ys[1:]
        if fwd:
            bi = ys.index(max(fwd), 1)
            ax.plot([bi], [ys[bi]], "o", markersize=6, color=SURFACE,
                    markeredgecolor=ACCENT, markeredgewidth=2, zorder=4)

        ax.set_title(NAMES.get(rid, rid), loc="left", fontsize=11.5,
                     fontweight="bold", color=INK, pad=22)

        ticks, labels = [], []
        for i, d in enumerate(days):
            if i % 2 == 0:
                ticks.append(i)
                labels.append(DOW[date.fromisoformat(d["iso"]).weekday()])
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels, fontsize=8.5, color=MUTED)
        ax.set_ylim(0, max(60, max(ys) + 12))
        ax.yaxis.set_major_locator(MultipleLocator(20))
        ax.tick_params(axis="y", labelsize=8.5, colors=MUTED, length=0)
        ax.grid(axis="y", color=GRID, linewidth=1, zorder=1)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)

    for ax in axes.flat[len(present):]:
        ax.set_visible(False)

    fig.suptitle("Индекс спроса на неделю вперёд", x=.06, y=.975,
                 ha="left", fontsize=15, fontweight="bold", color=INK)
    fig.text(.06, .915, "Закрашенная точка — сегодня, контурная — сильнейший день недели",
             ha="left", fontsize=9.5, color=MUTED)

    built = payload.get("built_at", "")
    if built:
        fig.text(.98, .915, built[:10], ha="right", fontsize=9, color=MUTED)

    fig.savefig(OUT, facecolor=SURFACE, bbox_inches="tight", pad_inches=.28)
    print(f"wrote {OUT.relative_to(ROOT)} - {len(present)} panels")
    return 0


if __name__ == "__main__":
    sys.exit(build())
