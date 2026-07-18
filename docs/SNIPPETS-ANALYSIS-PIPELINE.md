# Doc Intel — Backend tạo summary / terminology / important_clauses

**Không có** `app/analyze.py` hay `app/summarize.py`.
Toàn bộ map → reduce/stitch → schema API nằm ở:

| File | Vai trò |
|------|---------|
| **`app/pipeline.py`** | Map LLM, reduce/stitch, ghép `summary` / `terminology` / `important_clauses` |
| **`app/extract.py`** | `chunk_pages()` + `_smart_truncate()` — nơi **cắt text** đưa vào map |
| **`app/config.py`** | `max_map_chunks`, `chunk_pages`, `max_chars_per_chunk`, `llm_use_reduce` |
| **`app/llm.py`** | `achat_json` gọi model |
| **`app/domain_vn_legal.py`** | preextract (clause refs, dictionary terms) |
| **`web/api-bridge.js`** | `mapDocData(a)` — schema frontend đọc |

## Cơ chế “cắt” hiện tại (không còn đúng literal “12 đầu + 3 cuối”)

1. **`settings.max_map_chunks` (mặc định 6)** — `analyze_documents` gọi:
   ```python
   chunk_pages(..., max_chunks=cap)  # cap ≈ max_map_chunks / n_docs
   ```
2. Khi `max_chunks` set, `chunk_pages` **chia đều** toàn bộ trang thành k đoạn (không lấy 12+3):
   ```python
   start = (ci * n) // k
   end = ((ci + 1) * n) // k
   ```
3. Mỗi chunk nếu dài hơn `max_chars_per_chunk` (8000) → **`_smart_truncate`**:
   - ~45% đầu + ~25% giữa + phần đuôi → đây là “đầu/giữa/cuối” **trong chunk**, không phải 12 trang đầu + 3 trang cuối.
4. Reduce LLM **mặc định TẮT** (`llm_use_reduce=false`) → dùng `_stitch_from_maps` local.
5. `[:12]` trong stitch = **giới hạn số item** (main_content, decision_points…), **không** phải số trang.

## Schema API mà `api-bridge.js` đang dùng

```js
// mapDocData(a) — web/api-bridge.js
a.summary.context                 // string
a.summary.document_types          // string[]
a.summary.main_content            // string[] | {point, page?}[]
a.summary.decision_points         // {decision, page?, clause?, urgency?}[]
a.summary.impact                  // string[] | {impact, page?}[]
a.terminology[]                   // {term|name, explanation|expl, page?, clause?}
a.important_clauses[]             // {clause, summary|why_important, page?}
a.suggested_questions[]           // {question, purpose?, related_pages?}
a.related_documents[]             // {title, reason, type?}
a.page_index[]                    // {page, text, ...} — full text, lazy load
a.preextract.dictionary_terms[]   // bổ sung termList()
a.total_pages, a.job_id
```

---

## FILE: `app/config.py` (tham số map/chunk)

```python
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
JOBS = DATA / "jobs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8090

    # LLM provider: openai_compatible | gemini | xai
    llm_provider: str = "openai_compatible"
    xai_api_key: str = ""
    llm_api_key: str = ""
    gemini_api_key: str = ""
    llm_base_url: str = "https://9flare.com/api/v1"
    llm_model: str = "pro/claude-haiku-4-5"

    # 0 / negative = no page cut (user forbids truncating document content)
    max_pages_budget: int = 500
    target_seconds: int = 55
    map_concurrency: int = 4
    chunk_pages: int = 15
    # Map LLM sampling only (does NOT cut stored/displayed pages)
    max_map_chunks: int = 6
    max_chars_per_chunk: int = 8000
    llm_timeout_seconds: float = 35.0
    llm_force_json_mode: bool = False
    llm_use_reduce: bool = False
    qa_top_k: int = 6
    # UI: never truncate page body (full document for officials)
    ui_page_chars: int = 0
    ui_truncate_pages: bool = False

    # --- Gemini Vision OCR (PDF page → image → text) ---
    # auto: only sparse/empty pages; always: every page; off: never
    ocr_mode: str = "auto"
    ocr_model: str = "gemini-2.5-flash"
    ocr_concurrency: int = 4  # safer default (Notion review)
    ocr_dpi: int = 200  # cao hơn để bảng/chữ nhỏ ít mất nét
    ocr_api_key: str = ""  # optional override; else gemini_api_key

    # Upload limits
    max_upload_files: int = 10
    max_file_bytes: int = 40_000_000
    max_total_upload_bytes: int = 80_000_000
    cors_origins: str = "http://127.0.0.1:8090,http://localhost:8090"

    @property
    def api_key(self) -> str:
        return (
            self.llm_api_key
            or self.gemini_api_key
            or self.xai_api_key
            or os.getenv("GEMINI_API_KEY", "")
            or os.getenv("XAI_API_KEY", "")
            or os.getenv("LLM_API_KEY", "")
        )

    @property
    def provider(self) -> str:
        p = (self.llm_provider or "gemini").strip().lower()
        if p in ("google", "google-ai", "google_ai"):
            return "gemini"
        return p


settings = Settings()
UPLOADS.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)
```

---

## FILE: `app/pipeline.py` (TOÀN BỘ — summary / terms / clauses)

```python
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .config import settings
from .domain_vn_legal import (
    detect_document_signals,
    extract_clause_mentions,
    extract_legal_references,
    match_common_terms,
)
from .extract import DocumentText, chunk_pages, extract_document, extract_folder
from .llm import achat_json, llm_enabled
from .store import store

# Short prompts — long legal preambles make 9flare/Claude timeouts common.
MAP_SYSTEM = """Bạn là chuyên gia phân tích tài liệu họp/thẩm định cho cơ quan nhà nước Việt Nam.
Phân tích đoạn tài liệu được cấp (có thể là văn bản pháp luật VN, báo cáo khoa học, đề án, tờ trình hoặc tài liệu tiếng Anh).
Chỉ dùng nội dung được cấp; không bịa. Nếu tài liệu tiếng Anh, dịch và giải thích sang tiếng Việt.
Trả JSON thuần (không markdown):
{
  "doc_type_hints": [str],
  "main_points": [{"point": str, "page": int|null, "clause": str|null}],
  "decision_points": [{"decision": str, "page": int|null, "clause": str|null, "urgency": "cao|trung_binh|thap"}],
  "impacts": [{"impact": str, "scope": str, "page": int|null}],
  "terms": [{"term": str, "explanation": str, "page": int|null, "clause": str|null, "importance": "cao|trung_binh|thap"}],
  "important_clauses": [{"clause": str, "summary": str, "page": int|null, "why_important": str}],
  "legal_effects": [{"effect": str, "page": int|null, "clause": str|null}],
  "authorities_duties": [{"actor": str, "duty": str, "page": int|null, "clause": str|null}],
  "context_hints": [str],
  "related_regulations_hints": [{"title": str, "reason": str, "type": str|null}]
}
Yêu cầu:
- "terms": Trích ÍT NHẤT 3-5 thuật ngữ chuyên ngành quan trọng nhất trong đoạn (kỹ thuật, pháp lý, hành chính, khoa học). Giải thích ngắn gọn tiếng Việt.
- "main_points": Tối thiểu 2-4 ý chính có trong đoạn.
- "decision_points": Các điểm cần quyết định/phê duyệt; urgency=cao nếu khẩn.
- "context_hints": 1-2 câu mô tả bối cảnh đoạn này.
Tiếng Việt, ngắn gọn, chuẩn mực hành chính."""

REDUCE_SYSTEM = """Tổng hợp phân tích map thành tóm tắt họp cán bộ VN. JSON thuần:
{
  "context": str,
  "document_types": [str],
  "main_content": [str],
  "decision_points": [{"decision": str, "page": int|null, "clause": str|null, "urgency": "cao|trung_binh|thap", "source_file": str|null}],
  "impact": [str],
  "legal_effects": [{"effect": str, "page": int|null, "clause": str|null}],
  "authorities_duties": [{"actor": str, "duty": str, "page": int|null, "clause": str|null}],
  "terms": [{"term": str, "explanation": str, "page": int|null, "clause": str|null, "importance": "cao|trung_binh|thap", "source_file": str|null}],
  "important_clauses": [{"clause": str, "summary": str, "page": int|null, "why_important": str, "source_file": str|null}],
  "suggested_questions": [{"question": str, "purpose": str, "related_pages": [int]}],
  "related_documents": [{"title": str, "reason": str, "type": str}]
}
5–10 ý chính; 4–8 câu hỏi thẩm định; khử trùng lặp; không bịa. Tiếng Việt."""

def _merge_lists(items: list[dict[str, Any]], key: str) -> list[Any]:
    out: list[Any] = []
    for it in items:
        val = it.get(key) or []
        if isinstance(val, list):
            out.extend(val)
    return out


def _dedupe_terms(terms: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        term = str(t.get("term") or "").strip().lower()
        if not term or term in seen:
            continue
        seen.add(term)
        result.append(t)
        if len(result) >= limit:
            break
    return result


async def _map_chunk(
    sem: asyncio.Semaphore,
    chunk: dict[str, Any],
    idx: int,
    total: int,
) -> dict[str, Any]:
    async with sem:
        user = (
            f"Phân tích đoạn tài liệu họp/thẩm định — phần {idx + 1}/{total}.\n"
            f"Tệp: {chunk.get('source_file')}\n"
            f"Trang {chunk['page_start']}–{chunk['page_end']}:\n"
            f"YÊU CẦU QUAN TRỌNG: Trích xuất ÍT NHẤT 3-5 thuật ngữ chuyên ngành nổi bật trong đoạn này "
            f"(có thể là thuật ngữ kỹ thuật, pháp lý, khoa học, hành chính). "
            f"Nếu tài liệu bằng tiếng Anh, dịch thuật ngữ sang tiếng Việt trong phần explanation.\n"
            f"Chú ý thêm: căn cứ, Điều/Khoản/Điểm, thẩm quyền, hiệu lực, trách nhiệm thi hành.\n\n"
            f"{chunk['text'][: settings.max_chars_per_chunk]}"
        )
        try:
            data = await achat_json(MAP_SYSTEM, user, temperature=0.15, max_tokens=2200)
        except Exception as e:
            return {
                "error": str(e)[:200],
                "source_file": chunk.get("source_file"),
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
                "main_points": [],
                "decision_points": [],
                "impacts": [],
                "terms": [],
                "important_clauses": [],
                "context_hints": [],
                "related_regulations_hints": [],
            }
        data["_meta"] = {
            "source_file": chunk.get("source_file"),
            "page_start": chunk["page_start"],
            "page_end": chunk["page_end"],
        }
        # stamp source_file onto nested items when missing
        src = chunk.get("source_file")
        for key in ("decision_points", "terms", "important_clauses", "main_points", "impacts"):
            for item in data.get(key) or []:
                if isinstance(item, dict) and "source_file" not in item:
                    item["source_file"] = src
        return data


def _corpus_text(docs: list[DocumentText], max_chars: int = 200_000) -> str:
    parts: list[str] = []
    n = 0
    for d in docs:
        for p in d.pages:
            t = p.text or ""
            if not t:
                continue
            parts.append(t)
            n += len(t)
            if n >= max_chars:
                return "\n".join(parts)[:max_chars]
    return "\n".join(parts)


def _preextract_legal(docs: list[DocumentText]) -> dict[str, Any]:
    """Rule-based signals for VN legal/admin corpus (works with or without LLM)."""
    text = _corpus_text(docs)
    signals = detect_document_signals(text)
    refs = extract_legal_references(text, limit=40)
    clauses = extract_clause_mentions(text, limit=40)
    terms = match_common_terms(text, limit=20)
    return {
        "document_type_guess": signals.get("document_type_guess"),
        "signals": signals.get("signals") or [],
        "legal_references": refs,
        "clause_mentions": clauses,
        "dictionary_terms": terms,
    }


def _stitch_from_maps(
    maps: list[dict[str, Any]],
    docs: list[DocumentText],
    preextract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Local reduce (no LLM) — keeps SLA when proxy is slow."""
    pre = preextract or {}
    dtype = pre.get("document_type_guess") or "khac"
    signals = pre.get("signals") or []
    main_points = _merge_lists(maps, "main_points")
    main = []
    for p in main_points:
        if isinstance(p, dict) and p.get("point"):
            main.append(str(p["point"]))
        elif isinstance(p, str):
            main.append(p)
    decisions = _merge_lists(maps, "decision_points")
    impacts = []
    for x in _merge_lists(maps, "impacts"):
        if isinstance(x, dict) and x.get("impact"):
            impacts.append(str(x["impact"]))
        elif isinstance(x, str):
            impacts.append(x)
    hints = _merge_lists(maps, "context_hints")
    context = (
        f"Hồ sơ nhà nước/pháp luật: {len(docs)} tệp, {sum(d.total_pages for d in docs)} trang. "
        f"Loại VB (nhận diện): {dtype}. "
        + ("; ".join(str(s) for s in signals[:3]) + ". " if signals else "")
        + (" ".join(str(h) for h in hints[:2]) if hints else "")
    ).strip()
    questions = []
    # Từ decision_points
    for d in decisions[:4]:
        if isinstance(d, dict) and d.get("decision"):
            questions.append(
                {
                    "question": f"Về điểm quyết định: {d['decision'][:160]}?",
                    "purpose": "Thảo luận họp",
                    "related_pages": [d["page"]] if d.get("page") is not None else [],
                }
            )
    # Từ main_points — tạo câu hỏi phản biện
    for p in main_points[:4]:
        txt = p.get("point", p) if isinstance(p, dict) else str(p)
        if txt and len(questions) < 8:
            questions.append(
                {
                    "question": f"Cơ sở và bằng chứng cho nhận định: '{str(txt)[:120]}' là gì?",
                    "purpose": "Phản biện",
                    "related_pages": [p["page"]] if isinstance(p, dict) and p.get("page") else [],
                }
            )
    # Từ impacts
    for imp in _merge_lists(maps, "impacts")[:3]:
        txt = imp.get("impact", imp) if isinstance(imp, dict) else str(imp)
        if txt and len(questions) < 8:
            questions.append(
                {
                    "question": f"Tác động '{str(txt)[:100]}' được đánh giá và kiểm soát như thế nào?",
                    "purpose": "Đánh giá rủi ro",
                    "related_pages": [imp["page"]] if isinstance(imp, dict) and imp.get("page") else [],
                }
            )
    # Câu hỏi mặc định nếu vẫn ít
    default_qs = [
        {"question": "Thẩm quyền ban hành và căn cứ pháp lý của văn bản là gì?", "purpose": "Thẩm định", "related_pages": [1]},
        {"question": "Những điểm nào cần xin ý kiến lãnh đạo trước khi ban hành hoặc triển khai?", "purpose": "Chuẩn bị họp", "related_pages": []},
        {"question": "Các nguồn lực (ngân sách, nhân lực, thời gian) cần thiết có khả thi không?", "purpose": "Thực thi", "related_pages": []},
        {"question": "Rủi ro và thách thức lớn nhất khi thực hiện là gì, và giải pháp ứng phó?", "purpose": "Quản lý rủi ro", "related_pages": []},
        {"question": "Các bên liên quan chính và trách nhiệm cụ thể của từng bên là gì?", "purpose": "Phân công nhiệm vụ", "related_pages": []},
    ]
    for q in default_qs:
        if len(questions) >= 6:
            break
        questions.append(q)
    related = list(pre.get("legal_references") or []) + _merge_lists(
        maps, "related_regulations_hints"
    )
    return {
        "context": context[:1200],
        "document_types": [dtype] if dtype else [],
        "main_content": main[:12] or ["Đã trích các đoạn chính — xem panel thuật ngữ/điều khoản."],
        "decision_points": decisions[:12]
        or [
            {
                "decision": "Rà soát thẩm quyền và căn cứ ban hành",
                "page": None,
                "clause": None,
                "urgency": "cao",
            }
        ],
        "impact": impacts[:10] or ["Cần đánh giá tác động triển khai khi họp."],
        "legal_effects": _merge_lists(maps, "legal_effects")[:15],
        "authorities_duties": _merge_lists(maps, "authorities_duties")[:15],
        "terms": _dedupe_terms(
            _merge_lists(maps, "terms") + list(pre.get("dictionary_terms") or []), 40
        ),
        "important_clauses": _merge_lists(maps, "important_clauses")[:20]
        or [
            {"clause": c, "summary": c, "page": None, "why_important": "Xuất hiện trong hồ sơ"}
            for c in (pre.get("clause_mentions") or [])[:8]
        ],
        "suggested_questions": questions[:10],
        "related_documents": related[:15],
        "mode": "stitch_map",
    }


async def _reduce(
    maps: list[dict[str, Any]],
    file_names: list[str],
    total_pages: int,
    preextract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compact = {
        "files": file_names,
        "total_pages": total_pages,
        "preextract": {
            "document_type_guess": (preextract or {}).get("document_type_guess"),
            "signals": ((preextract or {}).get("signals") or [])[:8],
            "legal_references": ((preextract or {}).get("legal_references") or [])[:12],
        },
        "context_hints": _merge_lists(maps, "context_hints")[:15],
        "doc_type_hints": _merge_lists(maps, "doc_type_hints")[:10],
        "main_points": _merge_lists(maps, "main_points")[:20],
        "decision_points": _merge_lists(maps, "decision_points")[:15],
        "impacts": _merge_lists(maps, "impacts")[:12],
        "legal_effects": _merge_lists(maps, "legal_effects")[:10],
        "authorities_duties": _merge_lists(maps, "authorities_duties")[:12],
        "terms": _dedupe_terms(_merge_lists(maps, "terms"), 20),
        "important_clauses": _merge_lists(maps, "important_clauses")[:12],
        "related_regulations_hints": _merge_lists(maps, "related_regulations_hints")[:12],
    }
    raw = __import__("json").dumps(compact, ensure_ascii=False)[:18000]
    user = "Tổng hợp map → tóm tắt họp QPPL VN. INPUT:\n" + raw
    return await achat_json(REDUCE_SYSTEM, user, temperature=0.2, max_tokens=1800)


def _heuristic_fallback(docs: list[DocumentText], preextract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Offline/demo summary when LLM is unavailable — still VN legal-aware."""
    pre = preextract or _preextract_legal(docs)
    snippets: list[str] = []
    for d in docs:
        for p in d.pages[:3]:
            if p.text:
                snippets.append(f"[{d.filename} tr.{p.page}] {p.text[:240]}")
    dtype = pre.get("document_type_guess") or "khac"
    signals = pre.get("signals") or []
    context = (
        f"Hồ sơ nhà nước/pháp luật: {len(docs)} tệp, tổng {sum(d.total_pages for d in docs)} trang. "
        f"Nhận diện sơ bộ loại VB: {dtype}. "
        + ("; ".join(signals[:4]) + ". " if signals else "")
        + "Chế độ heuristic (chưa gọi LLM) — bật XAI_API_KEY để tóm tắt thẩm định đầy đủ."
    )
    important = [
        {
            "clause": c,
            "summary": f"Xuất hiện trong hồ sơ: {c}",
            "page": None,
            "why_important": "Cấu trúc điều khoản cần rà khi họp/thẩm định",
            "source_file": None,
        }
        for c in (pre.get("clause_mentions") or [])[:12]
    ]
    return {
        "context": context,
        "document_types": [dtype] if dtype else [],
        "main_content": snippets[:8] or ["Không trích được nội dung text."],
        "decision_points": [
            {
                "decision": "Rà soát thẩm quyền ban hành và căn cứ pháp lý của văn bản",
                "page": None,
                "clause": None,
                "urgency": "cao",
                "source_file": None,
            },
            {
                "decision": "Cần bật LLM (XAI_API_KEY) để trích điểm quyết định / hiệu lực tự động",
                "page": None,
                "clause": None,
                "urgency": "trung_binh",
                "source_file": None,
            },
        ],
        "impact": [
            "Tác động đến tổ chức thi hành, người dân/DN và tính thống nhất hệ thống QPPL cần phân tích đầy đủ bằng LLM."
        ],
        "legal_effects": [],
        "authorities_duties": [],
        "terms": pre.get("dictionary_terms") or [],
        "important_clauses": important,
        "suggested_questions": [
            {
                "question": "Văn bản thuộc loại gì (Luật, Nghị định, Thông tư, Quyết định…) và ai có thẩm quyền ban hành?",
                "purpose": "Xác định thẩm quyền",
                "related_pages": [1],
            },
            {
                "question": "Các căn cứ ban hành đã đầy đủ, còn hiệu lực và đúng thứ bậc pháp lý chưa?",
                "purpose": "Thẩm định hợp pháp",
                "related_pages": [1],
            },
            {
                "question": "Điều khoản nào quy định trách nhiệm thi hành, hiệu lực, bãi bỏ/sửa đổi?",
                "purpose": "Tổ chức thực hiện",
                "related_pages": [],
            },
            {
                "question": "Có chồng chéo hoặc mâu thuẫn với Luật/Nghị định/Thông tư liên quan không?",
                "purpose": "Thống nhất hệ thống pháp luật",
                "related_pages": [],
            },
            {
                "question": "Những điểm nào cần xin ý kiến lãnh đạo trước khi trình ban hành/phê duyệt?",
                "purpose": "Chuẩn bị họp",
                "related_pages": [],
            },
        ],
        "related_documents": pre.get("legal_references") or [],
        "mode": "heuristic",
        "domain": "phap_luat_va_hanh_chinh_nha_nuoc_viet_nam",
    }


def _build_page_index(docs: list[DocumentText]) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for d in docs:
        for p in d.pages:
            if not (p.text or "").strip() and not p.tables:
                continue
            index.append(
                {
                    "doc_id": d.doc_id,
                    "filename": d.filename,
                    "page": p.page,
                    "text": p.text,
                    "html": getattr(p, "html", "") or "",
                    "tables": getattr(p, "tables", None) or [],
                }
            )
    return index


def _timing_block(
    *,
    t0: float,
    extract_s: float = 0.0,
    index_s: float = 0.0,
    preextract_s: float = 0.0,
    map_s: float = 0.0,
    reduce_s: float = 0.0,
    save_s: float = 0.0,
    upload_s: float = 0.0,
    llm_used: bool = False,
    cold_start: bool = True,
) -> dict[str, Any]:
    """SLA timing — always measured on THIS request (not cache lookup)."""
    total = round(time.perf_counter() - t0, 3)
    phases = {
        "upload_save_seconds": round(upload_s, 3),
        "extract_seconds": round(extract_s, 3),
        "index_chunk_seconds": round(index_s, 3),
        "preextract_legal_seconds": round(preextract_s, 3),
        "llm_map_seconds": round(map_s, 3),
        "llm_reduce_seconds": round(reduce_s, 3),
        "persist_seconds": round(save_s, 3),
        "total_seconds": total,
    }
    return {
        "cold_start": cold_start,
        "measured_on": "this_request_wall_clock",
        "llm_used": llm_used,
        "target_seconds": settings.target_seconds,
        "sla_limit_seconds": 60,
        "within_60s": total < 60,
        "within_target": total < settings.target_seconds,
        "phases": phases,
        "note": (
            "elapsed_seconds = tổng thời gian xử lý request này từ lúc bắt đầu "
            "(upload/extract → phân tích → lưu). Mở lại job cũ KHÔNG tính là xử lý mới."
            + (
                " Chế độ heuristic (không LLM): chỉ extract + rule-based — rất nhanh, "
                "chưa phải SLA tóm tắt AI đầy đủ."
                if not llm_used
                else " Chế độ LLM full: map-reduce song song — đây mới là SLA họp gấp."
            )
        ),
    }


async def analyze_documents(
    docs: list[DocumentText],
    *,
    job_id: str | None = None,
    title: str | None = None,
    t0: float | None = None,
    extract_seconds: float = 0.0,
    upload_seconds: float = 0.0,
) -> dict[str, Any]:
    t0 = t0 if t0 is not None else time.perf_counter()
    job_id = job_id or store.new_job_id()
    total_pages = sum(d.total_pages for d in docs)
    file_meta = [
        {
            "filename": d.filename,
            "path": d.path,
            "doc_id": d.doc_id,
            "file_type": d.file_type,
            "total_pages": d.total_pages,
            "total_chars": d.total_chars,
            "engine": d.engine,
            "sha256": d.sha256,
            "warnings": d.warnings,
        }
        for d in docs
    ]

    t_index0 = time.perf_counter()
    # Build chunks across all docs — cap map calls for SLA
    chunks: list[dict[str, Any]] = []
    # Distribute max_map_chunks across files
    n_docs = max(1, len(docs))
    per_doc_cap = max(1, settings.max_map_chunks // n_docs)
    leftover = settings.max_map_chunks - per_doc_cap * n_docs
    for di, d in enumerate(docs):
        cap = per_doc_cap + (1 if di < leftover else 0)
        chunks.extend(
            chunk_pages(
                d.pages,
                pages_per_chunk=settings.chunk_pages,
                max_chars=settings.max_chars_per_chunk,
                source_file=d.filename,
                max_chunks=max(1, cap),
            )
        )

    page_index = _build_page_index(docs)
    index_s = time.perf_counter() - t_index0

    t_pre0 = time.perf_counter()
    preextract = _preextract_legal(docs) if docs else {}
    preextract_s = time.perf_counter() - t_pre0

    store.save(
        job_id,
        {
            "status": "processing",
            "created_at": time.time(),
            "title": title or (docs[0].filename if docs else "untitled"),
            "domain": "phap_luat_va_hanh_chinh_nha_nuoc_viet_nam",
            "files": file_meta,
            "total_pages": total_pages,
            "chunk_count": len(chunks),
            "page_index": page_index,
            "preextract": {
                "document_type_guess": preextract.get("document_type_guess"),
                "signals": preextract.get("signals"),
                "legal_references": preextract.get("legal_references"),
                "clause_mentions": (preextract.get("clause_mentions") or [])[:20],
            },
        },
    )

    if not docs:
        timing = _timing_block(
            t0=t0,
            extract_s=extract_seconds,
            upload_s=upload_seconds,
            index_s=index_s,
            preextract_s=preextract_s,
            llm_used=False,
        )
        result = {
            "job_id": job_id,
            "status": "error",
            "error": "Không có tài liệu hợp lệ",
            "elapsed_seconds": timing["phases"]["total_seconds"],
            "within_60s": timing["within_60s"],
            "timing": timing,
        }
        store.save(job_id, result)
        return result

    if not llm_enabled():
        summary = _heuristic_fallback(docs, preextract)
        t_save0 = time.perf_counter()
        timing = _timing_block(
            t0=t0,
            extract_s=extract_seconds,
            upload_s=upload_seconds,
            index_s=index_s,
            preextract_s=preextract_s,
            llm_used=False,
        )
        result = {
            "job_id": job_id,
            "status": "completed",
            "title": title or docs[0].filename,
            "domain": "phap_luat_va_hanh_chinh_nha_nuoc_viet_nam",
            "files": file_meta,
            "total_pages": total_pages,
            "chunk_count": len(chunks),
            "elapsed_seconds": timing["phases"]["total_seconds"],
            "within_60s": timing["within_60s"],
            "target_seconds": settings.target_seconds,
            "timing": timing,
            "summary": {
                "context": summary["context"],
                "document_types": summary.get("document_types") or [],
                "main_content": summary["main_content"],
                "decision_points": summary["decision_points"],
                "impact": summary["impact"],
                "legal_effects": summary.get("legal_effects") or [],
                "authorities_duties": summary.get("authorities_duties") or [],
            },
            "terminology": summary["terms"],
            "important_clauses": summary["important_clauses"],
            "suggested_questions": summary["suggested_questions"],
            "related_documents": summary["related_documents"],
            "preextract": preextract,
            "page_index": page_index,
            "llm_used": False,
            "created_at": time.time(),
            "warnings": ["LLM disabled — set XAI_API_KEY for full AI analysis"]
            + [w for d in docs for w in d.warnings],
        }
        store.save(job_id, result)
        timing["phases"]["persist_seconds"] = round(time.perf_counter() - t_save0, 3)
        result["timing"] = timing
        result["elapsed_seconds"] = timing["phases"]["total_seconds"] = round(
            time.perf_counter() - t0, 3
        )
        result["within_60s"] = result["elapsed_seconds"] < 60
        timing["within_60s"] = result["within_60s"]
        store.save(job_id, result)
        return result

    t_map0 = time.perf_counter()
    sem = asyncio.Semaphore(settings.map_concurrency)
    maps = await asyncio.gather(
        *[_map_chunk(sem, c, i, len(chunks)) for i, c in enumerate(chunks)]
    )
    map_s = time.perf_counter() - t_map0
    map_errors = [m for m in maps if m.get("error")]
    maps_ok = [m for m in maps if not m.get("error")]

    t_red0 = time.perf_counter()
    summary: dict[str, Any] = {}
    reduce_err: str | None = None
    # SLA: 9flare reduce often exceeds 30s alone — default to local stitch after map.
    # Set LLM_USE_REDUCE=true only when proxy is fast enough.
    elapsed_after_map = time.perf_counter() - t0
    # Threshold 45s: nếu map xong trước 45s, vẫn còn ~10-15s để reduce trong SLA 60s
    # LLM_USE_REDUCE=true cần được set trong .env để bật (mặc định tắt cho SLA an toàn)
    use_llm_reduce = (
        bool(maps_ok)
        and bool(getattr(settings, "llm_use_reduce", False))
        and elapsed_after_map < 45
    )
    try:
        if not maps_ok:
            reduce_err = "all_map_chunks_failed"
            summary = _stitch_from_maps([], docs, preextract)
        elif use_llm_reduce:
            summary = await _reduce(
                maps_ok, [d.filename for d in docs], total_pages, preextract=preextract
            )
            if not (summary.get("context") or summary.get("main_content")):
                reduce_err = "reduce_empty"
                summary = _stitch_from_maps(maps_ok, docs, preextract)
        else:
            reduce_err = f"skip_reduce_for_sla(elapsed_after_map={elapsed_after_map:.1f}s)"
            summary = _stitch_from_maps(maps_ok, docs, preextract)
    except Exception as e:
        reduce_err = str(e)[:200]
        summary = _stitch_from_maps(maps_ok, docs, preextract)
    reduce_s = time.perf_counter() - t_red0

    # Merge rule-based legal refs if model omitted them
    related = list(summary.get("related_documents") or [])
    seen_rel = {str(r.get("title", "")).lower() for r in related if isinstance(r, dict)}
    for ref in preextract.get("legal_references") or []:
        t = str(ref.get("title", "")).lower()
        if t and t not in seen_rel:
            related.append(ref)
            seen_rel.add(t)

    terms = summary.get("terms") or _dedupe_terms(_merge_lists(list(maps), "terms"))
    terms = _dedupe_terms(list(terms) + list(preextract.get("dictionary_terms") or []), 50)

    timing = _timing_block(
        t0=t0,
        extract_s=extract_seconds,
        upload_s=upload_seconds,
        index_s=index_s,
        preextract_s=preextract_s,
        map_s=map_s,
        reduce_s=reduce_s,
        llm_used=True,
    )
    result = {
        "job_id": job_id,
        "status": "completed",
        "title": title or docs[0].filename,
        "domain": "phap_luat_va_hanh_chinh_nha_nuoc_viet_nam",
        "files": file_meta,
        "total_pages": total_pages,
        "chunk_count": len(chunks),
        "elapsed_seconds": timing["phases"]["total_seconds"],
        "within_60s": timing["within_60s"],
        "target_seconds": settings.target_seconds,
        "timing": timing,
        "summary": {
            "context": summary.get("context") or "",
            "document_types": summary.get("document_types")
            or ([preextract.get("document_type_guess")] if preextract.get("document_type_guess") else []),
            "main_content": summary.get("main_content") or [],
            "decision_points": summary.get("decision_points") or [],
            "impact": summary.get("impact") or [],
            "legal_effects": summary.get("legal_effects")
            or _merge_lists(list(maps), "legal_effects"),
            "authorities_duties": summary.get("authorities_duties")
            or _merge_lists(list(maps), "authorities_duties"),
        },
        "terminology": terms,
        "important_clauses": summary.get("important_clauses")
        or _merge_lists(list(maps), "important_clauses"),
        "suggested_questions": summary.get("suggested_questions") or [],
        "related_documents": related,
        "preextract": {
            "document_type_guess": preextract.get("document_type_guess"),
            "signals": preextract.get("signals"),
            "legal_references": preextract.get("legal_references"),
            "clause_mentions": (preextract.get("clause_mentions") or [])[:30],
            "dictionary_terms": (preextract.get("dictionary_terms") or [])[:40],
        },
        "page_index": page_index,
        "llm_used": True,
        "model": settings.llm_model,
        "map_errors": map_errors[:5] if map_errors else [],
        "reduce_error": reduce_err,
        "warnings": [w for d in docs for w in d.warnings]
        + ([f"reduce_fallback: {reduce_err}"] if reduce_err else [])
        + ([f"map_chunk_errors: {len(map_errors)}"] if map_errors else []),
        "created_at": time.time(),
    }
    t_save0 = time.perf_counter()
    store.save(job_id, result)
    timing["phases"]["persist_seconds"] = round(time.perf_counter() - t_save0, 3)
    timing["phases"]["total_seconds"] = round(time.perf_counter() - t0, 3)
    timing["within_60s"] = timing["phases"]["total_seconds"] < 60
    timing["within_target"] = timing["phases"]["total_seconds"] < settings.target_seconds
    result["timing"] = timing
    result["elapsed_seconds"] = timing["phases"]["total_seconds"]
    result["within_60s"] = timing["within_60s"]
    store.save(job_id, result)
    return result


async def analyze_paths(
    paths: list[str],
    *,
    job_id: str | None = None,
    title: str | None = None,
    t0: float | None = None,
    upload_seconds: float = 0.0,
) -> dict[str, Any]:
    t0 = t0 if t0 is not None else time.perf_counter()
    t_ex0 = time.perf_counter()
    docs: list[DocumentText] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            docs.extend(extract_folder(path, max_pages=settings.max_pages_budget if settings.max_pages_budget and settings.max_pages_budget > 0 else None))
        elif path.is_file():
            docs.append(extract_document(path))
        else:
            raise FileNotFoundError(p)
    # Page budget: <=0 means keep ALL pages (cấm cắt nội dung)
    budget = settings.max_pages_budget
    if budget is not None and budget > 0:
        trimmed: list[DocumentText] = []
        used = 0
        for d in docs:
            if used >= budget:
                break
            if used + d.total_pages > budget:
                keep = budget - used
                d.pages = d.pages[:keep]
                d.total_pages = len(d.pages)
                d.total_chars = sum(p.char_count for p in d.pages)
                d.warnings.append(
                    f"Cắt còn {keep} trang theo max_pages_budget={budget} "
                    f"(đặt MAX_PAGES_BUDGET=0 để không cắt)."
                )
            trimmed.append(d)
            used += d.total_pages
        docs = trimmed
    extract_seconds = time.perf_counter() - t_ex0
    return await analyze_documents(
        docs,
        job_id=job_id,
        title=title,
        t0=t0,
        extract_seconds=extract_seconds,
        upload_seconds=upload_seconds,
    )


async def analyze_folder(folder: str, **kwargs: Any) -> dict[str, Any]:
    return await analyze_paths([folder], **kwargs)
```

---

## FILE: `app/extract.py` — chỉ `chunk_pages` + `_smart_truncate` (cắt input map)

```python
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PageText:
    page: int  # 1-based
    text: str
    char_count: int = 0
    html: str = ""  # optional rich HTML for UI (tables preserved)
    tables: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.char_count = len(self.text)


@dataclass
class DocumentText:
    path: str
    filename: str
    doc_id: str
    file_type: str
    pages: list[PageText] = field(default_factory=list)
    engine: str = ""
    total_pages: int = 0
    total_chars: int = 0
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    def full_text(self, page_markers: bool = True) -> str:
        parts: list[str] = []
        for p in self.pages:
            if page_markers:
                parts.append(f"\n----- TRANG {p.page} -----\n{p.text}")
            else:
                parts.append(p.text)
        return "\n".join(parts).strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ... (extract PDF omitted — see SNIPPETS-PDF-PIPELINE.md) ...

def chunk_pages(
    pages: list[PageText],
    *,
    pages_per_chunk: int = 8,
    max_chars: int = 9000,
    source_file: str = "",
    max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    """Group consecutive pages into analysis chunks for map-reduce."""
    n = len(pages)
    if n == 0:
        return []

    if max_chunks is not None and max_chunks > 0:
        k = min(max_chunks, n)
        chunks: list[dict[str, Any]] = []
        for ci in range(k):
            start = (ci * n) // k
            end = ((ci + 1) * n) // k
            batch = pages[start:end]
            if not batch:
                continue
            raw_parts: list[str] = []
            for p in batch:
                raw_parts.append(f"[Trang {p.page}]\n{p.text}")
            body = "\n".join(raw_parts)
            if len(body) > max_chars:
                body = _smart_truncate(body, max_chars)
            chunks.append(
                {
                    "chunk_id": f"{source_file}:{batch[0].page}-{batch[-1].page}",
                    "source_file": source_file,
                    "page_start": batch[0].page,
                    "page_end": batch[-1].page,
                    "text": body,
                    "char_count": len(body),
                }
            )
        return chunks

    chunks: list[dict[str, Any]] = []
    i = 0
    while i < n:
        batch: list[PageText] = []
        chars = 0
        while i < n and len(batch) < pages_per_chunk:
            p = pages[i]
            if batch and chars + p.char_count > max_chars:
                break
            batch.append(p)
            chars += p.char_count
            i += 1
            if chars >= max_chars:
                break
        if not batch:
            p = pages[i]
            text = p.text[:max_chars]
            batch = [PageText(page=p.page, text=text)]
            i += 1
        page_start = batch[0].page
        page_end = batch[-1].page
        body = "\n".join(f"[Trang {p.page}]\n{p.text}" for p in batch)
        if len(body) > max_chars:
            body = _smart_truncate(body, max_chars)
        chunks.append(
            {
                "chunk_id": f"{source_file}:{page_start}-{page_end}",
                "source_file": source_file,
                "page_start": page_start,
                "page_end": page_end,
                "text": body,
                "char_count": len(body),
            }
        )
    return chunks


def _smart_truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.45)
    mid = int(max_chars * 0.25)
    tail = max_chars - head - mid - 80
    mid_start = max(0, (len(text) // 2) - mid // 2)
    return (
        text[:head]
        + "\n\n[…đã rút gọn phần giữa để xử lý nhanh…]\n\n"
        + text[mid_start : mid_start + mid]
        + "\n\n[…]\n\n"
        + text[-tail:]
    )
```

---

## FILE: `app/llm.py`

```python
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openai import AsyncOpenAI, OpenAI

from .config import settings


def _client_kwargs() -> dict[str, Any]:
    key = settings.api_key
    if not key:
        raise RuntimeError(
            "Missing API key. Set LLM_API_KEY in environment / .env"
        )
    base = (settings.llm_base_url or "").rstrip("/") + "/"
    if settings.provider == "gemini" and "generativelanguage.googleapis.com" not in base:
        base = "https://generativelanguage.googleapis.com/v1beta/openai/"
    return {
        "api_key": key,
        "base_url": base,
        "timeout": float(settings.llm_timeout_seconds),
        "max_retries": 1,
    }


def sync_client() -> OpenAI:
    return OpenAI(**_client_kwargs())


def async_client() -> AsyncOpenAI:
    return AsyncOpenAI(**_client_kwargs())


def llm_enabled() -> bool:
    return bool(settings.api_key)


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def parse_json_object(text: str) -> dict[str, Any]:
    """Best-effort JSON object extraction from model output."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"value": obj}
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            return obj if isinstance(obj, dict) else {"value": obj}
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else {"value": obj}
        except json.JSONDecodeError:
            pass
    return {"raw": text}


def _use_json_mode() -> bool:
    # Many OpenAI-compatible proxies (9flare/Claude) hang or double-fail on response_format
    if settings.llm_force_json_mode:
        return True
    base = (settings.llm_base_url or "").lower()
    model = (settings.llm_model or "").lower()
    if "9flare" in base or "claude" in model or model.startswith("pro/"):
        return False
    if settings.provider in ("openai_compatible", "xai"):
        # xAI often supports json_object; 9flare already excluded
        return "x.ai" in base or "openai.com" in base
    return False


async def achat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    client = async_client()
    messages = [
        {
            "role": "system",
            "content": system
            + "\n\nTrả về đúng một JSON object hợp lệ, không markdown, không giải thích ngoài JSON.",
        },
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if _use_json_mode():
        kwargs["response_format"] = {"type": "json_object"}

    timeout = float(settings.llm_timeout_seconds)

    async def _call() -> str:
        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    try:
        content = await asyncio.wait_for(_call(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"LLM timeout after {timeout}s") from e
    except Exception as exc:
        exc_str = str(exc).lower()
        # Chỉ retry khi lỗi liên quan response_format không được hỗ trợ.
        # Không retry AuthenticationError, RateLimitError, v.v. (tốn thêm timeout vô ích).
        is_format_error = (
            "response_format" in exc_str
            or "invalid_request" in exc_str
            or "unsupported" in exc_str
        )
        if not is_format_error:
            raise
        kwargs.pop("response_format", None)
        try:
            content = await asyncio.wait_for(_call(), timeout=timeout)
        except asyncio.TimeoutError as e2:
            raise TimeoutError(f"LLM timeout after {timeout}s (retry)") from e2
    return parse_json_object(content)


async def achat_text(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    client = async_client()
    timeout = float(settings.llm_timeout_seconds)

    async def _call() -> str:
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    return await asyncio.wait_for(_call(), timeout=timeout)


def chat_json_sync(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    client = sync_client()
    messages = [
        {
            "role": "system",
            "content": system
            + "\n\nTrả về đúng một JSON object hợp lệ, không markdown.",
        },
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if _use_json_mode():
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception:
        kwargs.pop("response_format", None)
        resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    return parse_json_object(content)
```

---

## FILE: `app/domain_vn_legal.py` (preextract terms/clauses)

```python
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
```

---

## FILE: `web/api-bridge.js` — `mapDocData` + `termList` (schema consumer)

```javascript
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
    // Cite: dùng page từ item đầu nếu backend có, không thì tr.1–N
    function firstPage(items, key) {
      if (!items || !items.length) return null;
      var x = items[0];
      if (x && x.page != null) return x.page;
      if (x && x.pages && x.pages[0] != null) return x.pages[0];
      if (x && x.related_pages && x.related_pages[0] != null) return x.related_pages[0];
      return null;
    }
    function citeFor(page, fallbackLbl) {
      if (page != null) return { cite: "live-p" + page, citeTxt: "tr." + page };
      return {
        cite: "live-p1",
        citeTxt: pages > 1 ? "tr.1–" + pages : fallbackLbl || "tr.1",
      };
    }
    var cCtx = citeFor(null, pages ? "tr.1–" + pages : "tr.1");
    var cMain = citeFor(firstPage(s.main_content), "nội dung chính");
    var cDec = citeFor(firstPage(s.decision_points), "điểm quyết");
    var cImp = citeFor(firstPage(s.impact), "tác động");
    function four(c, m, d, i) {
      return [
        { ic: "map", lbl: "Bối cảnh", txt: c, cite: cCtx.cite, citeTxt: cCtx.citeTxt },
        { ic: "doc", lbl: "Nội dung chính", txt: m, cite: cMain.cite, citeTxt: cMain.citeTxt },
        { ic: "check", lbl: "Điểm cần quyết định", txt: d, cite: cDec.cite, citeTxt: cDec.citeTxt },
        { ic: "warn", lbl: "Tác động cần lưu ý", txt: i, cite: cImp.cite, citeTxt: cImp.citeTxt },
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
```

Generated: 2026-07-18T15:59:51+07:00
