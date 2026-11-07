"""Retired ad-hoc runner; original source is archived for audit only.

Use the guarded evaluation CLIs documented in docs/evidence-reporting.md.
This entry point performs no provider, database, checkpoint or label mutation.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    parser.exit(
        2,
        "Retired runner: use python -m app.evaluation.v3_cli or "
        "python -m app.evaluation.benchmark_cli with explicit inputs and budgets. "
        "Original source: docs/legacy-scripts/"
        "run_full_p7_p8_benchmark.py.txt (audit only).\n",
    )


if __name__ == "__main__":
    raise SystemExit(main())
