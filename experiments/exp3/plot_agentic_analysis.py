#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figure 10 (신규): Operator 자율 대응 분석
─────────────────────────────────────────────
Agentic mode 의 raw data 만을 사용해, **실제 Operator 가 어떻게 자율적으로
판단했는지**를 가시화한다. 1행 3열 layout.

  (a) Rebalance 발생 시점 ↔ victim latency 변화
        ─ 기본은 Scenario C  (5 victims + 5 aggressors + 5 idle bg)
        ─ Operator 가 boost/reclaim 한 시각에 vertical line 마커
        ─ "재조정 후 latency 즉시 개선" 가설을 검증

  (b) AgenticActions Timeline (per ChatSpace)
        ─ Y: ChatSpace (role + tier 별 정렬)
        ─ X: stress 시각 t=0 기준 상대 시간
        ─ marker color = action category (boosted/reclaimed/no_rebal)

  (c) Tier 별 보호 차등
        ─ Scenario C 의 priority tier victim vs standard tier victim
        ─ 각 mode 별 P95 비교 → "priority 가 더 강하게 보호받는다"
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
OUT_PNG = RESULTS_DIR / "exp3_fig10_agentic_analysis.png"
OUT_PDF = RESULTS_DIR / "exp3_fig10_agentic_analysis.pdf"

ACTION_PALETTE = {
    "boosted":     "#2ca02c",   # green — Operator decided to grow
    "reclaimed":   "#d62728",   # red   — Operator decided to shrink
    "no_rebal":    "#7f7f7f",   # gray  — no action
    "agentic_off": "#9467bd",
    "fallback":    "#17becf",
    "other":       "#bcbd22",
}
MODE_COLOR = {
    "baseline": "#1f77b4", "manual": "#2ca02c", "agentic": "#ff7f0e",
}


def load() -> List[Dict[str, Any]]:
    with open(DATA_PATH) as f:
        return json.load(f)


def find_record(records: List[Dict[str, Any]], scenario: str,
                mode: str, run_idx: int = 0) -> Optional[Dict[str, Any]]:
    for r in records:
        if (r["scenario"] == scenario and r["mode"] == mode
                and r.get("run_idx", 0) == run_idx):
            return r
    return None


def panel_a_rebalance_vs_latency(ax, rec: Dict[str, Any]) -> None:
    """Overlay rebalance event markers on the victim latency time-series."""
    if not rec or not rec.get("agentic"):
        ax.text(0.5, 0.5, "No agentic data available",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="gray")
        ax.set_title("(a) Rebalance Events vs Victim Latency",
                     fontweight="bold", pad=8)
        return

    # Per-victim latency lines (priority vs standard tier)
    series = rec.get("series_per_victim", {})
    stats_per = rec.get("stats_per_victim", {})

    for ns, s in series.items():
        if not s:
            continue
        info = stats_per.get(ns, {})
        tier = info.get("tier", "priority")
        color = "#ff7f0e" if tier == "priority" else "#1f77b4"
        ts = np.array([t for t, _ in s]); lat = np.array([v for _, v in s])
        ax.plot(ts, lat, color=color, alpha=0.18, lw=0.6)
        if len(lat) >= 9:
            ma = np.convolve(lat, np.ones(9) / 9, mode="valid")
            ax.plot(ts[8:], ma, color=color, lw=2.0,
                    label=f"victim/{tier}")

    # Vertical lines for rebalance events
    events = rec["agentic"].get("new_action_events", []) or []
    plotted_categories = set()
    for ev in events:
        cat = ev["category"]
        c = ACTION_PALETTE.get(cat, "black")
        ax.axvline(ev["t_rel_s"], color=c, alpha=0.55, lw=1.2,
                   ls="--",
                   label=(f"action: {cat}"
                          if cat not in plotted_categories else None))
        plotted_categories.add(cat)

    ax.set_xlabel("Time since stress start (s)")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("(a) Operator Rebalance Events vs Victim Latency",
                 fontweight="bold", pad=8)
    ax.grid(True, alpha=0.3)
    # Dedup legend
    handles, labels = ax.get_legend_handles_labels()
    seen = set(); h2, l2 = [], []
    for h, l in zip(handles, labels):
        if l not in seen:
            seen.add(l); h2.append(h); l2.append(l)
    if h2:
        ax.legend(h2, l2, loc="upper right", fontsize=8.5, framealpha=0.9)


def panel_b_actions_timeline(ax, rec: Dict[str, Any]) -> None:
    """Per-ChatSpace timeline of agenticActions."""
    if not rec or not rec.get("agentic"):
        ax.text(0.5, 0.5, "No agentic data available",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="gray")
        ax.set_title("(b) AgenticActions Timeline", fontweight="bold", pad=8)
        return

    events = rec["agentic"].get("new_action_events", []) or []
    if not events:
        ax.text(0.5, 0.5, "No new actions during stress window",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="gray")
        ax.set_title("(b) AgenticActions Timeline", fontweight="bold", pad=8)
        return

    # Order ChatSpaces by (role, tier, idx)
    role_rank = {"victim": 0, "aggressor": 1, "background": 2}
    tier_rank = {"priority": 0, "standard": 1, "internal": 2}
    cs_order: List[str] = []
    cs_meta: Dict[str, Dict[str, str]] = {}
    for ev in events:
        if ev["cs"] not in cs_meta:
            cs_meta[ev["cs"]] = {"role": ev["role"], "tier": ev["tier"]}
    cs_order = sorted(cs_meta.keys(),
                      key=lambda c: (role_rank.get(cs_meta[c]["role"], 9),
                                     tier_rank.get(cs_meta[c]["tier"], 9), c))
    y_idx = {c: i for i, c in enumerate(cs_order)}

    plotted_cats = set()
    for ev in events:
        c = ACTION_PALETTE.get(ev["category"], "black")
        ax.scatter(ev["t_rel_s"], y_idx[ev["cs"]],
                   color=c, s=55, edgecolor="black", linewidth=0.4, zorder=3,
                   label=(ev["category"]
                          if ev["category"] not in plotted_cats else None))
        plotted_cats.add(ev["category"])

    # Y labels with role + tier color
    role_color = {"victim": "#ff7f0e", "aggressor": "#d62728",
                  "background": "#7f7f7f"}
    ax.set_yticks(list(y_idx.values()))
    short_labels = []
    for c in cs_order:
        m = cs_meta[c]
        # cs name pattern: cs-exp3-c-victim-0  → "victim-0 (priority)"
        last2 = "-".join(c.split("-")[-2:])
        short_labels.append(f"{last2} ({m['tier']})")
    ax.set_yticklabels(short_labels, fontsize=8.5)
    for tick, c in zip(ax.get_yticklabels(), cs_order):
        tick.set_color(role_color.get(cs_meta[c]["role"], "black"))

    ax.set_xlabel("Time since stress start (s)")
    ax.set_title("(b) AgenticActions Timeline (per ChatSpace)",
                 fontweight="bold", pad=8)
    ax.grid(True, axis="x", alpha=0.3)
    ax.legend(loc="upper right", fontsize=8.5, framealpha=0.9,
              title="Action category", title_fontsize=9)
    ax.invert_yaxis()


def panel_c_tier_protection(ax, records: List[Dict[str, Any]]) -> None:
    """Compare priority-tier vs standard-tier victims under each mode (Scenario C)."""
    modes = ["baseline", "manual", "agentic"]
    p95_priority: Dict[str, float] = {}
    p95_standard: Dict[str, float] = {}

    for mode in modes:
        recs = [r for r in records if r["scenario"] == "C" and r["mode"] == mode]
        if not recs:
            continue
        # Average across runs
        prio = [r["stats_per_tier"].get("priority", {}).get("p95_ms")
                for r in recs if r.get("stats_per_tier", {}).get("priority")]
        std = [r["stats_per_tier"].get("standard", {}).get("p95_ms")
               for r in recs if r.get("stats_per_tier", {}).get("standard")]
        if prio: p95_priority[mode] = float(np.mean(prio))
        if std:  p95_standard[mode] = float(np.mean(std))

    if not (p95_priority or p95_standard):
        ax.text(0.5, 0.5, "Scenario C results required\n"
                          "(needs priority + standard victims)",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="gray")
        ax.set_title("(c) Tier-based Protection (Scenario C)",
                     fontweight="bold", pad=8)
        return

    x = np.arange(len(modes))
    w = 0.36
    prio_vals = [p95_priority.get(m, 0.0) for m in modes]
    std_vals = [p95_standard.get(m, 0.0) for m in modes]
    bars1 = ax.bar(x - w / 2, prio_vals, width=w, color="#ff7f0e",
                   alpha=0.92, edgecolor="black", linewidth=0.6,
                   label="priority tier victim")
    bars2 = ax.bar(x + w / 2, std_vals, width=w, color="#1f77b4",
                   alpha=0.92, edgecolor="black", linewidth=0.6,
                   label="standard tier victim")
    for bars in (bars1, bars2):
        for bar in bars:
            h = bar.get_height()
            if h <= 0: continue
            ax.annotate(f"{h:.0f}", xy=(bar.get_x() + bar.get_width() / 2, h),
                        xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", fontsize=8.5)
    ax.set_xticks(x)
    ax.set_xticklabels(["B1\nbaseline", "B2\nmanual", "B3\nagentic"],
                       fontweight="bold", fontsize=9)
    ax.set_ylabel("P95 Latency (ms)")
    ax.set_title("(c) Tier-based Protection Differential — Scenario C",
                 fontweight="bold", pad=8)
    ax.legend(loc="upper right", fontsize=9, framealpha=0.95)
    ax.grid(True, axis="y", alpha=0.3)

    # Differential annotation for agentic
    if "agentic" in p95_priority and "agentic" in p95_standard \
            and p95_standard["agentic"] > 0:
        diff = (p95_standard["agentic"] - p95_priority["agentic"]) \
               / p95_standard["agentic"] * 100
        ax.text(0.96, 0.04,
                f"B3 differential\npriority is {diff:.0f}% faster",
                transform=ax.transAxes, ha="right", va="bottom",
                fontsize=9, bbox=dict(boxstyle="round,pad=0.3",
                                      facecolor="#FFF8E7",
                                      edgecolor="lightgray"))


def main():
    data = load()

    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "Liberation Serif"],
        "font.size": 10, "axes.labelsize": 10.5, "axes.titlesize": 11,
        "legend.fontsize": 9.5,
        "grid.alpha": 0.3, "grid.linestyle": "--",
    })

    fig, axes = plt.subplots(1, 3, figsize=(18, 5.6))
    plt.subplots_adjust(left=0.06, right=0.985, top=0.84, bottom=0.16,
                        wspace=0.30)

    # Pick the most informative agentic run.
    # Priority: Scenario C (most realistic) → B → A.
    target = (find_record(data, "C", "agentic")
              or find_record(data, "B", "agentic")
              or find_record(data, "A", "agentic"))

    panel_a_rebalance_vs_latency(axes[0], target)
    panel_b_actions_timeline(axes[1], target)
    panel_c_tier_protection(axes[2], data)

    fig.suptitle(
        "Figure 10 — Agentic Operator's Autonomous Response to Noisy Neighbors\n"
        "(real-time decisions recorded in `ChatSpace.status.agenticActions`)",
        fontsize=13, fontweight="bold", y=0.99)

    fig.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    fig.savefig(OUT_PDF, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Saved {OUT_PNG}")
    print(f"✓ Saved {OUT_PDF}")

    # ── Console summary ──────────────────────────────────────────────
    if target and target.get("agentic"):
        ag = target["agentic"]
        print(f"\n  Agentic summary (Scenario {target['scenario']}, "
              f"run {target.get('run_idx', 0)}):")
        print(f"    Total actions during run    : {ag['total_actions']}")
        print(f"    New actions during stress   : {ag['new_actions_during_stress']}")
        if ag["first_response_s"] is not None:
            print(f"    First Operator response time: {ag['first_response_s']:.2f}s")
        for cat, n in (ag["action_counts"] or {}).items():
            print(f"      {cat:<12s}: {n}")


if __name__ == "__main__":
    main()
