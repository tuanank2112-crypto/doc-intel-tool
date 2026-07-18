#!/usr/bin/env bash
# Cold-upload SLA bench: always NEW file name → NEW job → measure wall clock.
# Usage:
#   ./scripts/bench_upload_sla.sh [pdf_path] [base_url]
set -euo pipefail
PDF="${1:-/home/lenkuy/doc-intel-tool/samples/de_an_mau_48trang.pdf}"
BASE="${2:-http://127.0.0.1:8090}"
TMP="/tmp/sla_upload_$(date +%s%N).pdf"
cp "$PDF" "$TMP"

echo "=== Cold upload SLA bench ==="
echo "file: $TMP ($(pdfinfo "$TMP" 2>/dev/null | awk '/Pages/{print $2}' || echo '?') pages)"
echo "url:  $BASE/v1/bench/upload"
echo

# curl wall clock + server timing JSON
curl -sS -X POST "$BASE/v1/bench/upload" \
  -F "files=@${TMP}" \
  -F "title=SLA cold bench $(date +%H:%M:%S)" \
  -o /tmp/sla_bench_result.json \
  -w "curl_http_code=%{http_code}\ncurl_time_total=%{time_total}s\ncurl_time_starttransfer=%{time_starttransfer}s\n"

echo
python3 - <<'PY'
import json
d=json.load(open("/tmp/sla_bench_result.json"))
print("job_id:", d.get("job_id"))
print("pages:", d.get("total_pages"), " chunks:", d.get("chunk_count"), " llm:", d.get("llm_used"))
print("server elapsed_seconds:", d.get("elapsed_seconds"))
print("request_wall_clock_seconds:", d.get("request_wall_clock_seconds"))
print("within_60s:", d.get("within_60s"))
print("timing.phases:")
for k,v in (d.get("timing") or {}).get("phases", {}).items():
    print(f"  {k}: {v}")
print("note:", (d.get("timing") or {}).get("note", d.get("how_to_read",""))[:220])
print()
ok = d.get("within_60s") and (d.get("request_wall_clock_seconds") or 999) < 60
print("RESULT:", "PASS <60s" if ok else "FAIL >=60s or error")
PY

rm -f "$TMP"
