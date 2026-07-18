"""
Bối cảnh miền: văn bản nhà nước / pháp luật Việt Nam.

Phạm vi: Hiến pháp, Bộ luật, Luật, Pháp lệnh, Nghị quyết, Nghị định,
Quyết định, Chỉ thị, Thông tư, Thông tư liên tịch, Công văn, Tờ trình,
Đề án, Báo cáo, Biên bản, Hợp đồng hành chính, v.v.
"""
from __future__ import annotations

import re
from typing import Any

# Phân loại văn bản QPPL & hành chính nhà nước (gợi ý cho model + API)
DOCUMENT_TYPES = [
    "hien_phap",
    "bo_luat",
    "luat",
    "phap_lenh",
    "nghi_quyet",
    "nghi_dinh",
    "quyet_dinh",
    "chi_thi",
    "thong_tu",
    "thong_tu_lien_tich",
    "cong_van",
    "to_trinh",
    "de_an",
    "bao_cao",
    "bien_ban",
    "huong_dan",
    "quy_che",
    "quy_dinh",
    "ke_hoach",
    "khac",
]

RELATED_DOC_TYPES = [
    "luat",
    "bo_luat",
    "nghi_dinh",
    "nghi_quyet",
    "thong_tu",
    "quyet_dinh",
    "chi_thi",
    "cong_van",
    "huong_dan",
    "quy_che",
    "bieu_mau",
    "van_ban_lien_quan",
    "khac",
]

# Regex nhận diện số hiệu / căn cứ phổ biến trong VB Việt Nam
LEGAL_REF_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "luat",
        re.compile(
            r"\bLuật\s+[A-ZÀ-Ỵa-zà-ỹ0-9\s\-–,]{3,80}?(?:số\s*)?\d{1,3}/\d{4}/QH\d{1,2}\b",
            re.I,
        ),
    ),
    (
        "bo_luat",
        re.compile(r"\bBộ\s*luật\s+[A-ZÀ-Ỵa-zà-ỹ\s]{3,60}(?:số\s*)?\d{1,3}/\d{4}/QH\d{1,2}\b", re.I),
    ),
    (
        "nghi_dinh",
        re.compile(r"\bNghị\s*định\s*(?:số\s*)?\d{1,3}/\d{4}/NĐ-CP\b", re.I),
    ),
    (
        "thong_tu",
        re.compile(r"\bThông\s*tư\s*(?:số\s*)?\d{1,3}/\d{4}/TT-[A-ZĐ]{1,10}\b", re.I),
    ),
    (
        "thong_tu_lien_tich",
        re.compile(r"\bThông\s*tư\s*liên\s*tịch\s*(?:số\s*)?\d{1,3}/\d{4}/TTLT-[A-ZĐ\-]{2,20}\b", re.I),
    ),
    (
        "quyet_dinh",
        re.compile(r"\bQuyết\s*định\s*(?:số\s*)?\d{1,5}/QĐ-[A-ZĐ0-9\-]{1,20}\b", re.I),
    ),
    (
        "nghi_quyet",
        re.compile(r"\bNghị\s*quyết\s*(?:số\s*)?\d{1,3}(?:/\d{4})?/(?:NQ-)?(?:CP|QH\d{0,2}|HĐND)?\b", re.I),
    ),
    (
        "chi_thi",
        re.compile(r"\bChỉ\s*thị\s*(?:số\s*)?\d{1,3}/CT-[A-ZĐ0-9\-]{1,15}\b", re.I),
    ),
    (
        "phap_lenh",
        re.compile(r"\bPháp\s*lệnh\s+[A-ZÀ-Ỵa-zà-ỹ\s]{0,40}(?:số\s*)?\d{1,3}/\d{4}/[A-Z0-9]+\b", re.I),
    ),
    (
        "cong_van",
        re.compile(r"\b(?:Công\s*văn|CV)\s*(?:số\s*)?\d{1,5}/[A-ZĐ0-9\-\.]+\b", re.I),
    ),
]

# Điều / khoản / điểm / mục / chương / phụ lục
# "Điểm" chỉ lấy điểm a–k theo cấu trúc QPPL, tránh dính "điểm cần/tiếp…"
CLAUSE_PATTERN = re.compile(
    r"(?:"
    r"Điều\s+\d+[a-z]?"
    r"|Khoản\s+\d+[a-z]?"
    r"|Điểm\s+[a-k]\)?(?=\s*(?:Khoản|Điều|và|,|;|\.|$))"
    r"|Mục\s+\d+[a-z]?"
    r"|Chương\s+[IVXLC\d]+"
    r"|Phụ\s*lục(?:\s+[A-ZĐ\d\-]+)?"
    r"|Điều\s+\d+[a-z]?\s*Khoản\s+\d+"
    r")",
    re.I,
)

# Thuật ngữ / viết tắt hành chính–pháp lý thường gặp (giải thích heuristic)
COMMON_LEGAL_TERMS: dict[str, str] = {
    "qppl": "Văn bản quy phạm pháp luật — văn bản do cơ quan nhà nước có thẩm quyền ban hành, có hiệu lực chung.",
    "vbqppl": "Văn bản quy phạm pháp luật.",
    "ubnd": "Ủy ban nhân dân — cơ quan hành chính nhà nước ở địa phương.",
    "hđnd": "Hội đồng nhân dân — cơ quan quyền lực nhà nước ở địa phương.",
    "cp": "Chính phủ.",
    "ttcp": "Thủ tướng Chính phủ.",
    "qh": "Quốc hội.",
    "bca": "Bộ Công an.",
    "btc": "Bộ Tài chính.",
    "bkhđt": "Bộ Kế hoạch và Đầu tư (tên gọi theo giai đoạn).",
    "bnv": "Bộ Nội vụ.",
    "btnmt": "Bộ Tài nguyên và Môi trường (theo giai đoạn tổ chức).",
    "byt": "Bộ Y tế.",
    "bgdđt": "Bộ Giáo dục và Đào tạo.",
    "mst": "Mã số thuế.",
    "nsnn": "Ngân sách nhà nước.",
    "nsđp": "Ngân sách địa phương.",
    "tthc": "Thủ tục hành chính.",
    "dvc": "Dịch vụ công (thường là dịch vụ công trực tuyến).",
    "cổng dvc": "Cổng Dịch vụ công.",
    "csdl": "Cơ sở dữ liệu.",
    "csdlqg": "Cơ sở dữ liệu quốc gia.",
    "cccd": "Căn cước công dân.",
    "vneid": "Tài khoản định danh điện tử / ứng dụng định danh.",
    "kyso": "Chữ ký số.",
    "hsm": "Hồ sơ mật / hoặc Hardware Security Module — tùy ngữ cảnh văn bản.",
    "thẩm định": "Xem xét, đánh giá tính hợp pháp, hợp lý, khả thi trước khi ban hành/phê duyệt.",
    "thẩm tra": "Xem xét, kiểm tra nội dung (thường của cơ quan dân cử) trước khi thông qua.",
    "ban hành": "Công bố chính thức văn bản để có hiệu lực theo thẩm quyền.",
    "hiệu lực": "Thời điểm văn bản bắt đầu có giá trị pháp lý.",
    "bãi bỏ": "Chấm dứt hiệu lực của văn bản/quy định cũ.",
    "sửa đổi, bổ sung": "Thay đổi hoặc thêm nội dung của văn bản đang có hiệu lực.",
    "thay thế": "Văn bản mới thay toàn bộ văn bản cũ.",
    "ủy quyền": "Giao cho cơ quan/cá nhân khác thực hiện một phần thẩm quyền.",
    "phân cấp": "Chuyển thẩm quyền từ cấp trên xuống cấp dưới theo quy định.",
    "phân quyền": "Giao quyền tự chủ, tự chịu trách nhiệm cho cấp dưới trong phạm vi luật định.",
    "công vụ": "Hoạt động thực thi nhiệm vụ, quyền hạn của cán bộ, công chức, viên chức.",
    "công chức": "Công dân được tuyển dụng, bổ nhiệm vào ngạch, chức vụ, chức danh trong cơ quan nhà nước.",
    "viên chức": "Người được tuyển dụng theo vị trí việc làm, làm việc tại đơn vị sự nghiệp công lập.",
    "xử phạt vphc": "Xử phạt vi phạm hành chính.",
    "vphc": "Vi phạm hành chính.",
    "tncn": "Thuế thu nhập cá nhân.",
    "tndn": "Thuế thu nhập doanh nghiệp.",
    "gtgt": "Thuế giá trị gia tăng (VAT).",
    "đấu thầu": "Lựa chọn nhà thầu cung cấp hàng hóa, dịch vụ, xây lắp theo pháp luật đấu thầu.",
    "đầu tư công": "Đầu tư của Nhà nước từ ngân sách và nguồn vốn hợp pháp khác theo Luật Đầu tư công.",
    "cph": "Cổ phần hóa (doanh nghiệp nhà nước) — nếu ngữ cảnh DNNN.",
    "dnnn": "Doanh nghiệp nhà nước.",
    "pccc": "Phòng cháy và chữa cháy.",
    "attt": "An toàn thông tin.",
    "anm": "An ninh mạng.",
    "bảo mật nhà nước": "Bảo vệ thông tin thuộc bí mật nhà nước theo pháp luật.",
    "công khai, minh bạch": "Nguyên tắc công bố thông tin theo quy định để người dân, tổ chức giám sát.",
}


DOMAIN_SYSTEM_PREAMBLE = """
BỐI CẢNH MIỀN (bắt buộc tuân thủ):
Bạn đang hỗ trợ cán bộ, công chức, viên chức và lãnh đạo cơ quan nhà nước Việt Nam
phân tích văn bản liên quan hệ thống pháp luật & hành chính nhà nước, bao gồm nhưng
không giới hạn:
- Hiến pháp, Bộ luật, Luật, Pháp lệnh;
- Nghị quyết (Quốc hội, Chính phủ, HĐND…);
- Nghị định của Chính phủ; Quyết định, Chỉ thị của Thủ tướng / cấp có thẩm quyền;
- Thông tư, Thông tư liên tịch của bộ, cơ quan ngang bộ;
- Công văn, tờ trình, đề án, kế hoạch, báo cáo, biên bản, hướng dẫn nghiệp vụ;
- Quy chế, quy định nội bộ cơ quan nhà nước; biểu mẫu thủ tục hành chính;
- Văn bản hướng dẫn thi hành, đính chính, hợp nhất, bãi bỏ, sửa đổi, bổ sung.

Nguyên tắc pháp lý khi tóm tắt / trả lời:
1) Không bịa số hiệu, điều, khoản, ngày ban hành, thẩm quyền, mức phạt, mức vốn.
2) Ưu tiên trích dẫn cấu trúc: Chương / Mục / Điều / Khoản / Điểm / Phụ lục + số trang.
3) Phân biệt: căn cứ ban hành, nội dung quy định, trách nhiệm thi hành, hiệu lực,
   điều khoản chuyển tiếp, bãi bỏ/thay thế.
4) Gợi ý văn bản liên quan phải hợp lý với hệ thống pháp luật VN (Luật → Nghị định →
   Thông tư → hướng dẫn địa phương). Nếu chỉ là gợi ý suy luận, ghi rõ "gợi ý".
5) Ngôn ngữ: tiếng Việt hành chính, rõ ràng, phù hợp họp lãnh đạo / tổ thẩm định.
""".strip()


def extract_legal_references(text: str, limit: int = 40) -> list[dict[str, str]]:
    """Trích số hiệu văn bản được nhắc trong nội dung (không dùng LLM)."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in LEGAL_REF_PATTERNS:
        for m in pattern.finditer(text or ""):
            title = re.sub(r"\s+", " ", m.group(0)).strip()
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append({"title": title, "type": kind, "reason": "Được dẫn chiếu trong văn bản đang phân tích"})
            if len(found) >= limit:
                return found
    return found


def extract_clause_mentions(text: str, limit: int = 50) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in CLAUSE_PATTERN.finditer(text or ""):
        c = re.sub(r"\s+", " ", m.group(0)).strip()
        k = c.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def match_common_terms(text: str, limit: int = 25) -> list[dict[str, Any]]:
    """Gắn cờ thuật ngữ nhà nước/pháp lý phổ biến xuất hiện trong văn bản."""
    lower = (text or "").lower()
    hits: list[dict[str, Any]] = []
    # longer keys first
    for term in sorted(COMMON_LEGAL_TERMS.keys(), key=len, reverse=True):
        if term.lower() in lower or term in lower:
            hits.append(
                {
                    "term": term.upper() if term.isupper() or len(term) <= 6 else term,
                    "explanation": COMMON_LEGAL_TERMS[term],
                    "page": None,
                    "clause": None,
                    "importance": "trung_binh",
                    "source": "dictionary_vn_legal",
                }
            )
        if len(hits) >= limit:
            break
    return hits


def detect_document_signals(text: str) -> dict[str, Any]:
    """Tín hiệu loại văn bản / căn cứ để đưa vào context.

    Ưu tiên tiêu đề/thể thức VB đang xử lý (đầu văn bản), không nhầm với VB chỉ được
    dẫn trong phần Căn cứ (vd. 'Căn cứ Luật …' không biến cả hồ sơ thành Luật).
    """
    raw = text or ""
    head = raw[:2500]
    signals: list[str] = []

    # Thứ tự ưu tiên: thể thức văn bản đang ban hành / trình (case-insensitive)
    title_checks = [
        (r"DỰ\s*THẢO\s+BỘ\s*LUẬT|^\s*BỘ\s*LUẬT\b", "bo_luat", "Thể thức: Bộ luật"),
        (r"DỰ\s*THẢO\s+LUẬT\b|^\s*LUẬT\b", "luat", "Thể thức: Luật"),
        (r"DỰ\s*THẢO\s+NGHỊ\s*ĐỊNH|^\s*NGHỊ\s*ĐỊNH\b", "nghi_dinh", "Thể thức: Nghị định"),
        (r"DỰ\s*THẢO\s+THÔNG\s*TƯ|^\s*THÔNG\s*TƯ\b", "thong_tu", "Thể thức: Thông tư"),
        (r"DỰ\s*THẢO\s+QUYẾT\s*ĐỊNH|^\s*QUYẾT\s*ĐỊNH\b", "quyet_dinh", "Thể thức: Quyết định"),
        (r"DỰ\s*THẢO\s+NGHỊ\s*QUYẾT|^\s*NGHỊ\s*QUYẾT\b", "nghi_quyet", "Thể thức: Nghị quyết"),
        (r"DỰ\s*THẢO\s+CHỈ\s*THỊ|^\s*CHỈ\s*THỊ\b", "chi_thi", "Thể thức: Chỉ thị"),
        (r"DỰ\s*THẢO\s+TỜ\s*TRÌNH|^\s*TỜ\s*TRÌNH\b", "to_trinh", "Thể thức: Tờ trình"),
        (r"DỰ\s*THẢO\s+ĐỀ\s*ÁN|^\s*ĐỀ\s*ÁN\b|phê\s*duyệt\s+Đề\s*án", "de_an", "Thể thức: Đề án"),
        (r"^\s*CÔNG\s*VĂN\b|Số:\s*\d+.*/.*\n.*V/v", "cong_van", "Thể thức: Công văn"),
        (r"DỰ\s*THẢO\s+BÁO\s*CÁO|^\s*BÁO\s*CÁO\b", "bao_cao", "Thể thức: Báo cáo"),
        (r"DỰ\s*THẢO\s+QUY\s*CHẾ|^\s*QUY\s*CHẾ\b", "quy_che", "Thể thức: Quy chế"),
        (r"DỰ\s*THẢO\s+KẾ\s*HOẠCH|^\s*KẾ\s*HOẠCH\b", "ke_hoach", "Thể thức: Kế hoạch"),
    ]
    dtype = "khac"
    for pat, code, label in title_checks:
        if re.search(pat, head, re.I | re.M):
            signals.append(label)
            if dtype == "khac":
                dtype = code

    # Dẫn chiếu trong căn cứ / nội dung (không đổi loại VB chính nếu đã có)
    cite_checks = [
        (r"\bBộ\s*luật\b", "Có dẫn Bộ luật"),
        (r"\bLuật\s+", "Có dẫn Luật"),
        (r"\bNghị\s*định\b", "Có dẫn Nghị định"),
        (r"\bThông\s*tư\b", "Có dẫn Thông tư"),
        (r"\bQuyết\s*định\b", "Có dẫn Quyết định"),
        (r"\bNghị\s*quyết\b", "Có dẫn Nghị quyết"),
    ]
    for pat, label in cite_checks:
        if re.search(pat, head, re.I):
            signals.append(label)

    if re.search(r"Căn\s*cứ", head, re.I):
        signals.append("Có phần căn cứ ban hành")
    if re.search(r"hiệu\s*lực", raw[:8000], re.I):
        signals.append("Có nội dung hiệu lực thi hành")
    if re.search(r"bãi\s*bỏ|thay\s*thế|sửa\s*đổi,\s*bổ\s*sung", raw[:8000], re.I):
        signals.append("Có nội dung bãi bỏ/thay thế/sửa đổi")

    # unique signals, keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for s in signals:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return {"document_type_guess": dtype, "signals": uniq}
