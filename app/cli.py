from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .pipeline import analyze_paths
from .qa import ask_job
from .store import store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="doc-intel",
        description="Smart document intel: summarize PDF/Word folders, Q&A with citations.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_an = sub.add_parser("analyze", help="Analyze files or a folder")
    p_an.add_argument("paths", nargs="+", help="PDF/DOCX paths and/or folders")
    p_an.add_argument("--title", default=None)
    p_an.add_argument("-o", "--output", default=None, help="Write full JSON to file")

    p_ask = sub.add_parser("ask", help="Ask a question about a job")
    p_ask.add_argument("job_id")
    p_ask.add_argument("question")

    p_get = sub.add_parser("get", help="Get job JSON")
    p_get.add_argument("job_id")

    p_list = sub.add_parser("list", help="List jobs")
    p_list.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)

    if args.cmd == "analyze":
        result = asyncio.run(analyze_paths(args.paths, title=args.title))
        slim = {k: v for k, v in result.items() if k != "page_index"}
        text = json.dumps(slim, ensure_ascii=False, indent=2)
        if args.output:
            open(args.output, "w", encoding="utf-8").write(
                json.dumps(result, ensure_ascii=False, indent=2)
            )
            print(f"job_id={result.get('job_id')} elapsed={result.get('elapsed_seconds')}s -> {args.output}")
        else:
            print(text)
        return 0 if result.get("status") == "completed" else 1

    if args.cmd == "ask":
        result = asyncio.run(ask_job(args.job_id, args.question))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("error") else 1

    if args.cmd == "get":
        data = store.load(args.job_id)
        if not data:
            print("job_not_found", file=sys.stderr)
            return 1
        data = {k: v for k, v in data.items() if k != "page_index"}
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "list":
        print(json.dumps({"jobs": store.list_jobs(args.limit)}, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
