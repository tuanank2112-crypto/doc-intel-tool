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

REDUCE_SYSTEM = """Bạn nhận các bản tóm tắt của TỪNG ĐOẠN thuộc CÙNG MỘT tài liệu QPPL/hành chính Việt Nam.
Viết TÓM TẮT TỔNG QUÁT, ngắn gọn, dễ hiểu cho TOÀN BỘ tài liệu:
- Không bỏ sót chủ đề chính của bất kỳ đoạn map nào.
- Gộp trùng lặp, sắp theo mạch nội dung.
- Giữ page/clause khi có; không bịa.

Trả JSON thuần:
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
context: 3–6 câu. main_content / decision_points / impact: đủ để cover các đoạn (có thể >10 ý nếu tài liệu dài).
terms: gộp thuật ngữ quan trọng từ mọi đoạn (khử trùng theo tên, giữ page).
4–10 câu hỏi thẩm định. Tiếng Việt."""

def _merge_lists(items: list[dict[str, Any]], key: str) -> list[Any]:
    out: list[Any] = []
    for it in items:
        val = it.get(key) or []
        if isinstance(val, list):
            out.extend(val)
    return out


def _dedupe(
    items: list[Any],
    key_fn,
    limit: int | None = None,
) -> list[Any]:
    """Khử trùng theo key; limit=None giữ tất cả."""
    seen: set[str] = set()
    out: list[Any] = []
    for it in items:
        try:
            k = str(key_fn(it) if it is not None else "").strip().lower()
        except Exception:
            k = str(it).strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
        if limit is not None and len(out) >= limit:
            break
    return out


def _dedupe_terms(terms: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """Khử trùng theo tên term; mặc định không cap (limit=None)."""
    cleaned: list[dict[str, Any]] = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        name = str(t.get("term") or t.get("name") or "").strip()
        if not name:
            continue
        item = dict(t)
        item["term"] = name
        cleaned.append(item)
    return _dedupe(cleaned, key_fn=lambda t: t.get("term") or t.get("name") or "", limit=limit)


def _item_limit(total_pages: int, floor: int = 12) -> int:
    """70 trang → ~23 ý; không cứng [:12]."""
    return max(floor, int(total_pages or 0) // 3)


async def _map_chunk(
    sem: asyncio.Semaphore,
    chunk: dict[str, Any],
    idx: int,
    total: int,
) -> dict[str, Any]:
    async with sem:
        # Full chunk text — không cắt giữa (chunk đã chia theo char budget)
        from .sanitize import sanitize_for_prompt, with_injection_guard

        body = chunk.get("text") or ""
        wrapped, _meta = sanitize_for_prompt(body, tag="TAI_LIEU")
        user = (
            f"Phân tích đoạn tài liệu họp/thẩm định — phần {idx + 1}/{total}.\n"
            f"Tệp: {chunk.get('source_file')}\n"
            f"Trang {chunk['page_start']}–{chunk['page_end']}:\n"
            f"YÊU CẦU QUAN TRỌNG: Trích xuất ÍT NHẤT 3-5 thuật ngữ chuyên ngành nổi bật trong đoạn này "
            f"(có thể là thuật ngữ kỹ thuật, pháp lý, khoa học, hành chính). "
            f"Nếu tài liệu bằng tiếng Anh, dịch thuật ngữ sang tiếng Việt trong phần explanation.\n"
            f"Chú ý thêm: căn cứ, Điều/Khoản/Điểm, thẩm quyền, hiệu lực, trách nhiệm thi hành.\n\n"
            f"{wrapped}"
        )
        try:
            data = await achat_json(
                with_injection_guard(MAP_SYSTEM), user, temperature=0.0, max_tokens=2200
            )
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
        # stamp source_file / default page onto nested items when missing
        src = chunk.get("source_file")
        p0 = chunk.get("page_start")
        for key in ("decision_points", "terms", "important_clauses", "main_points", "impacts"):
            for item in data.get(key) or []:
                if isinstance(item, dict):
                    if "source_file" not in item:
                        item["source_file"] = src
                    if item.get("page") is None and p0 is not None:
                        item["page"] = p0
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
    """Local reduce (no LLM) — fallback khi tắt LLM/reduce lỗi. Co giãn theo số trang."""
    pre = preextract or {}
    total_pages = sum(d.total_pages for d in docs)
    lim = _item_limit(total_pages)
    dtype = pre.get("document_type_guess") or "khac"
    signals = pre.get("signals") or []
    main_points = _merge_lists(maps, "main_points")
    main_raw: list[Any] = []
    for p in main_points:
        if isinstance(p, dict) and p.get("point"):
            main_raw.append(p)
        elif isinstance(p, str):
            main_raw.append({"point": p})
    main = [
        (x.get("point") if isinstance(x, dict) else str(x))
        for x in _dedupe(main_raw, key_fn=lambda x: x.get("point") if isinstance(x, dict) else x, limit=lim)
    ]
    decisions = _dedupe(
        [d for d in _merge_lists(maps, "decision_points") if isinstance(d, dict) and d.get("decision")],
        key_fn=lambda d: d.get("decision"),
        limit=lim,
    )
    impacts = _dedupe(
        [
            str(x.get("impact") if isinstance(x, dict) else x)
            for x in _merge_lists(maps, "impacts")
            if (isinstance(x, dict) and x.get("impact")) or isinstance(x, str)
        ],
        key_fn=lambda s: s,
        limit=max(10, lim // 2),
    )
    hints = _merge_lists(maps, "context_hints")
    context = (
        f"Hồ sơ nhà nước/pháp luật: {len(docs)} tệp, {total_pages} trang. "
        f"Loại VB (nhận diện): {dtype}. "
        + ("; ".join(str(s) for s in signals[:3]) + ". " if signals else "")
        + (" ".join(str(h) for h in hints[:4]) if hints else "")
    ).strip()
    questions = []
    for d in decisions[:6]:
        if isinstance(d, dict) and d.get("decision"):
            questions.append(
                {
                    "question": f"Về điểm quyết định: {d['decision'][:160]}?",
                    "purpose": "Thảo luận họp",
                    "related_pages": [d["page"]] if d.get("page") is not None else [],
                }
            )
    for p in main_points[:6]:
        txt = p.get("point", p) if isinstance(p, dict) else str(p)
        if txt and len(questions) < 10:
            questions.append(
                {
                    "question": f"Cơ sở và bằng chứng cho nhận định: '{str(txt)[:120]}' là gì?",
                    "purpose": "Phản biện",
                    "related_pages": [p["page"]] if isinstance(p, dict) and p.get("page") else [],
                }
            )
    for imp in _merge_lists(maps, "impacts")[:4]:
        txt = imp.get("impact", imp) if isinstance(imp, dict) else str(imp)
        if txt and len(questions) < 10:
            questions.append(
                {
                    "question": f"Tác động '{str(txt)[:100]}' được đánh giá và kiểm soát như thế nào?",
                    "purpose": "Đánh giá rủi ro",
                    "related_pages": [imp["page"]] if isinstance(imp, dict) and imp.get("page") else [],
                }
            )
    default_qs = [
        {"question": "Thẩm quyền ban hành và căn cứ pháp lý của văn bản là gì?", "purpose": "Thẩm định", "related_pages": [1]},
        {"question": "Những điểm nào cần xin ý kiến lãnh đạo trước khi ban hành hoặc triển khai?", "purpose": "Chuẩn bị họp", "related_pages": []},
        {"question": "Các nguồn lực (ngân sách, nhân lực, thời gian) cần thiết có khả thi không?", "purpose": "Thực thi", "related_pages": []},
        {"question": "Rủi ro và thách thức lớn nhất khi thực hiện là gì, và giải pháp ứng phó?", "purpose": "Quản lý rủi ro", "related_pages": []},
        {"question": "Các bên liên quan chính và trách nhiệm cụ thể của từng bên là gì?", "purpose": "Phân công nhiệm vụ", "related_pages": []},
    ]
    for q in default_qs:
        if len(questions) >= 8:
            break
        questions.append(q)
    related = list(pre.get("legal_references") or []) + _merge_lists(
        maps, "related_regulations_hints"
    )
    related = _dedupe(
        [r for r in related if isinstance(r, dict)],
        key_fn=lambda r: r.get("title") or "",
        limit=20,
    )
    # Terms: giữ tất cả (khử trùng theo tên) — phục vụ highlight phủ giữa tài liệu
    terms = _dedupe_terms(
        _merge_lists(maps, "terms") + list(pre.get("dictionary_terms") or []),
        limit=None,
    )
    clauses = _dedupe(
        [c for c in _merge_lists(maps, "important_clauses") if isinstance(c, dict)],
        key_fn=lambda c: c.get("clause") or c.get("summary") or "",
        limit=max(20, lim),
    ) or [
        {"clause": c, "summary": c, "page": None, "why_important": "Xuất hiện trong hồ sơ"}
        for c in (pre.get("clause_mentions") or [])[:8]
    ]
    return {
        "context": context[:1600],
        "document_types": [dtype] if dtype else [],
        "main_content": main or ["Đã trích các đoạn chính — xem panel thuật ngữ/điều khoản."],
        "decision_points": decisions
        or [
            {
                "decision": "Rà soát thẩm quyền và căn cứ ban hành",
                "page": None,
                "clause": None,
                "urgency": "cao",
            }
        ],
        "impact": impacts or ["Cần đánh giá tác động triển khai khi họp."],
        "legal_effects": _merge_lists(maps, "legal_effects")[: max(15, lim)],
        "authorities_duties": _merge_lists(maps, "authorities_duties")[: max(15, lim)],
        "terms": terms,
        "important_clauses": clauses,
        "suggested_questions": questions[:12],
        "related_documents": related,
        "mode": "stitch_map",
    }


async def _reduce(
    maps: list[dict[str, Any]],
    file_names: list[str],
    total_pages: int,
    preextract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """1 call reduce trên các bản map ngắn — phủ toàn văn, rẻ hơn map raw."""
    lim = _item_limit(total_pages)
    # Gói gọn từng đoạn: context + ý chính (không dump full map JSON)
    segment_briefs: list[str] = []
    for i, m in enumerate(maps):
        meta = m.get("_meta") or {}
        hints = m.get("context_hints") or []
        ctx = "; ".join(str(h) for h in hints[:3]) if hints else ""
        pts = []
        for p in (m.get("main_points") or [])[:6]:
            if isinstance(p, dict) and p.get("point"):
                pts.append(str(p["point"])[:200])
            elif isinstance(p, str):
                pts.append(p[:200])
        decs = []
        for d in (m.get("decision_points") or [])[:4]:
            if isinstance(d, dict) and d.get("decision"):
                decs.append(str(d["decision"])[:160])
        segment_briefs.append(
            f"[Đoạn {i + 1} | tr.{meta.get('page_start')}–{meta.get('page_end')}] "
            f"{ctx}\n- Ý: " + "; ".join(pts[:5])
            + (("\n- Quyết: " + "; ".join(decs[:3])) if decs else "")
        )
    joined = "\n\n".join(segment_briefs)
    # Terms / clauses gộp đầy đủ (không cap thấp) — reduce chỉ tóm tắt narrative
    all_terms = _dedupe_terms(_merge_lists(maps, "terms"), limit=None)
    all_clauses = _dedupe(
        [c for c in _merge_lists(maps, "important_clauses") if isinstance(c, dict)],
        key_fn=lambda c: c.get("clause") or "",
        limit=None,
    )
    compact = {
        "files": file_names,
        "total_pages": total_pages,
        "n_map_segments": len(maps),
        "target_main_items": lim,
        "preextract": {
            "document_type_guess": (preextract or {}).get("document_type_guess"),
            "signals": ((preextract or {}).get("signals") or [])[:8],
            "legal_references": ((preextract or {}).get("legal_references") or [])[:15],
        },
        "segment_briefs": joined[:14000],
        "decision_points": _merge_lists(maps, "decision_points")[:lim],
        "impacts": _merge_lists(maps, "impacts")[:lim],
        "legal_effects": _merge_lists(maps, "legal_effects")[:15],
        "authorities_duties": _merge_lists(maps, "authorities_duties")[:15],
        "terms_sample": all_terms[:80],
        "important_clauses_sample": all_clauses[:40],
        "related_regulations_hints": _merge_lists(maps, "related_regulations_hints")[:15],
    }
    raw = __import__("json").dumps(compact, ensure_ascii=False)[:20000]
    user = (
        f"Tài liệu {total_pages} trang, {len(maps)} đoạn map. "
        f"Viết tóm tắt tổng quát phủ mọi đoạn; ~{lim} ý main_content nếu cần.\n"
        f"INPUT:\n{raw}"
    )
    data = await achat_json(REDUCE_SYSTEM, user, temperature=0.0, max_tokens=3200)
    # Bổ sung terms/clauses từ map nếu reduce cắt bớt
    red_terms = _dedupe_terms(list(data.get("terms") or []) + all_terms, limit=None)
    data["terms"] = red_terms
    if not data.get("important_clauses"):
        data["important_clauses"] = all_clauses
    else:
        data["important_clauses"] = _dedupe(
            list(data.get("important_clauses") or []) + all_clauses,
            key_fn=lambda c: (c.get("clause") if isinstance(c, dict) else str(c)) or "",
            limit=None,
        )
    return data


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
            imgs = getattr(p, "images", None) or []
            if not (p.text or "").strip() and not p.tables and not imgs:
                continue
            index.append(
                {
                    "doc_id": d.doc_id,
                    "filename": d.filename,
                    "page": p.page,
                    "text": p.text,
                    "html": getattr(p, "html", "") or "",
                    "tables": getattr(p, "tables", None) or [],
                    # data-URL biểu đồ raster (born-digital only)
                    "images": list(imgs),
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
    # Chunk theo ngưỡng ký tự — co giãn; hard_max = max_map_chunks (không cắt giữa)
    chunks: list[dict[str, Any]] = []
    n_docs = max(1, len(docs))
    hard_cap = max(1, int(getattr(settings, "max_map_chunks", 40) or 40))
    # Phân bổ trần chunk theo số file (tổng ≤ hard_cap)
    per_doc_cap = max(1, hard_cap // n_docs)
    leftover = hard_cap - per_doc_cap * n_docs
    for di, d in enumerate(docs):
        cap = per_doc_cap + (1 if di < leftover else 0)
        chunks.extend(
            chunk_pages(
                d.pages,
                max_chars=settings.max_chars_per_chunk,
                source_file=d.filename,
                hard_max_chunks=max(1, cap),
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
    # Reduce 1 call (mặc định bật). Chỉ bỏ nếu map xong quá trễ (còn <8s cho SLA 60s).
    elapsed_after_map = time.perf_counter() - t0
    use_llm_reduce = (
        bool(maps_ok)
        and bool(getattr(settings, "llm_use_reduce", True))
        and elapsed_after_map < 52
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

    # Terminology: gộp map + reduce + dictionary — khử trùng, KHÔNG cap thấp
    terms = _dedupe_terms(
        list(summary.get("terms") or [])
        + _merge_lists(list(maps), "terms")
        + list(preextract.get("dictionary_terms") or []),
        limit=None,
    )

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
        "important_clauses": _dedupe(
            list(summary.get("important_clauses") or [])
            + _merge_lists(list(maps), "important_clauses"),
            key_fn=lambda c: (c.get("clause") if isinstance(c, dict) else str(c)) or "",
            limit=None,
        ),
        "suggested_questions": summary.get("suggested_questions") or [],
        "related_documents": related,
        "preextract": {
            "document_type_guess": preextract.get("document_type_guess"),
            "signals": preextract.get("signals"),
            "legal_references": preextract.get("legal_references"),
            "clause_mentions": (preextract.get("clause_mentions") or [])[:40],
            "dictionary_terms": (preextract.get("dictionary_terms") or [])[:80],
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
