"""
Smoke / regression eval for Docorum Q&A.

Usage (from repo root):
  cd repo && source .venv/bin/activate && python -m evals.run_eval
  python -m evals.run_eval --job-id <id>
  python -m evals.run_eval --threshold 0.6 --fold-diacritics
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

# Ensure repo root on path when run as python -m evals.run_eval
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.qa import ask_job  # noqa: E402  — required: call real QA, do not reimplement


def _fold(s: str, *, fold_diacritics: bool) -> str:
    s = (s or "").lower().strip()
    s = " ".join(s.split())
    if not fold_diacritics:
        return s
    # NFD then strip combining marks
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def answer_partial_score(
    answer: str,
    expect_contains: list[str],
    *,
    fold_diacritics: bool,
) -> float:
    if not expect_contains:
        return 1.0
    a = _fold(answer, fold_diacritics=fold_diacritics)
    hits = 0
    for needle in expect_contains:
        n = _fold(str(needle), fold_diacritics=fold_diacritics)
        if n and n in a:
            hits += 1
    return hits / len(expect_contains)


def answer_match_all(
    answer: str,
    expect_contains: list[str],
    *,
    fold_diacritics: bool,
) -> bool:
    return answer_partial_score(answer, expect_contains, fold_diacritics=fold_diacritics) >= 1.0 - 1e-9


def citation_match(
    citations: list[dict[str, Any]],
    *,
    expect_page: int | None,
    expect_clause: str | None,
    fold_diacritics: bool,
) -> bool:
    if expect_page is None and not expect_clause:
        return True
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        page_ok = True
        clause_ok = True
        if expect_page is not None:
            try:
                page_ok = int(c.get("page")) == int(expect_page)
            except (TypeError, ValueError):
                page_ok = False
        if expect_clause:
            cl = _fold(str(c.get("clause") or ""), fold_diacritics=fold_diacritics)
            want = _fold(str(expect_clause), fold_diacritics=fold_diacritics)
            # also allow clause text inside excerpt/answer-side fields
            clause_ok = want in cl if want else True
            if not clause_ok:
                # soft: clause may appear only in excerpt
                ex = _fold(str(c.get("excerpt") or ""), fold_diacritics=fold_diacritics)
                clause_ok = want in ex
        if page_ok and clause_ok:
            return True
    return False


def any_verified(citations: list[dict[str, Any]]) -> bool:
    return any(isinstance(c, dict) and c.get("verified") is True for c in (citations or []))


def load_testset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"Invalid JSONL line {line_no}: {e}") from e
    return rows


async def ensure_job_id(job_id: str | None, sample: Path) -> str:
    if job_id:
        return job_id
    if not sample.is_file():
        raise SystemExit(f"Sample PDF not found: {sample} (pass --job-id to skip analyze)")
    print(f"[eval] No --job-id — analyzing sample: {sample}", flush=True)
    from app.pipeline import analyze_paths  # local import; does not modify app/

    result = await analyze_paths([str(sample.resolve())], title="evals-smoke-sample")
    jid = result.get("job_id")
    if not jid:
        raise SystemExit(f"analyze_paths returned no job_id: keys={list(result.keys())}")
    print(f"[eval] job_id={jid} pages={result.get('total_pages')} elapsed={result.get('elapsed_seconds')}", flush=True)
    return str(jid)


async def run_one(
    job_id: str,
    case: dict[str, Any],
    *,
    fold_diacritics: bool,
) -> dict[str, Any]:
    q = str(case.get("question") or "").strip()
    expect_contains = case.get("expect_contains") or []
    if not isinstance(expect_contains, list):
        expect_contains = [str(expect_contains)]
    expect_page = case.get("expect_page")
    expect_clause = case.get("expect_clause")

    resp = await ask_job(job_id, q)
    if resp.get("error"):
        return {
            "id": case.get("id") or q[:40],
            "question": q,
            "error": resp.get("error"),
            "answer_match": False,
            "answer_partial": 0.0,
            "citation_match": False,
            "citation_verified": False,
            "answer_mode": "error",
            "llm_used": False,
            "confidence": None,
            "answer_preview": "",
        }

    answer = str(resp.get("answer") or "")
    citations = resp.get("citations") or []
    if not isinstance(citations, list):
        citations = []

    partial = answer_partial_score(answer, expect_contains, fold_diacritics=fold_diacritics)
    am = answer_match_all(answer, expect_contains, fold_diacritics=fold_diacritics)
    cm = citation_match(
        citations,
        expect_page=int(expect_page) if expect_page is not None else None,
        expect_clause=str(expect_clause) if expect_clause else None,
        fold_diacritics=fold_diacritics,
    )
    cv = any_verified(citations)

    return {
        "id": case.get("id") or q[:40],
        "question": q,
        "answer_match": am,
        "answer_partial": round(partial, 3),
        "citation_match": cm,
        "citation_verified": cv,
        "answer_mode": resp.get("answer_mode") or ("llm" if resp.get("llm_used") else "unknown"),
        "llm_used": bool(resp.get("llm_used")),
        "confidence": resp.get("confidence"),
        "citations_verified_count": resp.get("citations_verified_count"),
        "citations_total": resp.get("citations_total"),
        "answer_preview": answer.replace("\n", " ")[:120],
    }


def _pct(n: int, d: int) -> str:
    if d <= 0:
        return "n/a"
    return f"{100.0 * n / d:.1f}%"


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = [
        "id",
        "ans",
        "partial",
        "cite",
        "verif",
        "mode",
        "llm",
        "preview",
    ]
    col_w = {h: len(h) for h in headers}
    display_rows = []
    for r in rows:
        dr = {
            "id": str(r.get("id", "")),
            "ans": "PASS" if r.get("answer_match") else "FAIL",
            "partial": f"{float(r.get('answer_partial') or 0):.0%}",
            "cite": "Y" if r.get("citation_match") else "N",
            "verif": "Y" if r.get("citation_verified") else "N",
            "mode": str(r.get("answer_mode") or "")[:18],
            "llm": "Y" if r.get("llm_used") else "N",
            "preview": str(r.get("answer_preview") or r.get("error") or "")[:50],
        }
        display_rows.append(dr)
        for h in headers:
            col_w[h] = max(col_w[h], len(dr[h]))

    def fmt(row: dict[str, str]) -> str:
        return " | ".join(row[h].ljust(col_w[h]) for h in headers)

    sep = "-+-".join("-" * col_w[h] for h in headers)
    print(fmt({h: h for h in headers}))
    print(sep)
    for dr in display_rows:
        print(fmt(dr))


def summarize(rows: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"label": label, "n": 0}
    am = sum(1 for r in rows if r.get("answer_match"))
    cm = sum(1 for r in rows if r.get("citation_match"))
    cv = sum(1 for r in rows if r.get("citation_verified"))
    modes = Counter(str(r.get("answer_mode") or "unknown") for r in rows)
    llm_n = sum(1 for r in rows if r.get("llm_used"))
    return {
        "label": label,
        "n": n,
        "accuracy": am / n,
        "answer_match": am,
        "citation_hit": cm / n,
        "citation_verified_rate": cv / n,
        "modes": dict(modes),
        "llm_used_n": llm_n,
    }


def print_summary(s: dict[str, Any]) -> None:
    print(f"\n=== {s['label']} (n={s.get('n', 0)}) ===")
    if not s.get("n"):
        print("(empty subset)")
        return
    print(f"Accuracy (answer_match ALL): {s['accuracy']:.1%}  ({s['answer_match']}/{s['n']})")
    print(f"Citation-hit rate:           {s['citation_hit']:.1%}")
    print(f"Citation-verified rate:      {s['citation_verified_rate']:.1%}")
    print(f"answer_mode distribution:    {s['modes']}")
    print(f"llm_used:                    {s['llm_used_n']}/{s['n']}")


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Docorum Q&A smoke/regression eval")
    parser.add_argument(
        "--job-id",
        default=None,
        help="Existing analyzed job_id (recommended). If omitted, analyzes samples/de_an_mau_48trang.pdf",
    )
    parser.add_argument(
        "--testset",
        type=Path,
        default=_ROOT / "evals" / "qa_testset.jsonl",
        help="Path to JSONL testset",
    )
    parser.add_argument(
        "--sample",
        type=Path,
        default=_ROOT / "samples" / "de_an_mau_48trang.pdf",
        help="Sample PDF used when --job-id is omitted",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.6,
        help="Min accuracy for exit 0 (default 0.6)",
    )
    parser.add_argument(
        "--fold-diacritics",
        action="store_true",
        help="Strip Vietnamese diacritics when matching (default: keep diacritics)",
    )
    args = parser.parse_args(argv)

    cases = load_testset(args.testset)
    if not cases:
        print("Empty testset", file=sys.stderr)
        return 1

    job_id = await ensure_job_id(args.job_id, args.sample)
    print(f"[eval] Running {len(cases)} cases on job_id={job_id}", flush=True)
    print(f"[eval] fold_diacritics={args.fold_diacritics} threshold={args.threshold}", flush=True)

    rows: list[dict[str, Any]] = []
    for case in cases:
        r = await run_one(job_id, case, fold_diacritics=args.fold_diacritics)
        rows.append(r)
        status = "PASS" if r.get("answer_match") else "FAIL"
        print(
            f"  [{status}] {r.get('id')}: partial={r.get('answer_partial')} "
            f"cite={r.get('citation_match')} verif={r.get('citation_verified')} "
            f"mode={r.get('answer_mode')}",
            flush=True,
        )

    print("\n=== Per-case table ===")
    print_table(rows)

    overall = summarize(rows, label="Overall")
    print_summary(overall)

    llm_rows = [r for r in rows if r.get("llm_used")]
    llm_only = summarize(llm_rows, label="LLM-only subset")
    print_summary(llm_only)

    non_llm = sum(1 for r in rows if not r.get("llm_used"))
    if non_llm >= max(1, len(rows) // 2):
        print(
            "\n[WARN] Phần lớn câu có llm_used=false (thiếu API key / quota / fallback). "
            "Điểm Accuracy không phản ánh chất lượng LLM — xem cột answer_mode và LLM-only subset.",
            flush=True,
        )

    acc = float(overall.get("accuracy") or 0.0)
    if acc >= args.threshold:
        print(f"\n[RESULT] PASS accuracy={acc:.1%} >= threshold={args.threshold}")
        return 0
    print(f"\n[RESULT] FAIL accuracy={acc:.1%} < threshold={args.threshold}")
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
