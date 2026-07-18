# Doc Intel — Toàn bộ code xử lý PDF (để review)

Project: `/home/lenkuy/doc-intel-tool`

## Luồng xử lý PDF

```
Upload (main.py)
  → safe_filename + stream save (security_paths.py)
  → analyze_paths (pipeline.py)
       → extract_document / extract_folder (extract.py)   # PyMuPDF text + tables
       → enrich_pages_with_vision (vision_ocr.py)         # Gemini Vision OCR sparse pages
       → analyze_documents (pipeline.py)                  # map-reduce LLM + page_index
  → store job
  → API trả summary; UI lazy GET /pages
  → api-bridge.js renderStructuredBody + highlightTermsInDom  # hiển thị (không trích PDF)
```

## File liên quan

| File | Vai trò |
|------|---------|
| `app/extract.py` | PDF/DOCX → text + tables (PyMuPDF / pdfplumber / docx) |
| `app/vision_ocr.py` | Trang scan → PNG → Gemini Vision |
| `app/pipeline.py` | Ghép extract + OCR + LLM + page_index |
| `app/main.py` | Upload endpoints + lazy pages API |
| `app/security_paths.py` | Tên file an toàn, path allowlist |
| `app/config.py` | MAX_PAGES, OCR_*, upload limits |
| `web/api-bridge.js` | Render body (không phải extract; có trong SNIPPETS-LAYOUT-OCR.md) |

---

## FILE: `app/config.py` (cấu hình PDF/OCR/upload)

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

## FILE: `app/security_paths.py`

```python
"""Path allowlist — only paths under project data roots may be analyzed."""
from __future__ import annotations

import re
from pathlib import Path

from .config import ROOT, UPLOADS, JOBS, DATA

# Allowed roots for server-local analyze
ALLOWED_ROOTS: list[Path] = [
    UPLOADS.resolve(),
    (DATA / "demo").resolve() if (DATA / "demo").exists() else DATA.resolve(),
    (ROOT / "samples").resolve(),
    DATA.resolve(),
]


def safe_filename(name: str) -> str:
    base = Path(name or "doc.pdf").name
    base = re.sub(r"[^\w.\-() \u00C0-\u024F\u1E00-\u1EFF]+", "_", base, flags=re.UNICODE)
    base = base.strip("._ ") or "doc.pdf"
    if not base.lower().endswith((".pdf", ".docx")):
        base = base + ".pdf"
    return base[:180]


def is_under(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        root = root.resolve()
        path.relative_to(root)
        return True
    except Exception:
        return False


def resolve_allowed_path(raw: str) -> Path:
    """Resolve and enforce path is under ALLOWED_ROOTS. Raises ValueError if not."""
    if not raw or not str(raw).strip():
        raise ValueError("empty_path")
    # reject null bytes / odd schemes
    if "\x00" in raw or raw.startswith(("http://", "https://", "file:")):
        raise ValueError("invalid_path")
    p = Path(raw).expanduser()
    # relative → under UPLOADS
    if not p.is_absolute():
        p = (UPLOADS / p).resolve()
    else:
        p = p.resolve()
    for root in ALLOWED_ROOTS:
        if not root.exists():
            continue
        if is_under(p, root):
            if not p.exists():
                raise FileNotFoundError(str(p))
            return p
    raise PermissionError("path_not_allowed")


def resolve_allowed_paths(paths: list[str]) -> list[str]:
    return [str(resolve_allowed_path(p)) for p in paths]
```

---

## FILE: `app/extract.py` (trích xuất PDF/DOCX — text layer + bảng)

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
            cur_y = y if cur_y is None else (cur_y * 0.7 + y * 0.3)
        else:
            flush()
            cur = [w]
            cur_y = y
    flush()
    return lines


def _detect_aligned_table_from_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Heuristic: consecutive multi-gap lines with aligned x-positions → table.
    Outputs markdown/html tables (fallback when find_tables misses).
    """
    if len(lines) < 2:
        return []

    def line_cols(line: dict[str, Any]) -> list[str]:
        words = line["words"]
        if len(words) < 2:
            return [line["text"]]
        # split on large horizontal gaps
        cols: list[str] = []
        buf = [words[0][4]]
        for a, b in zip(words, words[1:]):
            gap = b[0] - a[2]
            if gap > 12:
                cols.append(" ".join(buf))
                buf = [b[4]]
            else:
                buf.append(b[4])
        cols.append(" ".join(buf))
        return cols

    # score lines that look multi-column
    multi = []
    for i, ln in enumerate(lines):
        cols = line_cols(ln)
        if len(cols) >= 2:
            multi.append((i, cols, ln))

    if len(multi) < 2:
        return []

    # cluster consecutive multi lines
    tables: list[dict[str, Any]] = []
    cluster: list[tuple[int, list[str], dict]] = [multi[0]]
    for item in multi[1:]:
        prev_i = cluster[-1][0]
        if item[0] <= prev_i + 2:  # allow 1 non-multi line skip
            cluster.append(item)
        else:
            if len(cluster) >= 2:
                tables.append(_cluster_to_table(cluster))
            cluster = [item]
    if len(cluster) >= 2:
        tables.append(_cluster_to_table(cluster))
    return [t for t in tables if t]


def _cluster_to_table(cluster: list[tuple[int, list[str], dict]]) -> dict[str, Any] | None:
    matrices = [c[1] for c in cluster]
    width = max(len(r) for r in matrices)
    if width < 2:
        return None
    matrix = [r + [""] * (width - len(r)) for r in matrices]
    # require some non-empty density
    filled = sum(1 for r in matrix for c in r if c.strip())
    if filled < width:
        return None
    md = _matrix_to_markdown(matrix)
    html = _matrix_to_html(matrix)
    y0 = min(c[2]["bbox"][1] for c in cluster)
    y1 = max(c[2]["bbox"][3] for c in cluster)
    x0 = min(c[2]["bbox"][0] for c in cluster)
    x1 = max(c[2]["bbox"][2] for c in cluster)
    return {
        "index": 0,
        "bbox": (x0, y0, x1, y1),
        "rows": len(matrix),
        "cols": width,
        "markdown": md,
        "html": html,
        "matrix": matrix,
        "source": "aligned_lines",
    }


def _page_text_excluding_tables(page: Any, table_bboxes: list[tuple[float, float, float, float]]) -> str:
    """Extract body text while skipping regions covered by detected tables."""
    try:
        blocks = page.get_text("blocks") or []
    except Exception:
        return _clean_page_text(page.get_text("text") or "")

    parts: list[str] = []
    for b in blocks:
        # block: x0,y0,x1,y1,text,block_no,block_type
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not str(text).strip():
            continue
        bb = (float(x0), float(y0), float(x1), float(y1))
        # skip if heavily overlapping a table
        skip = False
        for tb in table_bboxes:
            if _bbox_overlap(bb, tb):
                # if block mostly inside table bbox, skip
                skip = True
                break
        if skip:
            continue
        parts.append(str(text).strip())
    if not parts:
        return _clean_page_text(page.get_text("text") or "")
    return _clean_page_text("\n".join(parts))


def _extract_pdf_page(page: Any, page_no: int) -> PageText:
    """One PDF page: preserve tables as markdown + HTML."""
    tables = _extract_tables_pymupdf(page)
    if not tables:
        # fallback aligned-word heuristic
        lines = _reconstruct_lines_from_words(page)
        tables = _detect_aligned_table_from_lines(lines)
        for i, t in enumerate(tables):
            t["index"] = i + 1

    bboxes = [tuple(t["bbox"]) for t in tables if t.get("bbox")]
    body = _page_text_excluding_tables(page, bboxes) if tables else _clean_page_text(page.get_text("text") or "")

    # Compose plain text for LLM / search (markdown tables kept)
    text_parts: list[str] = []
    if body:
        text_parts.append(body)
    for t in tables:
        text_parts.append(f"\n[BẢNG {t.get('index', '')} — {t.get('rows')}×{t.get('cols')}]\n{t.get('markdown', '')}\n")

    # Compose HTML for UI
    html_parts: list[str] = []
    if body:
        # paragraphs
        for para in re.split(r"\n{2,}", body):
            p = para.strip()
            if not p:
                continue
            html_parts.append(f"<p>{_html_esc(p).replace(chr(10), '<br>')}</p>")
    for t in tables:
        html_parts.append(t.get("html") or "")

    return PageText(
        page=page_no,
        text="\n".join(text_parts).strip(),
        html="\n".join(html_parts),
        tables=[
            {
                "index": t.get("index"),
                "rows": t.get("rows"),
                "cols": t.get("cols"),
                "markdown": t.get("markdown"),
                "html": t.get("html"),
                "source": t.get("source", "find_tables"),
            }
            for t in tables
        ],
    )


def _extract_pdf(path: Path) -> tuple[list[PageText], str, list[str]]:
    warnings: list[str] = []
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError("PyMuPDF (pymupdf) is required for PDF extraction") from e

    doc = fitz.open(path)
    pages: list[PageText] = []
    try:
        n = doc.page_count

        def _one(i: int) -> PageText:
            page = doc.load_page(i)
            return _extract_pdf_page(page, i + 1)

        # Table detection is not always thread-safe across all builds — serialize for safety
        # but keep modest parallel for large docs when no tables needed is hard to know;
        # use sequential for correctness of find_tables.
        workers = 1 if n <= 2 else min(4, n)
        if workers == 1:
            pages = [_one(i) for i in range(n)]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_one, i): i for i in range(n)}
                tmp: dict[int, PageText] = {}
                for fut in as_completed(futs):
                    i = futs[fut]
                    tmp[i] = fut.result()
                pages = [tmp[i] for i in range(n)]

        empty = sum(1 for p in pages if not p.text)
        n_tables = sum(len(p.tables) for p in pages)
        if empty and empty == n:
            warnings.append("PDF có vẻ scan/ảnh — text layer trống; sẽ thử Gemini Vision OCR nếu bật.")
        elif empty:
            warnings.append(f"{empty}/{n} trang không có text (sẽ OCR Vision nếu bật).")
        if n_tables:
            warnings.append(f"Đã giữ cấu trúc {n_tables} bảng (markdown/HTML).")
        else:
            warnings.append(
                "Không phát hiện bảng có đường kẻ; đã thử căn cột theo vị trí chữ."
            )

        # --- Gemini Vision OCR (PDF → image → text), parallel ---
        engine = "pymupdf+tables"
        try:
            from .vision_ocr import enrich_pages_with_vision, ocr_enabled
            from .config import settings
            import asyncio

            if ocr_enabled():
                mode = (settings.ocr_mode or "auto").lower()
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # nested: run in new loop thread
                        import concurrent.futures

                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pages, ocr_warn = pool.submit(
                                lambda: asyncio.run(
                                    enrich_pages_with_vision(path, pages, mode=mode)
                                )
                            ).result()
                    else:
                        pages, ocr_warn = loop.run_until_complete(
                            enrich_pages_with_vision(path, pages, mode=mode)
                        )
                except RuntimeError:
                    pages, ocr_warn = asyncio.run(
                        enrich_pages_with_vision(path, pages, mode=mode)
                    )
                warnings.extend(ocr_warn)
                if any("OCR Gemini" in w for w in ocr_warn):
                    engine = "pymupdf+tables+gemini-vision"
            elif empty:
                warnings.append(
                    "Bật OCR: set GEMINI_API_KEY + OCR_MODE=auto|always (gemini-2.0-flash Vision)."
                )
        except Exception as e:
            warnings.append(f"OCR Vision lỗi: {str(e)[:160]}")

        return pages, engine, warnings
    finally:
        doc.close()


def _extract_docx(path: Path) -> tuple[list[PageText], str, list[str]]:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph
        from docx.table import Table
    except ImportError as e:
        raise RuntimeError("python-docx is required for Word extraction") from e

    document = Document(str(path))
    blocks: list[str] = []
    html_blocks: list[str] = []
    all_tables: list[dict[str, Any]] = []
    table_i = 0

    # Iterate body in order (paragraphs + tables)
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, document)
            t = (p.text or "").strip()
            if t:
                blocks.append(t)
                html_blocks.append(f"<p>{_html_esc(t)}</p>")
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            matrix: list[list[str]] = []
            for row in table.rows:
                # dedupe merged cell repeats in python-docx
                cells: list[str] = []
                seen_ids: set[int] = set()
                for cell in row.cells:
                    cid = id(cell._tc)
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    cells.append(_cell_clean(cell.text))
                matrix.append(cells)
            if matrix and any(any(c for c in r) for r in matrix):
                table_i += 1
                md = _matrix_to_markdown(matrix)
                html = _matrix_to_html(matrix, caption=f"Bảng {table_i}")
                blocks.append(f"\n[BẢNG {table_i} — {len(matrix)}×{max(len(r) for r in matrix)}]\n{md}\n")
                html_blocks.append(html)
                all_tables.append(
                    {
                        "index": table_i,
                        "rows": len(matrix),
                        "cols": max(len(r) for r in matrix),
                        "markdown": md,
                        "html": html,
                        "source": "docx",
                    }
                )

    full = "\n".join(blocks).strip()
    full_html = "\n".join(html_blocks)
    page_size = 2200
    pages: list[PageText] = []
    if not full:
        pages = [PageText(page=1, text="", html="", tables=[])]
    else:
        # Keep tables intact: split by paragraphs but don't cut mid-table
        chunks: list[tuple[str, str]] = []
        buf_t, buf_h = "", ""
        # simple split: use text blocks already joined — page by char with table boundaries
        parts = re.split(r"(\n\[BẢNG \d+[^\]]*\]\n(?:\|.+\n)+)", full)
        html_parts = re.split(r'(<table class="doc-table">.*?</table>)', full_html, flags=re.S)
        # Paging: associate tables với đúng page chứa markdown của bảng đó
        # (không đổ tất cả vào page 0 gây citation sai)
        for i in range(0, len(full), page_size):
            chunk = full[i : i + page_size]
            # Bảng xuất hiện trong chunk này (khớp 40 ký tự đầu markdown)
            t_in = [t for t in all_tables if t.get("markdown") and t["markdown"][:40] in chunk]
            # HTML: chỉ include đoạn html tương ứng với chunk (approximate)
            # Để đơn giản: attach full_html vào page đầu, page sau để trống
            h_slice = full_html if i == 0 else ""
            pages.append(
                PageText(
                    page=len(pages) + 1,
                    text=chunk,
                    html=h_slice,
                    tables=t_in,  # bảng gắn đúng page chứa nó
                )
            )
        # Thêm metadata tổng hợp vào page 1: chỉ ghi tables chưa xuất hiện ở page nào
        if pages:
            assigned = {t["index"] for page in pages for t in page.tables}
            unassigned = [t for t in all_tables if t["index"] not in assigned]
            if unassigned:
                # Các bảng không khớp chunk nào → gán vào page đầu
                pages[0].tables = pages[0].tables + unassigned
            if not pages[0].html:
                pages[0].html = full_html

    return (
        pages,
        "python-docx+tables",
        [
            "Word: bảng được giữ dạng markdown/HTML.",
            "Số trang Word là ước lượng theo độ dài.",
        ],
    )


def extract_document(path: str | Path) -> DocumentText:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    suffix = path.suffix.lower()
    sha = _sha256_file(path)
    doc_id = sha[:16]

    if suffix == ".pdf":
        pages, engine, warnings = _extract_pdf(path)
        file_type = "pdf"
    elif suffix in (".docx", ".doc"):
        if suffix == ".doc":
            raise ValueError("Định dạng .doc cũ không hỗ trợ — chuyển sang .docx hoặc PDF")
        pages, engine, warnings = _extract_docx(path)
        file_type = "docx"
    else:
        raise ValueError(f"Định dạng không hỗ trợ: {suffix}. Dùng PDF hoặc DOCX.")

    total_chars = sum(p.char_count for p in pages)
    return DocumentText(
        path=str(path),
        filename=path.name,
        doc_id=doc_id,
        file_type=file_type,
        pages=pages,
        engine=engine,
        total_pages=len(pages),
        total_chars=total_chars,
        sha256=sha,
        warnings=warnings,
    )


def extract_folder(
    folder: str | Path,
    *,
    patterns: tuple[str, ...] = ("*.pdf", "*.docx", "*.PDF", "*.DOCX"),
    max_pages: int | None = None,
) -> list[DocumentText]:
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))

    files: list[Path] = []
    seen: set[Path] = set()
    for pat in patterns:
        for p in sorted(folder.glob(pat)):
            if p.is_file() and p not in seen:
                seen.add(p)
                files.append(p)

    docs: list[DocumentText] = []
    page_budget = max_pages
    for f in files:
        doc = extract_document(f)
        if page_budget is not None:
            if page_budget <= 0:
                break
            if doc.total_pages > page_budget:
                doc.pages = doc.pages[:page_budget]
                doc.total_pages = len(doc.pages)
                doc.total_chars = sum(p.char_count for p in doc.pages)
                doc.warnings.append(f"Cắt còn {page_budget} trang theo budget.")
                page_budget = 0
            else:
                page_budget -= doc.total_pages
        docs.append(doc)
    return docs


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

## FILE: `app/vision_ocr.py` (OCR Gemini Vision)

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

    warnings.append(
        f"OCR Gemini Vision ({_ocr_model_name()}): {ok}/{len(need)} trang · "
        f"concurrency={_concurrency()} · dpi={_dpi()} · {elapsed}s"
    )
    return pages, warnings
```

---

## FILE: `app/pipeline.py` (điều phối extract → OCR → LLM → page_index)

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

## FILE: `app/main.py` (upload + pages API)

```python
from __future__ import annotations

import hashlib
import logging
import shutil
import time
import uuid
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
from .security_paths import resolve_allowed_path, resolve_allowed_paths, safe_filename
from .store import store
from .tools import TOOL_DEFINITIONS, call_tool, public_result

log = logging.getLogger("doc_intel.api")

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

# CORS: local demo only — override via CORS_ORIGINS env (comma-separated)
_cors = (getattr(settings, "cors_origins", None) or "http://127.0.0.1:8090,http://localhost:8090").split(",")
_cors = [o.strip() for o in _cors if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors if _cors != ["*"] else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


# --- Limits (Notion review) ---
MAX_UPLOAD_FILES = int(getattr(settings, "max_upload_files", 10) or 10)
MAX_FILE_BYTES = int(getattr(settings, "max_file_bytes", 40_000_000) or 40_000_000)
MAX_TOTAL_UPLOAD_BYTES = int(getattr(settings, "max_total_upload_bytes", 80_000_000) or 80_000_000)
CHUNK = 1024 * 1024  # 1MB stream chunks


class AnalyzeRequest(BaseModel):
    paths: list[str] = Field(..., description="Paths under server data/uploads or samples/")
    title: str | None = None


class FolderRequest(BaseModel):
    folder: str | None = None
    path: str | None = None
    title: str | None = None


class AskRequest(BaseModel):
    job_id: str
    question: str


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _public_error(status: int, code: str, *, detail: str | None = None) -> HTTPException:
    """Never leak stack traces / internal paths to clients."""
    body: dict[str, Any] = {"error": code}
    if detail and status < 500:
        body["detail"] = detail[:200]
    return HTTPException(status, body)


async def _stream_save_upload(upload: UploadFile, dest: Path, *, max_bytes: int) -> int:
    """Write upload to disk in chunks; return size. Raises HTTPException if too large."""
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await upload.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise _public_error(413, "file_too_large", detail=f"limit_mb={max_bytes // 1_000_000}")
            out.write(chunk)
    return size


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "doc-intel-tool",
        "version": __version__,
        "domain": "phap_luat_va_hanh_chinh_nha_nuoc_viet_nam",
        "llm_enabled": llm_enabled(),
        "llm_provider": settings.provider,
        "model": settings.llm_model if llm_enabled() else None,
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
    for name in ("tro-ly-hop-ubnd-v2.html", "index.html"):
        index_path = WEB / name
        if index_path.exists():
            return FileResponse(index_path)
    raise _public_error(404, "ui_not_found")


@app.get("/test.png")
def asset_test_png() -> FileResponse:
    p = WEB / "test.png"
    if not p.exists():
        raise _public_error(404, "not_found")
    return FileResponse(p)


@app.get("/Emblem_of_Vietnam.svg.webp")
def asset_emblem() -> FileResponse:
    p = WEB / "Emblem_of_Vietnam.svg.webp"
    if not p.exists():
        raise _public_error(404, "not_found")
    return FileResponse(p)


@app.get("/v1/tools")
def list_tools() -> dict[str, Any]:
    return {
        "tools": TOOL_DEFINITIONS,
        "howto": {
            "list": "GET /v1/tools",
            "call": "POST /v1/tools/call  body: {name, arguments}",
        },
    }


@app.post("/v1/tools/call")
async def tools_call(body: ToolCallRequest) -> JSONResponse:
    # whitelist tool names only
    allowed = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    if body.name not in allowed:
        return JSONResponse({"error": "unknown_tool", "name": body.name}, status_code=404)
    try:
        result = await call_tool(body.name, body.arguments)
    except PermissionError:
        return JSONResponse({"error": "path_not_allowed"}, status_code=403)
    except FileNotFoundError:
        return JSONResponse({"error": "path_not_found"}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": "bad_request", "detail": str(e)[:160]}, status_code=400)
    except Exception as e:
        log.exception("tools_call failed: %s", e)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    status = 404 if result.get("error") in ("job_not_found", "unknown_tool") else 200
    if result.get("error") in (
        "paths_required",
        "job_id_required",
        "empty_question",
        "job_id_and_question_required",
        "path_not_allowed",
    ):
        status = 400
    return JSONResponse(result, status_code=status)


@app.post("/v1/analyze")
async def analyze(body: AnalyzeRequest) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        safe = resolve_allowed_paths(body.paths)
        result = await analyze_paths(safe, title=body.title, t0=t0)
        return public_result(result, include_page_index=False)
    except PermissionError:
        raise _public_error(403, "path_not_allowed") from None
    except FileNotFoundError:
        raise _public_error(404, "path_not_found") from None
    except ValueError as e:
        raise _public_error(400, "bad_request", detail=str(e)) from None
    except Exception as e:
        log.exception("analyze failed: %s", e)
        raise _public_error(500, "analyze_failed") from None


@app.post("/v1/analyze/upload")
async def analyze_upload(
    files: list[UploadFile] = File(...),
    title: str | None = Form(default=None),
    include_pages: bool = Form(default=False),
    page_from: int = Form(default=1),
    page_to: int = Form(default=20),
) -> dict[str, Any]:
    """Upload + analyze. By default returns summary without full page_index (lazy)."""
    if not files:
        raise _public_error(400, "files_required")
    if len(files) > MAX_UPLOAD_FILES:
        raise _public_error(400, "too_many_files", detail=f"max={MAX_UPLOAD_FILES}")

    t0 = time.perf_counter()
    batch_id = uuid.uuid4().hex[:12]
    dest_dir = UPLOADS / batch_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    total_bytes = 0
    try:
        t_up0 = time.perf_counter()
        for f in files:
            name = safe_filename(f.filename or "doc.pdf")
            if not name.lower().endswith((".pdf", ".docx")):
                raise _public_error(400, "unsupported_file", detail=name)
            # unique name — never overwrite
            target = dest_dir / f"{uuid.uuid4().hex[:8]}_{name}"
            size = await _stream_save_upload(f, target, max_bytes=MAX_FILE_BYTES)
            total_bytes += size
            if total_bytes > MAX_TOTAL_UPLOAD_BYTES:
                raise _public_error(
                    413,
                    "total_upload_too_large",
                    detail=f"limit_mb={MAX_TOTAL_UPLOAD_BYTES // 1_000_000}",
                )
            saved.append(str(target))
        upload_seconds = time.perf_counter() - t_up0
        result = await analyze_paths(
            saved, title=title, t0=t0, upload_seconds=upload_seconds
        )
        result["request_wall_clock_seconds"] = round(time.perf_counter() - t0, 3)

        # Default: no full page dump (Notion review) — client loads pages lazily
        if include_pages:
            out = public_result(result, include_page_index=True, ui_truncate=False)
            out["page_index"] = _slice_pages(out.get("page_index") or [], page_from, page_to)
            out["pages_slice"] = {"from": page_from, "to": page_to}
        else:
            out = public_result(result, include_page_index=False)
            out["pages_available"] = result.get("total_pages")
            out["pages_hint"] = "GET /v1/jobs/{job_id}/pages?from=1&to=20"
        return out
    except HTTPException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        log.exception("upload_analyze_failed: %s", e)
        raise _public_error(500, "upload_analyze_failed") from None


def _slice_pages(pages: list[dict], page_from: int, page_to: int) -> list[dict]:
    page_from = max(1, int(page_from or 1))
    page_to = max(page_from, int(page_to or page_from))
    return [p for p in pages if page_from <= int(p.get("page") or 0) <= page_to]


@app.post("/v1/analyze/folder")
async def analyze_folder_endpoint(body: FolderRequest) -> dict[str, Any]:
    folder = body.folder or body.path
    if not folder:
        raise _public_error(400, "folder_required")
    t0 = time.perf_counter()
    try:
        safe = str(resolve_allowed_path(folder))
        result = await analyze_paths([safe], title=body.title, t0=t0)
        return public_result(result, include_page_index=False)
    except PermissionError:
        raise _public_error(403, "path_not_allowed") from None
    except FileNotFoundError:
        raise _public_error(404, "path_not_found") from None
    except Exception as e:
        log.exception("folder analyze failed: %s", e)
        raise _public_error(500, "analyze_failed") from None


@app.post("/v1/bench/upload")
async def bench_upload(
    files: list[UploadFile] = File(...),
    title: str | None = Form(default="SLA bench cold upload"),
) -> dict[str, Any]:
    t_client0 = time.perf_counter()
    if not files:
        raise _public_error(400, "files_required")
    t0 = time.perf_counter()
    batch_id = uuid.uuid4().hex[:12]
    dest_dir = UPLOADS / batch_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    try:
        t_up0 = time.perf_counter()
        total = 0
        for f in files[:MAX_UPLOAD_FILES]:
            name = safe_filename(f.filename or "doc.pdf")
            target = dest_dir / f"{uuid.uuid4().hex[:8]}_{name}"
            size = await _stream_save_upload(f, target, max_bytes=MAX_FILE_BYTES)
            total += size
            if total > MAX_TOTAL_UPLOAD_BYTES:
                raise _public_error(413, "total_upload_too_large")
            saved.append(str(target))
        upload_seconds = time.perf_counter() - t_up0
        result = await analyze_paths(
            saved, title=title, t0=t0, upload_seconds=upload_seconds
        )
        wall = round(time.perf_counter() - t_client0, 3)
        return {
            "bench": "cold_upload",
            "job_id": result.get("job_id"),
            "total_pages": result.get("total_pages"),
            "chunk_count": result.get("chunk_count"),
            "llm_used": result.get("llm_used"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "request_wall_clock_seconds": wall,
            "within_60s": wall < 60 and bool(result.get("within_60s")),
            "timing": result.get("timing"),
            "files": [Path(p).name for p in saved],
        }
    except HTTPException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        log.exception("bench_failed: %s", e)
        raise _public_error(500, "bench_failed") from None


@app.get("/v1/jobs")
def jobs(limit: int = 20) -> dict[str, Any]:
    return {"jobs": store.list_jobs(limit=min(limit, 100))}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, include_page_index: bool = False) -> dict[str, Any]:
    data = store.load(job_id)
    if not data:
        raise _public_error(404, "job_not_found")
    return public_result(data, include_page_index=include_page_index)


@app.get("/v1/jobs/{job_id}/pages")
def get_job_pages(
    job_id: str,
    page_from: int = 1,
    page_to: int = 20,
) -> dict[str, Any]:
    """Lazy-load page slice for UI (full content on disk)."""
    data = store.load(job_id)
    if not data:
        raise _public_error(404, "job_not_found")
    pages = data.get("page_index") or []
    # clamp range
    page_from = max(1, page_from)
    page_to = min(max(page_from, page_to), page_from + 49)  # max 50 pages per request
    sliced = _slice_pages(pages, page_from, page_to)
    # light tables only
    light = []
    for p in sliced:
        tables = [
            {
                "index": t.get("index"),
                "rows": t.get("rows"),
                "cols": t.get("cols"),
                "markdown": t.get("markdown"),
                "html": t.get("html"),
                "source": t.get("source"),
            }
            for t in (p.get("tables") or [])
            if isinstance(t, dict)
        ]
        light.append(
            {
                "doc_id": p.get("doc_id"),
                "filename": p.get("filename"),
                "page": p.get("page"),
                "text": p.get("text"),
                "html": p.get("html"),
                "tables": tables,
            }
        )
    return {
        "job_id": job_id,
        "total_pages": data.get("total_pages") or len(pages),
        "from": page_from,
        "to": page_to,
        "pages": light,
    }


@app.post("/v1/ask")
async def ask(body: AskRequest) -> dict[str, Any]:
    try:
        result = await ask_job(body.job_id, body.question)
    except Exception as e:
        log.exception("ask failed: %s", e)
        raise _public_error(500, "ask_failed") from None
    if result.get("error") == "job_not_found":
        raise _public_error(404, "job_not_found")
    if result.get("error") == "empty_question":
        raise _public_error(400, "empty_question")
    if result.get("error") == "job_not_ready":
        raise _public_error(409, "job_not_ready")
    return result


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
```

---

## FILE: `app/store.py` (lưu job + page_index trên disk)

```python
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .config import JOBS


class JobStore:
    """Lightweight JSON job store for documents + analysis + Q&A index."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or JOBS
        self.root.mkdir(parents=True, exist_ok=True)

    def new_job_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def job_dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, job_id: str, payload: dict[str, Any]) -> Path:
        """Atomic write: ghi vào .tmp rồi os.replace() — tránh race condition."""
        path = self.job_dir(job_id) / "job.json"
        payload = {**payload, "job_id": job_id, "updated_at": time.time()}
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        # Ghi sang file tạm cùng thư mục rồi rename (atomic trên Linux)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".job_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return path

    def load(self, job_id: str) -> dict[str, Any] | None:
        path = self.job_dir(job_id) / "job.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # File bị corrupt (vd: ghi dở) — coi như không có
            return None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p in sorted(self.root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_dir():
                continue
            data = self.load(p.name)
            if not data:
                continue
            items.append(
                {
                    "job_id": data.get("job_id", p.name),
                    "status": data.get("status"),
                    "title": data.get("title") or data.get("summary", {}).get("context", "")[:80],
                    "total_pages": data.get("total_pages"),
                    "elapsed_seconds": data.get("elapsed_seconds"),
                    "created_at": data.get("created_at"),
                    "files": [f.get("filename") for f in data.get("files", [])],
                }
            )
            if len(items) >= limit:
                break
        return items


store = JobStore()
```

---

## Ghi chú

- Frontend render PDF text: `web/api-bridge.js` — xem `docs/SNIPPETS-LAYOUT-OCR.md`.
- Phân tích LLM (không đụng file PDF): `app/llm.py`, `app/domain_vn_legal.py`, `app/qa.py`.
- Generated: 2026-07-18T15:47:11+07:00
