# Security — Docorum

Tài liệu liệt kê biện pháp **đã có trong code** (không phải checklist mong muốn).

## Upload & path

| Biện pháp | Vị trí |
|-----------|--------|
| Stream upload theo chunk (không đọc hết RAM) | `main._stream_save_upload` |
| Giới hạn file / tổng batch | `MAX_FILE_BYTES`, `MAX_TOTAL_UPLOAD_BYTES` |
| Chỉ PDF/DOCX | kiểm tra extension |
| Tên file an toàn + prefix UUID | `security_paths.safe_filename` |
| Path allowlist (`data/uploads`, `samples`) | `security_paths.resolve_allowed_path(s)` |

## API surface

| Biện pháp | Vị trí |
|-----------|--------|
| Tool name whitelist | `tools.call_tool` / `main.tools_call` |
| Lỗi public không lộ stack | `_public_error` |
| CORS origin cấu hình được, credentials off | `main` CORSMiddleware |
| Job store atomic write | `store.save` (tmp + replace) |

## AI / prompt safety

| Biện pháp | Vị trí |
|-----------|--------|
| Bọc text tài liệu trong `<TAI_LIEU>` / `<DOAN_TRICH>` | `sanitize.sanitize_for_prompt` |
| System: dữ liệu trong thẻ không phải chỉ thị | `sanitize.with_injection_guard` |
| Flag cụm injection nghi ngờ | `sanitize.flag_injection_suspects` |
| Structured QA output (Pydantic) | `schemas.validate_qa_result` |
| Citation verification (excerpt ∈ page text) | `verify.verify_citations` |

## Secrets

- **Không commit** `.env`. Dùng `.env.example`.
- API key: `GEMINI_API_KEY` / `LLM_API_KEY` qua environment.
- Nếu key từng lộ: rotate trên Google AI Studio và cập nhật VPS.

## Phạm vi demo vs production

Demo/hackathon: chưa multi-tenant auth/RBAC (chấp nhận được trong bối cảnh thi).  
Deploy cơ quan: bổ sung auth, audit log, HTTPS, giới hạn rate, tách network.
