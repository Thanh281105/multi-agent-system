"""Command-line entry points for public dataset preparation."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.data.tiki_books import (
    DEFAULT_ARCHIVE_SHA256,
    DEFAULT_DATASET_VERSION,
    DEFAULT_SOURCE_URL,
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
    download.add_argument("--url", default=DEFAULT_SOURCE_URL)
    download.add_argument("--expected-sha256", default=DEFAULT_ARCHIVE_SHA256)

    prepare = commands.add_parser(
        "prepare", help="prepare a normalized public snapshot"
    )
    prepare.add_argument("--archive", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--products", type=int, default=200)
    prepare.add_argument("--reviews-per-product", type=int, default=10)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--dataset-version", default=DEFAULT_DATASET_VERSION)
    prepare.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    prepare.add_argument("--force", action="store_true")

    import_snapshot = commands.add_parser(
        "import",
        help="import a prepared snapshot into the configured database",
    )
    import_snapshot.add_argument("--snapshot", type=Path, required=True)

    arguments = parser.parse_args()
    try:
        if arguments.command == "download":
            path = download_archive(
                destination=arguments.output,
                url=arguments.url,
                expected_sha256=arguments.expected_sha256,
            )
            print(f"Downloaded and verified: {path}")
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
        manifest = prepare_snapshot(
            archive_path=arguments.archive,
            output_dir=arguments.output,
            target_products=arguments.products,
            max_reviews_per_product=arguments.reviews_per_product,
            seed=arguments.seed,
            force=arguments.force,
            source_url=arguments.source_url,
            dataset_version=arguments.dataset_version,
        )
        print(
            "Prepared snapshot: "
            f"products={manifest.product_count} reviews={manifest.review_count} "
            f"sha256={manifest.snapshot_sha256}"
        )
    except DatasetPreparationError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
