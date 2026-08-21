from app.tools.ecommerce import get_product_reviews


def test_product_reviews_belong_to_requested_product() -> None:
    result = get_product_reviews(product_id=1)

    assert result["found"] is True
    assert result["product"]["id"] == 1
    assert result["product"]["name"] == "Tai nghe Bluetooth Nova Air S2"
    assert result["count"] == len(result["reviews"])
    assert result["count"] == 5
    assert all(review["product_id"] == 1 for review in result["reviews"])
    assert any("đau tai" in review["content"] for review in result["reviews"])


def test_product_reviews_report_missing_product_without_inventing_data() -> None:
    result = get_product_reviews(product_id=9999)

    assert result == {
        "found": False,
        "product": None,
        "count": 0,
        "reviews": [],
    }
