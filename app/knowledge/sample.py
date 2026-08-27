"""Clearly labeled synthetic knowledge documents for the thesis demo."""

SAMPLE_MARKET_DOCUMENTS: tuple[dict[str, str], ...] = (
    {
        "id": "sample_market_audio_2026",
        "title": "Tín hiệu danh mục tai nghe trong bộ dữ liệu demo",
        "content": (
            "Người mua trong dữ liệu mẫu ưu tiên giá, chất lượng âm thanh, pin, "
            "độ thoải mái và tốc độ giao hàng. Đây không phải báo cáo thị trường thật."
        ),
    },
    {
        "id": "sample_market_mobile_2026",
        "title": "Tín hiệu điện thoại trong bộ dữ liệu demo",
        "content": (
            "Các review mẫu thường nhắc màn hình, pin, camera, hiệu năng và giá. "
            "Các tín hiệu chỉ dùng để trình diễn kiến trúc và evaluation."
        ),
    },
    {
        "id": "sample_market_quality_2026",
        "title": "Giới hạn suy luận thị trường",
        "content": (
            "Thống kê từ dữ liệu tổng hợp không đại diện Shopee, Tiki hay Lazada. "
            "Kết luận production phải được tính lại sau khi ingest dữ liệu thật."
        ),
    },
)
