# Doc Intel — Snippets layout + OCR (copy sang Notion)

Project: `/home/lenkuy/doc-intel-tool`

## Nội dung
- `app/vision_ocr.py` — prompt OCR QPPL + clean + Gemini Vision
- `web/api-bridge.js` — bỏ Trang x/y, layout tiêu đề căn giữa, bảng, highlight DOM

---

## FILE: `app/vision_ocr.py`

```python
"""
Gemini Vision OCR — multimodal native (PDF page → image → text).

Không cần OCR engine riêng (Tesseract/EasyOCR). Dùng:
  PDF → pixmap (PyMuPDF) → Gemini Flash Vision (parallel)

Config (.env):
  OCR_MODE=auto|always|off     # auto: chỉ trang thiếu text
  OCR_CONCURRENCY=10
  OCR_DPI=150
  OCR_MODEL=gemini-2.5-flash
  GEMINI_API_KEY=...
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("vision_ocr")

OCR_PROMPT = """Bạn là công cụ trích xuất văn bản (OCR/extraction) chuyên xử lý VĂN BẢN QUY PHẠM PHÁP LUẬT tiếng Việt (nghị định, quyết định, thông tư, nghị quyết, chỉ thị, báo cáo, tờ trình).

NHIỆM VỤ: Chuyển trang tài liệu (ảnh) thành văn bản có cấu trúc, TRUNG THÀNH TUYỆT ĐỐI với nội dung hiển thị. Không tóm tắt, không diễn giải, không bịa thêm.

QUY TẮC BẮT BUỘC:

1. CHỈ lấy nội dung HIỂN THỊ cho người đọc.
   - TUYỆT ĐỐI loại bỏ mọi ký tự/mảnh markup kỹ thuật: thẻ HTML/XML (<...>), thuộc tính, dấu ">" hoặc "\\"" đứng lạc lõng, mã CSS, tooltip/title, chú thích ẩn, watermark, số dòng, header/footer lặp lại.
   - Nếu thấy chuỗi kiểu `...">`, `.">`, `<div>`, `class=...`: đó là RÁC, phải bỏ hoàn toàn, không đưa vào kết quả.

2. GIỮ ĐÚNG BỐ CỤC & THỨ TỰ ĐỌC (trên→dưới, trái→phải).
   - Không gộp các khối khác nhau vào cùng một đoạn.
   - Mỗi đoạn văn, mỗi câu là một đơn vị riêng; giữ đúng ranh giới câu, không dính chữ.
   - Nhận diện và giữ đúng các thành phần thể thức:
     • Quốc hiệu – Tiêu ngữ (CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM / Độc lập - Tự do - Hạnh phúc)
     • Tên cơ quan ban hành, số hiệu, địa danh và ngày tháng
     • Loại văn bản + trích yếu (NGHỊ ĐỊNH, QUYẾT ĐỊNH...)
     • Căn cứ ("Căn cứ...")
     • Chương / Mục / Điều / Khoản / Điểm — giữ nguyên đánh số
     • Khối chữ ký (chức danh, "(Đã ký)", họ tên)

3. CHÍNH TẢ TIẾNG VIỆT CHÍNH XÁC.
   - Giữ nguyên đầy đủ dấu thanh và dấu phụ (ă, â, ê, ô, ơ, ư, đ, và các dấu sắc/huyền/hỏi/ngã/nặng).
   - Không tự "sửa" hay thay từ; chép đúng như trên trang. Đặc biệt chú ý các cặp dễ sai: ĐỊNH/ĐẮNH, TỊCH/TỌH, phân biệt hoa/thường.

4. BẢNG BIỂU (QUAN TRỌNG — dễ mất chữ/sai chính tả):
   - Xuất bảng Markdown, đúng số hàng/cột, tiêu đề cột và TỪNG Ô.
   - Đọc CHẬM từng ô: không bỏ sót chữ cái, số, dấu %, đơn vị (triệu, tỷ, ha…).
   - Mỗi hàng Markdown phải có ĐỦ số cột (điền ô trống bằng khoảng trắng giữa || nếu ô trống).
   - Không gộp hai ô thành một; không cắt ngang từ giữa ô.
   - Giữ tên bảng ("Bảng 1. ...") ngay trên bảng.
   - Không làm phẳng bảng thành đoạn văn.

5. BIỂU ĐỒ / HÌNH ẢNH: chỉ ghi chú thích (caption) nếu có; không cố "đọc" số liệu bên trong hình.

6. KHÔNG BỊA. Nếu một vùng bị mờ/không đọc được, ghi [không đọc được]. Không suy đoán, không thêm nội dung từ trang khác.

7. THỂ THỨC TRÊN TRANG:
   - Quốc hiệu, tiêu ngữ, tên loại VB (NGHỊ ĐỊNH, QUYẾT ĐỊNH…) giữ nguyên chữ HOA và xuống dòng như trên trang.
   - Không ghi "Trang x/y" hay tên file vào kết quả.

ĐỊNH DẠNG ĐẦU RA: Markdown sạch.
- Quốc hiệu / tiêu ngữ / NGHỊ ĐỊNH / QUYẾT ĐỊNH…: dòng riêng, viết hoa đúng bản gốc.
- Điều/Khoản xuống dòng rõ ràng (# hoặc dòng "Điều n.").
- Bảng dùng cú pháp bảng Markdown.
- Không kèm bất kỳ thẻ HTML nào trong kết quả.

Chỉ trả về nội dung đã trích xuất, không kèm lời giải thích.
Nếu trang gần như trống hoặc chỉ là ảnh trang trí: trả về chuỗi rỗng.
"""

CLEAN_PROMPT_TEMPLATE = """Dưới đây là văn bản pháp luật tiếng Việt đã được OCR nhưng bị lỗi. Hãy LÀM SẠCH mà KHÔNG thay đổi nội dung thật:

1. Xóa mọi ký tự/mảnh rác kỹ thuật: thẻ HTML (<...>), các chuỗi kiểu `">`, `.">`, `class=`, dấu ngoặc kép/dấu ">" lạc lõng, ký tự zero-width, khoảng trắng thừa.
2. Tách lại các đoạn/câu bị gộp sai; khôi phục thứ tự đọc và ranh giới câu.
3. Sửa lỗi dính chữ, khôi phục dấu tiếng Việt bị mất/sai (chú ý ĐỊNH, TỊCH, các dấu phụ). KHÔNG được nuốt mất chữ cái trong ô bảng.
4. Giữ nguyên số hiệu, số Điều/Khoản/Điểm, số liệu và tên riêng — KHÔNG chỉnh sửa giá trị.
5. Bảng: khôi phục về dạng bảng Markdown đúng hàng/cột; mỗi hàng cùng số cột; không gộp/xóa ô; sửa ô bị cắt từ (vd "Nghị địn" → chỉ sửa nếu chắc chắn từ bị OCR dính, còn số liệu thì giữ nguyên).
6. Xóa dòng kiểu "Trang 1/69" hoặc tên file PDF nếu có.
7. Không thêm, không bớt, không tóm tắt nội dung.

Chỉ trả về văn bản đã làm sạch dưới dạng Markdown.

VĂN BẢN CẦN LÀM SẠCH:
\"\"\"
{text}
\"\"\"
"""


def _gemini_key() -> str:
    import os

    return (
        getattr(settings, "ocr_api_key", "")
        or settings.gemini_api_key
        or os.getenv("GEMINI_API_KEY", "")
        or os.getenv("OCR_API_KEY", "")
        or ""
    ).strip()


def ocr_enabled() -> bool:
    mode = (getattr(settings, "ocr_mode", "auto") or "auto").lower()
    if mode in ("off", "false", "0", "none"):
        return False
    return bool(_gemini_key())


def _ocr_model_name() -> str:
    return getattr(settings, "ocr_model", None) or "gemini-2.5-flash"


def _concurrency() -> int:
    return max(1, int(getattr(settings, "ocr_concurrency", 10) or 10))


def _dpi() -> int:
    return max(72, int(getattr(settings, "ocr_dpi", 150) or 150))


def page_needs_ocr(text: str, *, min_chars: int = 40) -> bool:
    """Heuristic: empty / too sparse / mostly non-text → OCR."""
    t = (text or "").strip()
    if len(t) < min_chars:
        return True
    # high ratio of replacement / control chars
    bad = sum(1 for c in t if ord(c) < 9 or c == "\ufffd")
    if bad > max(5, len(t) * 0.05):
        return True
    return False


def render_pdf_page_png(pdf_path: str | Path, page_index: int, dpi: int | None = None) -> bytes:
    """Render one PDF page (0-based) to PNG bytes via PyMuPDF — no poppler."""
    import fitz

    dpi = dpi or _dpi()
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    finally:
        doc.close()


def render_all_pages_png(pdf_path: str | Path, dpi: int | None = None) -> list[bytes]:
    import fitz

    dpi = dpi or _dpi()
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    doc = fitz.open(pdf_path)
    try:
        out: list[bytes] = []
        for i in range(doc.page_count):
            pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
            out.append(pix.tobytes("png"))
        return out
    finally:
        doc.close()


async def ocr_page_gemini(
    png_bytes: bytes,
    page_num: int,
    *,
    model_name: str | None = None,
) -> dict[str, Any]:
    """OCR one page image with Gemini Vision (async)."""
    key = _gemini_key()
    if not key:
        return {"page": page_num, "text": "", "error": "missing_GEMINI_API_KEY", "engine": "none"}

    model_name = model_name or _ocr_model_name()
    t0 = time.perf_counter()
    try:
        import google.generativeai as genai
        from PIL import Image

        genai.configure(api_key=key)
        model = genai.GenerativeModel(model_name)
        img = Image.open(io.BytesIO(png_bytes))
        # run blocking SDK in thread
        def _call() -> str:
            resp = model.generate_content([img, OCR_PROMPT])
            raw = (getattr(resp, "text", None) or "").strip()
            if not raw:
                return ""
            # Pass 2: làm sạch rác markup (prompt clean) — không đổi logic file khác
            clean_prompt = CLEAN_PROMPT_TEMPLATE.format(text=raw)
            resp2 = model.generate_content(clean_prompt)
            cleaned = (getattr(resp2, "text", None) or "").strip()
            return cleaned or raw

        text = await asyncio.to_thread(_call)
        return {
            "page": page_num,
            "text": text,
            "engine": f"gemini-vision:{model_name}",
            "elapsed": round(time.perf_counter() - t0, 3),
        }
    except Exception as e:
        log.warning("OCR page %s failed: %s", page_num, e)
        return {
            "page": page_num,
            "text": "",
            "error": str(e)[:240],
            "engine": f"gemini-vision:{model_name}",
            "elapsed": round(time.perf_counter() - t0, 3),
        }


async def ocr_pdf_parallel(
    pdf_path: str | Path,
    *,
    page_indices: list[int] | None = None,
    max_concurrent: int | None = None,
    dpi: int | None = None,
) -> list[dict[str, Any]]:
    """
    OCR selected pages (0-based indices) or all pages, with concurrency limit.

    Returns list of {page (1-based), text, engine, ...} sorted by page.
    """
    pdf_path = Path(pdf_path)
    max_concurrent = max_concurrent or _concurrency()
    dpi = dpi or _dpi()

    import fitz

    doc = fitz.open(pdf_path)
    try:
        n = doc.page_count
        if page_indices is None:
            page_indices = list(range(n))
        else:
            page_indices = [i for i in page_indices if 0 <= i < n]
    finally:
        doc.close()

    if not page_indices:
        return []

    sem = asyncio.Semaphore(max_concurrent)
    model_name = _ocr_model_name()

    async def one(i0: int) -> dict[str, Any]:
        async with sem:
            png = await asyncio.to_thread(render_pdf_page_png, pdf_path, i0, dpi)
            return await ocr_page_gemini(png, i0 + 1, model_name=model_name)

    results = await asyncio.gather(*[one(i) for i in page_indices])
    results = sorted(results, key=lambda r: r.get("page", 0))
    return list(results)


async def enrich_pages_with_vision(
    pdf_path: str | Path,
    pages: list[Any],
    *,
    mode: str | None = None,
) -> tuple[list[Any], list[str]]:
    """
    Mutate PageText list: fill sparse pages via Gemini Vision.

    mode:
      auto   — only pages with little text
      always — all pages
      off    — no-op
    """
    from .extract import PageText, _html_esc  # local import avoid cycle at module load

    mode = (mode or getattr(settings, "ocr_mode", "auto") or "auto").lower()
    warnings: list[str] = []
    if mode in ("off", "false", "0", "none"):
        return pages, warnings
    if not ocr_enabled():
        warnings.append("OCR Vision tắt: thiếu GEMINI_API_KEY / OCR_API_KEY")
        return pages, warnings

    need: list[int] = []
    if mode == "always":
        need = list(range(len(pages)))
    else:
        for i, p in enumerate(pages):
            if page_needs_ocr(getattr(p, "text", "") or ""):
                need.append(i)

    if not need:
        warnings.append("OCR Vision: không cần — đủ text layer")
        return pages, warnings

    t0 = time.perf_counter()
    results = await ocr_pdf_parallel(pdf_path, page_indices=need)
    elapsed = round(time.perf_counter() - t0, 3)
    ok = 0
    for r in results:
        pno = int(r.get("page") or 0)
        if pno < 1 or pno > len(pages):
            continue
        text = (r.get("text") or "").strip()
        if not text or r.get("error"):
            continue
        ok += 1
        pt: PageText = pages[pno - 1]
        # Prefer OCR body; keep existing tables markdown if any
        table_tail = ""
        if pt.tables:
            table_tail = "\n" + "\n".join(
                f"\n[BẢNG {t.get('index', '')} — {t.get('rows')}×{t.get('cols')}]\n{t.get('markdown', '')}\n"
                for t in pt.tables
            )
        pt.text = (text + table_tail).strip()
        # rebuild simple html paragraphs + keep table html
        html_parts = []
        for para in text.split("\n\n"):
            p = para.strip()
            if p:
                html_parts.append(f"<p>{_html_esc(p).replace(chr(10), '<br>')}</p>")
        for t in pt.tables or []:
            if t.get("html"):
                html_parts.append(t["html"])
        pt.html = "\n".join(html_parts)
        pt.char_count = len(pt.text)

    warnings.append(
        f"OCR Gemini Vision ({_ocr_model_name()}): {ok}/{len(need)} trang · "
        f"concurrency={_concurrency()} · dpi={_dpi()} · {elapsed}s"
    )
    return pages, warnings
```

---

## FILE: `web/api-bridge.js`

```javascript
/**
 * Doc Intel ↔ Frontend «Trợ lý họp UBND» (tro-ly-hop-ubnd-v2.html)
 * Không sửa CSS / scroll / tooltip / highlight của frontend.
 * Chỉ nạp dữ liệu thật + gọi API, dùng API render sẵn có của UI.
 */
(function () {
  "use strict";

  const API = "";

  /** @type {Record<string,{job_id:string,analysis:object}>} */
  const LIVE = {};
  /** @type {File[]} */
  let fileBag = [];

  function log() {
    if (typeof console !== "undefined")
      console.info.apply(console, ["[doc-intel]"].concat([].slice.call(arguments)));
  }

  function notify(msg) {
    if (Array.isArray(window.notifs)) {
      window.notifs.unshift({ t: String(msg), time: "Vừa xong" });
      if (typeof window.renderNotif === "function") window.renderNotif();
      var b = document.getElementById("notifBadge");
      if (b) b.classList.remove("hide");
    }
    log(msg);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ===== Backend → DOC_DATA (đúng schema frontend) ===== */
  function asText(items, key) {
    if (!items || !items.length) return "—";
    return items
      .map(function (x) {
        if (typeof x === "string") return x;
        return (x && (x[key] || x.point || x.decision || x.impact)) || "";
      })
      .filter(Boolean)
      .join(" ");
  }

  function clip(t, n) {
    t = String(t || "");
    return t.length > n ? t.slice(0, n - 1) + "…" : t;
  }

  function mapDocData(a) {
    var s = a.summary || {};
    var ctx = s.context || "—";
    var main = asText(s.main_content, "point");
    var dec = asText(s.decision_points, "decision");
    var imp = asText(s.impact, "impact");
    var pages = a.total_pages || 0;
    function four(c, m, d, i) {
      return [
        { ic: "map", lbl: "Bối cảnh", txt: c, cite: "live-p1", citeTxt: pages ? "tr.1–" + pages : "tr.1" },
        { ic: "doc", lbl: "Nội dung chính", txt: m, cite: "live-p1", citeTxt: "nội dung chính" },
        { ic: "check", lbl: "Điểm cần quyết định", txt: d, cite: "live-p1", citeTxt: "điểm quyết" },
        { ic: "warn", lbl: "Tác động cần lưu ý", txt: i, cite: "live-p1", citeTxt: "tác động" },
      ];
    }
    var terms = (a.terminology || []).map(function (t) {
      return {
        name: t.term || t.name || "—",
        cat: "law",
        catL: "pháp lý",
        expl: t.explanation || t.expl || "",
        cite: t.page != null ? "live-p" + t.page : "live-p1",
        citeTxt: (t.clause ? t.clause + " · " : "") + (t.page != null ? "tr." + t.page : "văn bản"),
      };
    });
    var items = (a.suggested_questions || []).map(function (q) {
      return {
        q: typeof q === "string" ? q : q.question || "",
        cite: q.related_pages && q.related_pages[0] != null ? "live-p" + q.related_pages[0] : "live-p1",
        citeTxt:
          q.related_pages && q.related_pages.length
            ? "tr." + q.related_pages.join(",")
            : q.purpose || "gợi ý",
      };
    });
    if (!items.length) {
      items = [{ q: "Thẩm quyền ban hành và căn cứ pháp lý của văn bản?", cite: "live-p1", citeTxt: "tr.1" }];
    }
    return {
      summaries: {
        1: four(clip(ctx, 160), clip(main, 140), clip(dec, 140), clip(imp, 120)),
        5: four(clip(ctx, 320), clip(main, 360), clip(dec, 300), clip(imp, 280)),
        0: four(ctx, main, dec, imp),
      },
      terms: terms,
      questions: [
        { grp: "Gợi ý chuẩn bị họp", color: "navy", items: items.slice(0, 4) },
        {
          grp: "Tác động & tuân thủ",
          color: "green",
          items: items.slice(4, 8).length ? items.slice(4, 8) : items.slice(0, 2),
        },
      ],
    };
  }

  /* ===== Nội dung văn bản: đúng class frontend (.term data-def) — tooltip #tip gốc ===== */
  // Fallback thuật ngữ hành chính–pháp lý (khi API trả ít term) để highlight vẫn chạy
  var FALLBACK_TERMS = [
    { name: "ngân sách nhà nước", def: "Toàn bộ các khoản thu, chi của Nhà nước trong một khoảng thời gian nhất định, được cơ quan nhà nước có thẩm quyền quyết định." },
    { name: "dự toán", def: "Kế hoạch thu, chi ngân sách được cấp có thẩm quyền giao hoặc phê duyệt cho kỳ ngân sách." },
    { name: "thẩm định", def: "Xem xét, đánh giá tính hợp pháp, hợp lý, khả thi trước khi ban hành hoặc phê duyệt." },
    { name: "thẩm quyền", def: "Quyền và trách nhiệm do pháp luật quy định cho cơ quan, người có chức vụ." },
    { name: "hiệu lực", def: "Thời điểm và phạm vi văn bản bắt đầu có giá trị pháp lý." },
    { name: "bãi bỏ", def: "Chấm dứt hiệu lực của văn bản hoặc quy định đã ban hành." },
    { name: "sửa đổi, bổ sung", def: "Thay đổi hoặc thêm nội dung của văn bản đang có hiệu lực." },
    { name: "căn cứ", def: "Văn bản, quy định làm cơ sở pháp lý để ban hành văn bản mới." },
    { name: "UBND", def: "Ủy ban nhân dân — cơ quan hành chính nhà nước ở địa phương." },
    { name: "HĐND", def: "Hội đồng nhân dân — cơ quan quyền lực nhà nước ở địa phương." },
    { name: "Nghị định", def: "Văn bản quy phạm pháp luật do Chính phủ ban hành." },
    { name: "Thông tư", def: "Văn bản QPPL do bộ trưởng, thủ trưởng cơ quan ngang bộ ban hành." },
    { name: "Quyết định", def: "Văn bản do cấp có thẩm quyền ban hành để quyết định một vấn đề cụ thể." },
    { name: "thủ tục hành chính", def: "Trình tự, cách thức thực hiện, hồ sơ và yêu cầu, điều kiện do cơ quan nhà nước quy định." },
    { name: "đơn vị sự nghiệp", def: "Tổ chức do Nhà nước thành lập để cung cấp dịch vụ công, không nhằm mục tiêu lợi nhuận." },
    { name: "nguồn tăng thu", def: "Phần thu ngân sách thực tế vượt so với dự toán được giao." },
    { name: "quỹ dự phòng", def: "Khoản ngân sách dành xử lý nhiệm vụ chi đột xuất, cấp bách." },
    { name: "phân cấp", def: "Chuyển một phần thẩm quyền từ cấp trên xuống cấp dưới theo quy định." },
    { name: "công chức", def: "Công dân được tuyển dụng, bổ nhiệm vào ngạch, chức vụ trong cơ quan nhà nước." },
    { name: "viên chức", def: "Người làm việc tại đơn vị sự nghiệp công lập theo vị trí việc làm." },
  ];

  function termList(a) {
    var out = [];
    var seen = {};
    function add(name, def) {
      name = String(name || "").trim();
      if (name.length < 2) return;
      var k = name.toLowerCase();
      if (seen[k]) return;
      seen[k] = 1;
      out.push({ name: name, def: def || "" });
    }
    (a.terminology || []).forEach(function (t) {
      add(t.term || t.name, t.explanation || t.expl || "");
    });
    (a.important_clauses || []).forEach(function (c) {
      add(c.clause, c.summary || c.why_important || "Điều khoản quan trọng trong văn bản");
    });
    var pe = a.preextract || {};
    (pe.dictionary_terms || []).forEach(function (t) {
      add(t.term || t.name, t.explanation || t.expl || "");
    });
    // Chỉ thêm fallback nếu term đó xuất hiện trong corpus (tránh highlight bừa)
    var corpus = "";
    (a.page_index || []).forEach(function (p) {
      corpus += " " + (p.text || "");
    });
    corpus = corpus.toLowerCase();
    FALLBACK_TERMS.forEach(function (t) {
      if (corpus.indexOf(t.name.toLowerCase()) >= 0) add(t.name, t.def);
    });
    out.sort(function (a, b) {
      return b.name.length - a.name.length;
    });
    return out.slice(0, 50);
  }

  /** Gắn <span class="term" data-def="..."> — đúng pattern UI UBND (tooltip #tip). */
  function decoratePlain(plain, terms) {
    var html = esc(plain);
    return applyTermsToEscapedText(html, terms).replace(/\n/g, "<br>");
  }

  function applyTermsToEscapedText(escapedText, terms) {
    if (!escapedText || !terms || !terms.length) return escapedText;
    var html = escapedText;
    terms.forEach(function (t) {
      if (!t.name || t.name.length < 2) return;
      // bỏ qua nếu đã được bọc .term
      var probe = t.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      var re = new RegExp("(" + probe + ")", "i");
      var once = false;
      html = html.replace(re, function (m, g, offset, full) {
        if (once) return m;
        // không gắn trong thẻ đang mở (rough: nếu gần đây có < không có >)
        var before = full.slice(Math.max(0, offset - 20), offset);
        if (before.indexOf("<") > before.indexOf(">")) return m;
        if (/class="term"/i.test(before)) return m;
        once = true;
        return (
          '<span class="term" data-def="' +
          esc(t.def || "").replace(/'/g, "&#39;") +
          '">' +
          g +
          "</span>"
        );
      });
    });
    return html;
  }

  /** Chạy trên HTML đã có thẻ: chỉ tô text node, không phá bảng/tag. */
  function decorateHtml(html, terms) {
    if (!html) return "";
    if (!terms || !terms.length) return html;
    var parts = String(html).split(/(<[^>]+>)/g);
    var inTerm = false;
    return parts
      .map(function (part) {
        if (!part) return part;
        if (part.charAt(0) === "<") {
          if (/^<span\b[^>]*class="term"/i.test(part)) inTerm = true;
          if (inTerm && /^<\/span>/i.test(part)) inTerm = false;
          return part;
        }
        if (inTerm) return part;
        // text có thể đã escape hoặc raw — ưu tiên treat as text content
        if (/[&<>]/.test(part) && !/&(?:amp|lt|gt|quot|#)/.test(part)) {
          part = esc(part);
        }
        return applyTermsToEscapedText(part, terms);
      })
      .join("");
  }

  // alias cũ
  function decorate(plain, terms) {
    return decoratePlain(plain, terms);
  }

  function mdTable(md) {
    var lines = String(md || "")
      .split("\n")
      .map(function (l) {
        return l.trim();
      })
      .filter(function (l) {
        return l.charAt(0) === "|";
      });
    if (lines.length < 2) return "";
    function split(line) {
      return line
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map(function (c) {
          return c.trim();
        });
    }
    var rows = lines
      .map(split)
      .filter(function (r) {
        return !r.every(function (c) {
          return /^:?-{3,}:?$/.test(c) || !c;
        });
      });
    if (!rows.length) return "";
    // compact table — palette khớp UI UBND, không sửa CSS gốc
    var wrap =
      'style="max-width:100%;overflow-x:auto;margin:8px 0 12px;border:1px solid #e7e3d8;border-radius:8px;background:#fff"';
    var table =
      'style="border-collapse:collapse;width:100%;font-size:11.5px;line-height:1.35"';
    var th =
      'style="border:1px solid #d8d3c5;padding:5px 8px;background:#1a1f2b;color:#ffcd00;font-weight:600;text-align:left;white-space:nowrap;font-size:11px"';
    var td0 =
      'style="border:1px solid #ebe6da;padding:4px 8px;vertical-align:top;word-break:break-word"';
    var td1 =
      'style="border:1px solid #ebe6da;padding:4px 8px;vertical-align:top;word-break:break-word;background:#fbf8f1"';
    var h = "<div " + wrap + "><table " + table + "><thead><tr>";
    rows[0].forEach(function (c) {
      h += "<th " + th + ">" + esc(c) + "</th>";
    });
    h += "</tr></thead><tbody>";
    rows.slice(1).forEach(function (r, ri) {
      h += "<tr>";
      var st = ri % 2 ? td1 : td0;
      r.forEach(function (c) {
        h += "<td " + st + ">" + esc(c) + "</td>";
      });
      h += "</tr>";
    });
    h += "</tbody></table></div>";
    return h;
  }

  /**
   * Render body text like official VN legal layout:
   * - Quốc hiệu / tiêu ngữ / NGHỊ ĐỊNH / VĂN BẢN… căn giữa, chữ to
   * - Điều/Khoản: heading trái
   * - Không hiện dòng "Trang x / y · filename"
   */
  function renderStructuredBody(text, terms, anchorId) {
    if (!text || !String(text).trim()) return "";
    var lines = String(text).replace(/\r/g, "").split("\n");
    var out = [];
    if (anchorId) out.push('<span id="' + esc(anchorId) + '"></span>');

    var buf = [];
    function flushPara() {
      if (!buf.length) return;
      var para = buf.join(" ").replace(/\s+/g, " ").trim();
      buf = [];
      if (!para) return;
      out.push("<p>" + decoratePlain(para, terms) + "</p>");
    }

    function isMdTableLine(ln) {
      return /^\s*\|/.test(ln);
    }

    var i = 0;
    while (i < lines.length) {
      var raw = lines[i];
      var ln = raw.trim();
      if (!ln) {
        flushPara();
        i++;
        continue;
      }

      // Markdown table block
      if (isMdTableLine(ln)) {
        flushPara();
        var mdLines = [];
        while (i < lines.length && (isMdTableLine(lines[i]) || !lines[i].trim())) {
          if (isMdTableLine(lines[i])) mdLines.push(lines[i].trim());
          i++;
        }
        // optional caption line before was flushed; check previous...
        out.push(mdTable(mdLines.join("\n")));
        continue;
      }

      // Skip OCR noise page labels if any
      if (/^Trang\s+\d+\s*\/\s*\d+/i.test(ln)) {
        i++;
        continue;
      }
      if (/^\[BẢNG\b/i.test(ln)) {
        flushPara();
        i++;
        continue;
      }

      // Heading levels — official form
      var upper = ln.toUpperCase();
      // Quốc hiệu
      if (/CỘNG\s*HÒA\s*XÃ\s*HỘI\s*CHỦ\s*NGHĨA\s*VIỆT\s*NAM/i.test(ln)) {
        flushPara();
        out.push(
          '<p style="text-align:center;font-weight:700;font-size:calc(15px * var(--fs,1));letter-spacing:.04em;margin:8px 0 2px;text-transform:uppercase">' +
            decoratePlain(ln, terms) +
            "</p>"
        );
        i++;
        continue;
      }
      // Tiêu ngữ
      if (/Độc\s*lập\s*[-–—]\s*Tự\s*do\s*[-–—]\s*Hạnh\s*phúc/i.test(ln)) {
        flushPara();
        out.push(
          '<p style="text-align:center;font-style:italic;font-size:calc(14px * var(--fs,1));margin:0 0 16px">' +
            decoratePlain(ln, terms) +
            "</p>"
        );
        i++;
        continue;
      }
      // Loại VB lớn: NGHỊ ĐỊNH, QUYẾT ĐỊNH, THÔNG TƯ, NGHỊ QUYẾT, CHỈ THỊ, LUẬT, BỘ LUẬT, VĂN BẢN...
      if (
        /^(NGHỊ\s*ĐỊNH|QUYẾT\s*ĐỊNH|THÔNG\s*TƯ(\s*LIÊN\s*TỊCH)?|NGHỊ\s*QUYẾT|CHỈ\s*THỊ|LUẬT|BỘ\s*LUẬT|PHÁP\s*LỆNH|CÔNG\s*VĂN|TỜ\s*TRÌNH|BÁO\s*CÁO|ĐỀ\s*ÁN|QUY\s*CHẾ|HƯỚNG\s*DẪN|VĂN\s*BẢN)(\s|$)/i.test(
          ln
        ) ||
        (/^(NGHỊ ĐỊNH|QUYẾT ĐỊNH|THÔNG TƯ|NGHỊ QUYẾT|CHỈ THỊ|VĂN BẢN)/i.test(upper) &&
          ln.length < 80)
      ) {
        flushPara();
        out.push(
          '<h1 style="text-align:center;font-family:\'Lora\',serif;font-size:calc(22px * var(--fs,1));font-weight:700;letter-spacing:.06em;margin:20px 0 10px;text-transform:uppercase;color:var(--ink,#000)">' +
            decoratePlain(ln, terms) +
            "</h1>"
        );
        i++;
        continue;
      }
      // Số hiệu / cơ quan ban hành (thường viết hoa, ngắn, căn giữa)
      if (
        (/^(ỦY\s*BAN|BỘ\s|CHÍNH\s*PHỦ|THỦ\s*TƯỚNG|HỘI\s*ĐỒNG)/i.test(ln) && ln.length < 100) ||
        (/^Số\s*[:：]/i.test(ln) && ln.length < 80)
      ) {
        flushPara();
        out.push(
          '<p style="text-align:center;font-weight:600;font-size:calc(14.5px * var(--fs,1));margin:4px 0">' +
            decoratePlain(ln, terms) +
            "</p>"
        );
        i++;
        continue;
      }
      // Địa danh, ngày
      if (
        /^[A-ZÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ][^,]{2,40},\s*ngày\s+\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+\d{4}/i.test(
          ln
        )
      ) {
        flushPara();
        out.push(
          '<p style="text-align:center;font-style:italic;margin:4px 0 14px">' +
            decoratePlain(ln, terms) +
            "</p>"
        );
        i++;
        continue;
      }
      // Trích yếu (về việc...)
      if (/^Về\s+việc\b/i.test(ln) || /^v\/v\b/i.test(ln)) {
        flushPara();
        out.push(
          '<p style="text-align:center;font-weight:600;font-size:calc(15.5px * var(--fs,1));margin:8px 0 18px">' +
            decoratePlain(ln, terms) +
            "</p>"
        );
        i++;
        continue;
      }
      // Chương / Mục
      if (/^(Chương|Mục|PHẦN)\s+/i.test(ln)) {
        flushPara();
        out.push(
          '<h2 style="text-align:center;font-family:\'Lora\',serif;font-size:calc(18px * var(--fs,1));font-weight:700;margin:22px 0 12px">' +
            decoratePlain(ln, terms) +
            "</h2>"
        );
        i++;
        continue;
      }
      // Điều
      if (/^Điều\s+\d+/i.test(ln)) {
        flushPara();
        out.push(
          '<h2 style="font-family:\'Lora\',serif;font-size:calc(17px * var(--fs,1));font-weight:600;margin:22px 0 10px">' +
            decoratePlain(ln, terms) +
            "</h2>"
        );
        i++;
        continue;
      }
      // Khoản đầu dòng dạng "1." "2."
      if (/^\d+\.\s+\S/.test(ln) && ln.length < 500) {
        flushPara();
        out.push(
          '<p style="margin:6px 0 6px 0;text-align:justify">' +
            decoratePlain(ln, terms) +
            "</p>"
        );
        i++;
        continue;
      }
      // Markdown heading
      if (/^#{1,3}\s+/.test(ln)) {
        flushPara();
        var level = (ln.match(/^#+/) || ["#"])[0].length;
        var ht = ln.replace(/^#+\s*/, "");
        if (level === 1) {
          out.push(
            '<h1 style="text-align:center;font-family:\'Lora\',serif;font-size:calc(22px * var(--fs,1));font-weight:700;margin:18px 0 10px;text-transform:uppercase">' +
              decoratePlain(ht, terms) +
              "</h1>"
          );
        } else {
          out.push(
            '<h2 style="font-family:\'Lora\',serif;font-size:calc(17px * var(--fs,1));font-weight:600;margin:18px 0 10px">' +
              decoratePlain(ht, terms) +
              "</h2>"
          );
        }
        i++;
        continue;
      }

      buf.push(ln);
      i++;
    }
    flushPara();
    return out.join("\n");
  }

  function buildDocHtml(analysis, filename) {
    var terms = termList(analysis);
    var pages = analysis.page_index || [];
    var parts = [];
    // FULL document — no "Trang x/y · filename" labels
    for (var i = 0; i < pages.length; i++) {
      var p = pages[i];
      var pid = "live-p" + p.page;
      var text = String(p.text || "").trim();

      // Prefer structured text (OCR markdown / extract) for legal layout
      if (text) {
        parts.push(renderStructuredBody(text, terms, pid));
        // Extra tables not already in markdown body
        if (p.tables && p.tables.length && text.indexOf("|") < 0) {
          p.tables.forEach(function (t) {
            if (t.markdown) parts.push(mdTable(t.markdown));
            else if (t.html) parts.push(String(t.html));
          });
        }
        continue;
      }
      if (p.html) {
        parts.push('<span id="' + pid + '"></span>');
        parts.push(String(p.html));
      }
    }
    return parts.join("\n");
  }

  function ensurePane(docId) {
    var el = document.querySelector('.docpane-inner[data-doc="' + docId + '"]');
    if (el) return el;
    var host = document.getElementById("docpane");
    el = document.createElement("div");
    el.className = "docpane-inner doc";
    el.setAttribute("data-doc", docId);
    el.style.display = "none";
    host.appendChild(el);
    return el;
  }

  /** Load pages in batches so UI stays responsive; full content still available. */
  async function loadAllPages(jobId, totalPages) {
    var all = [];
    var batch = 25;
    for (var from = 1; from <= totalPages; from += batch) {
      var to = Math.min(totalPages, from + batch - 1);
      var r = await fetch(
        API +
          "/v1/jobs/" +
          encodeURIComponent(jobId) +
          "/pages?page_from=" +
          from +
          "&page_to=" +
          to
      );
      if (!r.ok) throw new Error("pages_load_failed");
      var j = await r.json();
      (j.pages || []).forEach(function (p) {
        all.push(p);
      });
    }
    return all;
  }

  /**
   * DOM-safe term highlight: walk text nodes only (không regex HTML).
   * Dùng đúng class .term + data-def để tooltip #tip frontend hoạt động.
   */
  function highlightTermsInDom(root, terms) {
    if (!root || !terms || !terms.length) return 0;
    var count = 0;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        var p = node.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        if (p.closest("span.term, script, style, table")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);

    terms.forEach(function (t) {
      if (!t.name || t.name.length < 2) return;
      var re = new RegExp(t.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i");
      var done = false;
      nodes.forEach(function (textNode) {
        if (done) return;
        var val = textNode.nodeValue;
        var m = re.exec(val);
        if (!m) return;
        done = true;
        var idx = m.index;
        var matched = m[0];
        var before = val.slice(0, idx);
        var after = val.slice(idx + matched.length);
        var span = document.createElement("span");
        span.className = "term";
        span.setAttribute("data-def", t.def || "");
        span.textContent = matched;
        var parent = textNode.parentNode;
        if (!parent) return;
        if (before) parent.insertBefore(document.createTextNode(before), textNode);
        parent.insertBefore(span, textNode);
        if (after) parent.insertBefore(document.createTextNode(after), textNode);
        parent.removeChild(textNode);
        count++;
      });
    });
    return count;
  }

  async function applyAnalysis(docId, analysis, displayName) {
    LIVE[docId] = { job_id: analysis.job_id, analysis: analysis };
    window.CURRENT_JOB_ID = analysis.job_id;

    if (typeof DOC_DATA !== "undefined") DOC_DATA[docId] = mapDocData(analysis);
    if (typeof DOCS !== "undefined") {
      var types = (analysis.summary && analysis.summary.document_types) || [];
      DOCS[docId] = {
        name: displayName,
        badge: String(types[0] || "TÀI LIỆU").toUpperCase().slice(0, 16),
        meeting: "Đã tải lên",
      };
    }

    // Lazy-load full pages if upload response omitted page_index
    var total = analysis.total_pages || analysis.pages_available || 0;
    if ((!analysis.page_index || !analysis.page_index.length) && analysis.job_id && total) {
      try {
        notify("Đang nạp " + total + " trang văn bản…");
        analysis.page_index = await loadAllPages(analysis.job_id, total);
        LIVE[docId].analysis = analysis;
      } catch (e) {
        log("pages load error", e);
        notify("Không nạp được đầy đủ trang: " + e.message);
      }
    }

    var pane = ensurePane(docId);
    pane.className = "docpane-inner doc";
    pane.setAttribute("data-doc", docId);
    // Build HTML without regex term pass — highlight via DOM after insert
    var html = buildDocHtml(analysis, displayName);
    // strip final decorateHtml double-wrap risk: rebuild without decorateHtml end
    pane.innerHTML = html;

    // DOM-safe highlight (tooltip frontend)
    var terms = termList(analysis);
    var nTerm = highlightTermsInDom(pane, terms);

    var meta = document.querySelector(".doc-meta b[data-count]");
    if (meta) {
      meta.setAttribute("data-count", String(analysis.total_pages || total || 0));
      meta.textContent = String(analysis.total_pages || total || 0);
    }

    if (typeof switchDoc === "function") switchDoc(docId);
    else {
      if (typeof loadDocData === "function") loadDocData(docId);
      if (typeof renderSummary === "function")
        renderSummary(typeof curTier !== "undefined" ? curTier : 5);
      if (typeof renderTerms === "function" && typeof terms !== "undefined") renderTerms(terms);
      if (typeof renderQuestions === "function") renderQuestions();
    }

    try {
      if (window.ScrollTrigger && ScrollTrigger.refresh) ScrollTrigger.refresh();
    } catch (e) {}

    log("live doc ready", docId, "terms:", nTerm, "pages:", (analysis.page_index || []).length);
  }

  /* ===== Hook upload (File thật) ===== */
  function hookWhenReady() {
    if (typeof window.onPickFiles !== "function" || typeof window.confirmUpload !== "function") {
      setTimeout(hookWhenReady, 50);
      return;
    }

    var _onPick = window.onPickFiles;
    window.onPickFiles = function (files) {
      var list = [];
      Array.prototype.forEach.call(files || [], function (f) {
        var name = f && f.name ? f.name : String(f);
        if (!/\.(pdf|docx)$/i.test(name)) {
          notify("Chỉ hỗ trợ PDF/DOCX: " + name);
          return;
        }
        if (f instanceof File) fileBag.push(f);
        list.push(f);
      });
      // UI gốc: pendingFiles.push(f.name) — nhận File được
      return _onPick(list);
    };

    var _remove = window.removeFile;
    window.removeFile = function (k) {
      fileBag.splice(k, 1);
      return _remove(k);
    };

    var _open = window.openUpload;
    window.openUpload = function () {
      fileBag = [];
      return _open.apply(this, arguments);
    };

    window.confirmUpload = async function () {
      if (typeof selFolder === "undefined" || selFolder === null) return;
      if (!fileBag.length) {
        notify("Hãy chọn tệp PDF/DOCX");
        return;
      }
      var btn = document.getElementById("uploadConfirm");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Đang xử lý…";
      }
      notify("Đang tải lên & phân tích…");
      try {
        var fd = new FormData();
        fileBag.forEach(function (f) {
          fd.append("files", f);
        });
        var title =
          allMeetings && allMeetings[selFolder]
            ? allMeetings[selFolder].name
            : "Họp UBND";
        fd.append("title", title);

        var res = await fetch(API + "/v1/analyze/upload", { method: "POST", body: fd });
        var data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || "analyze failed");

        var nUp = meetings.length;
        var src = selFolder < nUp ? meetings[selFolder] : pastMeetings[selFolder - nUp];
        var docId = "live_" + (data.job_id || String(Date.now()));
        var label =
          (data.files && data.files[0] && data.files[0].filename) || fileBag[0].name;
        var displayName =
          fileBag.length > 1 ? label + " (+" + (fileBag.length - 1) + ")" : label;

        src.docs.push({ t: displayName, docId: docId, on: true });
        src.open = true;

        applyAnalysis(docId, data, displayName);
        if (typeof renderMeetings === "function") renderMeetings();
        if (typeof closeUpload === "function") closeUpload();

        var mi = allMeetings.findIndex(function (x) {
          return x.name === src.name;
        });
        if (mi >= 0) {
          allMeetings[mi].open = true;
          if (typeof setMeetBody === "function") setMeetBody(mi, true, true);
          var el = document.querySelector('.meet[data-i="' + mi + '"]');
          if (el) {
            el.classList.add("on");
            el.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
        }

        notify(
          "Xong · " +
            (data.elapsed_seconds != null ? data.elapsed_seconds + "s" : "?") +
            " · " +
            (data.within_60s ? "<60s" : "≥60s") +
            " · " +
            displayName
        );
        fileBag = [];
        if (Array.isArray(window.pendingFiles)) window.pendingFiles = [];
      } catch (e) {
        console.error(e);
        notify("Lỗi: " + e.message);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Tải lên";
        }
      }
    };

    /* ===== Q&A — cùng DOM/animation frontend, trả lời API ===== */
    window.addQ = function (q) {
      var thread = document.getElementById("thread");
      var ask = document.createElement("div");
      ask.className = "ask";
      ask.textContent = q;
      thread.appendChild(ask);
      var reduce =
        typeof window.reduce !== "undefined"
          ? window.reduce
          : window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!reduce && window.gsap)
        gsap.from(ask, { y: 12, autoAlpha: 0, duration: 0.4, ease: "back.out(1.6)" });
      if (typeof scrollThread === "function") scrollThread();

      var typing = document.createElement("div");
      typing.className = "ans";
      typing.innerHTML =
        '<div class="ans-body"><div class="typing"><i></i><i></i><i></i></div></div>';
      thread.appendChild(typing);
      if (typeof scrollThread === "function") scrollThread();
      var dotsTween;
      if (!reduce && window.gsap) {
        gsap.from(typing, { y: 8, autoAlpha: 0, duration: 0.3 });
        dotsTween = gsap.to(typing.querySelectorAll(".typing i"), {
          y: -5,
          duration: 0.35,
          repeat: -1,
          yoyo: true,
          stagger: 0.12,
          ease: "sine.inOut",
        });
      }

      var jobId = window.CURRENT_JOB_ID;
      if (typeof curDoc === "string" && LIVE[curDoc]) {
        jobId = LIVE[curDoc].job_id;
        window.CURRENT_JOB_ID = jobId;
      }

      function done(html) {
        if (dotsTween) dotsTween.kill();
        typing.remove();
        var ans = document.createElement("div");
        ans.className = "ans";
        ans.innerHTML = '<div class="ans-body">' + html + "</div>";
        thread.appendChild(ans);
        if (typeof scrollThread === "function") scrollThread();
        if (!reduce && window.gsap) {
          gsap.from(ans, { y: 10, autoAlpha: 0, duration: 0.4 });
          var chips = ans.querySelectorAll(".cites .chip");
          if (chips.length)
            gsap.from(chips, {
              scale: 0.8,
              autoAlpha: 0,
              stagger: 0.1,
              delay: 0.15,
              duration: 0.3,
              ease: "back.out(2)",
            });
          var chk = ans.querySelector(".vchk path");
          if (chk && chk.getTotalLength) {
            var len = chk.getTotalLength();
            gsap.fromTo(
              chk,
              { strokeDasharray: len, strokeDashoffset: len },
              { strokeDashoffset: 0, duration: 0.5, delay: 0.3, ease: "power2.out" }
            );
          }
        }
      }

      if (!jobId) {
        setTimeout(function () {
          done(
            "Tài liệu demo tĩnh chưa gắn máy chủ. Dùng <b>Nhập tài liệu</b> để tải PDF/DOCX và hỏi đáp kèm trích dẫn trang."
          );
        }, 700);
        return;
      }

      fetch(API + "/v1/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, question: q }),
      })
        .then(function (r) {
          return r.json().then(function (j) {
            return { ok: r.ok, j: j };
          });
        })
        .then(function (x) {
          if (!x.ok) throw new Error(x.j.detail || x.j.error || "ask failed");
          var j = x.j;
          var cites = (j.citations || [])
            .map(function (c) {
              var id = c.page != null ? "live-p" + c.page : "live-p1";
              var label =
                (c.clause ? c.clause + " · " : "") +
                (c.page != null ? "tr." + c.page : "văn bản");
              return (
                '<span class="chip" onclick="goCite(\'' +
                id +
                "')\">📍 " +
                esc(label) +
                "</span>"
              );
            })
            .join("");
          var modeLabel =
            j.answer_mode === "heuristic_search" || j.llm_used === false
              ? "Tìm kiếm thô (chưa LLM)"
              : "AI · đã đối chiếu văn bản gốc";
          done(
            esc(j.answer || "—").replace(/\n/g, "<br>") +
              (cites ? '<div class="cites">' + cites + "</div>" : "") +
              '<div class="verify"><svg class="i vchk" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>' +
              esc(modeLabel) +
              (j.confidence ? " · " + esc(j.confidence) : "") +
              "</div>"
          );
        })
        .catch(function (e) {
          done("Không trả lời được: " + esc(e.message));
        });
    };

    var _switch = window.switchDoc;
    if (typeof _switch === "function") {
      window.switchDoc = function (id) {
        var r = _switch.apply(this, arguments);
        if (LIVE[id]) window.CURRENT_JOB_ID = LIVE[id].job_id;
        return r;
      };
    }

    log("hooks ready — frontend UBND tỉnh + backend Doc Intel");
  }

  fetch(API + "/health")
    .then(function (r) {
      return r.json();
    })
    .then(function (h) {
      var note = document.querySelector(".login-note");
      if (note && h) {
        note.textContent = h.llm_enabled
          ? "UBND tỉnh · máy chủ sẵn sàng · " + (h.model || "AI")
          : "UBND tỉnh · máy chủ online · chưa bật AI key";
      }
    })
    .catch(function () {});

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hookWhenReady);
  } else {
    hookWhenReady();
  }
})();
```

---

