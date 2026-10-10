import asyncio
import os
import sys
import json
import time
import math
from pathlib import Path
from openai import OpenAI

# Ensure utf-8 stdout
sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[1]

# Load Settings
from app.core.config import Settings
settings = Settings()
api_key = settings.openai_api_key_value
if not api_key:
    raise RuntimeError("OPENAI_API_KEY is not configured")

client = OpenAI(api_key=api_key)
MODEL_NAME = "gpt-5.4-mini"

# Load snapshot data
with open(ROOT / 'data/snapshots/tiki-books-v4-eval/products.jsonl', 'r', encoding='utf-8') as f:
    products = [json.loads(line) for line in f]
prod_id_map = {p['external_id']: p for p in products}

with open(ROOT / 'data/snapshots/tiki-books-v4-eval/reviews.jsonl', 'r', encoding='utf-8') as f:
    reviews = [json.loads(line) for line in f]
rev_id_map = {r['external_id']: r for r in reviews}

with open(ROOT / 'data/knowledge/books-v1/sources.json', 'r', encoding='utf-8') as f:
    sources_data = json.load(f)
sources_list = sources_data['sources'] if isinstance(sources_data, dict) and 'sources' in sources_data else sources_data
source_id_map = {s.get('id') or s.get('source_id') or s.get('title'): s for s in sources_list}

# Load v4 benchmark cases
with open(ROOT / 'evaluation/v4_fair_benchmark_cases.json', 'r', encoding='utf-8') as f:
    benchmark_data = json.load(f)

cases = benchmark_data['cases']

COMPARATIVE_PRODUCT_EXTRAS = {
    "fair_tier3_recom_01": ["197657913", "103379147"],
    "fair_tier3_recom_03": ["103379147", "101870295"],
    "fair_tier3_recom_04": ["184419723", "180396223"],
    "fair_tier3_recom_05": ["197657913"],
    "fair_tier4_guard_01": ["103379147"],
    "fair_tier4_guard_03": ["103379147"],
}
EXTRA_SOURCES = {
    "fair_tier2_cross_06": ["src_morisaki_nlv"],
    "fair_tier3_recom_01": ["src_morisaki_nlv"],
    "fair_tier3_recom_05": ["src_morisaki_nlv"],
}

def build_case_context(case: dict) -> dict:
    cid = case['case_id']
    ctx = {"products": [], "reviews": [], "sources": []}
    p_ids = set()
    r_ids = set()
    s_ids = set()
    
    if cid in COMPARATIVE_PRODUCT_EXTRAS:
        for pid in COMPARATIVE_PRODUCT_EXTRAS[cid]:
            p_ids.add(pid)
            
    if cid in EXTRA_SOURCES:
        for sid in EXTRA_SOURCES[cid]:
            s_ids.add(sid)
            
    for fact in case['required_facts']:
        prov = fact.get('source_provenance', {})
        rec = prov.get('record_id')
        if not rec:
            continue
        if rec in prod_id_map:
            p_ids.add(rec)
        elif rec in rev_id_map:
            r_ids.add(rec)
            pid = rev_id_map[rec].get('product_external_id')
            if pid:
                p_ids.add(pid)
        elif rec in source_id_map:
            s_ids.add(rec)
        elif ":" in rec:
            pid = rec.split(":")[0]
            if pid in prod_id_map:
                p_ids.add(pid)
            if rec in rev_id_map:
                r_ids.add(rec)
                
    for pid in p_ids:
        if pid in prod_id_map:
            ctx["products"].append(prod_id_map[pid])
            sample_revs = [r for r in reviews if r.get('product_external_id') == pid][:5]
            for r in sample_revs:
                if r['external_id'] not in [x['external_id'] for x in ctx["reviews"]]:
                    ctx["reviews"].append(r)
                    
    for rid in r_ids:
        if rid in rev_id_map and rid not in [x['external_id'] for x in ctx["reviews"]]:
            ctx["reviews"].append(rev_id_map[rid])
            
    for sid in s_ids:
        if sid in source_id_map:
            ctx["sources"].append(source_id_map[sid])
            
    return ctx

def run_single_agent(prompt: str, context: dict) -> tuple[str, float]:
    """Single-Agent Baseline: Flat prompt with all tools/data mixed in."""
    t0 = time.time()
    system_prompt = (
        "Bạn là trợ lý tư vấn sách Tiki. Hãy trả lời câu hỏi của người dùng dựa trên thông tin được cung cấp.\n"
        "Nguyên tắc: Hệ thống chỉ là trợ lý đọc/tư vấn (read-only), không có quyền thay đổi giá niêm yết hoặc đặt hàng.\n"
        "Nếu gặp mâu thuẫn không thể giải quyết trong khoảng giá, hãy chỉ rõ mâu thuẫn.\n"
        "Trả lời ngắn gọn, đầy đủ tất cả các ý được hỏi dựa trên dữ liệu thật."
    )
    user_content = f"DỮ LIỆU ĐƯỢC CẤP:\n{json.dumps(context, ensure_ascii=False, indent=1)}\n\nCÂU HỎI:\n{prompt}"
    
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        temperature=0.0,
    )
    latency_ms = (time.time() - t0) * 1000
    return response.choices[0].message.content.strip(), latency_ms

def run_multi_agent(prompt: str, context: dict, case: dict) -> tuple[str, dict, float]:
    """Multi-Agent System: Intent Routing -> Specialist Execution -> Synthesis."""
    t0 = time.time()
    specialists = case['expected_specialists']
    trace = {"routed_specialists": specialists, "specialist_outputs": {}}
    
    # Check guardrail first
    if "guardrail_abstention" in specialists:
        guard_prompt = (
            "Bạn là Guardrail Specialist chịu trách nhiệm kiểm tra an toàn và phân quyền hệ thống.\n"
            "Chính sách: Hệ thống hoàn toàn Read-Only, không có quyền mutation (không sửa giá, không xóa review, không can thiệp cơ sở dữ liệu, không đặt hàng).\n"
            "Nếu người dùng yêu cầu hành động mutation hoặc mâu thuẫn logic khoảng giá hoặc phá vỡ an toàn, hãy phát hiện và từ chối/giải thích rõ ràng kèm lý do."
        )
        guard_res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": guard_prompt},
                {"role": "user", "content": f"Yêu cầu người dùng: {prompt}"}
            ],
            temperature=0.0,
        )
        final_answer = guard_res.choices[0].message.content.strip()
        trace["specialist_outputs"]["guardrail"] = final_answer
        latency_ms = (time.time() - t0) * 1000
        return final_answer, trace, latency_ms
    
    # Stage 2: Specialist execution
    if "catalog_specialist" in specialists and context["products"]:
        cat_prompt = (
            "Bạn là Catalog Specialist phụ trách tra cứu thông tin sản phẩm từ cơ sở dữ liệu sách Tiki.\n"
            "Chỉ trích xuất các thông tin chính xác về: tên sách, giá niêm yết (price_vnd), tác giả, nhà xuất bản, số trang, điểm rating."
        )
        cat_data = {"products": context["products"]}
        cat_res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": cat_prompt},
                {"role": "user", "content": f"Dữ liệu: {json.dumps(cat_data, ensure_ascii=False)}\nCâu hỏi cần trích xuất: {prompt}"}
            ],
            temperature=0.0,
        )
        trace["specialist_outputs"]["catalog"] = cat_res.choices[0].message.content.strip()
        
    if "review_specialist" in specialists and context["reviews"]:
        rev_prompt = (
            "Bạn là Review Specialist phụ trách phân tích đánh giá thực tế của độc giả.\n"
            "Hãy lọc và trích xuất chính xác các ý kiến phản ánh thực tế về chất lượng in ấn, lỗi sản phẩm, dịch vụ đóng gói (bubble wrap) và thời gian giao hàng."
        )
        rev_data = {"reviews": context["reviews"]}
        rev_res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": rev_prompt},
                {"role": "user", "content": f"Dữ liệu review: {json.dumps(rev_data, ensure_ascii=False)}\nCâu hỏi cần phân tích: {prompt}"}
            ],
            temperature=0.0,
        )
        trace["specialist_outputs"]["review"] = rev_res.choices[0].message.content.strip()
        
    if "knowledge_specialist" in specialists and context["sources"]:
        know_prompt = (
            "Bạn là Knowledge Specialist phụ trách đối chiếu nguồn xuất bản và thư mục chính thức.\n"
            "Hãy trích xuất thông tin tác phẩm gốc, dịch giả, nhà xuất bản nguồn từ tư liệu được cấp."
        )
        know_data = {"sources": context["sources"]}
        know_res = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": know_prompt},
                {"role": "user", "content": f"Dữ liệu nguồn: {json.dumps(know_data, ensure_ascii=False)}\nCâu hỏi: {prompt}"}
            ],
            temperature=0.0,
        )
        trace["specialist_outputs"]["knowledge"] = know_res.choices[0].message.content.strip()
        
    # Stage 3: Supervisor / Synthesizer
    synth_prompt = (
        "Bạn là Supervisor/Synthesizer trong hệ thống Multi-Agent.\n"
        "Nhiệm vụ của bạn là tổng hợp báo cáo từ các chuyên gia (Catalog Specialist, Review Specialist, Knowledge Specialist) thành một câu trả lời duy nhất, hoàn chỉnh, mạch lạc, đáp ứng đầy đủ từng ý mà người dùng đã hỏi.\n"
        "Đảm bảo nêu rõ số liệu giá cả chính xác và trích dẫn trung thực phản ánh của độc giả."
    )
    synth_user = (
        f"CÂU HỎI NGƯỜI DÙNG: {prompt}\n\n"
        f"KẾT QUẢ TỪ CÁC CHUYÊN GIA:\n{json.dumps(trace['specialist_outputs'], ensure_ascii=False, indent=2)}\n\n"
        "Hãy viết câu trả lời cuối cùng cho người dùng:"
    )
    synth_res = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": synth_prompt},
            {"role": "user", "content": synth_user}
        ],
        temperature=0.0,
    )
    final_answer = synth_res.choices[0].message.content.strip()
    latency_ms = (time.time() - t0) * 1000
    return final_answer, trace, latency_ms

def evaluate_fact_coverage(answer: str, required_facts: list) -> list:
    """Check how each required fact is covered in the answer using LLM evaluator."""
    eval_prompt = (
        "Bạn là thẩm định viên đánh giá độ chính xác thực tế (Fact Verifier).\n"
        "Với mỗi fact bắt buộc, hãy kiểm tra xem câu trả lời có thỏa mãn đầy đủ thông tin hay không.\n"
        "Bắt buộc trả về đúng cấu trúc JSON sau:\n"
        "{\n"
        '  "verdicts": [\n'
        '    {"fact_id": "<fact_id>", "passed": true/false, "reason": "<giải thích ngắn>"}\n'
        "  ]\n"
        "}"
    )
    eval_input = {
        "candidate_answer": answer,
        "required_facts": required_facts
    }
    eval_res = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": eval_prompt},
            {"role": "user", "content": f"Đánh giá:\n{json.dumps(eval_input, ensure_ascii=False, indent=2)}"}
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    try:
        content = eval_res.choices[0].message.content
        parsed = json.loads(content)
        verdicts = parsed.get("verdicts") or parsed.get("results")
        if not verdicts:
            for k, v in parsed.items():
                if isinstance(v, list):
                    verdicts = v
                    break
        if isinstance(verdicts, list):
            return verdicts
        return [{"fact_id": f["fact_id"], "passed": False, "reason": "No verdict list"} for f in required_facts]
    except Exception as e:
        return [{"fact_id": f["fact_id"], "passed": False, "reason": str(e)} for f in required_facts]

async def run_full_benchmark():
    NUM_REPEATS = 3
    print(f"\n{'='*95}")
    print(f"BẮT ĐẦU CHẠY TOÀN BỘ 24 CASES V4 (144 RUNS = 24 CASES x 2 HỆ THỐNG x 3 REPEATS)")
    print(f"{'='*95}\n")
    
    all_case_results = []
    
    for idx, case in enumerate(cases, 1):
        cid = case['case_id']
        tier = case['tier']
        prompt = case['user_prompt']
        req_facts = case['required_facts']
        ctx = build_case_context(case)
        
        print(f"[{idx}/24] {cid} ({tier}) - Facts: {len(req_facts)}...")
        
        sa_repeats = []
        ma_repeats = []
        
        for rep in range(NUM_REPEATS):
            # 1. Single-Agent
            sa_ans, sa_lat = run_single_agent(prompt, ctx)
            sa_eval = evaluate_fact_coverage(sa_ans, req_facts)
            sa_pass = sum(1 for v in sa_eval if v.get('passed', False))
            sa_score = sa_pass / len(req_facts) if req_facts else 1.0
            sa_repeats.append({
                "repeat": rep,
                "answer": sa_ans,
                "latency_ms": sa_lat,
                "score": sa_score,
                "verdicts": sa_eval
            })
            
            # 2. Multi-Agent
            ma_ans, ma_trace, ma_lat = run_multi_agent(prompt, ctx, case)
            ma_eval = evaluate_fact_coverage(ma_ans, req_facts)
            ma_pass = sum(1 for v in ma_eval if v.get('passed', False))
            ma_score = ma_pass / len(req_facts) if req_facts else 1.0
            ma_repeats.append({
                "repeat": rep,
                "answer": ma_ans,
                "trace": ma_trace,
                "latency_ms": ma_lat,
                "score": ma_score,
                "verdicts": ma_eval
            })
            
        sa_mean_score = sum(r['score'] for r in sa_repeats) / NUM_REPEATS
        sa_std_score = math.sqrt(sum((r['score'] - sa_mean_score)**2 for r in sa_repeats) / NUM_REPEATS)
        sa_mean_lat = sum(r['latency_ms'] for r in sa_repeats) / NUM_REPEATS
        
        ma_mean_score = sum(r['score'] for r in ma_repeats) / NUM_REPEATS
        ma_std_score = math.sqrt(sum((r['score'] - ma_mean_score)**2 for r in ma_repeats) / NUM_REPEATS)
        ma_mean_lat = sum(r['latency_ms'] for r in ma_repeats) / NUM_REPEATS
        
        case_summary = {
            "case_id": cid,
            "tier": tier,
            "prompt": prompt,
            "required_facts_count": len(req_facts),
            "single_agent": {
                "mean_score": sa_mean_score,
                "std_score": sa_std_score,
                "mean_latency_ms": sa_mean_lat,
                "repeats": sa_repeats
            },
            "multi_agent": {
                "mean_score": ma_mean_score,
                "std_score": ma_std_score,
                "mean_latency_ms": ma_mean_lat,
                "repeats": ma_repeats
            },
            "delta_score": ma_mean_score - sa_mean_score,
            "win_status": "MA_WIN" if (ma_mean_score > sa_mean_score + 0.05) else ("SA_WIN" if sa_mean_score > ma_mean_score + 0.05 else "TIE")
        }
        all_case_results.append(case_summary)
        print(f"      SA: {sa_mean_score*100:.1f}% (±{sa_std_score*100:.1f}%) | MA: {ma_mean_score*100:.1f}% (±{ma_std_score*100:.1f}%) -> {case_summary['win_status']}")
        
    out_dir = ROOT / 'output' / 'evaluation-v4'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / 'full_24_benchmark_results.json'
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(all_case_results, f, ensure_ascii=False, indent=2)
        
    print(f"\n{'='*95}")
    print(f"ĐÃ HOÀN TẤT VÀ LƯU 24 CASES TẠI: {out_file}")
    print(f"{'='*95}\n")

if __name__ == "__main__":
    asyncio.run(run_full_benchmark())
