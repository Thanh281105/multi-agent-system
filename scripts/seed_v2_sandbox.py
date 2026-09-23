"""Seed the isolated v2 demo offers from an existing database snapshot."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.v2.sandbox_seed import seed_demo_offers


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="SQLAlchemy URL for the already-migrated database",
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--store-id", default="demo")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    try:
        report = seed_demo_offers(
            sessions,
            tenant_id=args.tenant_id,
            store_id=args.store_id,
        )
    finally:
        engine.dispose()
    print(json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
