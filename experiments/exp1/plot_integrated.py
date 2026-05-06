#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
실험 1 통합 시각화 스크립트: 2행 3열 Subplots 생성
- 행(Row): Batch Size (5, 10)
- 열(Col): Complexity (1, 2, 3)
- 스타일: 논문용 고해상도 (300dpi), PDF/PNG 저장
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import matplotlib.pyplot as plt
import numpy as np

# 경로 설정
SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results_batch"
DATA_PATH = RESULTS_DIR / "exp1_batch_results.json"
OUTPUT_PNG = RESULTS_DIR / "exp1_integrated_subplots.png"
OUTPUT_PDF = RESULTS_DIR / "exp1_integrated_subplots.pdf"

def load_data(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"데이터 파일을 찾을 수 없습니다: {path}")
    with open(path, "r") as f:
        return json.load(f)

def get_mean_curve(records: List[Dict]) -> Tuple[np.ndarray, np.ndarray]:
    """여러 실행(runs)의 평균 곡선 계산"""
    if not records:
        return np.array([]), np.array([])
    
    # 모든 기록 중 최대 시간 찾기
    t_max = max(max(r["times"]) for r in records)
    t_grid = np.linspace(0, t_max, 300)
    
    vals = []
    for r in records:
        # 각 기록의 데이터를 t_grid에 맞춰 보간(interpolation)
        v = np.interp(t_grid, r["times"], r["success_rates"])
        vals.append(v)
    
    return t_grid, np.mean(vals, axis=0)

def main():
    # 1. 데이터 로드
    print(f"Loading data from {DATA_PATH}...")
    all_results = load_data(DATA_PATH)
    
    # 2. 그래프 설정
    # 폰트 설정 (시스템에 따라 다를 수 있으나 기본 sans-serif 활용)
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
        "grid.linestyle": "--"
    })

    batch_sizes = [5, 10]
    complexities = [1, 2, 3]
    
    fig, axes = plt.subplots(2, 3, figsize=(15, 10), sharex=True, sharey=True)
    plt.subplots_adjust(hspace=0.25, wspace=0.15)

    lines = [] # 범례용 라인 저장
    labels = []

    for r_idx, b_size in enumerate(batch_sizes):
        for c_idx, comp in enumerate(complexities):
            ax = axes[r_idx, c_idx]
            
            # 해당 조건의 데이터 필터링
            manual_recs = [r for r in all_results if r["mode"] == "manual" and r["batch_size"] == b_size and r["complexity"] == comp]
            agentic_recs = [r for r in all_results if r["mode"] == "agentic" and r["batch_size"] == b_size and r["complexity"] == comp]
            
            # 평균 곡선 계산
            t_m, y_m = get_mean_curve(manual_recs)
            t_a, y_a = get_mean_curve(agentic_recs)
            
            # 그래프 그리기
            l_m, = ax.plot(t_m, y_m, color="#1f77b4", linestyle="--", linewidth=2, label="Manual Script (Optimized)")
            l_a, = ax.plot(t_a, y_a, color="#ff7f0e", linestyle="-", linewidth=2.5, label="Agentic (Operator)")
            
            if r_idx == 0 and c_idx == 0:
                lines = [l_a, l_m]
                labels = ["Agentic (Operator)", "Manual Script (Optimized)"]

            # T-100% 계산 (완료 시간)
            def get_t100(t, y):
                for i, v in enumerate(y):
                    if v >= 99.9: # 부동소수점 오차 고려
                        return t[i]
                return t[-1] if len(t) > 0 else 0

            t100_m = get_t100(t_m, y_m)
            t100_a = get_t100(t_a, y_a)
            
            # 텍스트 상자 추가 (오른쪽 하단으로 위치 수정)
            textstr = "\n".join((
                f"T-100% (Agentic): {t100_a:.2f}s",
                f"T-100% (Manual): {t100_m:.2f}s",
                f"Speedup: {t100_m/t100_a:.2f}x" if t100_a > 0 else "N/A"
            ))
            props = dict(boxstyle='round', facecolor='white', alpha=0.8, edgecolor='lightgray')
            # x=0.55, y=0.35 정도로 우측 하단 영역에 배치 (범례와 겹치지 않게 조절)
            ax.text(0.95, 0.05, textstr, transform=ax.transAxes, fontsize=10,
                    verticalalignment='bottom', horizontalalignment='right', bbox=props)

            # 서브플롯 제목 및 그리드
            ax.set_title(f"Batch {b_size}, Complexity {comp}", fontweight='bold')
            ax.grid(True)
            ax.set_ylim(-5, 105)
            
            # 축 레이블 (가장자리만)
            if r_idx == 1:
                ax.set_xlabel("Time (seconds)")
            if c_idx == 0:
                ax.set_ylabel("Cumulative Success Rate (%)")

    # 전체 범례를 하단 중앙에 배치
    fig.legend(lines, labels, loc='lower center', ncol=2, bbox_to_anchor=(0.5, 0.02), 
               frameon=True, fontsize=14, borderpad=1)
    
    # 여백 조정 (범례 공간 확보)
    plt.tight_layout(rect=[0, 0.08, 1, 0.98])

    # 3. 저장
    print(f"Saving integrated plot to {OUTPUT_PNG} and {OUTPUT_PDF}...")
    plt.savefig(OUTPUT_PNG, dpi=300, bbox_inches='tight')
    plt.savefig(OUTPUT_PDF, bbox_inches='tight')
    
    print("✓ Successfully generated integrated subplots.")

if __name__ == "__main__":
    main()
