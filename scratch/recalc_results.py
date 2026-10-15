import json
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path("c:/Users/Thanh/Desktop/DACNTT/multi-agent-system")
sys.path.insert(0, str(ROOT))

from scripts.run_v4_full_benchmark import evaluate_fact_coverage

# Load cases
with open(ROOT / 'evaluation/v4_fair_benchmark_cases.json', 'r', encoding='utf-8') as f:
    cases_data = json.load(f)
cases_map = {c['case_id']: c for c in cases_data['cases']}

# Load results
results_file = ROOT / 'output/evaluation-v4/full_24_benchmark_results.json'
with open(results_file, 'r', encoding='utf-8') as f:
    results = json.load(f)

print("Re-evaluating cases 04 and 07...")
for item in results:
    cid = item['case_id']
    if cid in ['fair_tier2_cross_04', 'fair_tier2_cross_07']:
        case = cases_map[cid]
        req_facts = case['required_facts']
        
        # Re-evaluate SA
        for rep in item['single_agent']['repeats']:
            sa_eval = evaluate_fact_coverage(rep['answer'], req_facts)
            rep['verdicts'] = sa_eval
            sa_pass = sum(1 for v in sa_eval if v.get('passed', False))
            rep['score'] = sa_pass / len(req_facts)
            
        sa_mean = sum(r['score'] for r in item['single_agent']['repeats']) / len(item['single_agent']['repeats'])
        sa_std = math.sqrt(sum((r['score'] - sa_mean)**2 for r in item['single_agent']['repeats']) / len(item['single_agent']['repeats']))
        item['single_agent']['mean_score'] = sa_mean
        item['single_agent']['std_score'] = sa_std
        
        # Re-evaluate MA
        for rep in item['multi_agent']['repeats']:
            ma_eval = evaluate_fact_coverage(rep['answer'], req_facts)
            rep['verdicts'] = ma_eval
            ma_pass = sum(1 for v in ma_eval if v.get('passed', False))
            rep['score'] = ma_pass / len(req_facts)
            
        ma_mean = sum(r['score'] for r in item['multi_agent']['repeats']) / len(item['multi_agent']['repeats'])
        ma_std = math.sqrt(sum((r['score'] - ma_mean)**2 for r in item['multi_agent']['repeats']) / len(item['multi_agent']['repeats']))
        item['multi_agent']['mean_score'] = ma_mean
        item['multi_agent']['std_score'] = ma_std
        
        item['delta_score'] = ma_mean - sa_mean
        item['win_status'] = "MA_WIN" if (ma_mean > sa_mean + 0.05) else ("SA_WIN" if sa_mean > ma_mean + 0.05 else "TIE")
        print(f"[{cid}] Updated SA: {sa_mean*100:.1f}%, MA: {ma_mean*100:.1f}% -> {item['win_status']}")

with open(results_file, 'w', encoding='utf-8') as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\nSaved updated results to:", results_file)
