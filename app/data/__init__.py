"""Versioned public-data ingestion helpers."""

from app.data.contracts import (
    DataProfile,
    DataQualityReport,
    DatasetManifest,
    NormalizedProduct,
    NormalizedReview,
)
from app.data.quality import validate_quality_artifacts
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
    "DataProfile",
    "DataQualityReport",
    "DatasetManifest",
    "NormalizedProduct",
    "NormalizedReview",
    "download_archive",
    "prepare_snapshot",
    "validate_quality_artifacts",
]
