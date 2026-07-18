Bạn là senior code reviewer. Review dự án Doc Intel (UBND họp + AI) dưới đây.

Path: /home/lenkuy/doc-intel-tool
Stack: FastAPI, PyMuPDF tables, Gemini Vision OCR (scan), 9flare LLM tóm tắt, frontend HACKATHON tro-ly-hop-ubnd-v2 + api-bridge.js (không sửa CSS UI).

Yêu cầu review (tiếng Việt):
- Critical / High / Medium / Low (bugs, security, correctness)
- Rủi ro frontend glue (highlight/tooltip, upload, full pages)
- OCR + map-reduce performance
- 5–10 fix ưu tiên có action rõ


### app/main.py
```
from __future__ import annotations

from contextlib import asynccontextmanager

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import ROOT, UPLOADS, settings
from .llm import llm_enabled
from .pipeline import analyze_paths
from .qa import ask_job
from .store import store
from .tools import TOOL_DEFINITIONS, call_tool, public_result

app = FastAPI(
    title="Doc Intel Tool — Văn bản nhà nước & pháp luật Việt Nam",
    description=(
        "Tool AI-callable cho hồ sơ cơ quan nhà nước VN: Luật, Bộ luật, Nghị định, "
        "Thông tư, Quyết định, Chỉ thị, Công văn, Tờ trình, Đề án… "
        "Tóm tắt có cấu trúc, gắn cờ Điều–Khoản–thuật ngữ, gợi ý VB liên quan, "
        "hỏi đáp họp kèm trích dẫn trang/điều."
    ),
    version=__version__,
)

WEB = ROOT / "web"
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


class AnalyzeRequest(BaseModel):
    paths: list[str] = Field(..., description="Server-local file or folder paths")
    title: str | None = None


class AskRequest(BaseModel):
    job_id: str
    question: str


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "doc-intel-tool",
        "version": __version__,
        "domain": "phap_luat_va_hanh_chinh_nha_nuoc_viet_nam",
        "domain_label": "Văn bản nhà nước & pháp luật Việt Nam",
        "llm_enabled": llm_enabled(),
        "llm_provider": settings.provider,
        "model": settings.llm_model if llm_enabled() else None,
        "target_seconds": settings.target_seconds,
        "max_pages_budget": settings.max_pages_budget,
        "ocr_mode": getattr(settings, "ocr_mode", "auto"),
        "ocr_model": getattr(settings, "ocr_model", None),
        "ocr_ready": bool(
            getattr(settings, "gemini_api_key", None)
            or getattr(settings, "ocr_api_key", None)
            or __import__("os").getenv("GEMINI_API_KEY")
        ),
    }


@app.get("/")
@app.get("/tro-ly-hop-ubnd-v2.html")
def index() -> FileResponse:
    """Frontend UBND tỉnh (HACKATHON tro-ly-hop-ubnd-v2)."""
    # Prefer explicit UBND file name, fall back to index.html
    for name in ("tro-ly-hop-ubnd-v2.html", "index.html"):
        index_path = WEB / name
        if index_path.exists():
            return FileResponse(index_path)
    raise HTTPException(404, "Meeting UI not found")


@app.get("/test.png")
def asset_test_png() -> FileResponse:
    """Original frontend relative asset (do not rewrite HTML paths)."""
    p = WEB / "test.png"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/Emblem_of_Vietnam.svg.webp")
def asset_emblem() -> FileResponse:
    p = WEB / "Emblem_of_Vietnam.svg.webp"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/v1/tools")
def list_tools() -> dict[str, Any]:
    """OpenAI-style tool schemas for AI agents."""
    return {
        "tools": TOOL_DEFINITIONS,
        "howto": {
            "list": "GET /v1/tools",
            "call": "POST /v1/tools/call  body: {name, arguments}",
            "openai_functions": "Pass tools[] into chat.completions tools=…",
        },
    }


@app.post("/v1/tools/call")
async def tools_call(body: ToolCallRequest) -> JSONResponse:
    result = await call_tool(body.name, body.arguments)
    status = 404 if result.get("error") in ("job_not_found", "unknown_tool") else 200
    if result.get("error") in ("paths_required", "job_id_required", "empty_question", "job_id_and_question_required"):
        status = 400
    return JSONResponse(result, status_code=status)


@app.post("/v1/analyze")
async def analyze(body: AnalyzeRequest) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        result = await analyze_paths(body.paths, title=body.title, t0=t0)
    except FileNotFoundError as e:
        raise HTTPException(404, f"path_not_found: {e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"analyze_failed: {e}") from e
    return public_result(result)


@app.post("/v1/analyze/upload")
async def analyze_upload(
    files: list[UploadFile] = File(...),
    title: str | None = Form(default=None),
) -> dict[str, Any]:
    """Upload + analyze cold path — elapsed_seconds counts THIS request only."""
    if not files:
        raise HTTPException(400, "files_required")
    t0 = time.perf_counter()
    batch_id = hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:10]
    dest_dir = UPLOADS / batch_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        t_up0 = time.perf_counter()
        for f in files:
            name = Path(f.filename or "doc.pdf").name
            if not name.lower().endswith((".pdf", ".docx")):
                raise HTTPException(400, f"unsupported_file: {name}")
            target = dest_dir / name
            raw = await f.read()
            if len(raw) > 40_000_000:
                raise HTTPException(413, f"file_too_large: {name}")
            target.write_bytes(raw)
            saved.append(str(target))
        upload_seconds = time.perf_counter() - t_up0
        result = await analyze_paths(
            saved, title=title, t0=t0, upload_seconds=upload_seconds
        )
        # Attach client-visible wall clock for SLA proof
        result["request_wall_clock_seconds"] = round(time.perf_counter() - t0, 3)
        # Full page_index for UI — do not cut document content
        return public_result(
            result,
            include_page_index=True,
            ui_truncate=bool(settings.ui_truncate_pages),
        )
    except HTTPException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(dest_dir, ignore_errors=True)  # FIX: cleanup khi exception thường
        raise HTTPException(500, f"upload_analyze_failed: {e}") from e


@app.post("/v1/analyze/folder")
async def analyze_folder_endpoint(body: dict[str, Any]) -> dict[str, Any]:
    folder = body.get("folder") or body.get("path")
    if not folder:
        raise HTTPException(400, "folder_required")
    t0 = time.perf_counter()
    result = await analyze_paths([folder], title=body.get("title"), t0=t0)
    return public_result(result)


@app.post("/v1/bench/upload")
async def bench_upload(
    files: list[UploadFile] = File(...),
    title: str | None = Form(default="SLA bench cold upload"),
) -> dict[str, Any]:
    """
    Benchmark cold upload: always creates a NEW job.
    Returns only timing + page stats so you can prove <60s without opening old jobs.
    """
    t_client0 = time.perf_counter()
    # reuse analyze_upload logic via internal call
    if not files:
        raise HTTPException(400, "files_required")
    t0 = time.perf_counter()
    batch_id = hashlib.sha256(f"bench-{time.time()}".encode()).hexdigest()[:10]
    dest_dir = UPLOADS / batch_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        t_up0 = time.perf_counter()
        for f in files:
            name = Path(f.filename or "doc.pdf").name
            if not name.lower().endswith((".pdf", ".docx")):
                raise HTTPException(400, f"unsupported_file: {name}")
            target = dest_dir / f"bench_{int(time.time())}_{name}"
            raw = await f.read()
            if len(raw) >
...[truncated]...

```

### app/pipeline.py
```
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
        {"question": "Rủi ro và thách thức lớn nhất khi thực hiện là gì, và giải pháp ứng phó?", "purpose": "Quản lý rủi ro", "related_
...[truncated]...

```

### app/extract.py
```
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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_page_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cell_clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).replace("\x00", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _matrix_to_markdown(rows: list[list[str]]) -> str:
    """Render a 2D cell matrix as GitHub-style markdown table."""
    if not rows:
        return ""
    # normalize width
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    # drop fully empty rows/cols
    norm = [r for r in norm if any(c.strip() for c in r)]
    if not norm:
        return ""
    keep_cols = [i for i in range(width) if any(r[i].strip() for r in norm)]
    if not keep_cols:
        return ""
    norm = [[r[i] for i in keep_cols] for r in norm]
    width = len(keep_cols)

    def esc(c: str) -> str:
        return c.replace("|", "\\|").replace("\n", " ")

    header = norm[0]
    # if first row looks like data (all numbers), invent headers
    body_start = 1
    if width and all(re.fullmatch(r"[\d.,%\-\s]+", c or "") for c in header if c):
        header = [f"Cột {i + 1}" for i in range(width)]
        body_start = 0
    lines = [
        "| " + " | ".join(esc(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for r in norm[body_start:]:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


def _matrix_to_html(rows: list[list[str]], caption: str = "") -> str:
    """Compact, readable table HTML (inline styles — no frontend CSS edits)."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    norm = [r for r in norm if any(c.strip() for c in r)]
    if not norm:
        return ""
    # drop empty columns
    keep = [i for i in range(width) if any((r[i] or "").strip() for r in norm)]
    if not keep:
        return ""
    norm = [[r[i] for i in keep] for r in norm]

    wrap = (
        'style="max-width:100%;overflow-x:auto;margin:8px 0 12px;'
        'border:1px solid #e7e3d8;border-radius:8px;background:#fff"'
    )
    table = (
        'style="border-collapse:collapse;width:100%;font-size:11.5px;'
        'line-height:1.35;font-family:inherit"'
    )
    th = (
        'style="border:1px solid #d8d3c5;padding:5px 8px;background:#1a1f2b;'
        'color:#ffcd00;font-weight:600;text-align:left;white-space:nowrap;'
        'font-size:11px"'
    )
    td = (
        'style="border:1px solid #ebe6da;padding:4px 8px;vertical-align:top;'
        'color:#1a1f2b;word-break:break-word"'
    )
    td_alt = (
        'style="border:1px solid #ebe6da;padding:4px 8px;vertical-align:top;'
        'color:#1a1f2b;word-break:break-word;background:#fbf8f1"'
    )
    cap = (
        'style="caption-side:top;text-align:left;padding:6px 8px 2px;'
        'font-size:11px;font-weight:600;color:#5b5f66"'
    )

    parts = [f"<div {wrap}><table {table}>"]
    if caption:
        parts.append(f"<caption {cap}>{_html_esc(caption)}</caption>")
    parts.append("<thead><tr>")
    for c in norm[0]:
        parts.append(f"<th {th}>{_html_esc(c)}</th>")
    parts.append("</tr></thead><tbody>")
    for ri, r in enumerate(norm[1:]):
        parts.append("<tr>")
        cell_s = td_alt if ri % 2 else td
        for c in r:
            parts.append(f"<td {cell_s}>{_html_esc(c)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _html_esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bbox_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _extract_tables_pymupdf(page: Any) -> list[dict[str, Any]]:
    """Use PyMuPDF table finder when available."""
    out: list[dict[str, Any]] = []
    try:
        finder = page.find_tables()
    except Exception:
        return out
    tables = getattr(finder, "tables", None) or []
    for i, tab in enumerate(tables):
        try:
            raw = tab.extract() or []
        except Exception:
            continue
        matrix = [[_cell_clean(c) for c in (row or [])] for row in raw]
        if not matrix or sum(1 for r in matrix for c in r if c) < 2:
            continue
        bbox = tuple(float(x) for x in (tab.bbox if hasattr(tab, "bbox") else (0, 0, 0, 0)))
        md = _matrix_to_markdown(matrix)
        html = _matrix_to_html(matrix, caption=f"Bảng {i + 1}")
        out.append(
            {
                "index": i + 1,
                "bbox": bbox,
                "rows": len(matrix),
                "cols": max((len(r) for r in matrix), default=0),
                "markdown": md,
                "html": html,
                "matrix": matrix,
            }
        )
    return out


def _reconstruct_lines_from_words(page: Any) -> list[dict[str, Any]]:
    """Group words into visual lines (reading order)."""
    try:
        words = page.get_text("words") or []  # x0,y0,x1,y1,"word",block,line,wno
    except Exception:
        return []
    if not words:
        return []
    # sort by y then x
    words = sorted(words, key=lambda w: (round(w[1], 1), w[0]))
    lines: list[dict[str, Any]] = []
    cur: list[Any] = []
    cur_y: float | None = None
    y_tol = 3.5

    def flush() -> None:
        nonlocal cur, cur_y
        if not cur:
            return
        cur.sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in cur)
        x0 = min(w[0] for w in cur)
        y0 = min(w[1] for w in cur)
        x1 = max(w[2] for w in cur)
        y1 = max(w[3] for w in cur)
        # gaps between words → possible column separators
        gaps = []
        for a, b in zip(cur, cur[1:]):
            gap = b[0] - a[2]
            if gap > 8:
                gaps.append((gap, a[2], b[0], a[4], b[4]))
        lines.append(
            {
                "text": text,
                "bbox": (x0, y0, x1, y1),
                "words": cur[:],
                "gaps": gaps,
            }
        )
        cur = []
        cur_y = None

    for w in words:
        y = w[1]
        if cur_y is None or abs(y - cur_y) <= y_tol:
            cur.append(w)
            cur_y = 
...[truncated]...

```

### app/vision_ocr.py
```
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

OCR_PROMPT = """Bạn là công cụ trích xuất văn bản từ ảnh trang tài liệu hành chính/pháp lý Việt Nam.

Yêu cầu:
1. Trích TOÀN BỘ chữ đọc được trên trang (tiếng Việt + số + ký hiệu).
2. Giữ thứ tự đọc tự nhiên (trái→phải, trên→dưới).
3. Bảng: xuất dạng markdown table (| cột | ... |) nếu nhận ra hàng/cột.
4. Tiêu đề Điều/Khoản/Chương giữ nguyên xuống dòng.
5. Không tóm tắt, không giải thích, không thêm nội dung không có trên trang.
6. Nếu trang gần như trống hoặc chỉ là ảnh trang trí: trả về chuỗi rỗng.
"""


def ocr_enabled() -> bool:
    mode = (getattr(settings, "ocr_mode", "auto") or "auto").lower()
    if mode in ("off", "false", "0", "none"):
        return False
    return bool(settings.gemini_api_key or getattr(settings, "ocr_api_key", "") or "")


def _gemini_key() -> str:
    import os

    return (
        getattr(settings, "ocr_api_key", "")
        or settings.gemini_api_key
        or os.getenv("GEMINI_API_KEY", "")
        or os.getenv("OCR_API_KEY", "")
        or ""
    ).strip()


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
            return (getattr(resp, "text", None) or "").strip()

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
    Mutate PageText list: fill
...[truncated]...

```

### app/qa.py
```
from __future__ import annotations

import re
from typing import Any

from rank_bm25 import BM25Okapi

from .config import settings
from .llm import achat_json, llm_enabled
from .store import store

from .domain_vn_legal import DOMAIN_SYSTEM_PREAMBLE

QA_SYSTEM = f"""{DOMAIN_SYSTEM_PREAMBLE}

Bạn là trợ lý họp / thẩm định cho cán bộ, công chức cơ quan nhà nước Việt Nam.
Trả lời dựa CHỈ trên các đoạn trích hồ sơ (Luật, Nghị định, Thông tư, Quyết định, tờ trình, đề án…).
Trả JSON:
{{
  "answer": str,
  "citations": [{{"filename": str, "page": int, "clause": str|null, "excerpt": str}}],
  "confidence": "cao|trung_binh|thap",
  "not_found": bool
}}
Quy tắc:
- Tiếng Việt hành chính, ngắn gọn, rõ ràng; có thể dùng thuật ngữ pháp lý đúng chuẩn.
- Citation: số trang + Điều/Khoản/Điểm/Chương/Phụ lục nếu nhận diện được; số hiệu VB nếu có.
- Không suy diễn quy định ngoài đoạn trích; không bịa mức phạt, thẩm quyền, hiệu lực.
- Nếu hỏi về VB liên quan nhưng hồ sơ không nêu: nói rõ và gợi ý hướng tra cứu (không khẳng định chắc chắn).
- not_found=true khi thiếu căn cứ. excerpt ≤ ~200 ký tự từ nguồn.
"""

def _tokenize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^\w\sàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", " ", text)
    return [t for t in text.split() if len(t) > 1]


def _retrieve(page_index: list[dict[str, Any]], question: str, top_k: int) -> list[dict[str, Any]]:
    if not page_index:
        return []
    corpus_tokens = [_tokenize(p.get("text") or "") for p in page_index]
    # guard empty docs
    if not any(corpus_tokens):
        return page_index[:top_k]
    bm25 = BM25Okapi(corpus_tokens)
    scores = bm25.get_scores(_tokenize(question))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    hits: list[dict[str, Any]] = []
    for i in ranked[:top_k]:
        if scores[i] <= 0 and hits:
            break
        item = dict(page_index[i])
        item["score"] = float(scores[i])
        # trim for prompt
        item["text"] = (item.get("text") or "")[:2500]
        hits.append(item)
    return hits or [dict(page_index[0], text=(page_index[0].get("text") or "")[:2500], score=0.0)]


def _heuristic_answer(question: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    if not hits:
        return {
            "answer": "Không tìm thấy đoạn liên quan trong tài liệu đã nạp.",
            "citations": [],
            "confidence": "thap",
            "not_found": True,
            "llm_used": False,
        }
    top = hits[0]
    excerpt = (top.get("text") or "")[:220]
    return {
        "answer": (
            f"Theo đoạn liên quan nhất (trang {top.get('page')} — {top.get('filename')}): "
            f"{excerpt}…"
            "\n(Chế độ heuristic — bật XAI_API_KEY để trả lời hội thoại đầy đủ.)"
        ),
        "citations": [
            {
                "filename": top.get("filename"),
                "page": top.get("page"),
                "clause": None,
                "excerpt": excerpt,
            }
        ],
        "confidence": "trung_binh" if top.get("score", 0) > 0 else "thap",
        "not_found": False,
        "llm_used": False,
        "retrieved": [
            {"filename": h.get("filename"), "page": h.get("page"), "score": h.get("score")}
            for h in hits
        ],
    }


async def ask_job(job_id: str, question: str) -> dict[str, Any]:
    job = store.load(job_id)
    if not job:
        return {"error": "job_not_found", "job_id": job_id}
    if job.get("status") not in ("completed", "processing"):
        return {"error": "job_not_ready", "status": job.get("status"), "job_id": job_id}

    question = (question or "").strip()
    if not question:
        return {"error": "empty_question"}

    page_index = job.get("page_index") or []
    # enrich with terminology / clauses for better grounding
    extra_ctx: list[str] = []
    for t in (job.get("terminology") or [])[:15]:
        if isinstance(t, dict) and t.get
...[truncated]...

```

### app/tools.py
```
from __future__ import annotations

"""
OpenAI / agent-compatible tool definitions.

AI clients can:
1. GET /v1/tools  — list tool schemas
2. POST /v1/tools/call — execute by name
"""

from typing import Any

from .pipeline import analyze_paths
from .qa import ask_job
from .store import store

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "analyze_documents",
            "description": (
                "Phân tích thông minh văn bản nhà nước / pháp luật Việt Nam (PDF/Word hoặc thư mục): "
                "Luật, Bộ luật, Nghị định, Nghị quyết, Thông tư, Quyết định, Chỉ thị, Công văn, "
                "Tờ trình, Đề án, Quy chế… Trả về: bối cảnh thể chế, nội dung chính, điểm cần quyết, "
                "tác động, hiệu lực/bãi bỏ, trách nhiệm thi hành; gắn cờ Điều–Khoản–Điểm và thuật ngữ "
                "hành chính–pháp lý; gợi ý câu hỏi thẩm định và VB liên quan (Luật/NĐ/TT…). "
                "Tối ưu batch ~40–60 trang dưới ~60s. paths là đường dẫn local trên server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Absolute paths to PDF/DOCX files and/or folders.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional meeting/document set title.",
                    },
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analysis",
            "description": "Fetch a previous analysis job by job_id (summary, terms, questions).",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job id returned by analyze_documents."},
                    "include_page_index": {
                        "type": "boolean",
                        "description": "Include full page text index (large). Default false.",
                        "default": False,
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_document",
            "description": (
                "Hỏi đáp họp/thẩm định về hồ sơ văn bản nhà nước–pháp luật VN bằng tiếng Việt tự nhiên. "
                "Trả lời kèm trích dẫn trang và Điều/Khoản/Điểm (hoặc số hiệu VB) khi có trong hồ sơ."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "question": {
                        "type": "string",
                        "description": (
                            "Câu hỏi tiếng Việt, vd: 'Thẩm quyền ban hành?', "
                            "'Điều khoản hiệu lực?', 'Căn cứ Nghị định nào?'."
                        ),
                    },
                },
                "required": ["job_id", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "List recent document analysis jobs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
]


def _truncate_keep_tables(text: str, limit: int) -> str:
    """Truncate body text but never mid markdown table."""
    if len(text) <= limit:
        return text
    # split preserving table blocks
    import re

    parts = re.split(r"(\n\[BẢNG[^\]]*\]\n(?:\|.+\n)+)", text)
    out: list[str] = []
    n = 0
    for part in parts:
        if not part:
            continue
        is_table = part.lstrip().startswith("[BẢNG") or part.lstrip().startswith("|")
        if is_table:
            out.append(part)
            n += len(part)
            continue
        if n >= limit:
            break
        remain = limit - n
        if len(part) <= remain:
            out.append(part)
            n += len(part)
        else:
            out.append(part[:remain] + "\n[…rút gọn để hiển thị nhanh…]")
            break
    return "".join(out) if out else text[:limit] + "\n[…]"


def public_result(
    result: dict[str, Any],
    *,
    include_page_index: bool = False,
    ui_truncate: bool = False,
) -> dict[str, Any]:
    """Strip / trim heavy fields for API clients.

    Full page_index remains on disk (job store) for Q&A. UI can request a
    truncated page_index so the browser does not freeze rendering 80 dense pages.
    """
    from .config import settings

    out = dict(result)
    if not
...[truncated]...

```

### app/config.py
```
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
    ocr_concurrency: int = 10
    ocr_dpi: int = 150
    ocr_api_key: str = ""  # optional override; else gemini_api_key

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

### app/llm.py
```
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
          
...[truncated]...

```

### web/api-bridge.js
```
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

...[truncated]...

```
