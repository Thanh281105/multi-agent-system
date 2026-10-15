import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path("c:/Users/Thanh/Desktop/DACNTT/multi-agent-system")

def generate_report():
    results_path = ROOT / "output/evaluation-v4/full_24_benchmark_results.json"
    if not results_path.exists():
        print("Results file not found yet.")
        return

    with open(results_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    n = len(cases)
    sa_scores = [c["single_agent"]["mean_score"] for c in cases]
    ma_scores = [c["multi_agent"]["mean_score"] for c in cases]
    sa_lats = [c["single_agent"]["mean_latency_s"] for c in cases]
    ma_lats = [c["multi_agent"]["mean_latency_s"] for c in cases]
    sa_toks = [c["single_agent"]["mean_total_tokens"] for c in cases]
    ma_toks = [c["multi_agent"]["mean_total_tokens"] for c in cases]

    sa_verbatim_count = sum(1 for c in cases if c["single_agent"].get("verbatim_identical", False))
    ma_verbatim_count = sum(1 for c in cases if c["multi_agent"].get("verbatim_identical", False))

    mean_sa_score = sum(sa_scores) / n
    mean_ma_score = sum(ma_scores) / n
    mean_sa_lat = sum(sa_lats) / n
    mean_ma_lat = sum(ma_lats) / n
    mean_sa_tok = sum(sa_toks) / n
    mean_ma_tok = sum(ma_toks) / n
    measured_tok_ratio = mean_ma_tok / mean_sa_tok if mean_sa_tok > 0 else 1.0

    print("=" * 80)
    print("BÁO CÁO TỔNG HỢP BENCHMARK V4 ĐÃ HOÀN TẤT")
    print("=" * 80)
    print(f"Tổng số cases: {n} (144 runs độc lập qua 3 repeats)")
    print(f"Single-Agent Fact Coverage: {mean_sa_score*100:.2f}% | Latency: {mean_sa_lat:.3f} s | Tokens: {mean_sa_tok:.0f}")
    print(f"Multi-Agent Fact Coverage:  {mean_ma_score*100:.2f}% | Latency: {mean_ma_lat:.3f} s | Tokens: {mean_ma_tok:.0f}")
    print(f"Chênh lệch độ trễ: SA nhanh hơn {mean_ma_lat/mean_sa_lat:.2f} lần")
    print(f"Đo lường Token thực tế: MA tiêu thụ gấp {measured_tok_ratio:.2f} lần SA")
    print(f"Tính nhất quán nguyên văn (Verbatim Identity cả 3 lần): SA = {sa_verbatim_count}/{n} ({sa_verbatim_count/n*100:.1f}%), MA = {ma_verbatim_count}/{n} ({ma_verbatim_count/n*100:.1f}%)")
    print("=" * 80)

if __name__ == "__main__":
    generate_report()
