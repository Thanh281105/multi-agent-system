import sys, json
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

# Load snapshot
with open('data/snapshots/tiki-books-v4-eval/products.jsonl', 'r', encoding='utf-8') as f:
    products = [json.loads(line) for line in f]
prod_id_map = {p['external_id']: p for p in products}

with open('data/snapshots/tiki-books-v4-eval/reviews.jsonl', 'r', encoding='utf-8') as f:
    reviews = [json.loads(line) for line in f]
rev_id_map = {r['external_id']: r for r in reviews}

with open('data/knowledge/books-v1/sources.json', 'r', encoding='utf-8') as f:
    sources_data = json.load(f)
sources_list = sources_data['sources'] if isinstance(sources_data, dict) and 'sources' in sources_data else sources_data
source_id_map = {s.get('id') or s.get('source_id') or s.get('title'): s for s in sources_list}

with open('evaluation/v4_fair_benchmark_cases.json', 'r', encoding='utf-8') as f:
    v4 = json.load(f)

print(f"Validating {len(v4['cases'])} cases against snapshot ground truth...")

issues = []
for case in v4['cases']:
    cid = case['case_id']
    for fact in case['required_facts']:
        prov = fact.get('source_provenance', {})
        stype = prov.get('source_type')
        rec_id = prov.get('record_id')
        
        if stype == 'catalog_product':
            if rec_id not in prod_id_map:
                issues.append(f"[{cid}] Product {rec_id} not found in products.jsonl")
            else:
                p = prod_id_map[rec_id]
                field = prov.get('field')
                if field and field in p:
                    expected = prov.get('exact_value')
                    actual = p[field]
                    if expected != actual:
                        issues.append(f"[{cid}] Product {rec_id} field '{field}': expected {expected} != actual snapshot {actual}")
                        
        elif stype == 'customer_review':
            if rec_id not in rev_id_map:
                issues.append(f"[{cid}] Review {rec_id} not found in reviews.jsonl")
            else:
                r = rev_id_map[rec_id]
                quote = prov.get('exact_quote')
                full_text = (r.get('content') or '') + (r.get('title') or '')
                if quote and quote[:30] not in full_text:
                    issues.append(f"[{cid}] Review {rec_id} quote mismatch. Quote: '{quote[:30]}' not in review content.")

print(f"\nTotal validation issues found: {len(issues)}")
for iss in issues:
    print("  *", iss)
