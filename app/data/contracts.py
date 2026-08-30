"""Typed contracts emitted by public dataset preparation jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

DataProfile = Literal["test", "eval", "full"]


class NormalizedProduct(BaseModel):
    """Book facts normalized from the pinned Tiki Books archive."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    external_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=220)
    authors: list[str] = Field(default_factory=list)
    publisher: str | None = Field(default=None, max_length=220)
    category: str = Field(min_length=1, max_length=120)
    page_count: int | None = Field(default=None, ge=1, le=20_000)
    price_vnd: int = Field(ge=0, le=1_000_000_000)
    original_price_vnd: int | None = Field(default=None, ge=0, le=1_000_000_000)
    rating: float | None = Field(default=None, ge=0, le=5)
    source_popularity: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("source_popularity", "sold_count"),
    )
    source_review_count: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("source_review_count", "review_count"),
    )
    cover_url: str | None = Field(default=None, max_length=2_000)
    description: str = Field(min_length=1, max_length=4_000)
    platform: Literal["Tiki"] = "Tiki"
    seller_name: str | None = Field(default=None, max_length=160)
    source_metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )

    @property
    def sold_count(self) -> int:
        """Compatibility projection for the pre-book importer."""

        return self.source_popularity or 0

    @property
    def review_count(self) -> int:
        """Compatibility projection for pre-book callers."""

        return self.source_review_count


class NormalizedReview(BaseModel):
    """Review facts normalized without retaining customer identity."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=257)
    product_external_id: str = Field(min_length=1, max_length=128)
    rating: int = Field(ge=1, le=5)
    title: str | None = Field(default=None, max_length=1_000)
    content: str = Field(min_length=1, max_length=20_000)
    helpful_count: int = Field(default=0, ge=0)
    created_at: datetime | None = None
    source_metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )


class DatasetManifest(BaseModel):
    """Deterministic provenance and artifact identity for one snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    dataset_id: Literal["tiki-books"] = "tiki-books"
    dataset_version: str = Field(min_length=1, max_length=100)
    profile: DataProfile
    cleaner_version: str = Field(min_length=1, max_length=100)
    config_version: str = Field(min_length=1, max_length=100)
    taxonomy_version: str = Field(min_length=1, max_length=100)
    source_url: str = Field(min_length=1, max_length=500)
    source_license: str = Field(min_length=1, max_length=200)
    source_revision: int = Field(ge=1)
    raw_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_archive_bytes: int = Field(ge=1)
    retrieved_at: datetime
    sampling_seed: int
    sampling_policy: str = Field(min_length=1, max_length=500)
    source_files: list[str] = Field(min_length=1)
    required_product_external_ids: list[str] = Field(default_factory=list)
    product_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    products_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviews_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DataQualityReport(BaseModel):
    """PII-free evidence emitted by the deterministic quality gate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["pass", "fail"]
    profile: DataProfile
    cleaner_version: str = Field(min_length=1, max_length=100)
    config_version: str = Field(min_length=1, max_length=100)
    taxonomy_version: str = Field(min_length=1, max_length=100)
    raw_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_counts: dict[str, int]
    output_counts: dict[str, int]
    correction_counts: dict[str, int]
    drop_counts: dict[str, int]
    redaction_counts: dict[str, int]
    field_coverage: dict[str, float]
    output_hashes: dict[str, str]
    gate_failures: list[str] = Field(default_factory=list)
