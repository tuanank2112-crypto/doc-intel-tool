# Doc Intel Tool — Văn bản nhà nước & pháp luật Việt Nam

Tool **AI-callable** cho cán bộ, công chức, tổ thẩm định: tóm tắt hồ sơ **pháp luật & hành chính nhà nước** (PDF/Word), gắn cờ **Điều–Khoản–Điểm** & thuật ngữ, gợi ý câu hỏi họp, hỏi đáp tiếng Việt có trích dẫn trang/điều.

**Bối cảnh miền:** Hiến pháp, Bộ luật, Luật, Pháp lệnh, Nghị quyết, Nghị định, Quyết định, Chỉ thị, Thông tư / TTLT, Công văn, Tờ trình, Đề án, Báo cáo, Quy chế, hướng dẫn thi hành, v.v.

Thiết kế cho **thư mục ~40–60 trang**, mục tiêu xử lý **dưới 60 giây** (map-reduce song song + trích text PyMuPDF + pre-extract số hiệu VB).

## Tính năng

| Chức năng | Mô tả |
|-----------|--------|
| **Smart summarization** | Bối cảnh thể chế, loại VB, nội dung chính, điểm xin ý kiến, tác động tuân thủ |
| **Hiệu lực & trách nhiệm** | Hiệu lực / bãi bỏ / chuyển tiếp; cơ quan–trách nhiệm thi hành |
| **Terminology & clauses** | Điều–Khoản–Điểm + thuật ngữ hành chính–pháp lý (QPPL, UBND, TTHC…) |
| **Suggested questions** | Câu hỏi thẩm định + VB liên quan (Luật → NĐ → TT…) |
| **Meeting Q&A** | Hỏi tiếng Việt → trả lời kèm **trang / Điều–Khoản** |
| **AI tools API** | Schema OpenAI-compatible để agent gọi |

## Cài đặt

```bash
cd /home/lenkuy/doc-intel-tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### LLM tóm tắt (9flare / OpenAI-compatible)

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=flr-...
LLM_BASE_URL=https://9flare.com/api/v1
LLM_MODEL=pro/claude-haiku-4-5
```

### Gemini Vision OCR (PDF scan → đọc thẳng ảnh, không Tesseract)

```env
OCR_MODE=auto          # auto | always | off
OCR_MODEL=gemini-2.0-flash
OCR_CONCURRENCY=10     # free tier: 5; paid: 15–20
OCR_DPI=150            # 200 nếu scan mờ
GEMINI_API_KEY=...     # bắt buộc để bật Vision OCR
```

Pipeline:

```
PDF text layer (PyMuPDF + tables)
  → trang thiếu text? → render PNG → Gemini Vision (parallel, semaphore)
  → tóm tắt / hỏi đáp
```

Không có Gemini key: vẫn extract text layer; trang scan sẽ trống (cảnh báo trong `warnings`).

## Chạy server (API-only)

Repo **không kèm UI web** — dùng Swagger hoặc gọi HTTP/CLI.

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8090
# Health:  http://127.0.0.1:8090/health
# Swagger: http://127.0.0.1:8090/docs
```

## CLI

```bash
# Phân tích thư mục / file
python -m app.cli analyze /path/to/folder --title "Họp thẩm định"

# Hỏi đáp
python -m app.cli ask <job_id> "Mức đầu tư được đề xuất là bao nhiêu?"

python -m app.cli list
python -m app.cli get <job_id>
```

## API cho AI gọi

### 1. Lấy schema tool

```http
GET /v1/tools
```

Trả về 4 function:

- `analyze_documents` — paths (file/folder) → summary có cấu trúc  
- `get_analysis` — lấy lại job  
- `ask_document` — hỏi đáp có citation  
- `list_jobs`

### 2. Gọi tool

```bash
curl -s http://127.0.0.1:8090/v1/tools/call \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "analyze_documents",
    "arguments": {
      "paths": ["/home/lenkuy/doc-intel-tool/samples"],
      "title": "Họp demo"
    }
  }'
```

```bash
curl -s http://127.0.0.1:8090/v1/tools/call \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "ask_document",
    "arguments": {
      "job_id": "JOB_ID",
      "question": "Các điểm cần quyết trong cuộc họp là gì?"
    }
  }'
```

### 3. REST tương đương

| Method | Path | Mô tả |
|--------|------|--------|
| POST | `/v1/analyze` | `{ "paths": [...], "title": "..." }` |
| POST | `/v1/analyze/upload` | multipart files |
| POST | `/v1/analyze/folder` | `{ "folder": "..." }` |
| GET | `/v1/jobs/{id}` | Kết quả |
| POST | `/v1/ask` | `{ "job_id", "question" }` |

### 4. Gắn vào OpenAI-compatible agent

```python
import json, httpx
from openai import OpenAI

TOOLS = httpx.get("http://127.0.0.1:8090/v1/tools").json()["tools"]
client = OpenAI(api_key="...", base_url="https://api.x.ai/v1")

# Trong vòng tool-calling: khi model chọn tool name + arguments
def run_tool(name, arguments):
    r = httpx.post(
        "http://127.0.0.1:8090/v1/tools/call",
        json={"name": name, "arguments": arguments},
        timeout=120,
    )
    return r.json()
```

## Pipeline tốc độ (&lt; 60s)

1. **Extract** song song từng trang PDF (PyMuPDF) / DOCX (python-docx)  
2. **Chunk** ~8 trang / chunk (cấu hình `CHUNK_PAGES`)  
3. **Map** LLM song song (`MAP_CONCURRENCY=6`) → điểm chính, quyết định, thuật ngữ, điều khoản  
4. **Reduce** 1 lần LLM → summary họp + câu hỏi + VB liên quan  
5. **Index** trang + BM25 cho Q&A họp  

Env tinh chỉnh (`.env`):

```
MAP_CONCURRENCY=6
CHUNK_PAGES=8
TARGET_SECONDS=55
MAX_PAGES_BUDGET=80
LLM_MODEL=grok-4.5
```

## Mẫu PDF 48 trang

```bash
source .venv/bin/activate
python scripts/make_sample_pdf.py samples/de_an_mau_48trang.pdf 48
python -m app.cli analyze samples/ --title "Demo 48 trang"
```

## Cấu trúc

```
doc-intel-tool/
  app/
    main.py       # FastAPI
    tools.py      # Schema + dispatcher AI tools
    pipeline.py   # Map-reduce summarization
    extract.py    # PDF/Word
    qa.py         # Meeting Q&A + citations
    llm.py        # SpaceXAI client
    store.py      # Job JSON store
    cli.py
  web/index.html  # Giao diện họp
  samples/
  data/jobs/      # Kết quả phân tích
```

## Lưu ý

- PDF **scan/ảnh** cần OCR (hiện cảnh báo nếu không trích được text).  
- Thời gian thực tế phụ thuộc latency model và số chunk; tăng `MAP_CONCURRENCY` nếu rate-limit cho phép.  
- API key chỉ đặt server-side, không nhúng vào frontend.
