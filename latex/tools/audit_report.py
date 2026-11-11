from pathlib import Path
import json, hashlib, statistics, math, collections, random, csv, ast, subprocess, zipfile, argparse, sys
sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='Read-only artifact audit; no model calls.')
parser.add_argument('--repo',type=Path,default=ROOT/'outputs/multi-agent-system')
parser.add_argument('--out',type=Path,default=ROOT/'outputs/bao-cao-thanh-v4')
args=parser.parse_args()
REPO = args.repo.resolve()
OUT = args.out.resolve()
OUT.mkdir(exist_ok=True)
def load(path):
    return json.loads((REPO / path).read_text(encoding='utf-8'))
def lines(path):
    return [json.loads(s) for s in (REPO/path).read_text(encoding='utf-8').splitlines() if s.strip()]
def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()
cases = load('evaluation/v4_fair_benchmark_cases.json')['cases']
results = load('output/evaluation-v4/full_24_benchmark_results.json')
audit = {'revision': subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'],text=True).strip(), 'case_count':len(cases)}
audit['tiers'] = dict(collections.Counter(c['tier'] for c in cases))
audit['fact_count_once'] = sum(len(c['required_facts']) for c in cases)
audit['case_id_match'] = {c['case_id'] for c in cases} == {c['case_id'] for c in results}
byid = {c['case_id']:c for c in cases}
issues=[]
for item in results:
    for key in ('single_agent','multi_agent'):
        if len(item[key]['repeats'])!=3 or {r['repeat'] for r in item[key]['repeats']}!={0,1,2}:
            issues.append([item['case_id'],key,'invalid repeat identities'])
        for r in item[key]['repeats']:
            expected = {f['fact_id'] for f in byid[item['case_id']]['required_facts']}
            actual = [v['fact_id'] for v in r['verdicts']]
            if len(actual)!=len(set(actual)) or set(actual)!=expected:
                issues.append([item['case_id'],key,r['repeat'],'verdict ids mismatch'])
            score = sum(bool(v.get('passed',False)) for v in r['verdicts']) / len(expected)
            if abs(score-r['score'])>1e-10:
                issues.append([item['case_id'],key,r['repeat'],'score mismatch'])
            t = r['tokens']
            if t['prompt_tokens']+t['completion_tokens']!=t['total_tokens']:
                issues.append([item['case_id'],key,r['repeat'],'usage mismatch'])
audit['structural_issues'] = issues
for key in ('single_agent','multi_agent'):
    rr=[r for c in results for r in c[key]['repeats']]
    verdicts=[v for r in rr for v in r['verdicts']]
    audit[key]={
        'runs':len(rr),'macro_score':statistics.mean(statistics.mean(r['score'] for r in c[key]['repeats']) for c in results),
        'verdict_pass':sum(bool(v['passed']) for v in verdicts),'verdict_total':len(verdicts),
        'micro_score':sum(bool(v['passed']) for v in verdicts)/len(verdicts),
        'latency_mean_s':statistics.mean(r['latency_s'] for r in rr),
        'latency_median_s':statistics.median(r['latency_s'] for r in rr),
        'tokens_mean':statistics.mean(r['tokens']['total_tokens'] for r in rr),
        'tokens_total':sum(r['tokens']['total_tokens'] for r in rr),
        'verbatim_cases':sum(all(r['answer']==c[key]['repeats'][0]['answer'] for r in c[key]['repeats']) for c in results),
        'failed_facts':[[c['case_id'],r['repeat'],v] for c in results for r in c[key]['repeats'] for v in r['verdicts'] if not v['passed']],
    }
sa,ma=audit['single_agent'],audit['multi_agent']
audit['latency_ratio']=ma['latency_mean_s']/sa['latency_mean_s']
audit['tokens_ratio']=ma['tokens_mean']/sa['tokens_mean']
deltas=[statistics.mean(r['score'] for r in c['multi_agent']['repeats'])-statistics.mean(r['score'] for r in c['single_agent']['repeats']) for c in results]
avg=statistics.mean(deltas); se=statistics.stdev(deltas)/math.sqrt(len(deltas))
audit['paired']={'n_cases':len(deltas),'mean_delta':avg,'standard_error':se,'t_stat':avg/se,'t_ci95':[avg-2.068657610*se,avg+2.068657610*se], 'wins_ties_losses':dict(collections.Counter(c['win_status'] for c in results))}
rng=random.Random(20261022)
boot=sorted(statistics.mean(rng.choices(deltas,k=len(deltas))) for _ in range(20000))
audit['paired']['bootstrap_percentile_ci95']=[boot[499],boot[19499]]
audit['tier_summary']=[]
for tier in audit['tiers']:
    cc=[c for c in results if c['tier']==tier]
    row={'tier':tier,'cases':len(cc),'facts':sum(c['required_facts_count'] for c in cc)}
    for key in ('single_agent','multi_agent'):
        rr=[r for c in cc for r in c[key]['repeats']]
        row[key]={'score':statistics.mean(r['score'] for r in rr),'latency':statistics.mean(r['latency_s'] for r in rr),'tokens':statistics.mean(r['tokens']['total_tokens'] for r in rr)}
    audit['tier_summary'].append(row)
audit['snapshots']={}
for profile in ('test','eval'):
    base=f'data/snapshots/tiki-books-v4-{profile}'
    m=load(base+'/manifest.json'); q=load(base+'/quality-report.json')
    pp=lines(base+'/products.jsonl'); rr=lines(base+'/reviews.jsonl')
    hashes={f:sha(REPO/base/f) for f in ('products.jsonl','reviews.jsonl','quality-report.json')}
    audit['snapshots'][profile]={'products':len(pp),'reviews':len(rr),'unique_products':len({p['external_id'] for p in pp}),'unique_reviews':len({r['external_id'] for r in rr}),'orphan_reviews':sum(r['product_external_id'] not in {p['external_id'] for p in pp} for r in rr),'manifest':m,'quality_status':q.get('status'),'quality_top_keys':list(q),'hashes':hashes,'manifest_counts_match':len(pp)==m['product_count'] and len(rr)==m['review_count'],'product_hash_matches_manifest':hashes['products.jsonl']==m['products_sha256'],'review_hash_matches_manifest':hashes['reviews.jsonl']==m['reviews_sha256']}
source=load('data/knowledge/books-v1/sources.json')
audit['knowledge_sources_count']=len(source['sources']) if isinstance(source,dict) else len(source)
audit['knowledge_mapping_statuses']=dict(collections.Counter(m['mapping_status'] for m in load('data/knowledge/books-v1/mappings.json')['mappings']))
ma_runs=[r for c in results for r in c['multi_agent']['repeats']]
audit['ma_call_breakdown_token_mismatches']=sum(sum(v['total_tokens'] for v in r['trace']['call_breakdown'].values())!=r['tokens']['total_tokens'] for r in ma_runs)
audit['routing_modes']=dict(collections.Counter(c['routing_mode'] for c in results))
summary=load('evaluation/v3/results/p8-trace-20261013-r7/summary.json')
audit['r7']={k:summary.get(k) for k in ('classification','run_id','sut_git_revision','p8_started_cells','p8_completed_cells')}
audit['r7']['variants']={k:{kk:v.get(kk) for kk in ('observations','cases','work_groups','user_turns','generation_calls','input_tokens','output_tokens','known_cost_usd','observation_latency_seconds','user_turn_outcomes')} for k,v in summary['variants'].items()}
audit['publication_package_exists']=(REPO/'output/evaluation-v3/heldout-mini-luna-judge-v3-rating-fix-20261014/p8-publication').exists()
archive_root=REPO/'evaluation/v3/results/p8-trace-20261013-r7'
archive_manifest=load('evaluation/v3/results/p8-trace-20261013-r7/manifest.json')
archive_path=archive_root/archive_manifest['archive_path']
with zipfile.ZipFile(archive_path) as z:
    audit['r7_archive_checks']={'file_count':len(z.namelist()),'zip_crc_ok':z.testzip() is None,'archive_sha256_match':sha(archive_path)==archive_manifest['archive_sha256'],'members':[{'path':f['path'],'bytes_match':len(z.read(f['path']))==f['bytes'],'sha256_match':hashlib.sha256(z.read(f['path'])).hexdigest()==f['sha256']} for f in archive_manifest['files']]}
audit['source_files']={}
sources=[
'README.md','docs/architecture.md','docs/data.md','docs/api.md','docs/operations.md',
'app/v2/runtime.py','app/v2/contracts.py','app/v2/planning.py','app/v2/execution.py','app/v2/supervisor.py','app/v2/answers.py','app/v2/actions.py','app/v2/authorization.py','app/v2/registry.py','app/v2/tools.py',
'app/knowledge/retrieval.py','app/knowledge/citations.py','app/orchestrator/aggregator.py','app/agents/product/agent.py','app/orchestrator/executor.py',
'scripts/run_v4_full_benchmark.py','evaluation/v4_fair_benchmark_cases.json','output/evaluation-v4/full_24_benchmark_results.json','docs/v4_exploratory_benchmark_results.md','docs/p8_official_publication_benchmark_report.md','docs/reproducibility_package_and_tradeoff_report.md',
'evaluation/v3/results/p8-trace-20261013-r7/summary.json','evaluation/v3/results/p8-trace-20261013-r7/README.md',
'compose.yaml','.github/workflows/ci.yml','pyproject.toml','frontend/package.json','app/db/migrate.py',
'data/knowledge/books-v1/coverage.md','data/knowledge/books-v1/runtime-manifest.json','data/knowledge/books-v1/mappings.json','app/knowledge/grounding.py','app/agents/product/skills.py','app/orchestrator/scoring.py']
for name in sources:
    p=REPO/name
    if p.exists():
        audit['source_files'][name]={'sha256':sha(p),'lines':len(p.read_text(encoding='utf-8').splitlines())}
(OUT/'kiem-chung-so-lieu.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
with (OUT/'ket-qua-v4-theo-ca.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f);w.writerow(['case_id','tier','required_facts','SA_macro','MA_macro','SA_latency_s','MA_latency_s','SA_tokens','MA_tokens','winner'])
    for c in results:
        a,b=c['single_agent'],c['multi_agent'];w.writerow([c['case_id'],c['tier'],c['required_facts_count'],a['mean_score'],b['mean_score'],a['mean_latency_s'],b['mean_latency_s'],a['mean_total_tokens'],b['mean_total_tokens'],c['win_status']])
print(json.dumps({k:v for k,v in audit.items() if k not in ('snapshots','source_files')},ensure_ascii=False,indent=2))
for k,v in audit['snapshots'].items():
    print('SNAPSHOT',k,json.dumps({kk:vv for kk,vv in v.items() if kk not in ('manifest','hashes')},ensure_ascii=False))
print('MANIFEST',json.dumps(audit['snapshots']['eval']['manifest'],ensure_ascii=False))
