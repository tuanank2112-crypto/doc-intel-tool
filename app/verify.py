"""Citation verifier — hậu kiểm excerpt có thật trong đoạn retrieved."""
from __future__ import annotations

from typing import Any


def verify_citations(
    citations: list[Any],
    hits: list[dict[str, Any]],
    *,
    excerpt_prefix: int = 60,
) -> list[dict[str, Any]]:
    """
    Gắn field `verified` cho mỗi citation.
    Khớp (filename, page) với hits; excerpt (prefix) phải nằm trong text trang.
    """
    hit_pages: dict[tuple[Any, Any], str] = {}
    # Also index by page-only as fallback (model đôi khi lệch filename)
    by_page: dict[Any, str] = {}
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        fn = h.get("filename")
        pg = h.get("page")
        text = h.get("text") or ""
        hit_pages[(fn, pg)] = text
        if pg is not None and pg not in by_page:
            by_page[pg] = text

    verified: list[dict[str, Any]] = []
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        item = dict(c)
        text = hit_pages.get((item.get("filename"), item.get("page")), "")
        if not text and item.get("page") is not None:
            text = by_page.get(item.get("page"), "")
        excerpt = (item.get("excerpt") or "").strip()
        ok = False
        if excerpt and text:
            # normalize whitespace for fuzzy-ish match
            ex = " ".join(excerpt[:excerpt_prefix].lower().split())
            body = " ".join(text.lower().split())
            ok = bool(ex) and ex in body
            if not ok and len(ex) >= 20:
                # fallback: half prefix
                ok = ex[: max(20, excerpt_prefix // 2)] in body
        item["verified"] = bool(ok)
        verified.append(item)
    return verified


def apply_verification_to_answer(
    data: dict[str, Any],
    hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify citations; hạ confidence / not_found nếu không citation nào verified."""
    out = dict(data or {})
    cites = out.get("citations") or []
    verified = verify_citations(cites if isinstance(cites, list) else [], hits)
    out["citations"] = verified
    any_ok = any(c.get("verified") for c in verified)
    out["citations_verified_count"] = sum(1 for c in verified if c.get("verified"))
    out["citations_total"] = len(verified)
    if verified and not any_ok:
        # model bịa citation → không tin
        out["confidence"] = "thap"
        if not (out.get("answer") or "").strip():
            out["not_found"] = True
        # giữ answer nhưng gắn cờ
        out["evidence_weak"] = True
    elif not verified and out.get("not_found") is not True:
        # không citation — hạ confidence
        conf = str(out.get("confidence") or "trung_binh")
        if conf == "cao":
            out["confidence"] = "trung_binh"
    return out
