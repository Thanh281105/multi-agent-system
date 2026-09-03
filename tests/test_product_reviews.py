from app.tools.ecommerce import get_product_reviews


def test_product_reviews_belong_to_requested_product() -> None:
    result = get_product_reviews(product_id=1)

    assert result["found"] is True
    assert result["product"]["id"] == 1
    assert result["product"]["name"] == "Nhật Ký Tarot"
    assert result["product"]["publisher"] == "Nhà Xuất Bản Thế Giới"
    assert result["product"]["source_review_count"] == 303
    assert result["count"] == len(result["reviews"])
    assert result["count"] == 5
    assert all(review["product_id"] == 1 for review in result["reviews"])
    assert any(
        "đóng gói" in review["content"].casefold() for review in result["reviews"]
    )
    assert all(
        {"title", "helpful_count", "created_at"}.issubset(review)
        for review in result["reviews"]
    )


def test_product_reviews_report_missing_product_without_inventing_data() -> None:
    result = get_product_reviews(product_id=9999)

    assert result == {
        "found": False,
        "product": None,
        "count": 0,
        "reviews": [],
    }
