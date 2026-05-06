#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# 경로 설정
SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
DATA_PATH = RESULTS_DIR / "exp3_isolation_results.json"
OUT_PNG = RESULTS_DIR / "exp3_isolation_integrated.png"
OUT_PDF = RESULTS_DIR / "exp3_isolation_integrated.pdf"

COLOR_BASELINE = "#1f77b4"  # 파란색 (Baseline)
COLOR_OPERATOR = "#ff7f0e"  # 주황색 (Operator)

def load() -> List[Dict[str, Any]]:
    with open(DATA_PATH) as f:
        return json.load(f)

def main():
    data = load()
    baseline = next(r for r in data if r["mode"] == "baseline")
    operator = next(r for r in data if r["mode"] == "operator")

    b_lat, o_lat = np.array(baseline["latencies_ms"]), np.array(operator["latencies_ms"])
    b_ts, o_ts = np.array(baseline["timestamps"]), np.array(operator["timestamps"])
    b_s, o_s = baseline["stats"], operator["stats"]

    # 스타일 설정 (폰트 크기 미세 조정)
    plt.rcParams.update({
        "font.family": "serif",
        "font.serif": ["Times New Roman", "DejaVu Serif"],
        "font.size": 10,
        "axes.labelsize": 11,
        "axes.titlesize": 12,
        "legend.fontsize": 10,
        "grid.alpha": 0.2,
    })

    # 전체 레이아웃 설정
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), constrained_layout=True)
    
    # [수정] 전체 그래프 제목 추가
    # fig.suptitle("Figure 4. Noisy Neighbor Isolation Performance Analysis", fontsize=16, fontweight="bold")

    # 공통 텍스트 상자 스타일
    bbox_props = dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85, edgecolor="lightgray")

    # ── (a) Time-Series: Victim Latency ──────────────────────────
    ax = axes[0, 0]
    def moving_avg(arr, w=7):
        return np.convolve(arr, np.ones(w)/w, mode="valid")

    ax.plot(b_ts, b_lat, color=COLOR_BASELINE, alpha=0.1, lw=0.5)
    ax.plot(o_ts, o_lat, color=COLOR_OPERATOR, alpha=0.1, lw=0.5)
    
    w = 7
    l1, = ax.plot(b_ts[w-1:], moving_avg(b_lat, w), "--", color=COLOR_BASELINE, lw=2, label="Baseline (No Protection)")
    l2, = ax.plot(o_ts[w-1:], moving_avg(o_lat, w), "-", color=COLOR_OPERATOR, lw=2.5, label="Operator (Hard Isolation)")

    ax.set_ylabel("Latency (ms)")
    ax.set_xlabel("Time (seconds)")
    ax.set_title("(a) Victim Tenant Latency Time-Series", fontweight="bold", pad=10)
    
    # [수정] 상단 여백 확보 및 범례 위치 최적화
    ax.set_ylim(min(o_lat)*0.9, max(b_lat)*1.2)
    ax.legend(loc="upper right", frameon=True, shadow=False)
    ax.grid(True)

# ── (b) Latency CDF ─────────────────────────────────────────
    ax = axes[0, 1]
    b_sorted, o_sorted = np.sort(b_lat), np.sort(o_lat)
    b_cdf = np.arange(1, len(b_sorted) + 1) / len(b_sorted) * 100
    o_cdf = np.arange(1, len(o_sorted) + 1) / len(o_sorted) * 100

    ax.plot(b_sorted, b_cdf, "--", color=COLOR_BASELINE, lw=2)
    ax.plot(o_sorted, o_cdf, "-", color=COLOR_OPERATOR, lw=2.5)
    ax.set_ylabel("Cumulative Percentage (%)")
    ax.set_xlabel("Latency (ms)")
    ax.set_title("(b) Latency Distribution (CDF)", fontweight="bold", pad=10)
    ax.set_ylim(0, 108) 
    ax.grid(True)
    
    # [수정] P95 가이드라인 강화: 색상을 진하게 하고 굵기를 조정함
    ax.axhline(95, color="#555555", linestyle="--", alpha=0.7, lw=1.2)
    # [수정] 텍스트가 선 바로 위에 떠 있도록 위치와 폰트 가독성 개선
    ax.text(ax.get_xlim()[0] + (ax.get_xlim()[1] - ax.get_xlim()[0]) * 0.02, 
            96.5, "P95 Threshold (SLA)", color="#444444", fontsize=10, fontweight='bold')

    # ── (c) P95 / P99 Bar Comparison ─────────────────────────────
    ax = axes[1, 0]
    x = np.arange(2)
    width = 0.35

    bars1 = ax.bar(x - width/2, [b_s["p95_ms"], o_s["p95_ms"]], width, label="P95", color=[COLOR_BASELINE, COLOR_OPERATOR], alpha=0.8)
    bars2 = ax.bar(x + width/2, [b_s["p99_ms"], o_s["p99_ms"]], width, label="P99", color=[COLOR_BASELINE, COLOR_OPERATOR], alpha=0.4, hatch="////")
    
    ax.set_xticks(x)
    ax.set_xticklabels(["Baseline", "Operator"], fontweight="bold")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("(c) P95 / P99 Latency Comparison", fontweight="bold", pad=10)
    
    # 막대 위 숫자 표시
    for bar in bars1 + bars2:
        height = bar.get_height()
        ax.annotate(f'{height:.0f}', xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=9)

    improv = ((b_s["p95_ms"] - o_s["p95_ms"]) / b_s["p95_ms"]) * 100
    ax.text(0.05, 0.90, f"P95 Improvement: {improv:+.1f}%\n(Hatched: P99 Tail)", transform=ax.transAxes, bbox=bbox_props)
    ax.set_ylim(0, max(b_s["p99_ms"], o_s["p99_ms"]) * 1.25)

    # ── (d) Jitter (σ) Comparison ────────────────────────────────
    ax = axes[1, 1]
    jitter_bars = ax.bar(["Baseline", "Operator"], [b_s["std_ms"], o_s["std_ms"]], color=[COLOR_BASELINE, COLOR_OPERATOR], alpha=0.8, width=0.5)
    ax.set_ylabel("Jitter — Std Dev (ms)")
    ax.set_title("(d) Response Stability (Jitter)", fontweight="bold", pad=10)
    
    for bar in jitter_bars:
        height = bar.get_height()
        ax.annotate(f'{height:.1f}ms', xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight="bold")

    jit_red = ((b_s["std_ms"] - o_s["std_ms"]) / b_s["std_ms"]) * 100
    ax.text(0.05, 0.90, f"Jitter Reduction: {jit_red:+.1f}%", transform=ax.transAxes, bbox=bbox_props)
    ax.set_ylim(0, max(b_s["std_ms"], o_s["std_ms"]) * 1.3)

    # 저장 시 여백 조정 재확인
    plt.savefig(OUT_PNG, dpi=300, bbox_inches="tight")
    plt.savefig(OUT_PDF, bbox_inches="tight")
    plt.close()
    
    print(f"✓ Saved Figure 4 to {OUT_PNG}")

if __name__ == "__main__":
    main()