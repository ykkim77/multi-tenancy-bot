#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 2 시각화 (real Operator 버전)
─────────────────────────────────
2x2 grid:
  (a) Control Plane Latency vs N
  (b) Per-Tenant Avg Response Time vs N
  (c) Rebalance Decision Time — boxplot per N (agentic 전용)
       + 누적 분포(상단 inset) — Operator 가 결정하기까지 걸린 시간
  (d) Idle Detection Accuracy + AgenticActions Histogram (agentic 전용)
       Stacked bar(reclaimed/boosted/no_rebal/other) + accuracy 라인

- Static: 파란 점선   (#1f77b4)
- Agentic (Operator): 주황 실선 (#ff7f0e)
- 글로벌 범례 1회 (하단)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
DATA_PATH = RESULTS_DIR / "exp2_density_results.json"
OUT_PNG = RESULTS_DIR / "exp2_density_integrated.png"
OUT_PDF = RESULTS_DIR / "exp2_density_integrated.pdf"

COLOR_STATIC  = "#1f77b4"
COLOR_AGENTIC = "#ff7f0e"

# Stacked-bar palette for action categories
ACTION_PALETTE = {
    "reclaimed":  "#d62728",   # red — Operator decided "shrink"
    "boosted":    "#2ca02c",   # green — Operator decided "grow"
    "no_rebal":   "#7f7f7f",   # gray — no action
    "other":      "#bcbd22",
    "agentic_off":"#9467bd",
    "fallback":   "#17becf",
}
ACTION_ORDER = ["reclaimed", "boosted", "no_rebal", "other",
                "agentic_off", "fallback"]


def load() -> List[Dict[str, Any]]:
    with open(DATA_PATH) as f:
        return json.load(f)


def aggregate_basic(records: List[Dict], mode: str
                    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    recs = [r for r in records if r["mode"] == mode]
    ns = sorted({r["n_tenants"] for r in recs})
    cp, pt = [], []
    for n in ns:
        sub = [r for r in recs if r["n_tenants"] == n]
        cp.append(np.mean([r["avg_cp_latency"] for r in sub]) * 1000)
        pt.append(np.mean([r["avg_tenant_latency"] for r in sub]) * 1000)
    return np.array(ns), np.array(cp), np.array(pt)


def aggregate_agentic(records: List[Dict]
                      ) -> Tuple[np.ndarray, Dict[int, List[float]],
                                 Dict[int, float], Dict[int, Dict[str, int]]]:
    """Returns (ns, decision_times_per_n, accuracy_per_n, action_counts_per_n)."""
    recs = [r for r in records if r["mode"] == "agentic"]
    ns = sorted({r["n_tenants"] for r in recs})
    dt: Dict[int, List[float]] = {n: [] for n in ns}
    acc: Dict[int, float] = {}
    actions: Dict[int, Dict[str, int]] = {n: {} for n in ns}

    for n in ns:
        sub = [r for r in recs if r["n_tenants"] == n]
        for r in sub:
            ag = r.get("agentic") or {}
            dt[n].extend(ag.get("decision_times_s") or [])
            for k, v in (ag.get("action_counts") or {}).items():
                actions[n][k] = actions[n].get(k, 0) + int(v)
        acc[n] = float(np.mean([r["agentic"]["accuracy"] for r in sub]))
    return np.array(ns), dt, acc, actions


def main():
    data = load()
    ns_s, cp_s, pt_s = aggregate_basic(data, "static")
    ns_a, cp_a, pt_a = aggregate_basic(data, "agentic")
    ns_ag, dt_ag, acc_ag, actions_ag = aggregate_agentic(data)

    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "Liberation Serif"],
        "font.size": 11,
        "axes.labelsize": 12,
        "axes.titlesize": 12.5,
        "xtick.labelsize": 10, "ytick.labelsize": 10,
        "legend.fontsize": 10,
        "grid.alpha": 0.3, "grid.linestyle": "--",
    })

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    plt.subplots_adjust(hspace=0.30, wspace=0.22)
    legend_handles, legend_labels = [], []

    # ── (a) Control Plane Latency ────────────────────────────────────
    ax = axes[0, 0]
    if len(ns_s):
        ls, = ax.plot(ns_s, cp_s, "o--", color=COLOR_STATIC, lw=2,
                      markersize=6, label="Static (Fixed Quota)")
        legend_handles.append(ls); legend_labels.append("Static (Fixed Quota)")
    if len(ns_a):
        la, = ax.plot(ns_a, cp_a, "s-", color=COLOR_AGENTIC, lw=2.5,
                      markersize=6, label="Agentic Operator")
        legend_handles.append(la); legend_labels.append("Agentic Operator")
    ax.set_xlabel("Number of Tenants"); ax.set_ylabel("CP Latency (ms)")
    ax.set_title("(a) Control Plane Latency", fontweight="bold")
    ax.grid(True); ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True, nbins=10))

    # ── (b) Per-Tenant Response Time ─────────────────────────────────
    ax = axes[0, 1]
    if len(ns_s): ax.plot(ns_s, pt_s, "o--", color=COLOR_STATIC, lw=2, markersize=6)
    if len(ns_a): ax.plot(ns_a, pt_a, "s-", color=COLOR_AGENTIC, lw=2.5, markersize=6)
    ax.set_xlabel("Number of Tenants"); ax.set_ylabel("Avg Response Time (ms)")
    ax.set_title("(b) Per-Tenant Resource Listing Latency", fontweight="bold")
    ax.grid(True); ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True, nbins=10))

    # ── (c) Rebalance Decision Time (agentic only) ───────────────────
    ax = axes[1, 0]
    if len(ns_ag):
        positions = list(ns_ag)
        data_per_n = [dt_ag[n] for n in ns_ag]
        # Auto-width based on tenant spacing
        if len(positions) >= 2:
            n_step = float(np.median(np.diff(positions)))
        else:
            n_step = 1.0
        bp = ax.boxplot(
            data_per_n, positions=positions, widths=max(0.5, n_step * 0.55),
            patch_artist=True, showfliers=False,
            medianprops=dict(color="black", linewidth=1.6),
        )
        for patch in bp["boxes"]:
            patch.set_facecolor(COLOR_AGENTIC); patch.set_alpha(0.55)
            patch.set_edgecolor(COLOR_AGENTIC)
        # strip plot
        for n, vals in zip(ns_ag, data_per_n):
            if not vals:
                continue
            jit = np.random.uniform(-n_step * 0.12, n_step * 0.12, size=len(vals))
            ax.scatter(np.full(len(vals), n) + jit, vals,
                       s=10, alpha=0.5, color=COLOR_AGENTIC,
                       edgecolor="black", linewidth=0.3, zorder=3)
        # 30s rebalance cycle reference
        ax.axhline(30.0, color="gray", ls=":", lw=1.5, alpha=0.7)
        ax.text(positions[-1], 30.0, " Operator's 30 s\n re-balance cycle",
                fontsize=9, color="gray", va="bottom", ha="right")

        # Mean line
        means = [float(np.mean(v)) if v else np.nan for v in data_per_n]
        ax.plot(positions, means, "D-", color=COLOR_AGENTIC, lw=1.5,
                markersize=5, alpha=0.85, label="Mean")
    else:
        ax.text(0.5, 0.5, "No agentic decision data",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="gray")
    ax.set_xlabel("Number of Tenants")
    ax.set_ylabel("Time to Rebalance Decision (s)\n(creationTimestamp → action)")
    ax.set_title("(c) Operator Decision Latency", fontweight="bold")
    ax.grid(True, axis="y")
    if len(ns_ag):
        ax.set_xticks(list(ns_ag))
        ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True, nbins=10))

    # ── (d) Idle Detection Accuracy + Action Histogram ───────────────
    ax = axes[1, 1]
    if len(ns_ag):
        positions = list(ns_ag)
        if len(positions) >= 2:
            n_step = float(np.median(np.diff(positions)))
        else:
            n_step = 1.0
        bar_w = max(0.6, n_step * 0.6)

        # Stacked bars for action category breakdown
        bottoms = np.zeros(len(positions))
        stack_handles: List[Any] = []
        stack_labels: List[str] = []
        for cat in ACTION_ORDER:
            heights = np.array([actions_ag[n].get(cat, 0) for n in ns_ag],
                               dtype=float)
            if heights.sum() == 0:
                continue
            h = ax.bar(positions, heights, width=bar_w, bottom=bottoms,
                       color=ACTION_PALETTE[cat], alpha=0.85, edgecolor="white",
                       linewidth=0.5, label=cat.replace("_", " "))
            bottoms += heights
            stack_handles.append(h); stack_labels.append(cat.replace("_", " "))

        ax.set_xlabel("Number of Tenants")
        ax.set_ylabel("ChatSpaces (count)")
        ax.set_title("(d) AgenticActions & Idle Detection Accuracy",
                     fontweight="bold")
        ax.grid(True, axis="y")
        ax.set_xticks(positions)
        ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True, nbins=10))
        ax.legend(stack_handles, stack_labels,
                  title="Latest action", loc="upper left",
                  fontsize=8.5, title_fontsize=9, framealpha=0.85, ncol=2)

        # Accuracy on a twin axis
        ax2 = ax.twinx()
        accs = [acc_ag[n] * 100.0 for n in ns_ag]
        l_acc, = ax2.plot(positions, accs, "o-", color="black", lw=2.0,
                          markersize=7, label="Idle classification accuracy")
        ax2.set_ylabel("Accuracy (%)", color="black")
        ax2.set_ylim(0, 105)
        ax2.tick_params(axis="y", labelcolor="black")
        ax2.axhline(100, color="black", ls=":", lw=0.8, alpha=0.4)

        if l_acc not in legend_handles:
            legend_handles.append(l_acc)
            legend_labels.append("Idle classification accuracy")
    else:
        ax.text(0.5, 0.5, "No agentic data",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=11, color="gray")

    # ── Global legend ────────────────────────────────────────────────
    fig.legend(legend_handles, legend_labels,
               loc="lower center", ncol=len(legend_handles),
               bbox_to_anchor=(0.5, 0.005),
               frameon=True, fontsize=11, borderpad=0.8)

    fig.suptitle(
        "Experiment 2 — Tenant Density & Autonomous Re-balancing\n"
        "Static (kubectl, fixed quota) vs Agentic Operator (CRD-driven)",
        fontsize=13.5, fontweight="bold", y=0.995)
    plt.tight_layout(rect=[0, 0.05, 1, 0.97])

    fig.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    fig.savefig(OUT_PDF, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ Saved {OUT_PNG}")
    print(f"✓ Saved {OUT_PDF}")

    # ── Text summary ─────────────────────────────────────────────────
    print("\n  Operator Decision Summary:")
    for n in ns_ag:
        dts = dt_ag[n]
        cnts = actions_ag[n]
        line = f"    N={n:>3}  acc={acc_ag[n]*100:5.1f}%"
        if dts:
            line += (f"  decision: mean={np.mean(dts):5.2f}s "
                     f"median={np.median(dts):5.2f}s "
                     f"p95={np.percentile(dts, 95):5.2f}s")
        line += "   actions: " + ", ".join(
            f"{k}={cnts.get(k, 0)}" for k in ACTION_ORDER if cnts.get(k, 0) > 0)
        print(line)


if __name__ == "__main__":
    main()
