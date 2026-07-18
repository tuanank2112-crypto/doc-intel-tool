Dưới đây là review theo góc nhìn **senior code reviewer** cho dự án **Doc Intel** dựa trên các đoạn code bạn gửi. Vì code bị cắt ở vài file nên mình sẽ tập trung vào những rủi ro **có thể xác nhận từ snippet** và ưu tiên những điểm ảnh hưởng trực tiếp đến **độ đúng, bảo mật, hiệu năng, và tích hợp frontend**.

---

## Tóm tắt nhanh

Dự án có hướng đi tốt:  
- pipeline rõ ràng: **extract → OCR → map/reduce → QA**
- có ý thức về **SLA**, **fallback heuristic**, và **giữ nguyên frontend UI**
- có chia tách tương đối ổn giữa `main.py`, `pipeline.py`, `extract.py`, `vision_ocr.py`, `qa.py`, `llm.py`, `api-bridge.js`

Nhưng hiện tại có vài vấn đề đáng chú ý:

- **Critical**: API đang mở rộng bề mặt tấn công khá nhiều, đặc biệt là **đọc file local theo path người dùng gửi lên** và **upload xử lý nguyên file vào RAM**
- **High**: OCR + render trang PDF có nguy cơ làm **OOM / timeout / API rate-limit** khi tài liệu lớn hoặc scan nhiều trang
- **High**: frontend glue có rủi ro **lag browser**, **highlight sai**, và **vỡ tooltip** nếu HTML/text không được chuẩn hóa chặt
- **Medium**: lỗi/exception đang lộ chi tiết nội bộ ra response
- **Medium**: một số quyết định kiến trúc làm chất lượng trả lời không ổn định khi tài liệu dài

---

# 1) Mức độ rủi ro

## Critical

### 1.1. `/v1/analyze` và `/v1/analyze/folder` cho phép truyền path local từ client
Bạn đang nhận:
```py
class AnalyzeRequest(BaseModel):
    paths: list[str] = Field(..., description="Server-local file or folder paths")
```
và:
```py
folder = body.get("folder") or body.get("path")
result = await analyze_paths([folder], ...)
```

**Vấn đề**
- Nếu `analyze_paths` không kiểm soát chặt, client có thể ép server đọc bất kỳ đường dẫn nào trên máy chủ.
- Đây là **server-side path access** rõ ràng.
- `/v1/analyze/folder` còn yếu hơn vì dùng `dict[str, Any]`, không có validation.

**Tác động**
- Rò rỉ dữ liệu nội bộ
- Đọc nhầm file nhạy cảm
- Dẫn tới hành vi không mong muốn khi frontend gửi path lỗi

**Mức độ**: **Critical**

---

### 1.2. Upload endpoint đọc toàn bộ file vào memory
```py
raw = await f.read()
if len(raw) > 40_000_000:
```

**Vấn đề**
- `await f.read()` load toàn bộ file vào RAM.
- Giới hạn là **40MB/file**, nhưng:
  - nhiều file cùng lúc vẫn có thể làm nổ RAM
  - người dùng có thể upload nhiều file lớn trong một request
- Nếu là PDF scan nặng, memory spike có thể rất cao.

**Tác động**
- OOM
- worker restart
- nghẽn xử lý hàng loạt

**Mức độ**: **Critical/High** tùy môi trường deploy

---

## High

### 1.3. CORS quá mở
```py
allow_origins=["*"],
allow_credentials=True,
allow_methods=["*"],
allow_headers=["*"],
```

**Vấn đề**
- Đây là cấu hình quá rộng.
- `*` + `allow_credentials=True` là cấu hình rất dễ gây hiểu nhầm và không an toàn nếu sau này có cookie/session/auth.
- Hiện tại chưa thấy auth, nhưng đây là **mầm rủi ro** nếu deploy công khai.

**Mức độ**: **High**

---

### 1.4. OCR pipeline dễ gây bùng chi phí và timeout
Trong `vision_ocr.py`:
- render PDF page ra PNG
- OCR song song qua Gemini Vision
- mặc định `OCR_CONCURRENCY=10`
- `OCR_DPI=150`

**Vấn đề**
- Với scan PDF nhiều trang, mỗi trang render thành ảnh là tốn CPU + RAM.
- Concurrency 10 trên tài liệu lớn có thể tạo:
  - spike bộ nhớ
  - queue chậm
  - rate-limit từ Gemini
  - timeout tổng thể

**Mức độ**: **High**

---

### 1.5. Error handling đang trả message nội bộ ra client
Ví dụ:
```py
raise HTTPException(500, f"analyze_failed: {e}")
raise HTTPException(500, f"upload_analyze_failed: {e}")
```

**Vấn đề**
- Lộ chi tiết exception nội bộ ra API.
- Có thể lộ:
  - path
  - stack trace gián tiếp
  - thông tin provider / model / parsing failures

**Mức độ**: **High**

---

### 1.6. `/v1/tools/call` là một bề mặt thực thi tool trực tiếp
```py
result = await call_tool(body.name, body.arguments)
```

**Vấn đề**
- Nếu `call_tool` không whitelist thật chặt, đây là điểm mở rộng attack surface.
- Cần đảm bảo tool names và args được validate cứng.

**Mức độ**: **High** nếu tool registry chưa khóa chặt

---

## Medium

### 1.7. `analyze_folder_endpoint` không dùng schema Pydantic
```py
async def analyze_folder_endpoint(body: dict[str, Any]) -> dict[str, Any]:
```

**Vấn đề**
- Không có validation.
- Không có schema rõ ràng cho client.
- Dễ tạo lỗi runtime khó đoán.

**Mức độ**: **Medium**

---

### 1.8. Không thấy cơ chế giới hạn tổng số file / tổng dung lượng / tổng số trang theo job
Bạn có `max_pages_budget` nhưng từ snippet chưa thấy enforcement đủ chặt ở entrypoint.

**Vấn đề**
- Chỉ giới hạn file đơn lẻ chưa đủ.
- Job có thể có nhiều file và tổng pages rất lớn.

**Mức độ**: **Medium**

---

### 1.9. Tên file trong cùng batch có thể đè nhau
```py
target = dest_dir / name
```

**Vấn đề**
- Nếu user upload 2 file trùng tên, file sau ghi đè file trước.
- Với bộ hồ sơ, đây là lỗi correctness khó chịu.

**Mức độ**: **Medium**

---

### 1.10. `batch_id` và `bench batch_id` dựa trên time hash
```py
hashlib.sha256(f"{time.time()}".encode()).hexdigest()[:10]
```

**Vấn đề**
- Không phải security issue lớn vì đây chỉ là ID nội bộ.
- Nhưng có thể collision theo thời gian trong môi trường song song cực cao.
- Cũng đoán được phần nào.

**Mức độ**: **Low/Medium**

---

## Low

### 1.11. `/health` expose quá nhiều metadata runtime
Ví dụ:
- model
- ocr_ready
- target_seconds
- max_pages_budget

**Vấn đề**
- Không quá nguy hiểm nếu nội bộ.
- Nhưng nếu public internet thì đây là fingerprinting nhẹ.

**Mức độ**: **Low**

---

### 1.12. Fallback heuristic trả lời có thể gây hiểu nhầm
Trong `qa.py`, nếu không có LLM hoặc retrieval yếu, hệ thống vẫn trả kiểu:
> “Theo đoạn liên quan nhất…”

**Vấn đề**
- Cần phân biệt rõ giữa **trích xuất thật** và **heuristic fallback**.
- Nếu không, user có thể tin nhầm độ chắc chắn.

**Mức độ**: **Low/Medium**

---

# 2) Rủi ro frontend glue

Đây là phần khá quan trọng vì bạn đang giữ nguyên UI hackathon và chỉ “glue” bằng `api-bridge.js`.

## 2.1. Highlight/tooltip dễ vỡ vì xử lý HTML bằng regex
Đoạn:
```js
function decorateHtml(html, terms) { ... split(/(<[^>]+>)/g) ... }
```
và:
```js
return applyTermsToEscapedText(part, terms);
```

**Rủi ro**
- Regex-based HTML mutation là cực dễ lỗi:
  - bọc sai trong tag lồng nhau
  - phá table
  - highlight trùng
  - double-escape
- Nếu nội dung có markup phức tạp, tooltip `.term` rất dễ không còn ổn định.

**Triệu chứng**
- tooltip không hiện
- highlight sai vị trí
- text bị biến dạng
- bảng render lệch

**Mức độ**: **High**

---

## 2.2. Full page index rất dễ làm browser lag
Trong `main.py` có comment:
```py
# Full page_index for UI — do not cut document content
```

và trong `api-bridge.js`:
- `corpus += " " + (p.text || "")`
- highlight trên toàn bộ corpus
- map `summaries`, `terms`, `questions`

**Rủi ro**
- Nếu job có hàng chục trang text dày, page_index + decorate + DOM update sẽ chậm rõ.
- Nếu UI render toàn bộ page text lên DOM, browser có thể giật.

**Mức độ**: **High**

---

## 2.3. Upload UX có thể đẩy frontend vào trạng thái “kẹt”
Nếu user upload file lớn hoặc nhiều file:
- request lâu
- server có thể đang OCR
- frontend có thể không có progress chuẩn
- nếu API bridge không có abort/retry tốt, UX sẽ kẹt

**Mức độ**: **Medium/High**

---

## 2.4. `LIVE` state chỉ nằm trong memory của tab
```js
const LIVE = {};
let fileBag = [];
```

**Rủi ro**
- Reload là mất state
- nhiều tab khác nhau không đồng bộ
- tool mapping `/job_id → analysis` có thể lệch giữa session nếu không persist

**Mức độ**: **Medium**

---

## 2.5. `API = ""` dễ phụ thuộc ngầm vào same-origin
Nếu deploy khác origin hoặc qua reverse proxy, bridge có thể fail âm thầm.

**Mức độ**: **Medium**

---

# 3) OCR + map-reduce performance review

## 3.1. OCR song song hợp lý nhưng concurrency hiện đang “tham”
Trong `vision_ocr.py`:
```py
max_concurrent = max_concurrent or _concurrency()  # default 10
```

**Vấn đề**
- 10 concurrent page render + 10 concurrent Gemini calls có thể vượt ngưỡng an toàn với PDF scan.
- Với file nhiều trang, render là CPU-bound, OCR là network-bound.
- Nếu cả hai cùng chạy mạnh, bạn sẽ bị “dồn” ở cả CPU, RAM, và remote API.

**Khuyến nghị**
- Concurrency nên tách:
  - render concurrency thấp hơn
  - OCR concurrency giới hạn riêng
- Có backpressure theo số trang / tổng dung lượng ảnh

---

## 3.2. `render_all_pages_png` có thể đốt RAM
```py
out.append(pix.tobytes("png"))
```

Nếu dùng hàm này trên file lớn:
- giữ toàn bộ PNG bytes trong RAM
- cực dễ ngốn bộ nhớ

**Khuyến nghị**
- Chỉ render page theo nhu cầu
- Stream/queue theo batch nhỏ
- Không giữ tất cả ảnh trong list nếu không cần

---

## 3.3. `page_needs_ocr` heuristic quá đơn giản
```py
if len(t) < min_chars: return True
```

**Vấn đề**
- Page text có thể ngắn nhưng vẫn không cần OCR
- Page text có thể nhiều ký tự rác vẫn không OCR đúng theo thực trạng
- Với PDF scan lẫn text layer lỗi, heuristic có thể cho kết quả sai

**Khuyến nghị**
- Kết hợp thêm:
  - tỷ lệ ký tự in được
  - density từ text blocks
  - số từ
  - detection ảnh / text layer quality

---

## 3.4. Map-reduce chunking hiện phụ thuộc mạnh vào LLM
Nếu LLM fail:
- `_map_chunk` trả rỗng
- `_stitch_from_maps` dùng fallback heuristic

**Vấn đề**
- Result quality không ổn định giữa các loại tài liệu
- “terms” và “decision_points” phụ thuộc vào chunk extraction, dễ thiếu nếu chunk quá dài/ngắn

**Khuyến nghị**
- Tách rõ:
  - rule-based extraction cho legal cues
  - LLM chỉ làm tóm tắt / chuẩn hóa
- Với tài liệu pháp lý, luật hóa bằng regex / pattern sẽ ổn định hơn nhiều so với tin vào LLM cho toàn bộ.

---

## 3.5. `max_chars_per_chunk=8000`, `chunk_pages=15`, `max_map_chunks=6`
Với corpus dài:
- chunk 15 trang có thể quá to nếu là scan text dày
- nhưng nếu page ít text, lại quá nhỏ theo chiều semantic
- `max_map_chunks=6` có thể cắt mất nhiều nội dung

**Kết luận**
- Đây là trade-off hiện tại chưa được tune theo độ dài thực tế tài liệu.
- Cần benchmark theo nhóm:
  - văn bản hành chính ngắn
  - hồ sơ họp dài
  - scan nhiều trang
  - văn bản có bảng biểu

---

# 4) Review correctness / logic

## 4.1. Không thấy kiểm tra trùng tên file khi upload
Như đã nói, file sau overwrite file trước.

**Fix nên làm**
- rename theo `uuid4 + original_name`
- hoặc giữ subfolder riêng từng file

---

## 4.2. `analyze_paths` nhận list path nhưng path semantics chưa rõ
Bạn đang dùng cả:
- file local
- folder local

Nếu folder lớn:
- có thể quét cả file ngoài scope
- cần filter extension và canonicalize path

---

## 4.3. Trả full `page_index` cho UI là đúng về mặt nghiệp vụ nhưng dễ gây nặng
Câu comment:
```py
# Full page_index for UI — do not cut document content
```

Mình hiểu mục tiêu là “không cắt nội dung”, nhưng nên tách:
- **storage backend** giữ full
- **UI payload** có thể phân trang / lazy-load

Hiện tại bạn đang trả full thẳng cho browser, đây là điểm nghẽn lớn.

---

# 5) 5–10 fix ưu tiên, action rõ

Dưới đây là thứ tự mình khuyên làm ngay.

## Fix 1 — Khóa chặt input path cho `/v1/analyze` và `/v1/analyze/folder`
**Action**
- Chỉ cho phép path nằm trong một root directory an toàn.
- Canonicalize bằng `Path.resolve()`.
- Reject `..`, symlink ra ngoài root, absolute path ngoài allowlist.
- Thay `dict[str, Any]` bằng Pydantic model cho folder endpoint.

**Mục tiêu**
- Chặn SSRF nội bộ kiểu file/path access
- Giảm lỗi khó đoán

---

## Fix 2 — Đổi upload sang streaming / chunked write
**Action**
- Không `await f.read()` toàn bộ.
- Đọc theo chunk và ghi ra disk từng phần.
- Áp dụng **total request limit**, không chỉ limit từng file.
- Có giới hạn **số file/job**.

**Mục tiêu**
- Chặn OOM
- Ổn định worker khi upload file lớn

---

## Fix 3 — Tạo unique filename cho mỗi file upload
**Action**
- Lưu theo:
  - `dest_dir / f"{uuid4()}_{safe_name}"`
- Hoặc subdir theo file ID.

**Mục tiêu**
- Tránh overwrite khi trùng tên
- Giữ trace tốt hơn

---

## Fix 4 — Giảm concurrency OCR và tách render khỏi OCR
**Action**
- Giảm mặc định `OCR_CONCURRENCY` xuống mức an toàn hơn, ví dụ 3–5.
- Tách:
  - render queue
  - OCR queue
- Nếu tài liệu lớn, batch theo nhóm 3–5 trang.

**Mục tiêu**
- Tránh spike RAM/CPU
- Giảm timeout và rate-limit

---

## Fix 5 — Không render toàn bộ `page_index` vào DOM một lần
**Action**
- UI chỉ nhận summary + N trang đầu + lazy fetch phần còn lại.
- Hoặc API trả `page_index` theo phân trang.
- Nếu không sửa CSS UI, vẫn có thể thêm JS lazy render.

**Mục tiêu**
- Tránh browser lag
- Giữ highlight/tooltip ổn định hơn

---

## Fix 6 — Thay regex HTML decoration bằng DOM-safe approach
**Action**
- Parse DOM, chỉ decorate text nodes.
- Tránh replace raw HTML string.
- Nếu cần giữ tooltip hiện tại, bọc spans ngay trên text node.

**Mục tiêu**
- Chặn highlight phá markup
- Ổn định table / tooltip / nested tags

---

## Fix 7 — Chuẩn hóa error response
**Action**
- Log chi tiết ở server
- Client chỉ nhận message ngắn như:
  - `analyze_failed`
  - `upload_analyze_failed`
  - `ocr_failed`
- Không trả raw exception string ra ngoài

**Mục tiêu**
- Giảm leak nội bộ
- Đồng nhất API error handling

---

## Fix 8 — Giới hạn tổng pages / tổng bytes / tổng runtime theo job
**Action**
- Enforce `max_pages_budget` ngay trước khi OCR / map.
- Reject job vượt ngưỡng sớm.
- Có timeout/abort rõ ràng cho từng phase.

**Mục tiêu**
- Dự đoán SLA tốt hơn
- Tránh job “đào hố” quá sâu

---

## Fix 9 — Tách rõ “heuristic fallback” và “LLM answer”
**Action**
- Nếu fallback heuristic:
  - gắn cờ rõ trong response
  - UI hiển thị trạng thái “ước lượng / fallback”
- Với QA, trả `not_found=true` khi không đủ căn cứ thật sự.

**Mục tiêu**
- Giảm nhầm lẫn về độ tin cậy
- Tăng tính pháp lý cho output

---

## Fix 10 — Giảm fingerprinting ở `/health`
**Action**
- Chỉ giữ:
  - status
  - version
  - readiness tối thiểu
- Ẩn model / budget nếu endpoint public

**Mục tiêu**
- Giảm lộ metadata
- Ít thông tin cho attacker

---

# 6) Kết luận đánh giá

## Điểm mạnh
- Kiến trúc pipeline hợp lý
- Có quan tâm đến tài liệu pháp lý VN
- Có fallback để tránh fail toàn bộ
- Có chú ý UX frontend không muốn sửa CSS

## Điểm cần sửa ngay
1. **Path input security**
2. **Upload memory safety**
3. **OCR concurrency / timeout**
4. **Frontend HTML decoration**
5. **Không trả raw exception ra client**

Nếu bạn muốn, mình có thể làm tiếp một bản **review dạng PR comment theo từng file** với format:

- `file`
- `line/region`
- `issue`
- `severity`
- `recommendation`

để bạn đưa thẳng vào ticket hoặc code review checklist.