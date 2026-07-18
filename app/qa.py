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
            "answer": (
                "[Chế độ tìm kiếm thô — chưa dùng LLM] "
                "Không tìm thấy đoạn liên quan trong tài liệu đã nạp."
            ),
            "citations": [],
            "confidence": "thap",
            "not_found": True,
            "llm_used": False,
            "answer_mode": "heuristic_search",
        }
    top = hits[0]
    excerpt = (top.get("text") or "")[:220]
    return {
        "answer": (
            "[Chế độ tìm kiếm thô — chưa phải trả lời AI đầy đủ] "
            f"Đoạn có điểm liên quan cao nhất (trang {top.get('page')} — {top.get('filename')}):\n"
            f"«{excerpt}…»\n"
            "Gợi ý: bật LLM (API key) để được giải thích hội thoại + trích dẫn điều khoản."
        ),
        "citations": [
            {
                "filename": top.get("filename"),
                "page": top.get("page"),
                "clause": None,
                "excerpt": excerpt,
            }
        ],
        "confidence": "thap",  # heuristic never claims high confidence
        "not_found": False,
        "llm_used": False,
        "answer_mode": "heuristic_search",
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
        if isinstance(t, dict) and t.get("term"):
            extra_ctx.append(f"Thuật ngữ: {t.get('term')} — {t.get('explanation')} (tr.{t.get('page')})")
    for c in (job.get("important_clauses") or [])[:10]:
        if isinstance(c, dict):
            extra_ctx.append(
                f"Điều khoản: {c.get('clause')} — {c.get('summary')} (tr.{c.get('page')})"
            )

    hits = _retrieve(page_index, question, settings.qa_top_k)

    if not llm_enabled():
        out = _heuristic_answer(question, hits)
        out["job_id"] = job_id
        out["question"] = question
        return out

    passages = []
    for h in hits:
        passages.append(
            f"[{h.get('filename')} | trang {h.get('page')} | score={h.get('score', 0):.2f}]\n{h.get('text')}"
        )
    user = (
        f"Câu hỏi cán bộ: {question}\n\n"
        f"Tóm tắt ngữ cảnh (nếu có): {(job.get('summary') or {}).get('context', '')[:800]}\n\n"
        f"Thuật ngữ/điều khoản đã gắn cờ:\n" + "\n".join(extra_ctx[:20]) + "\n\n"
        f"Đoạn trích truy xuất:\n\n" + "\n\n---\n\n".join(passages)
    )
    try:
        data = await achat_json(QA_SYSTEM, user, temperature=0.1, max_tokens=1800)
    except Exception as e:
        # 429 quota / timeout / network: vẫn trả lời bằng tìm kiếm thô + thông báo rõ
        msg = str(e)
        low = msg.lower()
        if "429" in msg or "resource_exhausted" in low or "quota" in low:
            note = (
                "[LLM tạm quá tải / hết quota Gemini — trả lời bằng tìm kiếm thô trên văn bản] "
            )
        elif "timeout" in low:
            note = "[LLM timeout — trả lời bằng tìm kiếm thô trên văn bản] "
        else:
            note = f"[LLM lỗi: {msg[:160]} — trả lời bằng tìm kiếm thô] "
        out = _heuristic_answer(question, hits)
        out["job_id"] = job_id
        out["question"] = question
        out["answer"] = note + (out.get("answer") or "")
        out["llm_error"] = msg[:400]
        out["answer_mode"] = "heuristic_fallback"
        return out

    return {
        "job_id": job_id,
        "question": question,
        "answer": data.get("answer") or "",
        "citations": data.get("citations") or [],
        "confidence": data.get("confidence") or "trung_binh",
        "not_found": bool(data.get("not_found")),
        "llm_used": True,
        "answer_mode": "llm",
        "model": settings.llm_model,
        "retrieved": [
            {"filename": h.get("filename"), "page": h.get("page"), "score": h.get("score")}
            for h in hits
        ],
    }
