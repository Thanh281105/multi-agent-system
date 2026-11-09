import json
from pathlib import Path

import pytest

from app.evaluation import rejudge_cli
from app.evaluation.rejudge_collect import JudgeCallResult, JudgeRequest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "output" / "evaluation-v4" / "full_24_benchmark_results.json"
needs_v4 = pytest.mark.skipif(
    not RESULTS.exists(),
    reason="kết quả v4 nằm trong output/ (gitignored) và không có trên máy này",
)


class CountingCall:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, key: str, request: JudgeRequest) -> JudgeCallResult:
        self.calls += 1
        return JudgeCallResult(
            {"verdicts": [], "unsupported_extra_claims": []}, None, 10, 0, 5
        )


@needs_v4
def test_dry_run_makes_no_network_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    counter = CountingCall()
    code = rejudge_cli.main(
        ["dry-run", "v4", "--out", str(tmp_path)], call_factory=lambda k, m: counter
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert counter.calls == 0
    assert result["network_calls"] == 0
    assert result["cells"] == 144
    assert float(result["upper_bound_usd"]) > float(result["typical_estimate_usd"]) > 0
    assert not list(tmp_path.glob("v4-*"))  # dry-run không ghi artifact nào


def test_run_requires_budget(tmp_path: Path) -> None:
    counter = CountingCall()
    code = rejudge_cli.main(
        ["run", "v4", "--out", str(tmp_path), "--allow-network"],
        call_factory=lambda k, m: counter,
    )
    assert code == 2
    assert counter.calls == 0


def test_run_requires_allow_network(tmp_path: Path) -> None:
    counter = CountingCall()
    code = rejudge_cli.main(
        ["run", "v4", "--out", str(tmp_path), "--budget-usd", "1"],
        call_factory=lambda k, m: counter,
    )
    assert code == 2
    assert counter.calls == 0


def test_missing_api_key_fails_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("app.core.config.settings.openai_api_key", None, raising=False)
    counter = CountingCall()
    code = rejudge_cli.main(
        [
            "run",
            "v4",
            "--out",
            str(tmp_path),
            "--budget-usd",
            "1",
            "--allow-network",
        ],
        call_factory=lambda k, m: counter,
    )
    assert code == 2
    assert counter.calls == 0


@needs_v4
def test_sample_runs_with_fake_call_and_respects_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    counter = CountingCall()
    argv = [
        "sample", "v4", "--out", str(tmp_path), "--n", "3",
        "--budget-usd", "5", "--allow-network",
    ]  # fmt: skip
    assert rejudge_cli.main(argv, call_factory=lambda k, m: counter) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["cells"] == 18  # 3 ca x 2 hệ thống x 3 lần
    assert counter.calls == 18
    assert rejudge_cli.main(argv, call_factory=lambda k, m: counter) == 0
    assert counter.calls == 18  # lần hai: toàn bộ từ cache


def test_report_writes_manifest_with_hashes(tmp_path: Path) -> None:
    cases = {"cases": [{"case_id": "c1", "required_facts": [{"fact_id": "a"}]}]}
    results = [
        {
            "case_id": "c1",
            "single_agent": {"repeats": [{"repeat": 0, "score": 0.0}]},
            "multi_agent": {"repeats": [{"repeat": 0, "score": 1.0}]},
        }
    ]
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmp_path / "results.json").write_text(json.dumps(results), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    row = {
        "key": "c1|single_agent|0",
        "output": {
            "verdicts": [{"fact_id": "a", "supported": True, "reason": "ok"}],
            "unsupported_extra_claims": [],
        },
        "error": None,
        "input_tokens": 100,
        "cached_input_tokens": 0,
        "output_tokens": 20,
        "cost_usd": "0.00002",
    }
    (out / "v4-judgments.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    code = rejudge_cli.main(
        [
            "report",
            "v4",
            "--out",
            str(out),
            "--v4-results",
            str(tmp_path / "results.json"),
            "--v4-cases",
            str(tmp_path / "cases.json"),
        ]  # fmt: skip
    )
    assert code == 0
    manifest = json.loads((out / "v4-manifest.json").read_text(encoding="utf-8"))
    assert manifest["exploratory"] is True
    assert manifest["totals"]["rows"] == 1
    assert set(manifest["files"]) == {"v4-judgments.jsonl", "v4-report.json"}
    assert all(len(h) == 64 for h in manifest["files"].values())
