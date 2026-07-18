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
    # data:image/png;base64,... - bieu do/hinh nhung (khong phai full-page scan)
    images: list[str] = field(default_factory=list)
    ocr_applied: bool = False  # True neu trang duoc Gemini Vision lap text

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
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
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
    body_start = 1
    if width and all(re.fullmatch(r"[\d.,%\-\s]+", c or "") for c in header if c):
        header = [f"Cot {i + 1}" for i in range(width)]
        body_start = 0
    lines = [
        "| " + " | ".join(esc(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for r in norm[body_start:]:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


def _matrix_to_html(rows: list[list[str]], caption: str = "") -> str:
    """Compact, readable table HTML (inline styles - no frontend CSS edits)."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    norm = [r for r in norm if any(c.strip() for c in r)]
    if not norm:
        return ""
    keep = [i for i in range(width) if any((r[i] or "").strip() for r in norm)]
    if not keep:
        return ""
    norm = [[r[i] for i in keep] for r in norm]

    wrap = (
        'style="max-width:100%;overflow-x:auto;margin:8px 0 12px;'
        'border:1px solid #333;background:#fff"'
    )
    table = (
        'style="border-collapse:collapse;width:100%;font-size:11.5px;'
        'line-height:1.35;font-family:\'Times New Roman\',serif"'
    )
    th = (
        'style="border:1px solid #333;padding:5px 8px;background:#fff;'
        'color:#000;font-weight:700;text-align:left;white-space:normal;'
        'font-size:11px"'
    )
    td = (
        'style="border:1px solid #333;padding:4px 8px;vertical-align:top;'
        'color:#000;word-break:break-word"'
    )
    cap = (
        'style="caption-side:top;text-align:left;padding:6px 8px 2px;'
        'font-size:11px;font-weight:600;color:#5b5f66"'
    )

    def cellhtml(c: str) -> str:
        # PyMuPDF to_markdown() dat "<br>" cho xuong dong trong o -> giu xuong dong
        return _html_esc(c).replace("&lt;br&gt;", "<br>").replace("&lt;br/&gt;", "<br>")

    parts = [f"<div {wrap}><table {table}>"]
    if caption:
        parts.append(f"<caption {cap}>{_html_esc(caption)}</caption>")
    parts.append("<thead><tr>")
    for c in norm[0]:
        parts.append(f"<th {th}>{cellhtml(c)}</th>")
    parts.append("</tr></thead><tbody>")
    for r in norm[1:]:
        parts.append("<tr>")
        for c in r:
            parts.append(f"<td {td}>{cellhtml(c)}</td>")
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


def _bbox_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _page_has_ruling_lines(page: Any, *, min_h: int = 3, min_v: int = 1) -> bool:
    """Gate re: chi chay find_tables khi trang co duong ke bang.

    get_drawings() re hon find_tables rat nhieu -> bo qua trang toan chu.
    Bang thuc te (co vien) van duoc phat hien.
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return False
    h = v = 0
    for path in drawings:
        for it in path.get("items", []):
            kind = it[0]
            if kind == "l":  # line
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 1.0:
                    h += 1
                elif abs(p1.x - p2.x) < 1.0:
                    v += 1
            elif kind == "re":  # rectangle = 2 canh ngang + 2 canh doc
                h += 2
                v += 2
        if h >= min_h and v >= min_v:
            return True
    return h >= min_h and v >= min_v


def _extract_tables_pymupdf(page: Any) -> list[dict[str, Any]]:
    """PyMuPDF find_tables + to_markdown().

    Dung strategy="lines" de cat o theo DUONG VIEN THAT, khong theo canh chu
    (nguyen nhan "Din|h", "CO NG"). Bang cua tai lieu co vien nen rat hop.
    """
    out: list[dict[str, Any]] = []
    try:
        finder = page.find_tables(strategy="lines")
    except TypeError:
        try:
            finder = page.find_tables()
        except Exception:
            return out
    except Exception:
        return out
    tables = getattr(finder, "tables", None) or []
    for i, tab in enumerate(tables):
        matrix: list[list[str]] = []
        md = ""
        if hasattr(tab, "to_markdown"):
            try:
                md = str(tab.to_markdown() or "").strip()
            except Exception:
                md = ""
        try:
            raw = tab.extract() or []
            matrix = [[_cell_clean(c) for c in (row or [])] for row in raw]
        except Exception:
            matrix = []
        if not matrix and not md:
            continue
        if matrix and sum(1 for r in matrix for c in r if c) < 2:
            continue
        if not md and matrix:
            md = _matrix_to_markdown(matrix)
        if not md or "|" not in md:
            continue
        has_sep = any(
            len(row) > 1
            and all(re.fullmatch(r":?-{2,}:?", (c or "").strip() or "") for c in row)
            for row in [
                [c.strip() for c in ln.strip().strip("|").split("|")]
                for ln in md.splitlines()
                if ln.strip().startswith("|")
            ]
        )
        if not has_sep and matrix:
            md = _matrix_to_markdown(matrix)
        html = _matrix_to_html(matrix, caption=f"Bang {i + 1}") if matrix else ""
        if not html and md:
            rows_md = [
                [c.strip() for c in ln.strip().strip("|").split("|")]
                for ln in md.splitlines()
                if ln.strip().startswith("|")
                and not all(
                    re.fullmatch(r":?-{2,}:?", c.strip() or "")
                    for c in ln.strip().strip("|").split("|")
                )
            ]
            if rows_md:
                html = _matrix_to_html(rows_md, caption=f"Bang {i + 1}")
                matrix = rows_md
        bbox = tuple(
            float(x) for x in (tab.bbox if hasattr(tab, "bbox") else (0, 0, 0, 0))
        )
        out.append(
            {
                "index": i + 1,
                "bbox": bbox,
                "rows": len(matrix) if matrix else md.count("\n"),
                "cols": max((len(r) for r in matrix), default=0),
                "markdown": md,
                "html": html,
                "matrix": matrix,
                "source": "pymupdf_find_tables_lines",
            }
        )
    return out


def _page_text_excluding_tables(
    page: Any, table_bboxes: list[tuple[float, float, float, float]]
) -> str:
    """Extract body text while skipping regions covered by detected tables."""
    try:
        blocks = page.get_text("blocks") or []
    except Exception:
        return _clean_page_text(page.get_text("text") or "")

    parts: list[str] = []
    for b in blocks:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not str(text).strip():
            continue
        bb = (float(x0), float(y0), float(x1), float(y1))
        skip = False
        for tb in table_bboxes:
            if _bbox_overlap(bb, tb):
                skip = True
                break
        if skip:
            continue
        parts.append(str(text).strip())
    if not parts:
        return _clean_page_text(page.get_text("text") or "")
    return _clean_page_text("\n".join(parts))


def _extract_page_images_from_page(
    doc: Any,
    page: Any,
    *,
    min_w: int = 90,
    min_h: int = 90,
    full_page_ratio: float = 0.82,
) -> list[str]:
    """Trich anh raster nhung tren 1 trang -> data-URL PNG.

    DUNG LAI doc/page dang mo (KHONG fitz.open() lai moi trang) -> het O(N)
    lan parse lai file. Bo icon/con dau nho va anh gan full-page (trang scan).
    """
    import base64

    import fitz

    page_area = float(page.rect.width * page.rect.height) or 1.0
    out: list[str] = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            pix = fitz.Pixmap(doc, xref)
        except Exception:
            continue
        try:
            if pix.width < min_w or pix.height < min_h:
                continue
            if (pix.width * pix.height) > full_page_ratio * page_area:
                continue  # full-page scan / background
            if pix.n - pix.alpha >= 4:  # CMYK -> RGB
                pix = fitz.Pixmap(fitz.csRGB, pix)
            png = pix.tobytes("png")
            out.append("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
        finally:
            pix = None  # type: ignore[assignment]
    return out


def extract_page_images(
    pdf_path: str | Path,
    page_index: int,
    *,
    min_w: int = 90,
    min_h: int = 90,
    full_page_ratio: float = 0.82,
) -> list[str]:
    """Backward-compat wrapper (mo file 1 lan cho 1 trang).

    Duong dan chinh dung _extract_page_images_from_page voi doc dang mo.
    """
    import fitz

    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        return _extract_page_images_from_page(
            doc,
            page,
            min_w=min_w,
            min_h=min_h,
            full_page_ratio=full_page_ratio,
        )
    finally:
        doc.close()


def page_needs_ocr_local(text: str, *, min_chars: int = 40) -> bool:
    """Heuristic local (tranh import cycle voi vision_ocr luc load)."""
    t = (text or "").strip()
    if len(t) < min_chars:
        return True
    bad = sum(1 for c in t if ord(c) < 9 or c == "\ufffd")
    if bad > max(5, len(t) * 0.05):
        return True
    return False


def _extract_pdf_page(doc: Any, page: Any, page_no: int) -> PageText:
    """One PDF page: bang -> markdown + HTML; anh nhung dung lai doc dang mo.

    - Chi chay find_tables khi trang co duong ke (gate re) -> nhanh hon nhieu.
    - Da BO fallback aligned-lines (vua cham vua cat chu giua tu).
    """
    # find_tables(strategy="lines") da re san khi trang khong co bang ke -> goi
    # truc tiep, KHONG can _page_has_ruling_lines (tranh quet get_drawings 2 lan/trang).
    tables = _extract_tables_pymupdf(page)

    bboxes = [tuple(t["bbox"]) for t in tables if t.get("bbox")]
    body = (
        _page_text_excluding_tables(page, bboxes)
        if tables
        else _clean_page_text(page.get_text("text") or "")
    )

    # Plain text cho LLM / search (giu markdown bang)
    text_parts: list[str] = []
    if body:
        text_parts.append(body)
    for t in tables:
        text_parts.append(
            f"\n[BANG {t.get('index', '')} - {t.get('rows')}x{t.get('cols')}]\n"
            f"{t.get('markdown', '')}\n"
        )

    # HTML cho UI
    html_parts: list[str] = []
    if body:
        for para in re.split(r"\n{2,}", body):
            p = para.strip()
            if not p:
                continue
            html_parts.append(f"<p>{_html_esc(p).replace(chr(10), '<br>')}</p>")
    for t in tables:
        html_parts.append(t.get("html") or "")

    pt = PageText(
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

    # Bieu do / anh nhung (chi trang born-digital) - DUNG LAI doc dang mo
    if not page_needs_ocr_local(pt.text):
        try:
            pt.images = _extract_page_images_from_page(doc, page)
        except Exception:
            pt.images = []
    return pt


def _extract_pdf(path: Path) -> tuple[list[PageText], str, list[str]]:
    warnings: list[str] = []
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError("PyMuPDF (pymupdf) is required for PDF extraction") from e

    import os
    import time

    base = fitz.open(path)
    pages: list[PageText] = []
    try:
        n = base.page_count
        _t0 = time.time()

        # Song song hoa theo TRANG. fitz KHONG chia se duoc mot Document giua cac
        # thread -> moi worker mo doc RIENG (mo 1 lan/worker, KHONG reopen moi
        # trang). Ket hop find_tables(strategy="lines") -> nhanh & an toan.
        # Chinh bang bien moi truong EXTRACT_WORKERS (dat =1 de ep chay tuan tu).
        try:
            workers = int(os.environ.get("EXTRACT_WORKERS", "0") or 0)
        except ValueError:
            workers = 0
        if workers <= 0:
            workers = min(8, (os.cpu_count() or 4))
        workers = max(1, min(workers, n or 1))

        def _work_range(idxs: list[int]) -> list[tuple[int, PageText]]:
            wd = fitz.open(path)
            try:
                return [
                    (i, _extract_pdf_page(wd, wd.load_page(i), i + 1)) for i in idxs
                ]
            finally:
                wd.close()

        if workers == 1:
            pages = [
                _extract_pdf_page(base, base.load_page(i), i + 1) for i in range(n)
            ]
        else:
            buckets: list[list[int]] = [[] for _ in range(workers)]
            for i in range(n):
                buckets[i % workers].append(i)  # round-robin: rai deu trang bang
            collected: dict[int, PageText] = {}
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for part in ex.map(_work_range, [b for b in buckets if b]):
                    for idx, pt in part:
                        collected[idx] = pt
            pages = [collected[i] for i in range(n)]

        warnings.append(
            f"Trich xuat {n} trang trong {round(time.time() - _t0, 1)}s "
            f"({workers} luong)."
        )

        empty = sum(1 for p in pages if not p.text)
        n_tables = sum(len(p.tables) for p in pages)
        if empty and empty == n:
            warnings.append(
                "PDF co ve scan/anh - text layer trong; se thu Gemini Vision OCR neu bat."
            )
        elif empty:
            warnings.append(f"{empty}/{n} trang khong co text (se OCR Vision neu bat).")
        if n_tables:
            warnings.append(f"Da giu cau truc {n_tables} bang (markdown/HTML).")

        # --- Gemini Vision OCR (PDF -> image -> text), parallel ---
        engine = "pymupdf+tables"
        try:
            import asyncio
            import concurrent.futures

            from .vision_ocr import enrich_pages_with_vision, ocr_enabled
            from .config import settings

            if ocr_enabled():
                mode = (settings.ocr_mode or "auto").lower()

                # Ngan sach thoi gian cho OCR (giay) -> bao ve muc tieu <60s.
                # Chinh bang OCR_TIMEOUT_S; dat <=0 de bo gioi han.
                try:
                    ocr_budget = float(os.environ.get("OCR_TIMEOUT_S", "30") or 30)
                except ValueError:
                    ocr_budget = 30.0

                async def _run_ocr():
                    coro = enrich_pages_with_vision(path, pages, mode=mode)
                    if ocr_budget > 0:
                        return await asyncio.wait_for(coro, timeout=ocr_budget)
                    return await coro

                def _run_ocr_blocking():
                    # Luon dung event loop MOI -> an toan tren Python 3.13
                    # (khong dung asyncio.get_event_loop() da bi deprecated).
                    return asyncio.run(_run_ocr())

                try:
                    asyncio.get_running_loop()
                    # Dang trong 1 event loop -> day sang thread rieng de asyncio.run().
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        pages, ocr_warn = pool.submit(_run_ocr_blocking).result()
                except RuntimeError:
                    # Khong co event loop dang chay -> chay truc tiep.
                    pages, ocr_warn = _run_ocr_blocking()

                warnings.extend(ocr_warn)
                if any("OCR Gemini" in w for w in ocr_warn):
                    engine = "pymupdf+tables+gemini-vision"
            elif empty:
                warnings.append(
                    "Bat OCR: set GEMINI_API_KEY + OCR_MODE=auto|always (gemini-2.0-flash Vision)."
                )
        except (asyncio.TimeoutError, TimeoutError):
            warnings.append(
                "OCR Vision qua han (OCR_TIMEOUT_S) - bo qua, giu nguyen text da trich de dam bao <60s."
            )
        except Exception as e:
            warnings.append(f"OCR Vision loi: {str(e)[:160]}")

        # --- Anh nhung: da trich inline trong _extract_pdf_page ---
        # Trang duoc OCR (scan) -> khong dan anh full-page.
        n_imgs = 0
        for pt in pages:
            if getattr(pt, "ocr_applied", False):
                pt.images = []
            else:
                n_imgs += len(pt.images or [])
        if n_imgs:
            warnings.append(f"Da trich {n_imgs} anh/bieu do nhung (raster).")

        return pages, engine, warnings
    finally:
        base.close()


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
                html = _matrix_to_html(matrix, caption=f"Bang {table_i}")
                blocks.append(
                    f"\n[BANG {table_i} - {len(matrix)}x{max(len(r) for r in matrix)}]\n{md}\n"
                )
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
        for i in range(0, len(full), page_size):
            chunk = full[i : i + page_size]
            t_in = [
                t
                for t in all_tables
                if t.get("markdown") and t["markdown"][:40] in chunk
            ]
            h_slice = full_html if i == 0 else ""
            pages.append(
                PageText(
                    page=len(pages) + 1,
                    text=chunk,
                    html=h_slice,
                    tables=t_in,
                )
            )
        if pages:
            assigned = {t["index"] for page in pages for t in page.tables}
            unassigned = [t for t in all_tables if t["index"] not in assigned]
            if unassigned:
                pages[0].tables = pages[0].tables + unassigned
            if not pages[0].html:
                pages[0].html = full_html

    return (
        pages,
        "python-docx+tables",
        [
            "Word: bang duoc giu dang markdown/HTML.",
            "So trang Word la uoc luong theo do dai.",
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
            raise ValueError("Dinh dang .doc cu khong ho tro - chuyen sang .docx hoac PDF")
        pages, engine, warnings = _extract_docx(path)
        file_type = "docx"
    else:
        raise ValueError(f"Dinh dang khong ho tro: {suffix}. Dung PDF hoac DOCX.")

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
                doc.warnings.append(f"Cat con {page_budget} trang theo budget.")
                page_budget = 0
            else:
                page_budget -= doc.total_pages
        docs.append(doc)
    return docs


def chunk_pages(
    pages: list[PageText],
    *,
    pages_per_chunk: int = 8,  # legacy; ignored - chunk by char budget
    max_chars: int = 8000,
    source_file: str = "",
    max_chunks: int | None = None,
    hard_max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    """Chia trang thanh chunk map-reduce theo nguong ky tu (khong cat giua trang)."""
    import math

    n = len(pages)
    if n == 0:
        return []

    cap = hard_max_chunks if hard_max_chunks is not None else max_chunks
    if cap is None or cap <= 0:
        cap = 40

    batches: list[list[PageText]] = []
    cur: list[PageText] = []
    cur_len = 0
    for p in pages:
        t = getattr(p, "text", "") or ""
        tlen = len(t)
        if cur and cur_len + tlen > max_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += tlen
    if cur:
        batches.append(cur)

    if len(batches) > cap:
        step = math.ceil(len(batches) / cap)
        merged: list[list[PageText]] = []
        for i in range(0, len(batches), step):
            group: list[PageText] = []
            for b in batches[i : i + step]:
                group.extend(b)
            if group:
                merged.append(group)
        batches = merged

    chunks: list[dict[str, Any]] = []
    for batch in batches:
        body = "\n".join(f"[Trang {p.page}]\n{p.text or ''}" for p in batch)
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
