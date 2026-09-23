"""Regression tests for rescoring immutable real-model benchmark captures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_real_multi_agent_benchmark as benchmark


def test_score_only_reads_the_captured_sut_source_binding(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    expected = {
        "sut_source_sha256": "c" * 64,
        "sut_source_files": ["app/main.py", "app/gateway/routes.py"],
    }
    report_path.write_text(json.dumps(expected), encoding="utf-8")

    assert benchmark._load_captured_sut_source_manifest(report_path) == (
        expected["sut_source_sha256"],
        tuple(expected["sut_source_files"]),
    )


def test_score_only_preserves_the_captured_source_binding_when_writing_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_source = ("c" * 64, ("app/main.py", "app/gateway/routes.py"))
    monkeypatch.setattr(benchmark, "load_corpus", lambda _path: object())
    monkeypatch.setattr(
        benchmark,
        "score_artifact",
        lambda **_kwargs: {
            "sut_source_sha256": "f" * 64,
            "sut_source_files": ["app/current.py"],
        },
    )
    monkeypatch.setattr(benchmark, "_render_report", lambda _report: "report\n")

    output_directory = tmp_path / "rescored"
    benchmark._write_outputs(
        artifact={"results": []},
        corpus_path=tmp_path / "cases.json",
        output_directory=output_directory,
        captured_sut_source_manifest=captured_source,
    )

    report = json.loads((output_directory / "report.json").read_text("utf-8"))
    assert report["sut_source_sha256"] == captured_source[0]
    assert report["sut_source_files"] == list(captured_source[1])


def test_score_only_rejects_a_report_without_a_valid_captured_source_binding(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"sut_source_sha256": "invalid", "sut_source_files": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid captured SUT source binding"):
        benchmark._load_captured_sut_source_manifest(report_path)
