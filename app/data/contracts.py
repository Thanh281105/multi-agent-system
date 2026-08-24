"""Typed contracts emitted by public dataset preparation jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NormalizedProduct(BaseModel):
    """Product facts normalized from an external source."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=220)
    category: str = Field(min_length=1, max_length=80)
    price_vnd: int = Field(ge=0)
    original_price_vnd: int = Field(ge=0)
    rating: float = Field(ge=0, le=5)
    sold_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=4_000)
    platform: Literal["Tiki"] = "Tiki"
    seller_name: str = Field(min_length=1, max_length=160)
    source_metadata: dict[str, str | int | float] = Field(default_factory=dict)


class NormalizedReview(BaseModel):
    """Review facts normalized without retaining unnecessary customer identity."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=128)
    product_external_id: str = Field(min_length=1, max_length=128)
    rating: int = Field(ge=1, le=5)
    content: str = Field(min_length=1, max_length=20_000)
    helpful_count: int = Field(default=0, ge=0)
    created_at: datetime | None = None
    source_metadata: dict[str, str | int | float] = Field(default_factory=dict)


class DatasetManifest(BaseModel):
    """Reproducibility metadata for one prepared dataset snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    dataset_id: str = Field(min_length=1, max_length=100)
    dataset_version: str = Field(min_length=1, max_length=100)
    source_url: str = Field(min_length=1, max_length=500)
    source_license: str = Field(min_length=1, max_length=200)
    source_revision: int = Field(ge=1)
    raw_archive_sha256: str = Field(min_length=64, max_length=64)
    raw_archive_bytes: int = Field(ge=1)
    retrieved_at: datetime
    sampling_seed: int
    sampling_policy: str = Field(min_length=1, max_length=500)
    source_files: list[str] = Field(min_length=1)
    duplicate_product_rows: int = Field(ge=0)
    skipped_product_rows: int = Field(ge=0)
    skipped_review_rows: int = Field(ge=0)
    product_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    snapshot_sha256: str = Field(min_length=64, max_length=64)
