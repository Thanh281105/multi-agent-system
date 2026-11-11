"""Export an existing, complete v3 report bundle with its source evidence.

No models are called and no benchmark measurements are manufactured. For a
damaged legacy bundle, use `python -m app.evaluation.evidence_integrity` first.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.evaluation.verified_reporting import export_verified_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        result = export_verified_report(arguments.bundle, arguments.output)
    except (OSError, ValueError):
        parser.exit(
            1,
            "Evidence export refused: invalid/incomplete source artifacts or an "
            "existing output. Audit the source manifest and choose a new output.\n",
        )
    print(f"Exported verified source artifacts for run {result['run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
