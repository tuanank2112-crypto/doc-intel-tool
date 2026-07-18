#!/usr/bin/env python3
"""Minimal example: AI agent calls Doc Intel tools over HTTP."""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.getenv("DOC_INTEL_URL", "http://127.0.0.1:8090")


def main() -> int:
    paths = sys.argv[1:] or [os.path.join(os.path.dirname(__file__), "..", "samples")]
    client = httpx.Client(base_url=BASE, timeout=120.0)

    tools = client.get("/v1/tools").json()["tools"]
    print("Available tools:", [t["function"]["name"] for t in tools])

    print("\n== analyze_documents ==")
    r = client.post(
        "/v1/tools/call",
        json={"name": "analyze_documents", "arguments": {"paths": paths, "title": "Agent demo"}},
    )
    r.raise_for_status()
    analysis = r.json()
    job_id = analysis.get("job_id")
    print(json.dumps({k: analysis.get(k) for k in (
        "job_id", "status", "total_pages", "elapsed_seconds", "within_60s", "summary",
        "terminology", "suggested_questions", "related_documents",
    ) if k in analysis}, ensure_ascii=False, indent=2)[:3000])

    if not job_id:
        return 1

    print("\n== ask_document ==")
    r = client.post(
        "/v1/tools/call",
        json={
            "name": "ask_document",
            "arguments": {
                "job_id": job_id,
                "question": "Các điểm cần quyết định trong cuộc họp là gì?",
            },
        },
    )
    print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
