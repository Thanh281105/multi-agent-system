import inspect

import pytest

from app.tools.ecommerce import search_products


def test_search_products_applies_author_publisher_and_page_filters() -> None:
    result = search_products(
        author="Trang Anh",
        publisher="đại học quốc gia hà nội",
        min_page_count=600,
        max_page_count=610,
    )

    assert result["count"] == 1
    product = result["products"][0]
    assert product["id"] == 2
    assert product["authors"] == ["Trang Anh"]
    assert product["publisher"] == "Nhà Xuất Bản Đại Học Quốc Gia Hà Nội"
    assert product["page_count"] == 606
    assert product["platform"] == "Tiki"


def test_search_products_publisher_filter_is_case_insensitive() -> None:
    result = search_products(publisher="nhà xuất bản thế giới")

    assert result["count"] == 6
    assert all(
        product["publisher"] == "Nhà Xuất Bản Thế Giới"
        for product in result["products"]
    )


def test_search_products_applies_category_price_and_rating_filters() -> None:
    result = search_products(
        category="lập trình",
        max_price=100_000,
        min_rating=4.5,
    )

    assert result["count"] == 1
    assert result["products"][0]["id"] == 15
    assert result["products"][0]["category"] == "Lập Trình"


def test_search_products_can_resolve_snapshot_book_title() -> None:
    result = search_products(query="Sapiens")

    assert result["count"] == 1
    assert result["products"][0]["id"] == 13
    assert result["products"][0]["name"].startswith("Sapiens Lược Sử Loài Người")
    assert result["products"][0]["page_count"] == 600


def test_search_products_free_text_matches_author() -> None:
    result = search_products(query="Louie Stowell")

    assert result["count"] == 1
    assert result["products"][0]["id"] == 7


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({"min_page_count": 0}, "min_page_count"),
        ({"max_page_count": True}, "max_page_count"),
        ({"max_page_count": 20_001}, "max_page_count"),
        (
            {"min_page_count": 700, "max_page_count": 300},
            "min_page_count cannot exceed max_page_count",
        ),
    ],
)
def test_search_products_rejects_invalid_page_filters(
    filters: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        search_products(**filters)  # type: ignore[arg-type]


def test_search_contract_does_not_accept_a_platform_filter() -> None:
    assert "platform" not in inspect.signature(search_products).parameters


def test_search_products_returns_no_facts_for_unknown_product() -> None:
    result = search_products(query="Sách SuperDragon X999")

    assert result == {"count": 0, "products": []}
