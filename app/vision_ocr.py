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
import re
import time
from concurrent.futures import ThreadPoolExecutor
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

5. BIỂU ĐỒ / SƠ ĐỒ / HÌNH ẢNH CÓ DỮ LIỆU (biểu đồ tròn, cột, đường, miền...):
   - Luôn ghi chú thích / tên biểu đồ nếu có (vd "Biểu đồ 1. Cơ cấu thu ngân sách 2026").
   - NẾU biểu đồ có nhãn số / chú giải (legend) HIỂN THỊ RÕ RÀNG: trích số liệu thành BẢNG Markdown, mỗi mục một hàng (Tên mục | Giá trị/%). CHỈ chép số ĐỌC ĐƯỢC trên hình; TUYỆT ĐỐI không suy đoán, không nội suy, không bịa con số không hiển thị.
   - NẾU không có nhãn số rõ: mô tả NGẮN GỌN loại biểu đồ và xu hướng (vd "Biểu đồ cột: giá trị tăng dần từ 2021 đến 2025"), KHÔNG kèm con số cụ thể.
   - Ảnh trang trí (logo, quốc huy, hoa văn, con dấu): bỏ qua, không mô tả.

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

# Rác markup còn sót trong OCR → mới chạy pass clean (tránh 2 call/trang mặc định)
_GARBAGE_RE = re.compile(r'"?>|</?\w+[^>]*>|class\s*=', re.I)

import threading

# Module-level Gemini model (configure 1 lần) + shared thread pool
_model_lock = threading.Lock()
_model_cache: dict[str, Any] = {}  # key: api_key_prefix::model_name -> GenerativeModel
_executor: ThreadPoolExecutor | None = None


def _get_executor(concurrency: int) -> ThreadPoolExecutor:
    global _executor
    workers = max(4, concurrency * 2)
    if _executor is None or getattr(_executor, "_max_workers", 0) < workers:
        if _executor is not None:
            try:
                _executor.shutdown(wait=False)
            except Exception:
                pass
        _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr")
    return _executor


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


def _safe_text(resp: Any) -> str:
    """
    Đọc resp.text an toàn: Gemini raise ValueError khi bị chặn / không có candidate.
    getattr(resp, "text", None) VẪN kích hoạt property và ném lỗi.
    """
    try:
        candidates = getattr(resp, "candidates", None)
        if not candidates:
            return ""
        return (resp.text or "").strip()
    except Exception:
        return ""


def _get_model(api_key: str, model_name: str) -> Any:
    """Configure genai once per (key, model); reuse GenerativeModel."""
    cache_key = f"{api_key[:12]}::{model_name}"
    with _model_lock:
        if cache_key in _model_cache:
            return _model_cache[cache_key]
        import google.generativeai as genai
        from google.generativeai.types import HarmCategory, HarmBlockThreshold

        genai.configure(api_key=api_key)
        safety = None
        try:
            safety = {
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
        except Exception:
            safety = None

        kwargs: dict[str, Any] = {
            "model_name": model_name,
            "generation_config": {
                "temperature": 0,
                "max_output_tokens": 8192,
            },
        }
        if safety is not None:
            kwargs["safety_settings"] = safety
        model = genai.GenerativeModel(**kwargs)
        _model_cache[cache_key] = model
        return model


def _needs_clean(text: str) -> bool:
    if not text:
        return False
    return bool(_GARBAGE_RE.search(text))


def _call_gemini_with_retry(
    model: Any,
    parts: list[Any],
    *,
    max_retries: int = 3,
) -> str:
    """Blocking generate_content with exponential backoff on 429/transient errors."""
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = model.generate_content(parts)
            return _safe_text(resp)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            retryable = any(
                x in msg
                for x in (
                    "429",
                    "resource_exhausted",
                    "resource exhausted",
                    "quota",
                    "timeout",
                    "deadline",
                    "503",
                    "500",
                    "unavailable",
                    "internal",
                )
            )
            if not retryable or attempt >= max_retries - 1:
                raise
            delay = 0.5 * (2**attempt)  # 0.5s → 1s → 2s
            log.warning("Gemini retry %s/%s after %.1fs: %s", attempt + 1, max_retries, delay, e)
            time.sleep(delay)
    if last_err:
        raise last_err
    return ""


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
    executor: ThreadPoolExecutor | None = None,
) -> dict[str, Any]:
    """OCR one page image with Gemini Vision (async). Clean pass only if garbage detected."""
    key = _gemini_key()
    if not key:
        return {"page": page_num, "text": "", "error": "missing_GEMINI_API_KEY", "engine": "none"}

    model_name = model_name or _ocr_model_name()
    t0 = time.perf_counter()
    try:
        from PIL import Image

        model = _get_model(key, model_name)
        img = Image.open(io.BytesIO(png_bytes))

        def _call() -> str:
            raw = _call_gemini_with_retry(model, [img, OCR_PROMPT])
            if not raw:
                return ""
            # Pass 2 chỉ khi phát hiện rác markup (không nhân đôi call mặc định)
            if _needs_clean(raw):
                clean_prompt = CLEAN_PROMPT_TEMPLATE.format(text=raw)
                cleaned = _call_gemini_with_retry(model, [clean_prompt])
                return cleaned or raw
            return raw

        loop = asyncio.get_running_loop()
        if executor is not None:
            text = await loop.run_in_executor(executor, _call)
        else:
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
    executor = _get_executor(max_concurrent)

    async def one(i0: int) -> dict[str, Any]:
        async with sem:
            loop = asyncio.get_running_loop()
            png = await loop.run_in_executor(
                executor, render_pdf_page_png, pdf_path, i0, dpi
            )
            return await ocr_page_gemini(
                png, i0 + 1, model_name=model_name, executor=executor
            )

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
        # Chỉ nối table_tail khi OCR chưa xuất bảng Markdown (tránh nhân đôi)
        table_tail = ""
        if pt.tables and "|" not in text:
            table_tail = "\n" + "\n".join(
                f"\n[BẢNG {t.get('index', '')} — {t.get('rows')}×{t.get('cols')}]\n{t.get('markdown', '')}\n"
                for t in pt.tables
            )
        pt.text = (text + table_tail).strip()
        # html nhẹ cho fallback; frontend ưu tiên pt.text (markdown)
        html_parts = []
        for para in text.split("\n\n"):
            p = para.strip()
            if p:
                html_parts.append(f"<p>{_html_esc(p).replace(chr(10), '<br>')}</p>")
        # Chỉ gắn table html nếu chưa có bảng trong text OCR
        if "|" not in text:
            for t in pt.tables or []:
                if t.get("html"):
                    html_parts.append(t["html"])
        pt.html = "\n".join(html_parts)
        pt.char_count = len(pt.text)
        # Trang OCR = scan: không giữ ảnh nhúng (thường là full-page)
        pt.ocr_applied = True
        pt.images = []

    warnings.append(
        f"OCR Gemini Vision ({_ocr_model_name()}): {ok}/{len(need)} trang · "
        f"concurrency={_concurrency()} · dpi={_dpi()} · {elapsed}s"
    )
    return pages, warnings


# ---------------------------------------------------------------------------
# Doc so lieu tu ANH BIEU DO (chart) — cho trang BORN-DIGITAL (khong OCR trang)
# ---------------------------------------------------------------------------

CHART_PROMPT = """Bạn nhận 1 ẢNH được cắt từ một văn bản. Nhiệm vụ:

1) Nếu ảnh là BIỂU ĐỒ (tròn/donut, cột, đường, miền, thanh ngang...):
   - Dòng đầu: tiêu đề biểu đồ nếu có (in đậm), ví dụ: **Biểu đồ 2. Cơ cấu chi ngân sách 2026**
   - Sau đó xuất BẢNG Markdown 2 cột: | Mục | Giá trị |
   - CHỈ ghi số/nhãn ĐỌC ĐƯỢC rõ ràng trên ảnh (nhãn %, chú thích, trục). TUYỆT ĐỐI không suy đoán/bịa số.
   - Nếu là biểu đồ nhưng không có nhãn số rõ → thay bảng bằng 1 câu mô tả xu hướng, KHÔNG kèm số.
2) Nếu ảnh KHÔNG phải biểu đồ (logo, quốc huy, con dấu, ảnh chụp, hình trang trí) → trả lời DUY NHẤT: NO_CHART

Không thêm lời dẫn hay giải thích ngoài yêu cầu trên."""

NO_CHART_MARK = "NO_CHART"


def _data_url_to_png(data_url: str) -> bytes | None:
    import base64

    try:
        s = data_url
        if "," in s:
            s = s.split(",", 1)[1]
        return base64.b64decode(s)
    except Exception:
        return None


def _is_probable_chart_image(
    png_bytes: bytes, *, min_w: int = 200, min_h: int = 140
) -> bool:
    """Loc so bo TRUOC khi goi LLM: bo anh qua nho / ti le bat thuong (logo/dau)."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(png_bytes))
        w, h = im.size
        if w < min_w or h < min_h:
            return False
        ar = w / float(h or 1)
        # Bieu do thuong rong (~2-3:1) hoac donut (~1:1) nhung to. Loai dai/hep la.
        if ar < 0.5 or ar > 6.0:
            return False
        return True
    except Exception:
        return True  # khong chac -> cu thu, LLM se tra NO_CHART neu can


async def describe_chart_image(
    png_bytes: bytes,
    *,
    model_name: str | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> str:
    """Goi Gemini Vision doc 1 anh bieu do -> Markdown (tieu de + bang/mo ta).

    Tra ve '' neu khong phai bieu do (NO_CHART) hoac loi.
    """
    key = _gemini_key()
    if not key:
        return ""
    model_name = model_name or _ocr_model_name()
    try:
        from PIL import Image

        model = _get_model(key, model_name)
        img = Image.open(io.BytesIO(png_bytes))

        def _call() -> str:
            return _call_gemini_with_retry(model, [img, CHART_PROMPT])

        loop = asyncio.get_running_loop()
        if executor is not None:
            raw = await loop.run_in_executor(executor, _call)
        else:
            raw = await asyncio.to_thread(_call)
        raw = (raw or "").strip()
        if not raw or NO_CHART_MARK in raw.upper():
            return ""
        return raw
    except Exception as e:
        log.warning("Chart vision failed: %s", e)
        return ""


async def enrich_images_with_vision(
    pages: list[Any],
    *,
    mode: str | None = None,
    max_charts: int = 12,
) -> tuple[list[Any], list[str]]:
    """Doc so lieu tu ANH nhung (bieu do) tren cac trang BORN-DIGITAL.

    Voi moi anh la bieu do -> them 1 khoi Markdown (tieu de + bang so lieu) vao
    pt.text (de hien thi + phuc vu tim kiem/hoi dap). Chay song song, gioi han
    max_charts. KHONG dung cho trang đa OCR (scan) vi anh đa bi loai.
    """
    import os

    warnings: list[str] = []
    cv_mode = (
        mode
        or getattr(settings, "chart_vision", None)
        or os.getenv("CHART_VISION", "off")
        or "off"
    ).lower()
    if cv_mode in ("off", "false", "0", "none"):
        return pages, warnings
    if not ocr_enabled():
        return pages, warnings

    jobs: list[tuple[int, bytes]] = []
    for i, p in enumerate(pages):
        if getattr(p, "ocr_applied", False):
            continue
        for src in getattr(p, "images", None) or []:
            png = _data_url_to_png(str(src))
            if png is None or not _is_probable_chart_image(png):
                continue
            jobs.append((i, png))
            if len(jobs) >= max_charts:
                break
        if len(jobs) >= max_charts:
            break

    if not jobs:
        return pages, warnings

    t0 = time.perf_counter()
    conc = _concurrency()
    sem = asyncio.Semaphore(conc)
    executor = _get_executor(conc)
    model_name = _ocr_model_name()

    async def one(pi: int, png: bytes) -> tuple[int, str]:
        async with sem:
            md = await describe_chart_image(
                png, model_name=model_name, executor=executor
            )
            return pi, md

    results = await asyncio.gather(*[one(pi, png) for pi, png in jobs])
    ok = 0
    for pi, md in results:
        if not md:
            continue
        ok += 1
        pt = pages[pi]
        block = "\n\n[DỮ LIỆU BIỂU ĐỒ]\n" + md.strip() + "\n"
        pt.text = ((getattr(pt, "text", "") or "") + block).strip()
        try:
            pt.char_count = len(pt.text)
        except Exception:
            pass
    elapsed = round(time.perf_counter() - t0, 3)
    warnings.append(
        f"Chart Vision ({model_name}): {ok}/{len(jobs)} biểu đồ đọc số · "
        f"concurrency={conc} · {elapsed}s"
    )
    return pages, warnings
