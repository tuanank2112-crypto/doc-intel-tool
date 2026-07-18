from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
JOBS = DATA / "jobs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8090

    # LLM provider: openai_compatible | gemini | xai
    # (bắt buộc có field — settings.provider đọc llm_provider)
    llm_provider: str = "gemini"
    xai_api_key: str = ""
    llm_api_key: str = ""
    # Ưu tiên key từ .env; hardcode chỉ fallback dev (đừng commit key production)
    gemini_api_key: str = ""
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_model: str = "gemini-3.1-flash-lite"

    # 0 / negative = no page cut (user forbids truncating document content)
    max_pages_budget: int = 500
    target_seconds: int = 55
    # Map song song — 8–10 để ~25 chunk vẫn trong SLA
    map_concurrency: int = 8
    chunk_pages: int = 15  # legacy (chunk_pages now by char budget)
    # Trần số chunk map (co giãn theo độ dài; không cắt giữa nội dung)
    max_map_chunks: int = 40
    max_chars_per_chunk: int = 8000
    llm_timeout_seconds: float = 45.0
    llm_force_json_mode: bool = False
    # Reduce 1 call trên bản map ngắn — bật mặc định để tóm tắt phủ toàn văn
    llm_use_reduce: bool = True
    qa_top_k: int = 6
    # UI: never truncate page body (full document for officials)
    ui_page_chars: int = 0
    ui_truncate_pages: bool = False

    # --- Gemini Vision OCR (PDF page → image → text) ---
    # auto: only sparse/empty pages; always: every page; off: never
    ocr_mode: str = "auto"
    ocr_model: str = "gemini-3.1-flash-lite"
    ocr_concurrency: int = 8  # tang throughput OCR (don gian nhat de <60s);
    # neu API tier thap bi 429 nhieu -> ha ve 4-6; neu tier tra phi -> tang 10-12
    ocr_dpi: int = 180  # can bang net/toc do; ha 150 neu can nhanh hon nua,
    # nhung bang/chu nho co the mat net -> chi ha khi chat luong con du tot
    ocr_api_key: str = ""  # optional override; else gemini_api_key

    # Doc so lieu tu ANH bieu do (chart) tren trang born-digital qua Gemini Vision.
    # "off" = tat (mac dinh); "auto"/"on" = bat. Env override: CHART_VISION.
    chart_vision: str = "auto"

    # Upload limits
    max_upload_files: int = 10
    max_file_bytes: int = 40_000_000
    max_total_upload_bytes: int = 80_000_000
    cors_origins: str = "http://127.0.0.1:8090,http://localhost:8090"

    @property
    def api_key(self) -> str:
        return (
            self.llm_api_key
            or self.gemini_api_key
            or self.xai_api_key
            or os.getenv("GEMINI_API_KEY", "")
            or os.getenv("XAI_API_KEY", "")
            or os.getenv("LLM_API_KEY", "")
        )

    @property
    def provider(self) -> str:
        p = (self.llm_provider or "gemini").strip().lower()
        if p in ("google", "google-ai", "google_ai"):
            return "gemini"
        return p


settings = Settings()
UPLOADS.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)
