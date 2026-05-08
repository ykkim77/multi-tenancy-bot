#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figure 5: Automation Completeness Comparison
═════════════════════════════════════════════
3-panel layout:
  (a) Policy Coverage Rate (PCR)   — fraction of isolation policies applied
  (b) Drift Recovery Time (MTTR)   — broken-axis: Helm=inf, Agentic=sub-second
  (c) Human Intervention Score     — operator burden vs. tenant count

Usage: python3 plot_results.py [--input results/exp1_results.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.gridspec as mgridspec
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent

MODE_COLOR = {
    "helm-basic": "#AAAAAA",   # grey
    "helm":       "#4DAF4A",   # green
    "agentic":    "#E66F00",   # orange
}
MODE_LABEL = {
    "helm-basic": "Helm (Basic, NS+RQ)",
    "helm":       "Helm (Full Policy)",
    "agentic":    "Agentic Operator",
}
BATCH_LS = {5: "--", 10: "-", 25: "-.", 50: ":"}

INFINITY_BAR_HEIGHT = 110.0   # visual stand-in for "∞" in the MTTR upper panel
N_POLICIES = 7


def load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ── (a) PCR bar chart ──────────────────────────────────────────────────────────

def plot_pcr(ax: plt.Axes, pcr_data: List[dict]) -> None:
    """Bar chart: mean Policy Coverage Rate per mode with std-dev error bars."""
    by_mode: Dict[str, List[float]] = {}
    for r in pcr_data:
        by_mode.setdefault(r["mode"], []).append(r["pcr"] * 100)

    modes_order   = ["helm-basic", "helm", "agentic"]
    modes_present = [m for m in modes_order if m in by_mode]

    x      = np.arange(len(modes_present))
    means  = [np.mean(by_mode[m]) for m in modes_present]
    stds   = [np.std(by_mode[m])  for m in modes_present]

    bars = ax.bar(x, means, yerr=stds, capsize=6,
                  color=[MODE_COLOR[m] for m in modes_present],
                  edgecolor="black", linewidth=0.8,
                  alpha=0.82, width=0.55, error_kw={"elinewidth": 1.5})

    # Policy-count fraction inside each bar
    for bar, mode, mean in zip(bars, modes_present, means):
        n_pol = round(mean * N_POLICIES / 100)
        ax.text(bar.get_x() + bar.get_width() / 2, mean / 2,
                f"{n_pol}/{N_POLICIES}", ha="center", va="center",
                fontsize=11, fontweight="bold", color="white")
        ax.text(bar.get_x() + bar.get_width() / 2,
                mean + (max(stds) if stds else 0) + 2,
                f"{mean:.0f}%", ha="center", va="bottom", fontsize=10)

    # Reference lines
    ax.axhline(100, color="black", linewidth=1.0, linestyle="--", alpha=0.4)
    for pct, lbl in [(2/7*100, "2/7"), (5/7*100, "5/7"), (100, "7/7")]:
        ax.text(len(modes_present) - 0.1, pct + 1, lbl,
                ha="right", va="bottom", fontsize=8, color="gray")

    ax.set_xticks(x)
    ax.set_xticklabels([MODE_LABEL.get(m, m) for m in modes_present], fontsize=10)
    ax.set_ylabel("Policy Coverage Rate (%)", fontsize=11)
    ax.set_ylim(0, 120)
    ax.set_title("(a) Policy Coverage Rate (PCR)\nFraction of isolation policies applied",
                 fontsize=11, fontweight="bold", loc="left")
    ax.grid(True, axis="y", alpha=0.3)

    legend_patches = [
        mpatches.Patch(color=MODE_COLOR[m], label=MODE_LABEL.get(m, m), alpha=0.82)
        for m in modes_present
    ]
    ax.legend(handles=legend_patches, fontsize=9, loc="lower right")


# ── (b) MTTR broken-axis chart ────────────────────────────────────────────────
#
#  Upper panel  (~75 – 130 s): Helm "no recovery" bar
#  ////  break  ////
#  Lower panel  (0 – <2 s):    Agentic sub-second bar

def plot_mttr(fig: plt.Figure, spec, mttr_data: List[dict]) -> None:
    """
    Broken-axis MTTR comparison (academic standard for >100x scale difference).

    Parameters
    ----------
    fig  : parent Figure
    spec : SubplotSpec cell (e.g. gs[0, 1]) containing this panel
    """
    by_mode: Dict[str, List[Optional[float]]] = {}
    for r in mttr_data:
        by_mode.setdefault(r["mode"], []).append(r["mttr_s"])

    modes_order   = ["helm", "agentic"]
    modes_present = [m for m in modes_order if m in by_mode]
    x_pos         = np.arange(len(modes_present))

    # Per-mode statistics
    bar_h:   List[float] = []
    bar_std: List[float] = []
    is_inf:  List[bool]  = []

    for mode in modes_present:
        vals   = by_mode[mode]
        finite = [v for v in vals if v is not None]
        if mode == "helm" or not finite:
            bar_h.append(INFINITY_BAR_HEIGHT)
            bar_std.append(0.0)
            is_inf.append(True)
        else:
            bar_h.append(float(np.mean(finite)))
            bar_std.append(float(np.std(finite)))
            is_inf.append(False)

    # Dynamic y-limits
    ag_finite  = [v for v in by_mode.get("agentic", []) if v is not None]
    ag_max     = max(ag_finite) if ag_finite else 1.0
    lower_top  = max(ag_max * 2.2, 0.5)
    upper_bot  = INFINITY_BAR_HEIGHT * 0.68
    upper_top  = INFINITY_BAR_HEIGHT * 1.18

    # Two vertically stacked sub-axes inside the SubplotSpec cell
    inner  = mgridspec.GridSpecFromSubplotSpec(
        2, 1, subplot_spec=spec,
        height_ratios=[2, 2.5],
        hspace=0.06,
    )
    ax_top = fig.add_subplot(inner[0])
    ax_bot = fig.add_subplot(inner[1])

    # Draw bars in both axes (ylim will clip each to its relevant range)
    for i, (mode, h, std, inf) in enumerate(
            zip(modes_present, bar_h, bar_std, is_inf)):
        c = MODE_COLOR[mode]
        for ax in (ax_top, ax_bot):
            if inf:
                ax.bar(i, h, color=c, edgecolor="black", linewidth=0.8,
                       alpha=0.40, width=0.55, hatch="///")
            else:
                ax.bar(i, h, yerr=std, capsize=7, color=c,
                       edgecolor="black", linewidth=0.8,
                       alpha=0.82, width=0.55,
                       error_kw={"elinewidth": 1.5})
                if ax is ax_bot:
                    finite = [v for v in by_mode[mode] if v is not None]
                    if finite:
                        jit = np.random.uniform(-0.08, 0.08, size=len(finite))
                        ax.scatter(np.full(len(finite), i) + jit, finite,
                                   color=c, edgecolor="black",
                                   s=40, zorder=4, alpha=0.85)

    # Annotations
    for i, (mode, h, std, inf) in enumerate(
            zip(modes_present, bar_h, bar_std, is_inf)):
        if inf:
            mid = (upper_bot + upper_top) / 2
            ax_top.text(i, mid, u"\u221e\nNo Auto-\nRecovery",
                        ha="center", va="center", fontsize=12,
                        fontweight="bold", color=MODE_COLOR[mode])
            ax_top.text(i, upper_top * 0.97, "Manual intervention required",
                        ha="center", va="top", fontsize=8, color="dimgray")
        else:
            label_y = h + std + lower_top * 0.06
            ax_bot.text(i, label_y, f"{h:.3f} s",
                        ha="center", va="bottom", fontsize=10, fontweight="bold")

    # Y-limits
    ax_top.set_ylim(upper_bot, upper_top)
    ax_bot.set_ylim(0, lower_top)

    # Broken-axis spine styling
    ax_top.spines["bottom"].set_visible(False)
    ax_bot.spines["top"].set_visible(False)
    ax_top.tick_params(bottom=False, labelbottom=False)
    ax_bot.tick_params(top=False)

    # Diagonal break marks
    d  = 0.018
    kw = dict(color="k", clip_on=False, linewidth=1.2,
              transform=ax_top.transAxes)
    ax_top.plot((-d, +d),     (-d*1.5, +d*1.5), **kw)
    ax_top.plot((1-d, 1+d),   (-d*1.5, +d*1.5), **kw)
    kw["transform"] = ax_bot.transAxes
    ax_bot.plot((-d, +d),     (1-d*1.5, 1+d*1.5), **kw)
    ax_bot.plot((1-d, 1+d),   (1-d*1.5, 1+d*1.5), **kw)

    # Axis labels
    ax_top.set_ylabel("Recovery Time (s)", fontsize=11, labelpad=6)
    ax_bot.set_ylabel("Recovery Time (s)", fontsize=11, labelpad=6)

    # Range tags
    ax_top.text(0.98, 0.96,
                f"{upper_bot:.0f}\u2013{upper_top:.0f} s  (\u221e axis)",
                transform=ax_top.transAxes, fontsize=7.5,
                ha="right", va="top", color="dimgray", style="italic")
    ax_bot.text(0.98, 0.96,
                f"0\u2013{lower_top:.2f} s  (Agentic axis)",
                transform=ax_bot.transAxes, fontsize=7.5,
                ha="right", va="top", color="dimgray", style="italic")

    # X-ticks (bottom panel only)
    ax_bot.set_xticks(x_pos)
    ax_bot.set_xticklabels([MODE_LABEL.get(m, m) for m in modes_present],
                            fontsize=10)

    # Grid
    ax_top.grid(True, axis="y", alpha=0.3)
    ax_bot.grid(True, axis="y", alpha=0.3)

    # Title
    ax_top.set_title(
        "(b) Drift Recovery Time (MTTR)\nTime to restore a deleted NetworkPolicy",
        fontsize=11, fontweight="bold", loc="left",
    )

    # Legend
    legend_patches = []
    if "helm" in modes_present:
        legend_patches.append(
            mpatches.Patch(facecolor=MODE_COLOR["helm"], edgecolor="black",
                           linewidth=0.8, alpha=0.40, hatch="///",
                           label=f"{MODE_LABEL.get('helm','Helm')} — \u221e (no auto-heal)"),
        )
    if "agentic" in modes_present:
        ag_mean = bar_h[modes_present.index("agentic")]
        legend_patches.append(
            mpatches.Patch(facecolor=MODE_COLOR["agentic"], edgecolor="black",
                           linewidth=0.8, alpha=0.82,
                           label=f"{MODE_LABEL.get('agentic','Agentic')} — {ag_mean:.3f} s"),
        )
    ax_top.legend(handles=legend_patches, fontsize=9, loc="upper left",
                  framealpha=0.92)


# ── (c) HIS line chart ────────────────────────────────────────────────────────

def plot_his(ax: plt.Axes, his_data: List[dict]) -> None:
    """Line chart: Human Intervention Score vs. tenant count."""
    by_mode: Dict[str, Dict[int, int]] = {}
    for r in his_data:
        by_mode.setdefault(r["mode"], {})[r["n_tenants"]] = r["his_total"]

    for mode, vals in sorted(by_mode.items()):
        xs = sorted(vals.keys())
        ys = [vals[x] for x in xs]
        ax.plot(xs, ys, color=MODE_COLOR.get(mode, "#888"),
                linewidth=2.2, marker="o", markersize=6,
                label=MODE_LABEL.get(mode, mode))

    if "helm" in by_mode and "agentic" in by_mode:
        xs       = sorted(by_mode["helm"].keys())
        ys_helm  = [by_mode["helm"][x]    for x in xs]
        ys_agnt  = [by_mode["agentic"][x] for x in xs]
        ax.fill_between(xs, ys_agnt, ys_helm,
                        alpha=0.12, color="gray",
                        label="Automation savings")

    # Annotation at N=50
    if "helm" in by_mode and "agentic" in by_mode:
        n = 50
        if n in by_mode["helm"] and n in by_mode["agentic"]:
            diff = by_mode["helm"][n] - by_mode["agentic"][n]
            ax.annotate(f"N={n}: Helm\n+{diff} interventions",
                        xy=(n, by_mode["helm"][n]),
                        xytext=(n - 28, by_mode["helm"][n] + 5),
                        fontsize=9, color="gray",
                        arrowprops=dict(arrowstyle="->", color="gray", lw=1.0))

    ax.set_xlabel("Number of Tenants (N)", fontsize=11)
    ax.set_ylabel("Human Intervention Score (HIS, weighted)", fontsize=11)
    ax.set_title("(c) Human Intervention Score (HIS)\nOperator burden vs. tenant scale",
                 fontsize=11, fontweight="bold", loc="left")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9, loc="upper left")

    sample = his_data[0] if his_data else {}
    note = (f"Scenario: 1-hour operation\n"
            f"Drift events: {sample.get('n_drift_events', 3)}\n"
            f"Scale-ups: {sample.get('n_scale_ups', 2)}")
    ax.text(0.97, 0.05, note, transform=ax.transAxes, fontsize=8,
            ha="right", va="bottom", color="dimgray",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#F5F5F5",
                      edgecolor="#aaa", alpha=0.9))


# ── Main figure (3-panel: a | b | c) ─────────────────────────────────────────

def plot_figure5(data: dict, out_path: Path) -> None:
    """
    Layout: single row of three panels.
      col 0 — (a) PCR  (narrower — bar chart with 2-3 bars)
      col 1 — (b) MTTR broken-axis
      col 2 — (c) HIS  (wider — line chart with many points)
    """
    fig = plt.figure(figsize=(18, 7))
    gs  = fig.add_gridspec(1, 3, wspace=0.38, width_ratios=[1, 1, 1.3])

    ax_pcr = fig.add_subplot(gs[0, 0])
    # gs[0, 1] is split into two sub-axes inside plot_mttr
    ax_his = fig.add_subplot(gs[0, 2])

    # ── (a) PCR ──────────────────────────────────────────────────────────
    if "exp1a_pcr" in data:
        plot_pcr(ax_pcr, data["exp1a_pcr"])
    else:
        ax_pcr.text(0.5, 0.5, "PCR data unavailable\n(--skip-1a was used)",
                    ha="center", va="center", transform=ax_pcr.transAxes,
                    fontsize=11, color="gray")
        ax_pcr.set_title("(a) Policy Coverage Rate (PCR)", fontsize=11,
                         fontweight="bold", loc="left")

    # ── (b) MTTR (broken axis) ───────────────────────────────────────────
    if "exp1b_mttr" in data:
        plot_mttr(fig, gs[0, 1], data["exp1b_mttr"])
    else:
        ax_ph = fig.add_subplot(gs[0, 1])
        ax_ph.text(0.5, 0.5, "MTTR data unavailable\n(--skip-1b was used)",
                   ha="center", va="center", transform=ax_ph.transAxes,
                   fontsize=11, color="gray")
        ax_ph.set_title("(b) Drift Recovery Time (MTTR)", fontsize=11,
                        fontweight="bold", loc="left")

    # ── (c) HIS ──────────────────────────────────────────────────────────
    if "exp1c_his" in data:
        plot_his(ax_his, data["exp1c_his"])
    else:
        ax_his.text(0.5, 0.5, "HIS data unavailable",
                    ha="center", va="center", transform=ax_his.transAxes,
                    fontsize=11, color="gray")
        ax_his.set_title("(c) Human Intervention Score (HIS)", fontsize=11,
                         fontweight="bold", loc="left")

    fig.suptitle(
        "Figure 5 \u2014 Automation Completeness: Helm vs. Agentic Operator\n"
        "\"Single CR submission vs. repeated manual kubectl operations\"",
        fontsize=13, fontweight="bold", y=1.03,
    )
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Figure 5: Automation Completeness (PCR | MTTR | HIS)"
    )
    parser.add_argument("--input", default="results/exp1_results.json")
    parser.add_argument("--out",   default="results/fig5_automation_completeness.png")
    args = parser.parse_args()

    in_path  = SCRIPT_DIR / args.input
    out_path = SCRIPT_DIR / args.out

    if not in_path.exists():
        print(f"Error: {in_path} not found — run run_experiment.py first.")
        return 1

    data = load(in_path)
    print(f"Loaded: {in_path}")
    keys = [k for k in ["exp1a_pcr", "exp1b_mttr", "exp1c_his"] if k in data]
    print(f"  Sub-experiments present: {keys}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plot_figure5(data, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
