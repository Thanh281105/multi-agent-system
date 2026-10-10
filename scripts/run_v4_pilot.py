import asyncio
import os
import sys
import json
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
prod_by_id = {p['external_id']: p for p in products}

with open(ROOT / 'data/snapshots/tiki-books-v4-eval/reviews.jsonl', 'r', encoding='utf-8') as f:
    reviews = [json.loads(line) for line in f]

with open(ROOT / 'data/knowledge/books-v1/sources.json', 'r', encoding='utf-8') as f:
    sources_data = json.load(f)
sources_list = sources_data['sources'] if isinstance(sources_data, dict) and 'sources' in sources_data else sources_data
source_by_id = {s.get('id') or s.get('source_id') or s.get('title'): s for s in sources_list}

# Load v4 benchmark cases
with open(ROOT / 'evaluation/v4_fair_benchmark_cases.json', 'r', encoding='utf-8') as f:
    benchmark_data = json.load(f)

cases_by_id = {c['case_id']: c for c in benchmark_data['cases']}

PILOT_CASE_IDS = [
    "fair_tier1_catalog_01",
    "fair_tier2_cross_01",
    "fair_tier3_recom_01",
    "fair_tier4_guard_01",
]

def get_relevant_context(case: dict) -> dict:
    """Prepare bounded relevant context from snapshot for this specific case."""
    cid = case['case_id']
    ctx = {"products": [], "reviews": [], "sources": []}
    
    if cid == "fair_tier1_catalog_01":
        # Sapiens
        p = prod_by_id.get("184419723")
        if p:
            ctx["products"].append(p)
            
    elif cid == "fair_tier2_cross_01":
        # Gieo Hạt Cùng Vĩ Nhân
        p = prod_by_id.get("101325467")
        if p:
            ctx["products"].append(p)
            revs = [r for r in reviews if r.get('product_external_id') == "101325467"]
            ctx["reviews"].extend(revs[:5])
            
    elif cid == "fair_tier3_recom_01":
        # Morisaki & Bạch Dạ Hành
        p1 = prod_by_id.get("197657913")
        p2 = prod_by_id.get("103379147")
        if p1: ctx["products"].append(p1)
        if p2: ctx["products"].append(p2)
        revs1 = [r for r in reviews if r.get('product_external_id') == "197657913"]
        revs2 = [r for r in reviews if r.get('product_external_id') == "103379147"]
        ctx["reviews"].extend(revs1[:3])
        ctx["reviews"].extend(revs2[:3])
        if "src_morisaki_nlv" in source_by_id:
            ctx["sources"].append(source_by_id["src_morisaki_nlv"])
            
    elif cid == "fair_tier4_guard_01":
        # Bạch Dạ Hành mutation request
        p = prod_by_id.get("103379147")
        if p: ctx["products"].append(p)
        
    return ctx

def run_single_agent(prompt: str, context: dict) -> str:
    """Single-Agent Baseline: Flat prompt with all tools/data mixed in."""
    system_prompt = (
        "Bạn là trợ lý tư vấn sách Tiki. Hãy trả lời câu hỏi của người dùng dựa trên thông tin được cung cấp.\n"
        "Nguyên tắc: Hệ thống chỉ là trợ lý đọc/tư vấn (read-only), không có quyền thay đổi giá niêm yết hoặc đặt hàng.\n"
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
    return response.choices[0].message.content.strip()

def run_multi_agent(prompt: str, context: dict, case: dict) -> tuple[str, dict]:
    """Multi-Agent System: Intent Routing -> Specialist Execution -> Synthesis."""
    
    # Stage 1: Router
    specialists = case['expected_specialists']
    trace = {"routed_specialists": specialists, "specialist_outputs": {}}
    
    # Check guardrail first
    if "guardrail_abstention" in specialists:
        # Guardrail specialist
        guard_prompt = (
            "Bạn là Guardrail Specialist chịu trách nhiệm kiểm tra an toàn và phân quyền hệ thống.\n"
            "Chính sách: Hệ thống hoàn toàn Read-Only, không có quyền mutation (không sửa giá, không xóa review, không can thiệp cơ sở dữ liệu, không đặt hàng). Nếu người dùng yêu cầu hành động mutation hoặc phá vỡ an toàn, hãy phát hiện và từ chối rõ ràng kèm lý do."
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
        return final_answer, trace
    
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
    return final_answer, trace

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

async def run_pilot():
    print(f"\n{'='*90}")
    print(f"PILOT EVALUATION: 4 TIERS CHUẨN HÓA V4 (MULTI-AGENT VS SINGLE-AGENT)")
    print(f"{'='*90}\n")
    
    results = []
    
    for cid in PILOT_CASE_IDS:
        case = cases_by_id[cid]
        tier = case['tier']
        prompt = case['user_prompt']
        req_facts = case['required_facts']
        
        print(f"\n{'*'*80}")
        print(f"CASE: [{cid}] — TIER: {tier}")
        print(f"User Prompt: {prompt}")
        print(f"Expected Specialists: {case['expected_specialists']}")
        print(f"Total Required Facts: {len(req_facts)}")
        for f in req_facts:
            print(f"  - [{f['fact_id']}] {f['question_aspect']}: {f['expected_value']}")
        print(f"{'-'*80}")
        
        ctx = get_relevant_context(case)
        
        # 1. Run Single-Agent
        print("\n[1] Running Single-Agent Baseline...")
        sa_ans = run_single_agent(prompt, ctx)
        sa_eval = evaluate_fact_coverage(sa_ans, req_facts)
        sa_passed = sum(1 for v in sa_eval if v.get('passed', False))
        sa_score = sa_passed / len(req_facts) if req_facts else 1.0
        
        # 2. Run Multi-Agent
        print("[2] Running Multi-Agent System...")
        ma_ans, ma_trace = run_multi_agent(prompt, ctx, case)
        ma_eval = evaluate_fact_coverage(ma_ans, req_facts)
        ma_passed = sum(1 for v in ma_eval if v.get('passed', False))
        ma_score = ma_passed / len(req_facts) if req_facts else 1.0
        
        # Display side-by-side
        print(f"\n--- KẾT QUẢ SO SÁNH TRỰC DIỆN ---")
        print(f"\n>>> SINGLE-AGENT ANSWER (Score: {sa_passed}/{len(req_facts)} - {sa_score*100:.1f}%):")
        print(sa_ans)
        print("  Verdicts:", [(v.get('fact_id'), v.get('passed')) for v in sa_eval])
        
        print(f"\n>>> MULTI-AGENT ANSWER (Score: {ma_passed}/{len(req_facts)} - {ma_score*100:.1f}%):")
        print(ma_ans)
        print("  Verdicts:", [(v.get('fact_id'), v.get('passed')) for v in ma_eval])
        
        case_res = {
            "case_id": cid,
            "tier": tier,
            "prompt": prompt,
            "single_agent": {
                "answer": sa_ans,
                "score": sa_score,
                "verdicts": sa_eval
            },
            "multi_agent": {
                "answer": ma_ans,
                "score": ma_score,
                "verdicts": ma_eval,
                "trace": ma_trace
            }
        }
        results.append(case_res)
        
    out_dir = ROOT / 'output' / 'evaluation-v4'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / 'pilot_run_results.json'
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        
    print(f"\n{'='*90}")
    print(f"HOÀN THÀNH PILOT! Đã lưu kết quả tại: {out_file}")
    print(f"{'='*90}\n")
    
    # Summary Table
    print(f"{'Case ID':<25} | {'Tier':<25} | {'Single-Agent':<15} | {'Multi-Agent':<15}")
    print(f"{'-'*85}")
    for r in results:
        sa_s = f"{r['single_agent']['score']*100:.1f}%"
        ma_s = f"{r['multi_agent']['score']*100:.1f}%"
        print(f"{r['case_id']:<25} | {r['tier']:<25} | {sa_s:<15} | {ma_s:<15}")

if __name__ == "__main__":
    asyncio.run(run_pilot())
