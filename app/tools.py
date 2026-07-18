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
    if not include_page_index:
        out.pop("page_index", None)
    elif ui_truncate and out.get("page_index") and settings.ui_page_chars > 0:
        # Optional only — default is full content (ui_truncate_pages=false)
        limit = settings.ui_page_chars
        slim = []
        for p in out["page_index"]:
            text = p.get("text") or ""
            if len(text) > limit:
                text = _truncate_keep_tables(text, limit)
            html = p.get("html") or ""
            tables = p.get("tables") or []
            tables_light = [
                {
                    "index": t.get("index"),
                    "rows": t.get("rows"),
                    "cols": t.get("cols"),
                    "markdown": t.get("markdown"),
                    "html": t.get("html"),
                    "source": t.get("source"),
                }
                for t in tables
                if isinstance(t, dict)
            ]
            slim.append({**p, "text": text, "html": html, "tables": tables_light})
        out["page_index"] = slim
        out["page_index_truncated"] = True
    elif out.get("page_index"):
        # Always strip heavy matrix from tables (keep markdown+html full)
        slim = []
        for p in out["page_index"]:
            tables = p.get("tables") or []
            tables_light = [
                {
                    "index": t.get("index"),
                    "rows": t.get("rows"),
                    "cols": t.get("cols"),
                    "markdown": t.get("markdown"),
                    "html": t.get("html"),
                    "source": t.get("source"),
                }
                for t in tables
                if isinstance(t, dict)
            ]
            item = dict(p)
            item["tables"] = tables_light
            slim.append(item)
        out["page_index"] = slim
        out["page_index_truncated"] = False
    # Keep dictionary_terms (capped) so frontend can highlight/tooltip
    if isinstance(out.get("preextract"), dict):
        pe = dict(out["preextract"])
        dt = pe.get("dictionary_terms") or []
        if isinstance(dt, list) and len(dt) > 40:
            pe["dictionary_terms"] = dt[:40]
        out["preextract"] = pe
    return out


async def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    if name == "analyze_documents":
        paths = arguments.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            return {"error": "paths_required"}
        from .security_paths import resolve_allowed_paths

        try:
            safe = resolve_allowed_paths(list(paths))
        except PermissionError:
            return {"error": "path_not_allowed"}
        except FileNotFoundError:
            return {"error": "path_not_found"}
        except ValueError as e:
            return {"error": "bad_request", "detail": str(e)[:160]}
        result = await analyze_paths(safe, title=arguments.get("title"))
        return public_result(result, include_page_index=False)

    if name == "get_analysis":
        job_id = arguments.get("job_id")
        if not job_id:
            return {"error": "job_id_required"}
        data = store.load(job_id)
        if not data:
            return {"error": "job_not_found", "job_id": job_id}
        return public_result(data, include_page_index=bool(arguments.get("include_page_index")))

    if name == "ask_document":
        job_id = arguments.get("job_id")
        question = arguments.get("question")
        if not job_id or not question:
            return {"error": "job_id_and_question_required"}
        return await ask_job(job_id, question)

    if name == "list_jobs":
        limit = int(arguments.get("limit") or 20)
        return {"jobs": store.list_jobs(limit=limit)}

    return {"error": "unknown_tool", "name": name, "available": [t["function"]["name"] for t in TOOL_DEFINITIONS]}
