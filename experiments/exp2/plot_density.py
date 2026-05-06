#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 2 논문형 시각화: 2x2 Subplots

  (a) Control Plane Latency vs. Number of Tenants
  (b) Per-Tenant Avg Response Time vs. Number of Tenants
  (c) Total Provisioning Time vs. Number of Tenants
  (d) Density Gain — Overhead Ratio (Dynamic / Static)

글로벌 범례를 하단 중앙에 1회만 배치.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

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


def load() -> List[Dict[str, Any]]:
    with open(DATA_PATH) as f:
        return json.load(f)


def aggregate(records: List[Dict], mode: str):
    """Return sorted (tenant_counts, mean_cp_ms, mean_tenant_ms, mean_total_s)."""
    recs = [r for r in records if r["mode"] == mode]
    ns = sorted(set(r["n_tenants"] for r in recs))
    cp, tn, tt = [], [], []
    for n in ns:
        sub = [r for r in recs if r["n_tenants"] == n]
        cp.append(np.mean([r["avg_cp_latency"] for r in sub]) * 1000)
        tn.append(np.mean([r["avg_tenant_latency"] for r in sub]) * 1000)
        tt.append(np.mean([r["total_time"] for r in sub]))
    return np.array(ns), np.array(cp), np.array(tn), np.array(tt)


def find_saturation(ns, latencies, threshold_factor=2.0):
    """Saturation point = first N where latency > threshold_factor * baseline."""
    if len(latencies) < 2:
        return None
    baseline = latencies[0]
    for i, lat in enumerate(latencies):
        if lat > baseline * threshold_factor:
            return ns[i]
    return None


def main():
    data = load()
    ns_s, cp_s, tn_s, tt_s = aggregate(data, "static")
    ns_d, cp_d, tn_d, tt_d = aggregate(data, "dynamic")

    # ── Style ────────────────────────────────────────────────────────
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif", "Liberation Serif"],
        "font.size": 11,
        "axes.labelsize": 12,
        "axes.titlesize": 13,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 12,
        "grid.alpha": 0.3,
        "grid.linestyle": "--",
    })

    COLOR_STATIC = "#1f77b4"  # 파란색 (Manual/Static)
    COLOR_DYNAMIC = "#ff7f0e" # 주황색 (Agentic/Dynamic)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    plt.subplots_adjust(hspace=0.25, wspace=0.20)

    lines_for_legend = []
    labels_for_legend = []

    # ── (a) Control Plane Latency ────────────────────────────────────
    ax = axes[0, 0]
    l1, = ax.plot(ns_s, cp_s, "o--", color=COLOR_STATIC, lw=2, markersize=5,
                  label="Static (Fixed Quota)")
    l2, = ax.plot(ns_d, cp_d, "s-", color=COLOR_DYNAMIC, lw=2.5, markersize=5,
                  label="Dynamic (Operator Re-balancing)")
    lines_for_legend = [l2, l1]
    labels_for_legend = ["Dynamic (Operator)", "Static (Baseline)"]

    # Saturation Point 계산
    sat_s = find_saturation(ns_s, cp_s)
    sat_d = find_saturation(ns_d, cp_d)

    ax.set_xlabel("Number of Tenants")
    ax.set_ylabel("CP Latency (ms)")
    ax.set_title("(a) Control Plane Latency", fontweight="bold")
    ax.grid(True)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # ── (b) Per-Tenant Response Time ─────────────────────────────────
    ax = axes[0, 1]
    ax.plot(ns_s, tn_s, "o--", color=COLOR_STATIC, lw=2, markersize=5)
    ax.plot(ns_d, tn_d, "s-", color=COLOR_DYNAMIC, lw=2.5, markersize=5)
    ax.set_xlabel("Number of Tenants")
    ax.set_ylabel("Avg Response Time (ms)")
    ax.set_title("(b) Per-Tenant Response Time", fontweight="bold")
    ax.grid(True)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # ── (c) Total Provisioning Time (with Intelligent Overhead) ──────
    ax = axes[1, 0]
    ax.plot(ns_s, tt_s, "o--", color=COLOR_STATIC, lw=2, markersize=5)
    ax.plot(ns_d, tt_d, "s-", color=COLOR_DYNAMIC, lw=2.5, markersize=5)

    # 지능적 오버헤드 강조 (Fill between)
    common_n = np.intersect1d(ns_s, ns_d)
    if len(common_n) > 0:
        idx_s = [list(ns_s).index(n) for n in common_n]
        idx_d = [list(ns_d).index(n) for n in common_n]
        # Dynamic이 Static보다 시간이 더 걸리는 부분(오버헤드)을 시각화
        ax.fill_between(common_n, tt_s[idx_s], tt_d[idx_d],
                        where=(tt_d[idx_d] >= tt_s[idx_s]),
                        alpha=0.2, color=COLOR_DYNAMIC, label="Intelligent Overhead")

    ax.set_xlabel("Number of Tenants")
    ax.set_ylabel("Total Time (s)")
    ax.set_title("(c) Total Provisioning Time", fontweight="bold")
    ax.grid(True)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # ── (d) Density Gain & Efficiency ────────────────────────────────
    ax = axes[1, 1]
    common_ns = np.intersect1d(ns_s, ns_d)
    if len(common_ns) > 0:
        idx_s = [list(ns_s).index(n) for n in common_ns]
        idx_d = [list(ns_d).index(n) for n in common_ns]

        overhead_ratio = cp_d[[list(ns_d).index(n) for n in common_ns]] / cp_s[idx_s]
        density_gain = tt_s[idx_s] / tt_d[idx_d]

        # 바 그래프로 효율성 표시
        ax.bar(common_ns - 0.2, density_gain, width=0.4, color=COLOR_DYNAMIC, alpha=0.7, label="Density Gain")
        ax.bar(common_ns + 0.2, overhead_ratio, width=0.4, color=COLOR_STATIC, alpha=0.5, label="CP Overhead Ratio")
        
        # 기준선 (Ratio=1.0) 추가
        ax.axhline(y=1.0, color="black", ls="--", lw=1.5, alpha=0.6)
        ax.text(ns_s[0], 1.05, "Baseline (1.0)", fontsize=9, color="black", alpha=0.7)

        # 최대 Density Gain 정보 추출
        max_dg_idx = np.argmax(density_gain)
        max_dg_val = density_gain[max_dg_idx]
        max_dg_n = common_ns[max_dg_idx]
        
        # 텍스트 상자 추가
        props = dict(boxstyle="round", facecolor="white", alpha=0.85, edgecolor="lightgray")
        ax.text(0.95, 0.95, f"Max Density Gain: {max_dg_val:.2f}x\n@ N={max_dg_n} tenants",
                transform=ax.transAxes, fontsize=10, va="top", ha="right", bbox=props)

    ax.set_xlabel("Number of Tenants")
    ax.set_ylabel("Efficiency Ratio")
    ax.set_title("(d) Density Gain & Efficiency", fontweight="bold")
    ax.grid(True)
    ax.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # ── 글로벌 범례 (하단 중앙) ──────────────────────────────────────
    fig.legend(lines_for_legend, labels_for_legend,
               loc="lower center", ncol=2, bbox_to_anchor=(0.5, 0.02),
               frameon=True, fontsize=12, borderpad=1)

    plt.tight_layout(rect=[0, 0.08, 1, 0.98])

    fig.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    fig.savefig(OUT_PDF, bbox_inches="tight")
    plt.close(fig)

    print(f"✓ Saved {OUT_PNG}")
    print(f"✓ Saved {OUT_PDF}")

    # ── Density Gain Summary ─────────────────────────────────────────
    if len(common_ns) > 0:
        print("\n  Density Gain Summary:")
        for i, n in enumerate(common_ns):
            print(f"    N={n:>2}  Speedup={density_gain[i]:.2f}x  CP-Overhead-Ratio={overhead_ratio[i]:.2f}")

        print(f"\n  ★ Maximum Density Gain = {max_dg_val:.2f}x at N={max_dg_n} tenants")

        # Saturation analysis
        print(f"\n  Saturation Point (2x baseline CP latency):")
        print(f"    Static  : N = {sat_s if sat_s else '> ' + str(int(ns_s[-1]))}")
        print(f"    Dynamic : N = {sat_d if sat_d else '> ' + str(int(ns_d[-1]))}")

        if sat_s and sat_d:
            extra = sat_d - sat_s
            print(f"    → Dynamic supports {extra} more tenants before saturation")
        elif sat_s and not sat_d:
            print(f"    → Dynamic has NOT saturated (Static saturated at N={sat_s})")


if __name__ == "__main__":
    main()
