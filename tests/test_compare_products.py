import pytest

from app.tools.ecommerce import compare_products


def test_compare_products_preserves_requested_product_order() -> None:
    result = compare_products(product_ids=[2, 1])

    assert result["count"] == 2
    assert [product["id"] for product in result["products"]] == [2, 1]
    assert result["missing_product_ids"] == []
    assert result["products"][0]["name"] == "Tai nghe Gaming Sonic G5"
    assert result["products"][1]["name"] == "Tai nghe Bluetooth Nova Air S2"


def test_compare_products_limits_number_of_products() -> None:
    with pytest.raises(ValueError, match="at most five"):
        compare_products(product_ids=[1, 2, 3, 4, 5, 6])
