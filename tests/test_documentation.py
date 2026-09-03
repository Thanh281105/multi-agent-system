"""Documentation integrity and evidence-boundary regression checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "PRODUCT.md",
    PROJECT_ROOT / "docs" / "architecture.md",
    PROJECT_ROOT / "docs" / "api.md",
    PROJECT_ROOT / "docs" / "data.md",
    PROJECT_ROOT / "docs" / "operations.md",
    PROJECT_ROOT / "docs" / "evaluation.md",
    PROJECT_ROOT / "frontend" / "README.md",
)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_documented_local_links_exist_inside_repository(document: Path) -> None:
    assert document.is_file()
    content = document.read_text(encoding="utf-8")

    for raw_target in MARKDOWN_LINK.findall(content):
        target = raw_target.strip().strip("<>")
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        local_path = target.split("#", maxsplit=1)[0]
        if not local_path:
            continue
        resolved = (document.parent / local_path).resolve()
        assert resolved.is_relative_to(PROJECT_ROOT), raw_target
        assert resolved.exists(), f"broken link in {document.name}: {raw_target}"


def test_thesis_claims_keep_sample_and_evaluation_limits_visible() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs" / "architecture.md").read_text(
        encoding="utf-8"
    )
    data = (PROJECT_ROOT / "docs" / "data.md").read_text(encoding="utf-8")
    evaluation = (PROJECT_ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")
    operations = (PROJECT_ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    workflow = (PROJECT_ROOT / "workflow.md").read_text(encoding="utf-8")

    assert "production-oriented" in readme
    assert "snapshot lịch sử Tiki Books" in readme
    assert "200 sách" in readme and "1.773 review" in readme
    assert "data/snapshots/tiki-books-v4-sample" not in readme
    assert "KNOWLEDGE_BACKEND=static" not in readme
    assert "chưa đưa vào routing hay aggregation" in architecture
    assert "chưa có long-term user memory" in architecture
    assert "Knowledge/RAG" in architecture and "disabled" in architecture
    assert "quality-report.json" in data
    assert "tiki-books:kaggle-v4:eval" in data
    assert '"knowledge": "disabled"' in operations
    assert "deterministic_book_catalog_v2" in evaluation
    assert "tiki_books_vi_28_v1" in evaluation
    assert "real_model_captured" in evaluation
    assert "source-manifest" in evaluation
    assert "không phải external validity" in evaluation
    assert "không có checked-in live-LLM result" in evaluation
    assert "Tài liệu thiết kế lịch sử" in workflow
