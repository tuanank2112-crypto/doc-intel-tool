from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .config import JOBS


class JobStore:
    """Lightweight JSON job store for documents + analysis + Q&A index."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or JOBS
        self.root.mkdir(parents=True, exist_ok=True)

    def new_job_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def job_dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, job_id: str, payload: dict[str, Any]) -> Path:
        """Atomic write: ghi vào .tmp rồi os.replace() — tránh race condition."""
        path = self.job_dir(job_id) / "job.json"
        payload = {**payload, "job_id": job_id, "updated_at": time.time()}
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        # Ghi sang file tạm cùng thư mục rồi rename (atomic trên Linux)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".job_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return path

    def load(self, job_id: str) -> dict[str, Any] | None:
        path = self.job_dir(job_id) / "job.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # File bị corrupt (vd: ghi dở) — coi như không có
            return None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p in sorted(self.root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_dir():
                continue
            data = self.load(p.name)
            if not data:
                continue
            items.append(
                {
                    "job_id": data.get("job_id", p.name),
                    "status": data.get("status"),
                    "title": data.get("title") or data.get("summary", {}).get("context", "")[:80],
                    "total_pages": data.get("total_pages"),
                    "elapsed_seconds": data.get("elapsed_seconds"),
                    "created_at": data.get("created_at"),
                    "files": [f.get("filename") for f in data.get("files", [])],
                }
            )
            if len(items) >= limit:
                break
        return items


store = JobStore()
