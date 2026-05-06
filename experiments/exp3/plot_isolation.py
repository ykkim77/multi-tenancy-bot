#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figure 9 (재설계): Hard Isolation across 3 modes × 3 scenarios
─────────────────────────────────────────────────────────────────
Layout: 3 rows (scenarios A/B/C) × 4 columns (a..d)

  (a) Time-series : victim latency over stress window
  (b) CDF         : latency distribution
  (c) P95 / P99   : tail-latency bar chart
  (d) Jitter (σ)  : stability bar chart

Mode color palette (consistent with Exp1/Exp2):
  baseline → blue dashed   (#1f77b4)   — no protection
  manual   → green dotted  (#2ca02c)   — current best practice (static)
  agentic  → orange solid  (#ff7f0e)   — real Operator (dynamic)

글로벌 범례는 figure 하단 중앙에 1회.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
DATA_PATH = RESULTS_DIR / "exp3_isolation_results.json"
OUT_PNG = RESULTS_DIR / "exp3_fig9_isolation.png"
OUT_PDF = RESULTS_DIR / "exp3_fig9_isolation.pdf"

# Color & style per mode
MODE_STYLE: Dict[str, Dict[str, Any]] = {
    "baseline": {"color": "#1f77b4", "ls": "--", "marker": "o",
                 "label": "B1  Baseline (no protection)"},
    "manual":   {"color": "#2ca02c", "ls": ":",  "marker": "s",
                 "label": "B2  Manual policy (static)"},
    "agentic":  {"color": "#ff7f0e", "ls": "-",  "marker": "D",
                 "label": "B3  Agentic Operator (autonomous)"},
}
MODE_ORDER = ["baseline", "manual", "agentic"]
SCENARIO_ORDER = ["A", "B", "C"]
SCENARIO_TITLE = {
    "A": "Scenario A — 1 victim + 1 aggressor (basic)",
    "B": "Scenario B — 1 victim + 3 aggressors (multi-attack)",
    "C": "Scenario C — 5 victims + 5 aggressors (multi-tenant SLA)",
}


def load() -> List[Dict[str, Any]]:
    with open(DATA_PATH) as f:
        return json.load(f)


def collect_series(records: List[Dict[str, Any]], scenario: str, mode: str
                   ) -> Tuple[np.ndarray, np.ndarray]:
    """Concatenate all victim time-series across runs into (timestamps, latencies_ms)."""
    ts: List[float] = []
    lat: List[float] = []
    for r in records:
        if r["scenario"] != scenario or r["mode"] != mode:
            continue
        for series in r.get("series_per_victim", {}).values():
            for t, v in series:
                ts.append(t); lat.append(v)
    return np.array(ts), np.array(lat)


def stats_for(records: List[Dict[str, Any]], scenario: str, mode: str
              ) -> Optional[Dict[str, float]]:
    recs = [r for r in records if r["scenario"] == scenario and r["mode"] == mode]
    if not recs:
        return None
    keys = ("mean_ms", "median_ms", "p95_ms", "p99_ms", "std_ms")
    return {k: float(np.mean([r["stats_overall"][k] for r in recs])) for k in keys}


def moving_average(arr: np.ndarray, window: int = 9) -> np.ndarray:
    if len(arr) < window:
        return arr
    return np.convolve(arr, np.ones(window) / window, mode="valid")


def main():
    data = load()

    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "Liberation Serif"],
        "font.size": 9.5,
        "axes.labelsize": 10.5,
        "axes.titlesize": 10.5,
        "legend.fontsize": 10,
        "grid.alpha": 0.3, "grid.linestyle": "--",
    })

    fig, axes = plt.subplots(3, 4, figsize=(17.5, 12), constrained_layout=False)
    plt.subplots_adjust(left=0.05, right=0.985, top=0.945, bottom=0.085,
                        hspace=0.45, wspace=0.30)

    legend_handles, legend_labels = [], []

    for row, scenario in enumerate(SCENARIO_ORDER):
        # ── (a) Time-series ─────────────────────────────────────────
        ax = axes[row, 0]
        for mode in MODE_ORDER:
            t, lat = collect_series(data, scenario, mode)
            if len(t) == 0:
                continue
            order = np.argsort(t)
            t, lat = t[order], lat[order]
            style = MODE_STYLE[mode]
            ax.plot(t, lat, color=style["color"], alpha=0.15, lw=0.6)
            w = 9
            if len(lat) >= w:
                line, = ax.plot(t[w - 1:], moving_average(lat, w),
                                ls=style["ls"], color=style["color"], lw=2.0,
                                label=style["label"])
                if style["label"] not in legend_labels:
                    legend_handles.append(line); legend_labels.append(style["label"])
        ax.set_xlabel("Time (s)" if row == 2 else "")
        ax.set_ylabel("Latency (ms)")
        ax.set_title(f"(a{row + 1}) Latency Time-Series", fontweight="bold")
        ax.grid(True)

        # ── (b) CDF ─────────────────────────────────────────────────
        ax = axes[row, 1]
        for mode in MODE_ORDER:
            _, lat = collect_series(data, scenario, mode)
            if len(lat) == 0:
                continue
            ls = np.sort(lat)
            cdf = np.arange(1, len(ls) + 1) / len(ls) * 100
            style = MODE_STYLE[mode]
            ax.plot(ls, cdf, ls=style["ls"], color=style["color"], lw=2.0)
        ax.axhline(95, color="#555", ls="--", lw=1.0, alpha=0.55)
        xlim = ax.get_xlim()
        ax.text(xlim[0] + (xlim[1] - xlim[0]) * 0.02, 96.5,
                "P95", color="#444", fontsize=9, fontweight="bold")
        ax.set_xlabel("Latency (ms)" if row == 2 else "")
        ax.set_ylabel("Cumulative %")
        ax.set_title(f"(b{row + 1}) Latency CDF", fontweight="bold")
        ax.set_ylim(0, 105)
        ax.grid(True)

        # ── (c) P95 / P99 grouped bars ──────────────────────────────
        ax = axes[row, 2]
        x = np.arange(len(MODE_ORDER))
        width = 0.36
        p95s, p99s = [], []
        for mode in MODE_ORDER:
            s = stats_for(data, scenario, mode) or {"p95_ms": 0, "p99_ms": 0}
            p95s.append(s["p95_ms"]); p99s.append(s["p99_ms"])
        colors = [MODE_STYLE[m]["color"] for m in MODE_ORDER]
        b1 = ax.bar(x - width / 2, p95s, width=width, color=colors, alpha=0.85,
                    edgecolor="black", linewidth=0.6, label="P95")
        b2 = ax.bar(x + width / 2, p99s, width=width, color=colors, alpha=0.45,
                    edgecolor="black", linewidth=0.6, hatch="////", label="P99")
        for bars in (b1, b2):
            for bar in bars:
                h = bar.get_height()
                ax.annotate(f"{h:.0f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                            xytext=(0, 2), textcoords="offset points",
                            ha="center", va="bottom", fontsize=8)
        ax.set_xticks(x)
        ax.set_xticklabels(["B1", "B2", "B3"], fontweight="bold")
        ax.set_ylabel("Latency (ms)")
        ax.set_title(f"(c{row + 1}) P95 / P99 Tail Latency", fontweight="bold")
        ax.legend(loc="upper right", fontsize=8.5)

        # P95 improvement annotation: agentic vs baseline
        if p95s[0] > 0 and p95s[2] > 0:
            improv = (p95s[0] - p95s[2]) / p95s[0] * 100
            ax.text(0.04, 0.95, f"B3 vs B1\nP95 {improv:+.1f}%",
                    transform=ax.transAxes, ha="left", va="top",
                    fontsize=8.5, bbox=dict(boxstyle="round,pad=0.25",
                                            facecolor="white", alpha=0.85,
                                            edgecolor="lightgray"))
        ax.grid(True, axis="y")

        # ── (d) Jitter (σ) ──────────────────────────────────────────
        ax = axes[row, 3]
        stds = []
        for mode in MODE_ORDER:
            s = stats_for(data, scenario, mode) or {"std_ms": 0}
            stds.append(s["std_ms"])
        x_jitter = np.arange(len(MODE_ORDER))
        bars = ax.bar(x_jitter, stds, color=colors, alpha=0.85, width=0.6,
                      edgecolor="black", linewidth=0.6)
        ax.set_xticks(x_jitter)
        ax.set_xticklabels(["B1", "B2", "B3"], fontweight="bold")
        for bar in bars:
            h = bar.get_height()
            ax.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", fontsize=9, fontweight="bold")
        if stds[0] > 0:
            jr = (stds[0] - stds[2]) / stds[0] * 100
            ax.text(0.04, 0.95, f"B3 vs B1\nσ {jr:+.1f}%",
                    transform=ax.transAxes, ha="left", va="top", fontsize=8.5,
                    bbox=dict(boxstyle="round,pad=0.25", facecolor="white",
                              alpha=0.85, edgecolor="lightgray"))
        ax.set_ylabel("Std Dev (ms)")
        ax.set_title(f"(d{row + 1}) Jitter (σ)", fontweight="bold")
        ax.grid(True, axis="y")

        # Row label on the left of the row
        axes[row, 0].text(-0.30, 0.5, SCENARIO_TITLE[scenario],
                          transform=axes[row, 0].transAxes,
                          ha="center", va="center", rotation=90,
                          fontsize=11, fontweight="bold", color="#333")

    fig.legend(legend_handles, legend_labels,
               loc="lower center", ncol=len(legend_handles),
               bbox_to_anchor=(0.5, 0.005),
               frameon=True, fontsize=10.5, borderpad=0.7)

    fig.suptitle(
        "Figure 9 — Hard Isolation under Noisy Neighbors\n"
        "Baseline (no protection) vs Manual policy (static) vs Agentic Operator (autonomous)",
        fontsize=13, fontweight="bold", y=0.99)

    fig.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    fig.savefig(OUT_PDF, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Saved {OUT_PNG}")
    print(f"✓ Saved {OUT_PDF}")


if __name__ == "__main__":
    main()
