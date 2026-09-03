import pytest

from app.tools.ecommerce import compare_products


def test_compare_products_preserves_requested_product_order() -> None:
    result = compare_products(product_ids=[13, 2])

    assert result["count"] == 2
    assert [product["id"] for product in result["products"]] == [13, 2]
    assert result["missing_product_ids"] == []
    assert result["products"][0]["name"].startswith("Sapiens Lược Sử Loài Người")
    assert result["products"][0]["authors"] == ["Yuval Noah Harari"]
    assert result["products"][0]["page_count"] == 600
    assert result["products"][1]["authors"] == ["Trang Anh"]
    assert result["products"][1]["publisher"] == (
        "Nhà Xuất Bản Đại Học Quốc Gia Hà Nội"
    )


def test_compare_products_limits_number_of_products() -> None:
    with pytest.raises(ValueError, match="at most five"):
        compare_products(product_ids=[1, 2, 3, 4, 5, 6])
