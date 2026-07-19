"""Pydantic schemas for LLM structured outputs."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    filename: Optional[str] = None
    page: Optional[int] = None
    clause: Optional[str] = None
    excerpt: Optional[str] = None
    verified: Optional[bool] = None

    model_config = {"extra": "ignore"}


class QAResult(BaseModel):
    answer: str = ""
    citations: list[Citation] = Field(default_factory=list)
    confidence: Literal["cao", "trung_binh", "thap"] = "trung_binh"
    not_found: bool = False

    model_config = {"extra": "ignore"}

    @field_validator("confidence", mode="before")
    @classmethod
    def _norm_conf(cls, v: Any) -> str:
        s = str(v or "trung_binh").strip().lower()
        if s in ("cao", "high"):
            return "cao"
        if s in ("thap", "thấp", "low"):
            return "thap"
        return "trung_binh"

    @field_validator("answer", mode="before")
    @classmethod
    def _norm_answer(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("citations", mode="before")
    @classmethod
    def _norm_cites(cls, v: Any) -> list:
        if not v:
            return []
        if isinstance(v, list):
            return v
        return []


def validate_qa_result(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """
    Validate LLM QA JSON. Returns (dict, None) on success or (None, error).
    Rejects pure raw/unparsed payloads.
    """
    if not isinstance(data, dict):
        return None, "not_a_dict"
    if data.get("raw") and not data.get("answer"):
        return None, "unparsed_raw"
    try:
        obj = QAResult.model_validate(data)
        return obj.model_dump(), None
    except Exception as e:
        return None, str(e)[:200]
