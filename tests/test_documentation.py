"""Documentation integrity and evidence-boundary regression checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "docs" / "architecture.md",
    PROJECT_ROOT / "docs" / "api.md",
    PROJECT_ROOT / "docs" / "operations.md",
    PROJECT_ROOT / "docs" / "evaluation.md",
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
    evaluation = (PROJECT_ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")

    assert "dữ liệu mẫu tổng hợp" in readme
    assert "production-oriented" in readme
    assert "chưa đưa free-text memory vào inference" in readme
    assert "Chưa có long-term" in readme
    assert "chưa đưa vào routing hay aggregation" in architecture
    assert "chưa có long-term user memory" in architecture
    assert "baseline_unavailable" in evaluation
    assert "source-manifest" in evaluation
    assert "không phải external validity" in evaluation
