#!/usr/bin/env python3
"""Generate a multi-page sample Vietnamese administrative PDF for smoke tests."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import fitz
except ImportError:
    print("pymupdf required", file=sys.stderr)
    sys.exit(1)


SAMPLE_PAGES = [
    (
        "DỰ THẢO QUYẾT ĐỊNH\n"
        "Về việc phê duyệt Đề án chuyển đổi số đơn vị hành chính cấp huyện\n\n"
        "Căn cứ Luật Tổ chức chính quyền địa phương;\n"
        "Căn cứ Nghị định 73/2019/NĐ-CP về quản lý đầu tư ứng dụng CNTT;\n"
        "Xét đề nghị của Phòng Văn hóa – Thông tin.\n\n"
        "Điều 1. Phê duyệt Đề án chuyển đổi số giai đoạn 2025–2027 với các mục tiêu:\n"
        "1. 100% thủ tục hành chính mức độ 3, 4 được cung cấp trực tuyến;\n"
        "2. Tích hợp dữ liệu dân cư với CSDL quốc gia;\n"
        "3. Đào tạo kỹ năng số cho tối thiểu 80% công chức."
    ),
    (
        "Điều 2. Kinh phí thực hiện\n"
        "1. Tổng mức đầu tư dự kiến: 12,5 tỷ đồng từ ngân sách địa phương và nguồn xã hội hóa.\n"
        "2. Phân kỳ: 2025 — 4 tỷ; 2026 — 5 tỷ; 2027 — 3,5 tỷ.\n"
        "3. Chủ đầu tư: UBND huyện; đơn vị thường trực: Phòng VH–TT.\n\n"
        "Điều 3. Trách nhiệm tổ chức thực hiện\n"
        "1. Phòng VH–TT xây dựng kế hoạch chi tiết trong 30 ngày kể từ ngày ban hành.\n"
        "2. Phòng Tài chính – Kế hoạch thẩm định nguồn vốn.\n"
        "3. Các xã, thị trấn phối hợp triển khai điểm tiếp nhận hồ sơ số."
    ),
    (
        "Điều 4. Các điểm cần thảo luận tại cuộc họp\n"
        "1. Có chấp thuận mức đầu tư 12,5 tỷ đồng hay điều chỉnh xuống 10 tỷ?\n"
        "2. Có ưu tiên xã hội hóa phần mềm một cửa điện tử không?\n"
        "3. Thời hạn hoàn thành tích hợp dữ liệu: quý IV/2026 hay 2027?\n\n"
        "Thuật ngữ:\n"
        "- DVC: Dịch vụ công trực tuyến.\n"
        "- CSDLQG: Cơ sở dữ liệu quốc gia về dân cư.\n"
        "- Mức độ 3, 4: Mức độ cung cấp dịch vụ công theo quy định Chính phủ.\n"
        "- Xã hội hóa: Huy động nguồn lực ngoài ngân sách nhà nước."
    ),
    (
        "Phụ lục — Tác động dự kiến\n"
        "1. Người dân: giảm thời gian đi lại, minh bạch tiến độ hồ sơ.\n"
        "2. Công chức: thay đổi quy trình nghiệp vụ, cần đào tạo lại.\n"
        "3. Rủi ro: chậm tiến độ nếu hạ tầng mạng xã/thị trấn chưa đồng bộ;\n"
        "   thiếu nhân sự CNTT; an toàn thông tin.\n"
        "4. Văn bản liên quan cần rà soát: Luật Giao dịch điện tử, Nghị định 47/2020/NĐ-CP,\n"
        "   Quyết định phê duyệt kiến trúc Chính phủ điện tử tỉnh."
    ),
]


def make_pdf(path: Path, pages: int = 48) -> None:
    doc = fitz.open()
    base = SAMPLE_PAGES
    # DejaVu supports Vietnamese diacritics (base Helvetica does not)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    fontname = "f0"
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_font(fontname=fontname, fontfile=font_path)
        body = base[i % len(base)]
        text = f"Trang {i + 1}/{pages}\n\n{body}\n\n(Mẫu mô phỏng — Doc Intel Tool)"
        # textbox wraps long lines for readability
        page.insert_textbox(
            fitz.Rect(48, 48, 547, 794),
            text,
            fontsize=10.5,
            fontname=fontname,
            align=fitz.TEXT_ALIGN_LEFT,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()
    print(f"Wrote {path} ({pages} pages)")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "samples" / "de_an_mau_48trang.pdf"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 48
    make_pdf(out, n)
