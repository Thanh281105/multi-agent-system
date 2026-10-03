"""Book eligibility shared by historical reads and sandbox offer seeding."""

from sqlalchemy import String, and_, cast, or_
from sqlalchemy.sql.elements import ColumnElement

from app.models.product import Product

_NON_BOOK_CATEGORIES = (
    "sữa",
    "điện thoại",
    "laptop",
    "máy tính bảng",
    "tai nghe",
    "tivi",
    "tủ lạnh",
    "máy giặt",
)
_BOOK_CATEGORY_PREFIXES = ("sách", "truyện", "book")


def is_book_product(product: Product) -> bool:
    category = product.category.strip().casefold()
    return not any(
        category.startswith(value) for value in _NON_BOOK_CATEGORIES
    ) and bool(
        product.page_count is not None
        or product.authors
        or (product.publisher and product.publisher.strip())
        or category.startswith(_BOOK_CATEGORY_PREFIXES)
    )


def book_product_filter() -> ColumnElement[bool]:
    return and_(
        *(~Product.category.ilike(f"{prefix}%") for prefix in _NON_BOOK_CATEGORIES),
        or_(
            Product.page_count.is_not(None),
            cast(Product.authors, String).not_in(("[]", "null")),
            and_(Product.publisher.is_not(None), Product.publisher != ""),
            *(
                Product.category.ilike(f"{prefix}%")
                for prefix in _BOOK_CATEGORY_PREFIXES
            ),
        ),
    )
