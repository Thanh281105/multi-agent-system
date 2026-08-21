from app.tools.ecommerce import search_products


def test_search_products_applies_category_price_and_rating_filters() -> None:
    result = search_products(
        category="Tai nghe",
        max_price=1_000_000,
        min_rating=4.5,
    )

    assert result["count"] >= 2
    assert result["count"] == len(result["products"])
    assert all(product["category"] == "Tai nghe" for product in result["products"])
    assert all(product["price"] <= 1_000_000 for product in result["products"])
    assert all(product["rating"] >= 4.5 for product in result["products"])


def test_search_products_category_filter_is_case_insensitive() -> None:
    result = search_products(
        category="tai nghe",
        max_price=1_000_000,
        min_rating=4.5,
    )

    assert result["count"] == 2
    assert all(product["category"] == "Tai nghe" for product in result["products"])


def test_search_products_can_resolve_seeded_product_name() -> None:
    result = search_products(query="Nova Air S2")

    assert result["count"] == 1
    assert result["products"][0]["id"] == 1
    assert result["products"][0]["name"] == "Tai nghe Bluetooth Nova Air S2"


def test_search_products_returns_no_facts_for_unknown_product() -> None:
    result = search_products(query="SuperDragon X999")

    assert result == {"count": 0, "products": []}
