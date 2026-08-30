"""Command-line entry points for public dataset preparation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from app.data.contracts import DataProfile
from app.data.quality import SnapshotQualityError, validate_quality_artifacts
from app.data.tiki_books import (
    DatasetPreparationError,
    download_archive,
    prepare_snapshot,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ecommerce-data")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="download a pinned source archive")
    download.add_argument(
        "--output", type=Path, default=Path("data/raw/tiki-books-v4.zip")
    )

    prepare = commands.add_parser(
        "prepare", help="prepare a normalized public snapshot"
    )
    prepare.add_argument(
        "--archive",
        type=Path,
        default=Path("data/raw/tiki-books-v4.zip"),
    )
    prepare.add_argument("--profile", choices=("test", "eval", "full"), required=True)
    prepare.add_argument("--output", type=Path)
    prepare.add_argument("--force", action="store_true")

    validate = commands.add_parser(
        "validate",
        help="verify a prepared snapshot quality report and artifact hashes",
    )
    validate.add_argument("--snapshot", type=Path, required=True)

    import_snapshot = commands.add_parser(
        "import",
        help="import a prepared snapshot into the configured database",
    )
    import_snapshot.add_argument("--snapshot", type=Path, required=True)

    arguments = parser.parse_args()
    try:
        if arguments.command == "download":
            path = download_archive(destination=arguments.output)
            print(f"Downloaded and verified: {path}")
            return
        if arguments.command == "validate":
            manifest, _ = validate_quality_artifacts(arguments.snapshot)
            print(
                "Validated snapshot: "
                f"profile={manifest.profile} products={manifest.product_count} "
                f"reviews={manifest.review_count} sha256={manifest.snapshot_sha256}"
            )
            return
        if arguments.command == "import":
            from app.db.public_import import import_public_snapshot

            counts = import_public_snapshot(snapshot_dir=arguments.snapshot)
            print(
                "Imported snapshot: "
                f"source_id={counts['source_id']} products={counts['products']} "
                f"reviews={counts['reviews']}"
            )
            return
        profile = cast(DataProfile, arguments.profile)
        output = arguments.output or Path(f"data/snapshots/tiki-books-v4-{profile}")
        manifest = prepare_snapshot(
            archive_path=arguments.archive,
            output_dir=output,
            profile=profile,
            force=arguments.force,
        )
        print(
            "Prepared snapshot: "
            f"products={manifest.product_count} reviews={manifest.review_count} "
            f"sha256={manifest.snapshot_sha256}"
        )
    except (DatasetPreparationError, SnapshotQualityError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
