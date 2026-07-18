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
                "images": p.get("images") or [],
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
