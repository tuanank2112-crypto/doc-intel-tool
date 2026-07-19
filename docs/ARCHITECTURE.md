# Architecture — Docorum

## Tổng quan

Docorum là trợ lý họp AI-native cho văn bản QPPL / hành chính Việt Nam:

```
Upload PDF/DOCX
  → extract (PyMuPDF text + tables + images)
  → vision OCR (Gemini, chỉ trang thiếu text)
  → map-reduce summary (LLM song song)
  → page_index + terminology + clauses
  → Q&A (BM25 retrieve → LLM → citation verify)
```

UI web (`web/index.html` + `api-bridge.js`) gọi REST API; không chứa logic AI.

## Thành phần chính

| Module | Vai trò |
|--------|---------|
| `app/main.py` | FastAPI: upload, analyze, ask, tools, serve UI |
| `app/extract.py` | Text layer + bảng + ảnh nhúng |
| `app/vision_ocr.py` | Gemini Vision OCR trang sparse |
| `app/pipeline.py` | Map-reduce tóm tắt / term / clauses |
| `app/llm.py` | Client LLM (Gemini OpenAI-compatible) |
| `app/qa.py` | Retrieval BM25 + hỏi đáp |
| `app/verify.py` | **Citation verification** (excerpt ∈ text trang) |
| `app/schemas.py` | **Structured output** Pydantic (QAResult) |
| `app/sanitize.py` | **Prompt-injection guard** (wrap `<TAI_LIEU>`) |
| `app/security_paths.py` | Allowlist path upload/analyze |
| `app/domain_vn_legal.py` | Preextract Điều–Khoản, dictionary terms |
| `app/store.py` | Job JSON store |
| `app/tools.py` | OpenAI-style tool definitions + whitelist |

## Luồng analyze

1. Stream upload → `data/uploads/` (chunk 1MB, uuid prefix).
2. `extract_document` → pages; OCR nếu `page_needs_ocr`.
3. `chunk_pages` theo `max_chars` (không cắt giữa bằng smart-truncate).
4. Map LLM song song (`map_concurrency`) → stitch/reduce.
5. Lưu job + `page_index` (lazy UI: `GET /v1/jobs/{id}/pages`).

## Luồng ask (AI-native)

1. BM25 top-k trang.
2. Sanitize đoạn trích → prompt.
3. LLM JSON → `validate_qa_result`.
4. `verify_citations` → field `verified` / hạ confidence nếu evidence yếu.
5. Fallback heuristic nếu LLM/schema fail.

## API chính

- `POST /v1/analyze/upload`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/pages`
- `POST /v1/ask`
- `GET /v1/tools` · `POST /v1/tools/call`
- `GET /health` · `GET /docs`

## Mục tiêu SLA

Thiết kế cho hồ sơ ~40–70 trang, map song song + OCR có chọn lọc; `within_60s` đo trên wall-clock từng request.
