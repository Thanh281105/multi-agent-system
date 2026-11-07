"""Summarize submitted ratings; the former embedded simulation is retired.

Requires a hashed v3 blind packet and explicit ratings. This command cannot
attest that labels were collected from humans or create a P8 calibration freeze.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.evaluation.human_calibration import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
