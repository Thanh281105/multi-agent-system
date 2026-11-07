"""Export a measured comparison from a complete, validated v3 evidence bundle.

This compatibility entry point does not execute an ablation. Comparing two
frozen variants does not isolate one component unless the experiment controls
all other differences. No causal or statistical superiority is inferred here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.evaluation.v3_models import PACKAGE7_VARIANT_ORDER  # noqa: E402
from app.evaluation.verified_reporting import export_verified_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--baseline", choices=PACKAGE7_VARIANT_ORDER, required=True)
    parser.add_argument("--candidate", choices=PACKAGE7_VARIANT_ORDER, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        export_verified_report(
            arguments.bundle,
            arguments.output,
            baseline=arguments.baseline,
            candidate=arguments.candidate,
        )
    except (OSError, ValueError):
        parser.exit(
            1,
            "Comparison export refused: invalid/incomplete evidence, absent "
            "comparison, or an existing output. Audit the source manifest.\n",
        )
    print("Exported source comparison; isolated attribution is not established")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
