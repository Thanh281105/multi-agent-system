"""Versioned public-data ingestion helpers."""

from app.data.contracts import (
    DatasetManifest,
    NormalizedProduct,
    NormalizedReview,
)
from app.data.tiki_books import (
    DEFAULT_ARCHIVE_SHA256,
    DEFAULT_DATASET_VERSION,
    DEFAULT_SOURCE_URL,
    download_archive,
    prepare_snapshot,
)

__all__ = [
    "DEFAULT_ARCHIVE_SHA256",
    "DEFAULT_DATASET_VERSION",
    "DEFAULT_SOURCE_URL",
    "DatasetManifest",
    "NormalizedProduct",
    "NormalizedReview",
    "download_archive",
    "prepare_snapshot",
]
