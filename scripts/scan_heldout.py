import json

heldout_path = 'output/evaluation-v3/heldout-live-v3/heldout-checkpoint.v3.jsonl'
records = []
with open(heldout_path, 'r', encoding='utf-8') as f:
    for line in f:
        records.append(json.loads(line))

completed_records = [r for r in records if r.get('event') == 'completed']
print(f"Total completed records: {len(completed_records)}")

rank_citations_found = 0
obs_at_mismatches = 0
missing_cat_product = 0

for r in completed_records:
    receipt = r['payload']['receipt']
    for tr in receipt.get('turn_results', []):
        res = tr.get('result_payload', {}).get('result', {})
        ev_list = res.get('evidence', [])
        ev_map = {e['evidence_id']: e for e in ev_list}
        cat_map = {e['source_id']: e for e in ev_list if e.get('source_id', '').startswith('catalog_product_')}
        for e in ev_list:
            if e.get('observed_at') == '2026-08-24T10:47:30.042026Z':
                obs_at_mismatches += 1
        for cit in res.get('citations', []):
            ev = ev_map.get(cit.get('evidence_id'))
            if ev and ev.get('source_id', '').startswith('rank_'):
                rank_citations_found += 1
                pid = ev['source_id'].split('_')[1]
                if f'catalog_product_{pid}' not in cat_map:
                    missing_cat_product += 1

print(f"Rank citations found: {rank_citations_found}, missing matching catalog product: {missing_cat_product}")
print(f"Observed at mismatches in evidence: {obs_at_mismatches}")
