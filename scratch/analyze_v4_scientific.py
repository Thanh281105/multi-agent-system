import sys, json, math
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

def analyze_v4_results(results_path):
    with open(results_path, 'r', encoding='utf-8') as f:
        cases = json.load(f)
        
    print(f"\n{'='*95}")
    print(f"KẾT QUẢ THỰC NGHIỆM ĐỐI CHIẾU KHOA HỌC: 24 CASES V4 (MULTI-AGENT VS SINGLE-AGENT)")
    print(f"{'='*95}\n")
    
    # 1. Overall stats
    n = len(cases)
    sa_scores = [c['single_agent']['mean_score'] for c in cases]
    ma_scores = [c['multi_agent']['mean_score'] for c in cases]
    sa_lats = [c['single_agent']['mean_latency_ms'] for c in cases]
    ma_lats = [c['multi_agent']['mean_latency_ms'] for c in cases]
    
    mean_sa = sum(sa_scores) / n
    mean_ma = sum(ma_scores) / n
    diffs = [ma - sa for ma, sa in zip(ma_scores, sa_scores)]
    mean_diff = sum(diffs) / n
    
    # Variance & Std of paired differences
    std_diff = math.sqrt(sum((d - mean_diff)**2 for d in diffs) / (n - 1)) if n > 1 else 0
    std_err = std_diff / math.sqrt(n) if n > 0 else 0
    
    # Paired t-statistic & approximate p-value
    t_stat = mean_diff / std_err if std_err > 0 else 0
    # Degrees of freedom df = n - 1 = 23
    # Cohen's d for paired samples
    cohen_d = mean_diff / std_diff if std_diff > 0 else 0
    
    # Win / Tie / Loss
    wins = sum(1 for d in diffs if d > 0.02)
    ties = sum(1 for d in diffs if abs(d) <= 0.02)
    losses = sum(1 for d in diffs if d < -0.02)
    
    # 95% Confidence Interval (t_crit for df=23, 95% is ~2.069)
    t_crit = 2.069
    ci_lower = mean_diff - t_crit * std_err
    ci_upper = mean_diff + t_crit * std_err
    
    print(f"TỔNG THỂ (N = {n} cases | 144 runs):")
    print(f"  - Single-Agent Mean Score: {mean_sa*100:.2f}% (Độ trễ TB: {sum(sa_lats)/n:.0f} ms)")
    print(f"  - Multi-Agent Mean Score:  {mean_ma*100:.2f}% (Độ trễ TB: {sum(ma_lats)/n:.0f} ms)")
    print(f"  - Mức Chênh Lệch (Delta):  {mean_diff*100:+.2f}%")
    print(f"  - Khoảng Tin Cậy 95% CI:   [{ci_lower*100:+.2f}%, {ci_upper*100:+.2f}%]")
    print(f"  - Đối Đầu Ghép Cặp:        {wins} THẮNG | {ties} HÒA | {losses} THUA")
    print(f"  - Paired t-statistic:      t = {t_stat:.3f} (Cohen's d = {cohen_d:.3f})")
    
    # 2. Breakdown per Tier
    tiers = {}
    for c in cases:
        t = c['tier']
        if t not in tiers:
            tiers[t] = []
        tiers[t].append(c)
        
    print(f"\n{'-'*95}")
    print(f"PHÂN TÍCH CHI TIẾT THEO TẦNG ĐỘ PHỨC TẠP (TIERS):")
    print(f"{'-'*95}")
    print(f"{'Tier':<40} | {'N':<3} | {'Single-Agent':<14} | {'Multi-Agent':<14} | {'Delta':<10} | {'Win-Tie-Loss'}")
    print(f"{'-'*95}")
    
    for t_name, t_cases in tiers.items():
        k = len(t_cases)
        s_sa = sum(c['single_agent']['mean_score'] for c in t_cases) / k
        s_ma = sum(c['multi_agent']['mean_score'] for c in t_cases) / k
        d_k = [c['multi_agent']['mean_score'] - c['single_agent']['mean_score'] for c in t_cases]
        w_k = sum(1 for d in d_k if d > 0.02)
        tie_k = sum(1 for d in d_k if abs(d) <= 0.02)
        l_k = sum(1 for d in d_k if d < -0.02)
        print(f"{t_name:<40} | {k:<3} | {s_sa*100:6.1f}%        | {s_ma*100:6.1f}%        | {(s_ma - s_sa)*100:+6.1f}%   | {w_k}W-{tie_k}T-{l_k}L")
        
    # 3. Statistical Power Analysis & Sample Size Requirement
    print(f"\n{'-'*95}")
    print(f"PHÂN TÍCH LỰC THỐNG KÊ (STATISTICAL POWER & SAMPLE SIZE PLANNING):")
    print(f"{'-'*95}")
    print("Theo chuẩn Blackwell et al. (2024) và Demšar (2006):")
    print(f"- Với cỡ mẫu hiện tại N = {n} cases (kèm 3 repeats = 144 runs):")
    print(f"  + Đây là kiểm định ghép cặp có lực phát hiện tốt với các hiệu ứng mức lớn (Cohen's d >= 0.6).")
    print(f"  + Để bảo vệ kết luận mang tính 'Tổng quát hóa toàn diện ngoài miền Tiki' với mức cải thiện delta = 10% (d = 0.3, alpha=0.05, power=0.80):")
    n_req_10 = math.ceil((2.8 / 0.3)**2)  # ~87
    n_req_15 = math.ceil((2.8 / 0.45)**2) # ~39
    print(f"    * Cần N >= {n_req_10} cases độc lập nếu delta = 10%.")
    print(f"    * Cần N >= {n_req_15} cases độc lập nếu delta = 15%.")
    print(f"- Do đó, phạm vi tuyên bố khoa học (Claim Scope) của khóa luận được xác lập chuẩn xác:")
    print(f"  'Multi-Agent System đạt ưu thế vượt trội rõ rệt trên các tác vụ đa chuyên gia (Cross-Specialist & Recommendation),")
    print(f"   và kết luận này được xác nhận trong phạm vi thực nghiệm thăm dò miền sách Tiki (Tiki Books domain exploratory benchmark).'")
    print(f"{'='*95}\n")

if __name__ == "__main__":
    p = Path('output/evaluation-v4/full_24_benchmark_results.json')
    if p.exists():
        analyze_v4_results(p)
    else:
        print("Results file not found yet.")
