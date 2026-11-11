"""Compare synthetic development regressions, never publish benchmark accuracy."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_CHILD = """
import pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import app
assert pathlib.Path(app.__file__).resolve() == root / 'app' / '__init__.py'
print('APP_SOURCE=' + str(root), flush=True)
import pytest
sys.exit(pytest.main(sys.argv[2:]))
"""


def _run(source: Path, output: Path, tests: Path, label: str) -> dict[str, object]:
    if not (source / "app/__init__.py").is_file():
        raise ValueError("source must contain the application and its test helpers")
    junit = output / f"{label}.xml"
    env = {**os.environ, "OPENAI_API_KEY": "", "PYTHONIOENCODING": "utf-8"}
    argv = [
        sys.executable,
        "-c",
        _CHILD,
        str(source),
        "--import-mode=importlib",
        f"--rootdir={source}",
        str(tests),
        "-q",
        "--tb=short",
        f"--junitxml={junit}",
        f"--basetemp={output / (label + '-tmp')}",
    ]
    with (output / f"{label}.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            argv,
            cwd=source,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
    if completed.returncode not in (0, 1) or not junit.is_file():
        raise RuntimeError(f"{label} probe execution failed; inspect its log")
    cases = []
    for case in ET.parse(junit).iter("testcase"):
        status = (
            "error"
            if case.find("error") is not None
            else "failed"
            if case.find("failure") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        cases.append({"id": case.attrib["name"], "status": status})
    return {
        "source": str(source),
        "exit_code": completed.returncode,
        "source_manifest_sha256": hashlib.sha256(
            json.dumps(
                {
                    p.relative_to(source).as_posix(): hashlib.sha256(
                        p.read_bytes()
                    ).hexdigest()
                    for p in sorted((source / "app").rglob("*.py"))
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    tests = source / "tests/test_runtime_quality.py"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary: dict[str, object] = {
        "schema_version": "runtime_quality_probes_v1",
        "study_kind": "synthetic_development_regression_probes",
        "not_a_live_benchmark": True,
        "does_not_estimate_heldout_accuracy": True,
        "probe_sha256": hashlib.sha256(tests.read_bytes()).hexdigest(),
    }
    if args.baseline_source is not None:
        summary["baseline"] = _run(
            args.baseline_source.resolve(), output, tests, "baseline"
        )
    candidate = _run(source, output, tests, "candidate")
    summary["candidate"] = candidate
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if candidate["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
