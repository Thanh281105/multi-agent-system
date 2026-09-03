"""Versioned, deterministic Tiki Books preparation profiles."""

from __future__ import annotations

from dataclasses import dataclass

from app.data.contracts import DataProfile

CONFIG_VERSION = "tiki-books-profiles-1.0.0"
DEFAULT_SAMPLING_SEED = 42

_TEST_REQUIRED_PRODUCT_IDS = (
    "105778970",
    "106185389",
    "109298270",
    "113583126",
    "113656455",
    "114228620",
    "116835635",
    "130996084",
    "147999778",
    "1667307",
    "169262882",
    "171057415",
    "184419723",
    "185071145",
    "189765038",
    "190707002",
    "193226972",
    "19588948",
    "19918848",
    "204649516",
    "46755109",
    "47273263",
    "74488127",
    "91277007",
)


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Frozen sampling policy for one reproducible snapshot profile."""

    name: DataProfile
    target_products: int | None
    max_reviews_per_product: int | None
    target_reviews: int | None = None
    required_product_external_ids: tuple[str, ...] = ()

    @property
    def sampling_policy(self) -> str:
        if self.name == "full":
            return (
                "All valid products and reviews after deterministic cleaning; "
                "no profile sampling."
            )
        global_cap = (
            f"; globally capped at {self.target_reviews} reviews"
            if self.target_reviews is not None
            else ""
        )
        return (
            "Stable SHA-256 ranking with category round-robin product coverage; "
            f"at most {self.max_reviews_per_product} reviews per product, "
            f"stratified by rating after cleaning{global_cap}."
        )


PROFILE_CONFIGS: dict[DataProfile, ProfileConfig] = {
    "test": ProfileConfig(
        name="test",
        target_products=24,
        max_reviews_per_product=5,
        required_product_external_ids=_TEST_REQUIRED_PRODUCT_IDS,
    ),
    "eval": ProfileConfig(
        name="eval",
        target_products=200,
        max_reviews_per_product=10,
        target_reviews=1_773,
        required_product_external_ids=_TEST_REQUIRED_PRODUCT_IDS,
    ),
    "full": ProfileConfig(
        name="full",
        target_products=None,
        max_reviews_per_product=None,
    ),
}


def get_profile_config(profile: DataProfile) -> ProfileConfig:
    """Return the immutable configuration for a supported profile."""

    return PROFILE_CONFIGS[profile]
