# Docorum — Trợ Lý Cuộc họp

Trợ lý họp **AI-native** cho văn bản quy phạm / hành chính Việt Nam: OCR + map-reduce tóm tắt, thuật ngữ & điều khoản, hỏi đáp có **xác minh trích dẫn**, guardrail chống prompt injection từ nội dung tài liệu.

## AI-native (có trong code)

| Năng lực | Module |
|----------|--------|
| Pipeline OCR + map-reduce | `extract` · `vision_ocr` · `pipeline` |
| RAG + citation verification | `qa` · `verify` |
| Structured LLM output (schema) | `schemas` |
| Prompt-injection guard | `sanitize` |
| Path allowlist, stream upload, tool whitelist | `security_paths` · `main` · `tools` |

Chi tiết: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/SECURITY.md](docs/SECURITY.md)

## Tính năng sản phẩm

- Tóm tắt 4 mục (bối cảnh, nội dung, điểm quyết, tác động)
- Thuật ngữ + điều khoản quan trọng
- Gợi ý câu hỏi họp
- Q&A tiếng Việt + citation trang (có `verified`)
- UI họp: upload PDF/DOCX, ghi chú, xóa thư mục kỳ họp

## Cài đặt

```bash
git clone https://github.com/tuanank2112-crypto/docorum.git
cd docorum
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# điền GEMINI_API_KEY trong .env
```

### Cấu hình model (Gemini)

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=...
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_MODEL=gemini-3.1-flash-lite
OCR_MODE=auto
OCR_MODEL=gemini-3.1-flash-lite
OCR_CONCURRENCY=8
OCR_DPI=180
```

## Chạy

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8090
```

| | |
|---|---|
| UI | http://127.0.0.1:8090 |
| Health | http://127.0.0.1:8090/health |
| OpenAPI | http://127.0.0.1:8090/docs |

Đăng nhập demo: khóa **`ubnd2026`** (chọn phòng ban bất kỳ).

## API nhanh

```bash
# Upload + phân tích
curl -s -F "files=@samples/de_an_mau_48trang.pdf" -F "title=Demo" \
  http://127.0.0.1:8090/v1/analyze/upload

# Hỏi đáp
curl -s -X POST http://127.0.0.1:8090/v1/ask \
  -H 'Content-Type: application/json' \
  -d '{"job_id":"<JOB_ID>","question":"Điểm cần quyết định chính là gì?"}'
```

Tool schema (agent): `GET /v1/tools` · `POST /v1/tools/call`

## Cấu trúc repo

```
app/                 # backend AI + API
web/
  index.html         # UI
  api-bridge.js      # glue API ↔ UI
  Emblem_of_Vietnam.svg.webp
  test.png           # watermark trống đồng (login + sidebar) — tên file lịch sử, đang được dùng
docs/
  ARCHITECTURE.md
  SECURITY.md
evals/               # smoke/regression Q&A
samples/             # PDF demo (không chứa dữ liệu thật)
scripts/
requirements.txt
.env.example
```

## Đánh giá (Evals)

Smoke / regression set **6 câu** bám `samples/de_an_mau_48trang.pdf`  
(**nhãn đã đối chiếu với text gốc của PDF**).  
Sẽ mở rộng 50–100 câu khi có corpus thi. Kết quả có cột `answer_mode`; **không dùng điểm khi LLM off** làm điểm chính.

```bash
cd docorum && source .venv/bin/activate && python -m evals.run_eval
# khuyến nghị (tránh analyze lại, tiết kiệm quota):
python -m evals.run_eval --job-id <JOB_ID_DA_ANALYZE>
python -m evals.run_eval --threshold 0.6
```

Metric: answer_match (ALL `expect_contains`) + `answer_partial` %, citation page/clause hit, citation verified; tổng kết Overall và **LLM-only subset**.

Ví dụ 1 lần chạy smoke (`--job-id`, model Gemini; số liệu thay đổi theo model/quota):  
**6/6 answer match**, citation hit ~67%, verified ~83%.

## Ghi chú

- Mục tiêu xử lý hồ sơ ~40–70 trang; `within_60s` đo theo từng request.
- Free-tier Gemini: mỗi PDF = nhiều call (map + OCR) — hạ `MAP_CONCURRENCY` / `OCR_CONCURRENCY` nếu 429.
- Không commit `.env`.
