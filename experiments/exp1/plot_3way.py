#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1 시각화: Manual vs Helm vs Agentic
─────────────────────────────────────────
좌측: 누적 성공 곡선 (X = elapsed_s, Y = cumulative success %)
       모드별로 색을 통일하고 batch별로 line-style 분리.
우측: 모드별 ready_latency_s box/strip plot (+ Agentic은 서버 측 timing 오버레이).
하단: Mann-Whitney U 통계 결과 박스.

실행: python3 plot_3way.py [--input results/exp1_results.json]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"

# Color palette — consistent with Exp2/Exp3 papers.
MODE_COLOR = {
    "manual":  "#377EB8",   # blue
    "helm":    "#4DAF4A",   # green
    "agentic": "#E66F00",   # orange
}
MODE_MARKER = {"manual": "s", "helm": "^", "agentic": "o"}
MODE_LABEL = {
    "manual":  "Manual (kubectl, sequential)",
    "helm":    "Helm (chart install)",
    "agentic": "Agentic Operator (CRD)",
}
BATCH_LS = {5: "--", 10: "-", 25: "-.", 50: ":"}


def load(path: Path) -> List[dict]:
    with open(path) as f:
        return json.load(f)


def mean_curve(records: List[dict], common_ts: np.ndarray) -> np.ndarray:
    """Step-interpolate each curve onto `common_ts`, then average."""
    samples = []
    for rec in records:
        t = np.asarray(rec["times_s"], dtype=float)
        y = np.asarray(rec["cumulative_pct"], dtype=float)
        if len(t) < 2:
            continue
        # Step-wise: take y[k] for the largest t[k] <= ts.
        idx = np.searchsorted(t, common_ts, side="right") - 1
        idx = np.clip(idx, 0, len(t) - 1)
        samples.append(y[idx])
    if not samples:
        return np.zeros_like(common_ts)
    return np.mean(samples, axis=0)


def plot(data: List[dict], out_path: Path) -> None:
    modes_present = sorted({r["mode"] for r in data},
                           key=lambda m: ["manual", "helm", "agentic"].index(m)
                           if m in ["manual", "helm", "agentic"] else 99)
    batches = sorted({r["batch_size"] for r in data})

    fig = plt.figure(figsize=(15, 8))
    gs = fig.add_gridspec(2, 3, height_ratios=[3, 1.2], width_ratios=[2.2, 1.0, 1.0],
                          hspace=0.45, wspace=0.32)

    # ─── (a) Cumulative success curves ──────────────────────────────
    ax = fig.add_subplot(gs[0, 0])
    t_max = max((max(r["times_s"]) for r in data if r["times_s"]), default=10.0)
    common_ts = np.linspace(0, t_max, 400)

    for mode in modes_present:
        for batch in batches:
            recs = [r for r in data if r["mode"] == mode and r["batch_size"] == batch]
            if not recs:
                continue
            y = mean_curve(recs, common_ts)
            ax.plot(common_ts, y,
                    color=MODE_COLOR[mode], linewidth=2.2,
                    linestyle=BATCH_LS.get(batch, "-"),
                    label=f"{MODE_LABEL[mode]}, B={batch}")

    ax.set_xlabel("Elapsed time (s)", fontsize=12)
    ax.set_ylabel("Cumulative success rate (%)", fontsize=12)
    ax.set_title("(a) Cumulative tenant readiness curve",
                 fontsize=13, fontweight="bold", loc="left")
    ax.set_ylim(0, 105)
    ax.set_xlim(0, t_max * 1.02)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", fontsize=9, framealpha=0.95)

    # ─── (b) Ready latency box/strip ────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    box_data, box_labels, box_colors = [], [], []
    for mode in modes_present:
        for batch in batches:
            vals = [r["ready_latency_s"] for r in data
                    if r["mode"] == mode and r["batch_size"] == batch
                    and r["ready_latency_s"] == r["ready_latency_s"]]
            if vals:
                box_data.append(vals)
                box_labels.append(f"{mode[0].upper()}\nB={batch}")
                box_colors.append(MODE_COLOR[mode])

    if box_data:
        bp = ax2.boxplot(box_data, patch_artist=True, widths=0.55, showfliers=False)
        for patch, c in zip(bp["boxes"], box_colors):
            patch.set_facecolor(c); patch.set_alpha(0.55); patch.set_edgecolor(c)
        for med in bp["medians"]:
            med.set_color("black"); med.set_linewidth(1.8)
        # strip
        for i, (vals, c) in enumerate(zip(box_data, box_colors), start=1):
            jitter = np.random.uniform(-0.1, 0.1, size=len(vals))
            ax2.scatter(np.full(len(vals), i) + jitter, vals,
                        color=c, edgecolor="black", s=28, zorder=3)
        ax2.set_xticklabels(box_labels, fontsize=9)

    ax2.set_ylabel("Ready latency (s)", fontsize=11)
    ax2.set_title("(b) Client-side ready latency",
                  fontsize=12, fontweight="bold", loc="left")
    ax2.grid(True, alpha=0.3, axis="y")

    # ─── (c) Server-side timing for Agentic ─────────────────────────
    # Uses "duration" = e2e_duration (client submit → conditions[Ready])
    # which has µs precision and is never 0, even for sub-second reconciles.
    ax3 = fig.add_subplot(gs[0, 2])
    server_records = [r for r in data if r.get("server_side")]
    if server_records:
        per_tenant_durs: Dict[int, List[float]] = {}
        n_e2e_total = 0
        for r in server_records:
            for t in r["server_side"]["per_tenant"]:
                # "duration" = e2e_duration if available, server_duration otherwise
                d = t.get("duration") or t.get("e2e_duration") or t.get("server_duration", 0.0)
                per_tenant_durs.setdefault(r["batch_size"], []).append(d)
                if "e2e_duration" in t:
                    n_e2e_total += 1
        positions, vals_list, lbls = [], [], []
        for i, batch in enumerate(sorted(per_tenant_durs.keys()), start=1):
            positions.append(i); vals_list.append(per_tenant_durs[batch])
            lbls.append(f"B={batch}\n(n={len(per_tenant_durs[batch])})")
        bp = ax3.boxplot(vals_list, positions=positions, patch_artist=True,
                         widths=0.55, showfliers=False)
        for patch in bp["boxes"]:
            patch.set_facecolor(MODE_COLOR["agentic"]); patch.set_alpha(0.55)
            patch.set_edgecolor(MODE_COLOR["agentic"])
        for med in bp["medians"]:
            med.set_color("black"); med.set_linewidth(1.8)
        for pos, vals in zip(positions, vals_list):
            jitter = np.random.uniform(-0.1, 0.1, size=len(vals))
            ax3.scatter(np.full(len(vals), pos) + jitter, vals,
                        color=MODE_COLOR["agentic"],
                        edgecolor="black", s=20, alpha=0.7, zorder=3)
        ax3.set_xticks(positions); ax3.set_xticklabels(lbls, fontsize=9)
        if n_e2e_total > 0:
            ax3.text(0.02, 0.98, f"E2E timing (µs precision)\nn={n_e2e_total}",
                     transform=ax3.transAxes, fontsize=8.5, va="top", ha="left",
                     bbox=dict(boxstyle="round,pad=0.3", facecolor="#FFF8E7",
                               edgecolor="#aaa", alpha=0.9))
    else:
        ax3.text(0.5, 0.5, "No Agentic data\n(server-side timings)",
                 ha="center", va="center", fontsize=11, color="gray",
                 transform=ax3.transAxes)
    ax3.set_ylabel("Operator response time (s)\n(client submit → conditions[Ready])",
                   fontsize=10)
    ax3.set_title("(c) Agentic — E2E server-side timing",
                  fontsize=12, fontweight="bold", loc="left")
    ax3.grid(True, alpha=0.3, axis="y")

    # ─── (d) Stats panel ────────────────────────────────────────────
    ax4 = fig.add_subplot(gs[1, :]); ax4.axis("off")
    txt_lines: List[str] = ["STATISTICAL VALIDATION (Mann-Whitney U, two-sided):"]
    for batch in batches:
        per_mode = {m: [r["ready_latency_s"] for r in data
                        if r["mode"] == m and r["batch_size"] == batch
                        and r["ready_latency_s"] == r["ready_latency_s"]]
                    for m in modes_present}
        labelled = {m: v for m, v in per_mode.items() if len(v) >= 2}
        if "agentic" not in labelled or len(labelled) < 2:
            continue
        txt_lines.append(f"\n  Batch B = {batch}:")
        for m in modes_present:
            if m == "agentic" or m not in labelled:
                continue
            try:
                u, p = stats.mannwhitneyu(labelled["agentic"], labelled[m],
                                          alternative="two-sided")
                med_a = float(np.median(labelled["agentic"]))
                med_b = float(np.median(labelled[m]))
                speedup = med_b / med_a if med_a > 0 else float("inf")
                sig = "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "n.s."
                txt_lines.append(
                    f"    Agentic vs {m:<7s}: U={u:7.1f}, p={p:.4f} {sig}   "
                    f"median={med_a:.2f}s vs {med_b:.2f}s   speedup×{speedup:.2f}"
                )
            except ValueError as e:
                txt_lines.append(f"    Agentic vs {m}: insufficient samples ({e})")

    # Server-side summary (Agentic, very rigorous)
    server_records = [r for r in data if r.get("server_side")]
    if server_records:
        all_durs: List[float] = []
        for r in server_records:
            all_durs.extend([t["server_duration"] for t in r["server_side"]["per_tenant"]])
        if all_durs:
            txt_lines.append("\n  Agentic — server-side (creationTimestamp → conditions[Ready]):")
            txt_lines.append(
                f"    n={len(all_durs):d}  "
                f"mean={np.mean(all_durs):.3f}s  "
                f"median={np.median(all_durs):.3f}s  "
                f"p95={np.percentile(all_durs, 95):.3f}s  "
                f"max={np.max(all_durs):.3f}s"
            )

    ax4.text(0.01, 0.98, "\n".join(txt_lines), transform=ax4.transAxes,
             ha="left", va="top", fontsize=10, family="monospace",
             bbox=dict(facecolor="#FFF8E7", edgecolor="#888", boxstyle="round,pad=0.6"))

    fig.suptitle("Experiment 1 — Provisioning Speed: Manual vs Helm vs Agentic Operator",
                 fontsize=14, fontweight="bold", y=0.995)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ Saved {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="results/exp1_results.json")
    parser.add_argument("--out", default="results/exp1_3way.png")
    args = parser.parse_args()

    in_path = SCRIPT_DIR / args.input
    out_path = SCRIPT_DIR / args.out
    if not in_path.exists():
        print(f"Error: {in_path} not found. Run run_experiment.py first.")
        return 1
    data = load(in_path)
    print(f"Loaded {len(data)} records from {in_path}")
    plot(data, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
