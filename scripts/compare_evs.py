import json

pilot_path = 'output/evaluation-v3/pilot-live-v3/pilot-checkpoint.v3.jsonl'
with open(pilot_path, 'r', encoding='utf-8') as f:
    for line in f:
        r = json.loads(line)
        if r.get('turn_id') == 'turn_4c8593eb758a367e2aeb08b51f08b7737dc7113a81c25bdd20782d2350d0c423' and r.get('event') == 'completed':
            receipt = r['payload']['receipt']
            turn_res = receipt['turn_results'][-1]['result_payload']['result']
            ev_map = {e['evidence_id']: e for e in turn_res['evidence']}
            rank_ev = ev_map['evd_7a9d9521b87ef1b1c1ce4f3215699b68245eb1aaff77dbd5c075981529f8']
            for k, v in rank_ev.items():
                print(f"rank_ev {k}: {repr(v)[:100]}")
            
            cat_ev = ev_map['evd_6ff330036989f1bbcfa6957119c28e88547d5e712587598adddc2506ae18']
            for k, v in cat_ev.items():
                print(f"cat_ev  {k}: {repr(v)[:100]}")
