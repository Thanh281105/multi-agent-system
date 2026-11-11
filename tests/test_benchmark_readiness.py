"""Admission and profile boundaries must fail before provider side effects."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.evaluation import v3_cli
from app.evaluation.benchmark_readiness import (
    audit_dependencies,
    prepare_human_review,
    prepare_readiness,
)
from app.evaluation.protocol import canonical_json_bytes
from app.evaluation.source_identity import (
    SourceIdentityError,
    capture_source_identity,
    freeze_source_identity,
    require_same_source,
)
from app.shared.budget import (
    BudgetLimitExceededError,
    ProviderBudgetContext,
    generation_input_token_limit,
    provider_budget_scope,
)
from app.shared.model_runtime import OpenAIModelRuntime
from tests.test_evaluation_evidence_integrity import _blind_fixture
from tests.test_model_runtime import FakeClient, ParsedAnswer


def _repository(path: Path) -> Path:
    path.mkdir()
    for args in [
        ("init",),
        ("config", "user.name", "Synthetic Test"),
        ("config", "user.email", "test@example.invalid"),
    ]:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "app").mkdir()
    (path / "app/__init__.py").write_text("", encoding="utf-8")
    (path / "app/runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / ".gitignore").write_text(".env\n*.ignored.py\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )
    return path


@pytest.mark.parametrize("change", ["tracked", "staged", "untracked"])
def test_live_source_refuses_every_dirty_tree(tmp_path: Path, change: str) -> None:
    root = _repository(tmp_path / "source")
    capture_source_identity(root)
    target = root / ("app/new.py" if change == "untracked" else "app/runtime.py")
    target.write_text("VALUE = 2\n", encoding="utf-8")
    if change == "staged":
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    with pytest.raises(SourceIdentityError, match="source_tree_dirty"):
        capture_source_identity(root)


def test_identity_freezes_actual_bytes_and_detects_new_commit(tmp_path: Path) -> None:
    root = _repository(tmp_path / "source")
    (root / ".env").write_text("SECRET=not-real\n", encoding="utf-8")
    identity = capture_source_identity(root)
    assert all(item.path != ".env" for item in identity.files)
    output = tmp_path / "identity.json"
    freeze_source_identity(output, identity)
    before = output.read_bytes()
    freeze_source_identity(output, identity)
    assert output.read_bytes() == before
    (root / "app/runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    for args in [("add", "."), ("commit", "-m", "second")]:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    with pytest.raises(SourceIdentityError, match="source_identity_drift"):
        require_same_source(root, identity)
    with pytest.raises(SourceIdentityError, match="stored_source_identity_drift"):
        freeze_source_identity(output, capture_source_identity(root))
    assert output.read_bytes() == before


def test_ancestor_repo_and_ignored_runtime_files_are_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path / "source")
    with pytest.raises(SourceIdentityError, match="repository_root_mismatch"):
        capture_source_identity(root / "app")
    (root / "app/hidden.ignored.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(SourceIdentityError, match="runtime_file_untracked"):
        capture_source_identity(root)


def test_profile_input_bound_rejects_before_quote_or_transport() -> None:
    class Manifest:
        service_tier = "standard"

        def resolve(self, *_args: object) -> None:
            pass

        def quote(self, **_kwargs: object) -> None:
            raise AssertionError("oversized request reached ledger quote")

    client = FakeClient([])
    client.max_retries = 0
    runtime = OpenAIModelRuntime("synthetic-test-key", client=client, max_retries=0)
    budget = ProviderBudgetContext(
        ledger=SimpleNamespace(manifest=Manifest()),
        scope_id="bounded_test",
        purpose="benchmark",
        max_input_tokens_per_generation=12_000,
    )
    with pytest.raises(BudgetLimitExceededError, match="generation_payload_limit"):
        runtime._prepare_budgeted_request(
            budget=budget,
            model="gpt-5.4-mini",
            stage="test",
            agent_id="tester",
            instructions="Return the schema",
            input_text="x" * 12_000,
            schema=ParsedAnswer,
            max_output_tokens=200,
            reasoning_effort="none",
        )
    assert not client.responses.requests
    with provider_budget_scope(budget):
        assert generation_input_token_limit() == 12_000
    assert generation_input_token_limit() == 16_000


@pytest.mark.parametrize("limit", [0, -1, True, 16_001])
def test_profile_cannot_widen_global_bound(limit: int) -> None:
    with pytest.raises(ValueError, match="generation input bound"):
        ProviderBudgetContext(
            ledger=SimpleNamespace(),
            scope_id="bounded_test",
            purpose="chat",
            max_input_tokens_per_generation=limit,
        )


def test_human_kit_starts_with_no_labels_and_keeps_unblinding_out(
    tmp_path: Path,
) -> None:
    blinded = _blind_fixture()
    packet = tmp_path / "packet.json"
    packet.write_bytes(canonical_json_bytes(blinded.packet))
    output = tmp_path / "human"
    result = prepare_human_review(packet, output)
    assert result["submitted_labels"] == 0
    assert result["not_a_calibration_freeze"] is True
    for slot in ("reviewer_a", "reviewer_b"):
        rows = [
            json.loads(line)
            for line in (output / slot / "ratings.jsonl").read_text().splitlines()
        ]
        assert len(rows) == 4 * 6
        assert all(row["score"] is None for row in rows)
        copy = json.loads((output / slot / "review-packet.json").read_bytes())
        assert set(copy) == {"schema_version", "source_packet_sha256", "answers"}
        assert "variant_id" not in json.dumps(copy)
    before = (output / "reviewer_a/ratings.jsonl").read_bytes()
    with pytest.raises(FileExistsError):
        prepare_human_review(packet, output)
    assert (output / "reviewer_a/ratings.jsonl").read_bytes() == before


def test_readiness_reports_real_corpus_gaps_without_connecting_services(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    result = prepare_readiness(root, tmp_path / "readiness")
    assert result["provider_calls"] == result["database_connections"] == 0
    assert result["live_status"] == "not_tested_api_deferred_by_user"
    assert result["coverage"] == {"exact_work": 20, "ambiguous": 17, "unmatched": 163}
    assert result["corpus_review_queue_count"] == 180
    assert result["measured_live_quality"] is None
    rows = [
        json.loads(s)
        for s in (tmp_path / "readiness/corpus-review-queue.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 180 and all(r["candidate_source_url"] is None for r in rows)


@pytest.mark.parametrize("line", ["pytest>=1", "pytest", "pytest==9.*"])
def test_dependency_audit_requires_exact_pins(tmp_path: Path, line: str) -> None:
    (tmp_path / "requirements-dev.lock").write_text(line, encoding="utf-8")
    with pytest.raises(ValueError, match="lock_not_exact"):
        audit_dependencies(tmp_path)


def _live_inputs() -> v3_cli.EvaluationV3CLIInputs:
    root = Path(__file__).resolve().parents[1]
    args = v3_cli._parser().parse_args(
        [
            "validate",
            "--project-root",
            str(root),
            "--experiment",
            str(
                root / "evaluation/v3/mini-generation-luna-judge-v1/experiment.v3.json"
            ),
        ]
    )
    return v3_cli._load_inputs(args)


def test_dirty_live_admission_precedes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _live_inputs()

    def dirty(_root: Path) -> None:
        raise SourceIdentityError("source_tree_dirty")

    def forbidden(**_kwargs: object) -> None:
        raise AssertionError("dirty source reached credential configuration")

    monkeypatch.setattr(v3_cli, "capture_source_identity", dirty)
    monkeypatch.setattr("app.core.config.Settings", forbidden)
    with pytest.raises(v3_cli.EvaluationV3CLIError) as caught:
        v3_cli._build_live_executor_factory(inputs)
    assert caught.value.code == "source_tree_dirty"


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0", "101"])
def test_operator_cap_rejects_invalid_or_wider_budget(amount: str) -> None:
    inputs = replace(_live_inputs(), operator_budget_usd=Decimal(amount))
    with pytest.raises(v3_cli.EvaluationV3CLIError) as caught:
        v3_cli._operator_budget_nano_usd(inputs)
    assert caught.value.code == "operator_budget_invalid"


def test_operator_cap_can_narrow_frozen_reservation_limit() -> None:
    inputs = replace(_live_inputs(), operator_budget_usd=Decimal("10"))
    assert v3_cli._operator_budget_nano_usd(inputs) == 10_000_000_000
