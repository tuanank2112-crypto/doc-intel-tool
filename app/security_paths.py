"""Path allowlist — only paths under project data roots may be analyzed."""
from __future__ import annotations

import re
from pathlib import Path

from .config import ROOT, UPLOADS, JOBS, DATA

# Allowed roots for server-local analyze
ALLOWED_ROOTS: list[Path] = [
    UPLOADS.resolve(),
    (DATA / "demo").resolve() if (DATA / "demo").exists() else DATA.resolve(),
    (ROOT / "samples").resolve(),
    DATA.resolve(),
]


def safe_filename(name: str) -> str:
    base = Path(name or "doc.pdf").name
    base = re.sub(r"[^\w.\-() \u00C0-\u024F\u1E00-\u1EFF]+", "_", base, flags=re.UNICODE)
    base = base.strip("._ ") or "doc.pdf"
    if not base.lower().endswith((".pdf", ".docx")):
        base = base + ".pdf"
    return base[:180]


def is_under(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        root = root.resolve()
        path.relative_to(root)
        return True
    except Exception:
        return False


def resolve_allowed_path(raw: str) -> Path:
    """Resolve and enforce path is under ALLOWED_ROOTS. Raises ValueError if not."""
    if not raw or not str(raw).strip():
        raise ValueError("empty_path")
    # reject null bytes / odd schemes
    if "\x00" in raw or raw.startswith(("http://", "https://", "file:")):
        raise ValueError("invalid_path")
    p = Path(raw).expanduser()
    # relative → under UPLOADS
    if not p.is_absolute():
        p = (UPLOADS / p).resolve()
    else:
        p = p.resolve()
    for root in ALLOWED_ROOTS:
        if not root.exists():
            continue
        if is_under(p, root):
            if not p.exists():
                raise FileNotFoundError(str(p))
            return p
    raise PermissionError("path_not_allowed")


def resolve_allowed_paths(paths: list[str]) -> list[str]:
    return [str(resolve_allowed_path(p)) for p in paths]
