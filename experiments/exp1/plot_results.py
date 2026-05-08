#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Figure 5: 자동화 완성도 비교 (Automation Completeness)
═══════════════════════════════════════════════════════
4-패널 구성:
  (a) Policy Coverage Rate (PCR) — 격리 정책 자동 적용 비율
  (b) Drift Recovery Time (MTTR) — Helm=∞, Agentic=~30s
  (c) Human Intervention Score (HIS) — 테넌트 수 대비 운영자 개입 횟수
  (d) 누적 수렴 곡선 — 완전한 스택 완료 시점 (보조)

실행: python3 plot_results.py [--input results/exp1_results.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent

MODE_COLOR = {
    "helm-basic": "#AAAAAA",   # grey
    "helm":       "#4DAF4A",   # green
    "agentic":    "#E66F00",   # orange
}
MODE_LABEL = {
    "helm-basic": "Helm (기본, NS+RQ)",
    "helm":       "Helm (현재 chart)",
    "agentic":    "Agentic Operator",
}
BATCH_LS = {5: "--", 10: "-", 25: "-.", 50: ":"}

INFINITY_BAR_HEIGHT = 110.0  # visual height for "∞" bars in MTTR chart
N_POLICIES = 7


def load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ── (a) PCR bar chart ─────────────────────────────────────────────────

def plot_pcr(ax: plt.Axes, pcr_data: List[dict]) -> None:
    """
    Bar chart: Policy Coverage Rate per mode.
    Each bar = mean PCR across tenants.  Error bar = std dev.
    Horizontal lines show policy count thresholds.
    """
    by_mode: Dict[str, List[float]] = {}
    for r in pcr_data:
        by_mode.setdefault(r["mode"], []).append(r["pcr"] * 100)

    modes_order = ["helm-basic", "helm", "agentic"]
    modes_present = [m for m in modes_order if m in by_mode]

    x = np.arange(len(modes_present))
    means = [np.mean(by_mode[m]) for m in modes_present]
    stds  = [np.std(by_mode[m])  for m in modes_present]
    colors = [MODE_COLOR[m] for m in modes_present]

    bars = ax.bar(x, means, yerr=stds, capsize=6,
                  color=colors, edgecolor="black", linewidth=0.8,
                  alpha=0.82, width=0.55, error_kw={"elinewidth": 1.5})

    # Policy count labels inside bars
    for bar, mode, mean in zip(bars, modes_present, means):
        n_pol = round(mean * N_POLICIES / 100)
        ax.text(bar.get_x() + bar.get_width() / 2, mean / 2,
                f"{n_pol}/{N_POLICIES}", ha="center", va="center",
                fontsize=11, fontweight="bold", color="white")
        ax.text(bar.get_x() + bar.get_width() / 2, mean + max(stds) + 2,
                f"{mean:.0f}%", ha="center", va="bottom", fontsize=10)

    # Reference lines
    ax.axhline(100, color="black", linewidth=1.0, linestyle="--", alpha=0.4)
    for pct, label in [(2/7*100, "2/7"), (5/7*100, "5/7"), (100, "7/7")]:
        ax.text(len(modes_present) - 0.1, pct + 1, label,
                ha="right", va="bottom", fontsize=8, color="gray")

    ax.set_xticks(x)
    ax.set_xticklabels([MODE_LABEL.get(m, m) for m in modes_present],
                       fontsize=10)
    ax.set_ylabel("Policy Coverage Rate (%)", fontsize=11)
    ax.set_ylim(0, 120)
    ax.set_title("(a) Policy Coverage Rate (PCR)\n격리 정책 자동 적용 비율",
                 fontsize=12, fontweight="bold", loc="left")
    ax.grid(True, axis="y", alpha=0.3)

    legend_patches = [
        mpatches.Patch(color=MODE_COLOR[m], label=MODE_LABEL.get(m, m), alpha=0.82)
        for m in modes_present
    ]
    ax.legend(handles=legend_patches, fontsize=9, loc="lower right")


# ── (b) MTTR bar chart ────────────────────────────────────────────────

def plot_mttr(ax: plt.Axes, mttr_data: List[dict]) -> None:
    """
    Bar chart: MTTR per mode.
    Helm bars are shown at INFINITY_BAR_HEIGHT with "∞" label and hatching.
    Agentic bars show mean ± std.
    """
    by_mode: Dict[str, List[Optional[float]]] = {}
    for r in mttr_data:
        by_mode.setdefault(r["mode"], []).append(r["mttr_s"])

    modes_order = ["helm", "agentic"]
    modes_present = [m for m in modes_order if m in by_mode]

    x = np.arange(len(modes_present))
    bar_heights, bar_stds, bar_colors, inf_flags = [], [], [], []

    for mode in modes_present:
        vals = by_mode[mode]
        finite = [v for v in vals if v is not None]
        n_inf  = sum(1 for v in vals if v is None)

        if mode == "helm" or n_inf == len(vals):
            bar_heights.append(INFINITY_BAR_HEIGHT)
            bar_stds.append(0)
            inf_flags.append(True)
        elif finite:
            bar_heights.append(np.mean(finite))
            bar_stds.append(np.std(finite))
            inf_flags.append(False)
        else:
            bar_heights.append(0)
            bar_stds.append(0)
            inf_flags.append(False)

        bar_colors.append(MODE_COLOR[mode])

    for i, (mode, h, std, is_inf) in enumerate(
            zip(modes_present, bar_heights, bar_stds, inf_flags)):
        if is_inf:
            ax.bar(i, h, color=MODE_COLOR[mode], edgecolor="black",
                   linewidth=0.8, alpha=0.4, width=0.55,
                   hatch="///", label=f"{MODE_LABEL.get(mode, mode)} (∞)")
            ax.text(i, h / 2, "∞\n자동 복구\n불가",
                    ha="center", va="center", fontsize=13,
                    fontweight="bold", color=MODE_COLOR[mode])
            ax.text(i, h + 2, "No Auto-Recovery", ha="center", va="bottom",
                    fontsize=9, color="dimgray")
        else:
            ax.bar(i, h, yerr=std, capsize=7, color=MODE_COLOR[mode],
                   edgecolor="black", linewidth=0.8, alpha=0.82, width=0.55,
                   error_kw={"elinewidth": 1.5},
                   label=MODE_LABEL.get(mode, mode))

            # Individual run dots
            finite = [v for v in by_mode[mode] if v is not None]
            if finite:
                jitter = np.random.uniform(-0.08, 0.08, size=len(finite))
                ax.scatter(np.full(len(finite), i) + jitter, finite,
                           color=MODE_COLOR[mode], edgecolor="black",
                           s=40, zorder=4, alpha=0.8)
                ax.text(i, h + std + 3, f"{h:.1f}s",
                        ha="center", va="bottom", fontsize=10)

    ax.set_xticks(x)
    ax.set_xticklabels([MODE_LABEL.get(m, m) for m in modes_present], fontsize=10)
    ax.set_ylabel("Recovery Time (s)", fontsize=11)
    ax.set_ylim(0, INFINITY_BAR_HEIGHT * 1.15)
    ax.set_title("(b) Drift Recovery Time (MTTR)\n정책 드리프트 후 자동 복구 시간",
                 fontsize=12, fontweight="bold", loc="left")
    ax.grid(True, axis="y", alpha=0.3)

    # RequeueAfter reference line
    ax.axhline(30, color="steelblue", linewidth=1.2, linestyle=":",
               label="RequeueAfter ~30s")
    ax.legend(fontsize=9, loc="upper right")


# ── (c) HIS line chart ────────────────────────────────────────────────

def plot_his(ax: plt.Axes, his_data: List[dict]) -> None:
    """
    Line chart: Human Intervention Score vs tenant count.
    Shows divergence between Helm (O(N) + drift overhead) and Agentic (O(N) only).
    """
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
        xs = sorted(by_mode["helm"].keys())
        ys_helm   = [by_mode["helm"][x]    for x in xs]
        ys_agnt   = [by_mode["agentic"][x] for x in xs]
        ax.fill_between(xs, ys_agnt, ys_helm,
                        alpha=0.12, color="gray",
                        label="자동화 절약 구간")

    # Annotation for N=50
    if "helm" in by_mode and "agentic" in by_mode:
        n = 50
        if n in by_mode["helm"] and n in by_mode["agentic"]:
            diff = by_mode["helm"][n] - by_mode["agentic"][n]
            ax.annotate(f"N={n}: Helm이\n{diff}회 더 개입",
                        xy=(n, by_mode["helm"][n]),
                        xytext=(n - 25, by_mode["helm"][n] + 5),
                        fontsize=9, color="gray",
                        arrowprops=dict(arrowstyle="->", color="gray", lw=1.0))

    ax.set_xlabel("테넌트 수 (N)", fontsize=11)
    ax.set_ylabel("운영자 부담 지수 (HIS, 복잡도 가중치)", fontsize=11)
    ax.set_title("(c) Human Intervention Score (HIS)\n테넌트 수 증가 시 운영 부담 비교",
                 fontsize=12, fontweight="bold", loc="left")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=9, loc="upper left")

    # Add scenario note
    sample = his_data[0] if his_data else {}
    note = (f"시나리오: 1시간 운영\n"
            f"드리프트 이벤트: {sample.get('n_drift_events', 3)}회\n"
            f"확장: {sample.get('n_scale_ups', 2)}회")
    ax.text(0.97, 0.05, note, transform=ax.transAxes, fontsize=8,
            ha="right", va="bottom", color="dimgray",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#F5F5F5",
                      edgecolor="#aaa", alpha=0.9))


# ── (d) Cumulative convergence curve ─────────────────────────────────

def _mean_curve(records: List[dict], common_ts: np.ndarray) -> np.ndarray:
    samples = []
    for rec in records:
        t = np.asarray(rec["times_s"], dtype=float)
        y = np.asarray(rec["cumulative_pct"], dtype=float)
        if len(t) < 2:
            continue
        idx = np.clip(np.searchsorted(t, common_ts, side="right") - 1, 0, len(t) - 1)
        samples.append(y[idx])
    return np.mean(samples, axis=0) if samples else np.zeros_like(common_ts)


def plot_convergence(ax: plt.Axes, conv_data: List[dict]) -> None:
    modes_present = sorted({r["mode"] for r in conv_data},
                           key=lambda m: ["helm", "agentic"].index(m)
                           if m in ["helm", "agentic"] else 99)
    batches = sorted({r["batch_size"] for r in conv_data})

    t_max = max((max(r["times_s"]) for r in conv_data if r["times_s"]), default=10.0)
    common_ts = np.linspace(0, t_max, 400)

    for mode in modes_present:
        for batch in batches:
            recs = [r for r in conv_data
                    if r["mode"] == mode and r["batch_size"] == batch]
            if not recs:
                continue
            y = _mean_curve(recs, common_ts)
            ax.plot(common_ts, y,
                    color=MODE_COLOR.get(mode, "#888"), linewidth=2.0,
                    linestyle=BATCH_LS.get(batch, "-"),
                    label=f"{MODE_LABEL.get(mode, mode)}, B={batch}")

    ax.set_xlabel("경과 시간 (s)", fontsize=11)
    ax.set_ylabel("누적 완료율 (%)", fontsize=11)
    ax.set_title("(d) 누적 수렴 곡선 (보조)\n완전한 격리 스택 완료 시점",
                 fontsize=12, fontweight="bold", loc="left")
    ax.set_ylim(0, 105)
    ax.set_xlim(0, t_max * 1.02)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", fontsize=9, framealpha=0.95)


# ── Main plot ─────────────────────────────────────────────────────────

def plot_figure5(data: dict, out_path: Path) -> None:
    fig = plt.figure(figsize=(16, 11))
    gs  = fig.add_gridspec(2, 2, hspace=0.42, wspace=0.32)

    ax_pcr  = fig.add_subplot(gs[0, 0])
    ax_mttr = fig.add_subplot(gs[0, 1])
    ax_his  = fig.add_subplot(gs[1, 0])
    ax_conv = fig.add_subplot(gs[1, 1])

    if "exp1a_pcr" in data:
        plot_pcr(ax_pcr, data["exp1a_pcr"])
    else:
        ax_pcr.text(0.5, 0.5, "PCR 데이터 없음\n(--skip-1a 사용됨)",
                    ha="center", va="center", transform=ax_pcr.transAxes,
                    fontsize=12, color="gray")
        ax_pcr.set_title("(a) Policy Coverage Rate", fontsize=12,
                         fontweight="bold", loc="left")

    if "exp1b_mttr" in data:
        plot_mttr(ax_mttr, data["exp1b_mttr"])
    else:
        ax_mttr.text(0.5, 0.5, "MTTR 데이터 없음\n(--skip-1b 사용됨)",
                     ha="center", va="center", transform=ax_mttr.transAxes,
                     fontsize=12, color="gray")
        ax_mttr.set_title("(b) Drift Recovery Time (MTTR)", fontsize=12,
                          fontweight="bold", loc="left")

    if "exp1c_his" in data:
        plot_his(ax_his, data["exp1c_his"])
    else:
        ax_his.text(0.5, 0.5, "HIS 데이터 없음",
                    ha="center", va="center", transform=ax_his.transAxes,
                    fontsize=12, color="gray")

    if "exp1d_convergence" in data:
        plot_convergence(ax_conv, data["exp1d_convergence"])
    else:
        ax_conv.text(0.5, 0.5, "수렴 곡선 데이터 없음\n(--skip-1d 사용됨)",
                     ha="center", va="center", transform=ax_conv.transAxes,
                     fontsize=12, color="gray")
        ax_conv.set_title("(d) 누적 수렴 곡선 (보조)", fontsize=12,
                          fontweight="bold", loc="left")

    fig.suptitle(
        "Figure 5 — 자동화 완성도 비교: Helm vs Agentic Operator\n"
        "\"CR 한 줄 vs. 다수의 수동 작업\"",
        fontsize=14, fontweight="bold", y=1.01,
    )
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"  ✓ Figure 5 저장: {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Figure 5: Automation Completeness (PCR | MTTR | HIS | Convergence)"
    )
    parser.add_argument("--input", default="results/exp1_results.json")
    parser.add_argument("--out",   default="results/fig5_automation_completeness.png")
    args = parser.parse_args()

    in_path  = SCRIPT_DIR / args.input
    out_path = SCRIPT_DIR / args.out

    if not in_path.exists():
        print(f"Error: {in_path} 없음 — run_experiment.py 먼저 실행하세요.")
        return 1

    data = load(in_path)
    print(f"결과 로드: {in_path}")
    keys = [k for k in ["exp1a_pcr", "exp1b_mttr", "exp1c_his", "exp1d_convergence"]
            if k in data]
    print(f"  포함된 서브실험: {keys}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plot_figure5(data, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
