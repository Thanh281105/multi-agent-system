"""Deterministic synthetic Vietnamese e-commerce dataset."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models.product import Product
from app.models.review import Review
from app.models.shop import Shop

SHOPS: list[dict[str, Any]] = [
    {"id": 1, "name": "Nova Store", "platform": "Shopee", "rating": 4.8},
    {"id": 2, "name": "Tiki Tech Hub", "platform": "Tiki", "rating": 4.7},
    {"id": 3, "name": "LazMall Home", "platform": "Lazada", "rating": 4.6},
    {"id": 4, "name": "Beauty Corner", "platform": "Shopee", "rating": 4.9},
    {"id": 5, "name": "Gia Dụng Xanh", "platform": "Tiki", "rating": 4.5},
]

PRODUCTS: list[dict[str, Any]] = [
    {
        "id": 1,
        "shop_id": 1,
        "name": "Tai nghe Bluetooth Nova Air S2",
        "category": "Tai nghe",
        "price": 799_000,
        "original_price": 999_000,
        "rating": 4.7,
        "sold_count": 2_450,
        "description": "Tai nghe không dây gọn nhẹ, âm thanh cân bằng, pin 28 giờ.",
        "platform": "Shopee",
    },
    {
        "id": 2,
        "shop_id": 2,
        "name": "Tai nghe Gaming Sonic G5",
        "category": "Tai nghe",
        "price": 699_000,
        "original_price": 899_000,
        "rating": 4.5,
        "sold_count": 3_100,
        "description": "Tai nghe gaming có micro rõ, âm bass mạnh và đèn RGB.",
        "platform": "Tiki",
    },
    {
        "id": 3,
        "shop_id": 1,
        "name": "Tai nghe không dây Echo Buds Pro",
        "category": "Tai nghe",
        "price": 1_290_000,
        "original_price": 1_590_000,
        "rating": 4.6,
        "sold_count": 1_820,
        "description": "Tai nghe chống ồn chủ động, hộp sạc USB-C và âm trường rộng.",
        "platform": "Shopee",
    },
    {
        "id": 4,
        "shop_id": 3,
        "name": "Tai nghe chống ồn QuietMax Q1",
        "category": "Tai nghe",
        "price": 899_000,
        "original_price": 1_099_000,
        "rating": 4.4,
        "sold_count": 980,
        "description": "Tai nghe chụp tai có chống ồn cơ bản và đệm tai mềm.",
        "platform": "Lazada",
    },
    {
        "id": 5,
        "shop_id": 2,
        "name": "Tai nghe thể thao FitBeat Mini",
        "category": "Tai nghe",
        "price": 499_000,
        "original_price": 649_000,
        "rating": 4.2,
        "sold_count": 4_250,
        "description": "Tai nghe in-ear chống mồ hôi, phù hợp chạy bộ và tập gym.",
        "platform": "Tiki",
    },
    {
        "id": 6,
        "shop_id": 1,
        "name": "Điện thoại Nova X5 5G",
        "category": "Điện thoại",
        "price": 6_990_000,
        "original_price": 7_490_000,
        "rating": 4.6,
        "sold_count": 1_980,
        "description": "Điện thoại 5G màn hình AMOLED, camera 50MP và pin 5.000mAh.",
        "platform": "Shopee",
    },
    {
        "id": 7,
        "shop_id": 2,
        "name": "Điện thoại Tiki One Lite",
        "category": "Điện thoại",
        "price": 4_290_000,
        "original_price": 4_790_000,
        "rating": 4.3,
        "sold_count": 2_650,
        "description": "Điện thoại phổ thông hiệu năng ổn định, màn hình 6.5 inch.",
        "platform": "Tiki",
    },
    {
        "id": 8,
        "shop_id": 3,
        "name": "Lazada Pixel M12",
        "category": "Điện thoại",
        "price": 8_490_000,
        "original_price": 8_990_000,
        "rating": 4.5,
        "sold_count": 1_120,
        "description": "Điện thoại camera kép, sạc nhanh và thiết kế kính tối giản.",
        "platform": "Lazada",
    },
    {
        "id": 9,
        "shop_id": 4,
        "name": "Beauty Phone S10",
        "category": "Điện thoại",
        "price": 5_490_000,
        "original_price": 5_990_000,
        "rating": 4.1,
        "sold_count": 760,
        "description": "Điện thoại selfie với bộ lọc chân dung và bộ nhớ 256GB.",
        "platform": "Shopee",
    },
    {
        "id": 10,
        "shop_id": 5,
        "name": "Gia Dụng Mobile A3",
        "category": "Điện thoại",
        "price": 3_190_000,
        "original_price": 3_490_000,
        "rating": 4.0,
        "sold_count": 540,
        "description": "Điện thoại cơ bản cho nhu cầu liên lạc và giải trí hằng ngày.",
        "platform": "Tiki",
    },
    {
        "id": 11,
        "shop_id": 2,
        "name": "Laptop NovaBook Air 14",
        "category": "Laptop",
        "price": 15_990_000,
        "original_price": 17_490_000,
        "rating": 4.8,
        "sold_count": 630,
        "description": "Laptop mỏng nhẹ cho học tập và văn phòng, màn hình 14 inch.",
        "platform": "Tiki",
    },
    {
        "id": 12,
        "shop_id": 1,
        "name": "Laptop Sonic Pro 15",
        "category": "Laptop",
        "price": 22_490_000,
        "original_price": 24_990_000,
        "rating": 4.6,
        "sold_count": 420,
        "description": "Laptop hiệu năng cao với 16GB RAM và SSD 1TB cho lập trình.",
        "platform": "Shopee",
    },
    {
        "id": 13,
        "shop_id": 3,
        "name": "LazBook Student 13",
        "category": "Laptop",
        "price": 11_990_000,
        "original_price": 13_490_000,
        "rating": 4.4,
        "sold_count": 350,
        "description": (
            "Laptop sinh viên pin tốt, bàn phím tiện dụng và trọng lượng nhẹ."
        ),
        "platform": "Lazada",
    },
    {
        "id": 14,
        "shop_id": 4,
        "name": "Laptop Beauty Creator 16",
        "category": "Laptop",
        "price": 28_990_000,
        "original_price": 31_990_000,
        "rating": 4.7,
        "sold_count": 210,
        "description": "Laptop màn hình màu đẹp cho thiết kế và chỉnh sửa video.",
        "platform": "Shopee",
    },
    {
        "id": 15,
        "shop_id": 5,
        "name": "Laptop GreenOffice 14",
        "category": "Laptop",
        "price": 13_490_000,
        "original_price": 14_490_000,
        "rating": 4.2,
        "sold_count": 290,
        "description": "Laptop văn phòng tiết kiệm điện, đủ dùng cho công việc cơ bản.",
        "platform": "Tiki",
    },
    {
        "id": 16,
        "shop_id": 4,
        "name": "Kem chống nắng SunGlow SPF50",
        "category": "Mỹ phẩm",
        "price": 189_000,
        "original_price": 249_000,
        "rating": 4.8,
        "sold_count": 8_900,
        "description": "Kem chống nắng không nâng tông quá mức, phù hợp da dầu.",
        "platform": "Shopee",
    },
    {
        "id": 17,
        "shop_id": 4,
        "name": "Serum cấp ẩm AquaLeaf",
        "category": "Mỹ phẩm",
        "price": 329_000,
        "original_price": 399_000,
        "rating": 4.6,
        "sold_count": 6_500,
        "description": "Serum cấp ẩm nhẹ với kết cấu thấm nhanh cho da thường.",
        "platform": "Shopee",
    },
    {
        "id": 18,
        "shop_id": 1,
        "name": "Son lì Velvet Rose",
        "category": "Mỹ phẩm",
        "price": 249_000,
        "original_price": 299_000,
        "rating": 4.5,
        "sold_count": 4_800,
        "description": "Son lì màu hồng đất, chất son mịn và dễ tán.",
        "platform": "Shopee",
    },
    {
        "id": 19,
        "shop_id": 2,
        "name": "Sữa rửa mặt ClearMint",
        "category": "Mỹ phẩm",
        "price": 159_000,
        "original_price": 199_000,
        "rating": 4.1,
        "sold_count": 3_200,
        "description": "Sữa rửa mặt làm sạch dịu, dùng được cho da hỗn hợp.",
        "platform": "Tiki",
    },
    {
        "id": 20,
        "shop_id": 3,
        "name": "Mặt nạ đất sét PureClay",
        "category": "Mỹ phẩm",
        "price": 219_000,
        "original_price": 279_000,
        "rating": 4.3,
        "sold_count": 2_100,
        "description": "Mặt nạ đất sét hỗ trợ làm sạch vùng chữ T và bã nhờn.",
        "platform": "Lazada",
    },
    {
        "id": 21,
        "shop_id": 5,
        "name": "Nồi chiên HomeChef 6L",
        "category": "Đồ gia dụng",
        "price": 1_590_000,
        "original_price": 1_990_000,
        "rating": 4.7,
        "sold_count": 1_760,
        "description": "Nồi chiên không dầu 6L, màn hình cảm ứng và 8 chương trình.",
        "platform": "Tiki",
    },
    {
        "id": 22,
        "shop_id": 3,
        "name": "Máy xay đa năng FreshMix",
        "category": "Đồ gia dụng",
        "price": 890_000,
        "original_price": 1_090_000,
        "rating": 4.4,
        "sold_count": 1_240,
        "description": "Máy xay đa năng cối thủy tinh, phù hợp sinh tố gia đình.",
        "platform": "Lazada",
    },
    {
        "id": 23,
        "shop_id": 5,
        "name": "Ấm siêu tốc GreenKettle 1.7L",
        "category": "Đồ gia dụng",
        "price": 349_000,
        "original_price": 449_000,
        "rating": 4.2,
        "sold_count": 2_980,
        "description": "Ấm siêu tốc inox 1.7L, tự ngắt khi sôi và dễ vệ sinh.",
        "platform": "Tiki",
    },
    {
        "id": 24,
        "shop_id": 1,
        "name": "Robot hút bụi NovaClean R2",
        "category": "Đồ gia dụng",
        "price": 4_990_000,
        "original_price": 5_990_000,
        "rating": 4.5,
        "sold_count": 460,
        "description": (
            "Robot hút bụi lau nhà có bản đồ phòng và điều khiển qua ứng dụng."
        ),
        "platform": "Shopee",
    },
    {
        "id": 25,
        "shop_id": 4,
        "name": "Đèn bàn học BrightDesk",
        "category": "Đồ gia dụng",
        "price": 279_000,
        "original_price": 359_000,
        "rating": 4.6,
        "sold_count": 2_300,
        "description": "Đèn bàn LED có ba chế độ sáng và cổ đèn điều chỉnh được.",
        "platform": "Shopee",
    },
    {
        "id": 26,
        "shop_id": 2,
        "name": "Sạc nhanh VoltPro 65W",
        "category": "Phụ kiện",
        "price": 449_000,
        "original_price": 599_000,
        "rating": 4.7,
        "sold_count": 5_400,
        "description": "Củ sạc GaN 65W hai cổng, hỗ trợ laptop và điện thoại.",
        "platform": "Tiki",
    },
    {
        "id": 27,
        "shop_id": 1,
        "name": "Bàn phím cơ NovaKey K87",
        "category": "Phụ kiện",
        "price": 799_000,
        "original_price": 999_000,
        "rating": 4.5,
        "sold_count": 2_780,
        "description": "Bàn phím cơ layout 87 phím, switch êm và kết nối USB-C.",
        "platform": "Shopee",
    },
    {
        "id": 28,
        "shop_id": 3,
        "name": "Chuột không dây Glide M3",
        "category": "Phụ kiện",
        "price": 299_000,
        "original_price": 399_000,
        "rating": 4.3,
        "sold_count": 3_650,
        "description": (
            "Chuột không dây công thái học, dùng pin lâu và theo dõi chính xác."
        ),
        "platform": "Lazada",
    },
    {
        "id": 29,
        "shop_id": 5,
        "name": "Giá đỡ laptop FoldStand",
        "category": "Phụ kiện",
        "price": 259_000,
        "original_price": 329_000,
        "rating": 4.4,
        "sold_count": 1_850,
        "description": (
            "Giá đỡ laptop gấp gọn bằng hợp kim nhôm, có thể điều chỉnh độ cao."
        ),
        "platform": "Tiki",
    },
    {
        "id": 30,
        "shop_id": 4,
        "name": "Webcam StreamCam FHD",
        "category": "Phụ kiện",
        "price": 649_000,
        "original_price": 799_000,
        "rating": 4.2,
        "sold_count": 1_090,
        "description": "Webcam Full HD có micro kép cho học online và họp trực tuyến.",
        "platform": "Shopee",
    },
]

IMPORTANT_REVIEW_PROFILES: dict[int, list[tuple[int, str]]] = {
    1: [
        (5, "Giao hàng nhanh, đóng gói kỹ, âm thanh tốt."),
        (5, "Pin dùng lâu, kết nối ổn định và hàng giống mô tả."),
        (4, "Âm bass tốt nhưng đeo lâu hơi đau tai."),
        (5, "Shop tư vấn nhiệt tình, giá hợp lý."),
        (3, "Pin hơi yếu khi bật âm lượng lớn, nhưng chất âm khá ổn."),
    ],
    2: [
        (5, "Âm bass mạnh, micro rõ, chơi game nghe bước chân tốt."),
        (4, "Đèn đẹp nhưng phần mềm điều khiển hơi khó dùng."),
        (4, "Đeo chắc tai, dây dài vừa phải và giao hàng nhanh."),
        (5, "Giá tốt trong tầm tiền, đóng gói cẩn thận."),
        (4, "Âm lượng lớn nhưng đệm tai hơi nóng khi dùng lâu."),
    ],
    3: [
        (5, "Chống ồn hiệu quả, âm trường rộng và hộp sạc đẹp."),
        (5, "Kết nối nhanh, nghe podcast rất rõ."),
        (4, "Âm thanh tốt nhưng giá hơi cao."),
        (5, "Đóng gói chắc chắn, hàng đúng như mô tả."),
        (4, "Ứng dụng đôi lúc kết nối chậm với điện thoại."),
    ],
    4: [
        (5, "Đeo êm, chống ồn vừa đủ cho văn phòng."),
        (4, "Âm thanh ổn nhưng bass chưa thật sâu."),
        (4, "Giao hàng nhanh, sản phẩm đúng mô tả."),
        (3, "Đóng gói móp hộp nhưng tai nghe không bị ảnh hưởng."),
        (4, "Pin tốt, phù hợp nghe nhạc hằng ngày."),
    ],
    5: [
        (5, "Nhẹ và bám tai khi chạy bộ, giá rất ổn."),
        (4, "Âm thanh đủ dùng nhưng âm lượng chưa thật lớn."),
        (4, "Giao nhanh, đóng gói tốt."),
        (4, "Pin ổn trong các buổi tập ngắn."),
        (3, "Micro hơi nhỏ khi gọi ngoài đường."),
    ],
}

GENERIC_REVIEW_TEMPLATES: dict[str, list[str]] = {
    "Điện thoại": [
        "Hàng giống mô tả, màn hình đẹp và giao hàng nhanh.",
        "Pin dùng ổn trong một ngày, giá hợp lý.",
        "Đóng gói kỹ, shop phản hồi nhanh.",
        "Camera đủ dùng nhưng chụp tối chưa thật tốt.",
        "Máy chạy mượt, chưa gặp lỗi sau vài tuần sử dụng.",
    ],
    "Laptop": [
        "Máy chạy nhanh, đóng gói chắc chắn và giao đúng hẹn.",
        "Màn hình đẹp, bàn phím gõ dễ chịu.",
        "Pin dùng ổn nhưng sạc hơi nóng.",
        "Shop tư vấn rõ ràng, sản phẩm đúng cấu hình.",
        "Quạt hơi ồn khi chạy tác vụ nặng.",
    ],
    "Mỹ phẩm": [
        "Đóng gói kỹ, sản phẩm mới và dùng khá thích.",
        "Chất kem dễ tán, giao hàng nhanh.",
        "Mùi hơi nồng nhưng hiệu quả ổn.",
        "Hàng giống mô tả, giá hợp lý.",
        "Da hợp sản phẩm, chưa thấy kích ứng.",
    ],
    "Đồ gia dụng": [
        "Sản phẩm chắc chắn, hướng dẫn sử dụng dễ hiểu.",
        "Đóng gói kỹ, giao hàng đúng hẹn.",
        "Dùng ổn trong gia đình, giá hợp lý.",
        "Máy hơi ồn nhưng hoạt động tốt.",
        "Shop hỗ trợ nhanh khi cần hỏi thêm.",
    ],
    "Phụ kiện": [
        "Hàng giống mô tả, hoàn thiện tốt.",
        "Đóng gói cẩn thận, giao nhanh.",
        "Giá hợp lý và dùng ổn cho nhu cầu hằng ngày.",
        "Thiết kế đẹp nhưng dây hơi ngắn.",
        "Shop phản hồi nhanh, sản phẩm hoạt động tốt.",
    ],
}


def seed_database(
    *,
    db_engine: Engine | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> dict[str, int]:
    """Create tables and replace seed rows with a deterministic dataset."""

    target_engine = db_engine or engine
    target_factory = session_factory or SessionLocal
    Base.metadata.create_all(target_engine)

    random.seed(42)
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    with target_factory.begin() as session:
        session.execute(delete(Review))
        session.execute(delete(Product))
        session.execute(delete(Shop))

        session.add_all(
            [Shop(**shop_data, created_at=base_time) for shop_data in SHOPS]
        )
        session.add_all(
            [
                Product(
                    **product_data,
                    created_at=base_time + timedelta(minutes=product_data["id"]),
                    updated_at=base_time + timedelta(minutes=product_data["id"]),
                )
                for product_data in PRODUCTS
            ]
        )
        session.flush()
        session.add_all(_build_reviews(base_time))

    with target_factory() as session:
        counts = {
            "shops": session.scalar(select(func.count()).select_from(Shop)) or 0,
            "products": session.scalar(select(func.count()).select_from(Product)) or 0,
            "reviews": session.scalar(select(func.count()).select_from(Review)) or 0,
        }
    return counts


def _build_reviews(base_time: datetime) -> list[Review]:
    reviews: list[Review] = []
    review_id = 1
    for product_data in PRODUCTS:
        product_id = int(product_data["id"])
        profile = IMPORTANT_REVIEW_PROFILES.get(product_id)
        if profile is None:
            templates = GENERIC_REVIEW_TEMPLATES[str(product_data["category"])]
            profile = [
                (
                    _review_rating(float(product_data["rating"])),
                    random.choice(templates),
                )
                for _ in range(5)
            ]
        for offset, (rating, content) in enumerate(profile):
            reviews.append(
                Review(
                    id=review_id,
                    product_id=product_id,
                    rating=rating,
                    content=content,
                    created_at=base_time + timedelta(days=product_id, minutes=offset),
                )
            )
            review_id += 1
    return reviews


def _review_rating(product_rating: float) -> int:
    if product_rating >= 4.7:
        return random.choice([4, 5, 5, 5])
    if product_rating >= 4.4:
        return random.choice([3, 4, 4, 5])
    return random.choice([3, 3, 4, 4, 5])


def main() -> None:
    counts = seed_database()
    print("Seed complete")
    print(f"Shops: {counts['shops']}")
    print(f"Products: {counts['products']}")
    print(f"Reviews: {counts['reviews']}")


if __name__ == "__main__":
    main()
