"""Chấm lại 144 câu trả lời v4 bằng LLM judge được cấp nguồn thật.

Judge nhận câu hỏi, câu trả lời, các required_facts của ca và bản ghi nguồn
(snapshot sản phẩm/đánh giá, tri thức, chính sách) dựng xác định từ
`source_provenance`. Đây là phân tích thăm dò: bộ v4 được thiết kế sau khi xem
lỗi P8.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.evaluation.rejudge_collect import JudgeRequest

MAX_OUTPUT_TOKENS = 1_500
KNOWLEDGE_SOURCES = Path("data/knowledge/books-v1/sources.json")
VARIANTS = ("single_agent", "multi_agent")

INSTRUCTIONS = """Bạn là giám khảo chấm câu trả lời của một trợ lý sách tiếng Việt.
Bạn nhận: câu hỏi, câu trả lời, và danh sách "facts" cần có. Mỗi fact gồm
claim_blueprint (điều câu trả lời phải nêu), expected_value (giá trị đúng) và
source_context (bản ghi nguồn thật dùng làm căn cứ).

Với MỖI fact, đặt supported=true chỉ khi câu trả lời nêu đúng nghĩa điều
claim_blueprint yêu cầu VÀ giữ đúng giá trị expected_value. Nếu blueprint yêu
cầu một chuỗi cụ thể (ví dụ tên sách đầy đủ có hậu tố như "(Tái Bản 2022)")
thì thiếu hoặc khác chuỗi đó là supported=false, kể cả khi chung ý nghĩa.
Chấp nhận diễn đạt khác và định dạng số khác (191.000đ = 191000 VND) khi giá
trị không đổi. Giải thích ngắn trong reason.

Thông tin bổ sung trong câu trả lời KHÔNG bị phạt nếu có căn cứ trong
source_context của bất kỳ fact nào. Thông tin bổ sung mang tính dữ kiện mà
không có căn cứ nào trong source_context thì liệt kê trong
unsupported_extra_claims (mỗi mục một câu ngắn); không phạt điểm fact vì điều
đó. Chỉ trả JSON đúng schema."""


class V4Verdict(BaseModel):
    fact_id: str
    supported: bool
    reason: str


class V4Judgment(BaseModel):
    verdicts: list[V4Verdict]
    unsupported_extra_claims: list[str]


def _jsonl_by_id(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[str(row["external_id"])] = row
    return rows


class SourceStore:
    def __init__(self, snapshot_dir: Path, knowledge_path: Path) -> None:
        self.products = _jsonl_by_id(snapshot_dir / "products.jsonl")
        self.reviews = _jsonl_by_id(snapshot_dir / "reviews.jsonl")
        sources = json.loads(knowledge_path.read_text(encoding="utf-8"))["sources"]
        self.knowledge = {s["source_id"]: s for s in sources}


def build_fact_context(fact: Mapping[str, Any], store: SourceStore) -> str:
    prov = fact["source_provenance"]
    kind = prov["source_type"]
    record_id = str(prov["record_id"])
    parts: list[str] = []
    if kind in ("catalog_product", "catalog_and_knowledge", "cross_domain_synthesis"):
        product = store.products.get(record_id)
        if product is None:
            parts.append(f"[không tìm thấy bản ghi sản phẩm {record_id}]")
        else:
            keep = {
                k: product.get(k)
                for k in (
                    "name",
                    "authors",
                    "publisher",
                    "category",
                    "price_vnd",
                    "original_price_vnd",
                    "page_count",
                    "rating",
                    "description",
                )
            }
            parts.append("Sản phẩm: " + json.dumps(keep, ensure_ascii=False))
    if kind == "customer_review":
        review = store.reviews.get(record_id)
        if review is None:
            parts.append(f"[không tìm thấy bản ghi đánh giá {record_id}]")
        else:
            keep = {k: review.get(k) for k in ("title", "content", "rating")}
            parts.append("Đánh giá: " + json.dumps(keep, ensure_ascii=False))
    if kind == "knowledge_excerpt":
        source = store.knowledge.get(record_id)
        if source is None:
            parts.append(f"[không tìm thấy nguồn tri thức {record_id}]")
        else:
            parts.append(
                f"Nguồn tri thức {record_id}: {source.get('content_markdown', '')}"
            )
    for field in ("exact_quote", "exact_value", "policy_rule"):
        if field in prov:
            parts.append(f"{field}: {prov[field]}")
    return "\n".join(parts)


def _case_index(cases_path: Path) -> dict[str, Mapping[str, Any]]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    return {c["case_id"]: c for c in cases}


def request_key(case_id: str, variant: str, repeat: int) -> str:
    return f"{case_id}|{variant}|{repeat}"


def build_requests(
    results_path: Path,
    cases_path: Path,
    snapshot_dir: Path,
    *,
    knowledge_path: Path = KNOWLEDGE_SOURCES,
    case_ids: Sequence[str] | None = None,
) -> dict[str, JudgeRequest]:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    cases = _case_index(cases_path)
    store = SourceStore(snapshot_dir, knowledge_path)
    wanted = set(case_ids) if case_ids is not None else None
    requests: dict[str, JudgeRequest] = {}
    for result in results:
        case_id = result["case_id"]
        if wanted is not None and case_id not in wanted:
            continue
        facts = [
            {
                "fact_id": f["fact_id"],
                "claim_blueprint": f["claim_blueprint"],
                "expected_value": f["expected_value"],
                "source_context": build_fact_context(f, store),
            }
            for f in cases[case_id]["required_facts"]
        ]
        for variant in VARIANTS:
            for repeat in result[variant]["repeats"]:
                payload = {
                    "question": result["prompt"],
                    "answer": repeat["answer"],
                    "facts": facts,
                }
                requests[request_key(case_id, variant, repeat["repeat"])] = (
                    JudgeRequest(
                        instructions=INSTRUCTIONS,
                        input_text=json.dumps(payload, ensure_ascii=False),
                        schema=V4Judgment,
                        max_output_tokens=MAX_OUTPUT_TOKENS,
                    )
                )
    return requests


def _new_score(row: Mapping[str, Any], fact_ids: Sequence[str]) -> float | None:
    if row.get("error") is not None or row.get("output") is None:
        return None
    verdicts = {v["fact_id"]: bool(v["supported"]) for v in row["output"]["verdicts"]}
    return sum(verdicts.get(f, False) for f in fact_ids) / len(fact_ids)


def summarize(
    results_path: Path,
    cases_path: Path,
    cache_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    cases = _case_index(cases_path)
    per_case: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    errors = 0
    for result in results:
        case_id = result["case_id"]
        fact_ids = [f["fact_id"] for f in cases[case_id]["required_facts"]]
        entry: dict[str, Any] = {"case_id": case_id}
        for variant in VARIANTS:
            old: list[float] = []
            new: list[float] = []
            for repeat in result[variant]["repeats"]:
                key = request_key(case_id, variant, repeat["repeat"])
                row = cache_rows.get(key)
                if row is None:
                    continue
                score = _new_score(row, fact_ids)
                if score is None:
                    errors += 1
                    continue
                old.append(repeat["score"])
                new.append(score)
                if score != repeat["score"]:
                    changed.append({"key": key, "old": repeat["score"], "new": score})
                for claim in row["output"].get("unsupported_extra_claims", []):
                    extras.append({"key": key, "claim": claim})
            entry[variant] = {
                "old_mean": sum(old) / len(old) if old else None,
                "new_mean": sum(new) / len(new) if new else None,
                "n": len(new),
            }
        per_case.append(entry)

    def overall(variant: str, field: str) -> float | None:
        values = [c[variant][field] for c in per_case if c[variant][field] is not None]
        return sum(values) / len(values) if values else None

    return {
        "per_case": per_case,
        "overall": {
            v: {"old": overall(v, "old_mean"), "new": overall(v, "new_mean")}
            for v in VARIANTS
        },
        "changed_repeats": changed,
        "unsupported_extra_claims": extras,
        "errors": errors,
    }
