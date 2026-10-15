import sys, json
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path

# Load products
with open('data/snapshots/tiki-books-v4-eval/products.jsonl', 'r', encoding='utf-8') as f:
    products = [json.loads(line) for line in f]
prod_map = {p['name']: p for p in products}
prod_id_map = {p['external_id']: p for p in products}

# Load reviews
with open('data/snapshots/tiki-books-v4-eval/reviews.jsonl', 'r', encoding='utf-8') as f:
    reviews = [json.loads(line) for line in f]

# Load sources
with open('data/knowledge/books-v1/sources.json', 'r', encoding='utf-8') as f:
    sources = json.load(f)
if isinstance(sources, dict) and 'sources' in sources:
    sources_list = sources['sources']
else:
    sources_list = sources

print(f"Products: {len(products)}, Reviews: {len(reviews)}, Sources: {len(sources_list)}")

# Search for specific books
keywords = [
    "Sapiens", "Morisaki", "Bạch Dạ Hành", "Thiên Quan Tứ Phúc", 
    "Gieo Hạt Cùng Vĩ Nhân", "Ông Nội Vượt Ngục", "Nghệ Thuật PR", 
    "Bill Marriott", "Start-up Nation", "Những người phụ nữ bé nhỏ",
    "7 Thói Quen"
]

print("\n=== MATCHED PRODUCTS IN SNAPSHOT ===")
found_prods = {}
for kw in keywords:
    matched = [p for p in products if kw.lower() in p['name'].lower()]
    print(f"\nKeyword '{kw}' matched {len(matched)} product(s):")
    for m in matched[:2]:
        print(f"  - ext_id: {m['external_id']} | Name: '{m['name']}' | Price: {m['price_vnd']} | Rating: {m['rating']} | Reviews: {m['source_review_count']}")
        print(f"    Authors: {m.get('authors')} | Publisher: {m.get('publisher')} | Page count: {m.get('page_count')}")
        found_prods[m['name']] = m

# Check reviews for found products
print("\n=== SAMPLE REVIEWS WITH EXACT QUOTES ===")
for name, p in found_prods.items():
    pid = p['external_id']
    revs = [r for r in reviews if r.get('product_external_id') == pid]
    if revs:
        print(f"\nReviews for '{name}' (total: {len(revs)}):")
        for r in revs[:3]:
            content = r.get('content') or r.get('comment') or r.get('title')
            print(f"  - rev_id: {r.get('external_id')} | Stars: {r.get('rating')} | Content: '{str(content).strip()[:120]}'")

# Check sources
print("\n=== MATCHED SOURCES IN KNOWLEDGE ===")
for s in sources_list:
    sid = s.get('id') or s.get('source_id')
    title = s.get('title') or s.get('name')
    print(f"Source: {sid} | Title: {title}")
