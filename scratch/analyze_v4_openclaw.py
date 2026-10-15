import sys
import json
import math

sys.stdout.reconfigure(encoding='utf-8')

data = json.load(open('output/evaluation-v4/full_24_benchmark_results.json', encoding='utf-8'))
N = len(data)

sa_scores = [c['single_agent']['mean_score'] * 100 for c in data]
ma_scores = [c['multi_agent']['mean_score'] * 100 for c in data]

sa_lats = [c['single_agent']['mean_latency_s'] for c in data]
ma_lats = [c['multi_agent']['mean_latency_s'] for c in data]

sa_toks = [c['single_agent']['mean_total_tokens'] for c in data]
ma_toks = [c['multi_agent']['mean_total_tokens'] for c in data]

mean_sa_score = sum(sa_scores) / N
mean_ma_score = sum(ma_scores) / N
diffs = [m - s for m, s in zip(ma_scores, sa_scores)]
mean_diff = sum(diffs) / N
s_diff = math.sqrt(sum((d - mean_diff)**2 for d in diffs) / (N - 1)) if N > 1 else 0
se_diff = s_diff / math.sqrt(N)
t_stat = mean_diff / se_diff if se_diff > 0 else 0
ci_lower = mean_diff - 2.069 * se_diff
ci_upper = mean_diff + 2.069 * se_diff

mean_sa_lat = sum(sa_lats) / N
mean_ma_lat = sum(ma_lats) / N

mean_sa_tok = sum(sa_toks) / N
mean_ma_tok = sum(ma_toks) / N

wins = [c['win_status'] for c in data]

print(f"=== KẾT QUẢ BENCHMARK V4 (OPENCLAW ARCHITECTURE) ===")
print(f"Tổng số ca: {N} ca (144 runs độc lập)")
print(f"Macro Fact Coverage: SA = {mean_sa_score:.2f}% | MA = {mean_ma_score:.2f}% (Chênh lệch: {mean_diff:+.2f}%)")
print(f"Paired t-test: t = {t_stat:.3f}, 95% CI: [{ci_lower:.2f}%, {ci_upper:.2f}%]")
print(f"Độ trễ trung bình: SA = {mean_sa_lat:.3f}s | MA = {mean_ma_lat:.3f}s (Ratio: {mean_ma_lat/mean_sa_lat:.2f}x)")
print(f"Token trung bình: SA = {mean_sa_tok:.0f} | MA = {mean_ma_tok:.0f} (Ratio: {mean_ma_tok/mean_sa_tok:.2f}x)")
print(f"Kết quả từng ca: MA Thắng = {wins.count('MA_WIN')}, Hòa = {wins.count('TIE')}, SA Thắng = {wins.count('SA_WIN')}")

# Chi tiết theo Tier
tiers = sorted(list(set(c['tier'] for c in data)))
print("\n--- Chi tiết theo Tier ---")
for t in tiers:
    sub = [c for c in data if c['tier'] == t]
    sa_t = sum(c['single_agent']['mean_score'] for c in sub) / len(sub) * 100
    ma_t = sum(c['multi_agent']['mean_score'] for c in sub) / len(sub) * 100
    lat_s = sum(c['single_agent']['mean_latency_s'] for c in sub) / len(sub)
    lat_m = sum(c['multi_agent']['mean_latency_s'] for c in sub) / len(sub)
    tok_s = sum(c['single_agent']['mean_total_tokens'] for c in sub) / len(sub)
    tok_m = sum(c['multi_agent']['mean_total_tokens'] for c in sub) / len(sub)
    print(f"[{t}] (N={len(sub)}): SA {sa_t:.1f}% ({lat_s:.2f}s, {tok_s:.0f}t) | MA {ma_t:.1f}% ({lat_m:.2f}s, {tok_m:.0f}t)")
