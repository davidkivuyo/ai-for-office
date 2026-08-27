"""Benchmark both Ollama nodes against the same prompts per AGENTS §17.

Usage:
  NEXUS_TOKEN=<token> python scripts/benchmark_models.py --api http://localhost:8000

Token is read from the NEXUS_TOKEN environment variable (obtain via POST /api/auth/login).
Set NEXUS_TOKEN in the environment; do not pass tokens as command-line arguments.

Records latency_ms, output_length, success/failure; human_quality_score is filled
manually in the CSV afterwards per instructions (do not auto-score).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import httpx

# Eight categories per spec
PROMPTS = [
    {"id": "summarization", "prompt": "Summarize the following memo in 3 bullet points: Q3 revenue was $4.2M, up 8% QoQ but 2% below target. North region overspent on logistics by $112k. Recommend cost controls for Q4."},
    {"id": "email_drafting", "prompt": "Draft a concise professional email to the finance team requesting a variance review meeting next week. Tone: polite, direct."},
    {"id": "rewriting", "prompt": "Rewrite this paragraph in clearer business English: 'The project was delayed because of unforeseen issues that happened during implementation and caused timeline slippage.'"},
    {"id": "structured_json", "prompt": 'Extract as JSON: "Invoice INV-9001 from Acme Corp dated 2026-08-01 for $12,500 due 2026-08-31." Return {invoice_id, vendor, amount, due_date}.'},
    {"id": "basic_reasoning", "prompt": "If a team of 4 analysts each reviews 30 documents per day, how many days to review 900 documents? Show steps."},
    {"id": "spreadsheet_formula", "prompt": "Explain Excel formula =VLOOKUP(A2, $B$2:$D$100, 3, FALSE) to a non-technical office user in 2-3 sentences."},
    {"id": "document_extraction", "prompt": "From: 'Contract #C-889 valid 2026-01-01 to 2026-12-31, penalty $500/day late.' Extract parties, dates, penalty in a table."},
    {"id": "short_business_report", "prompt": "Write a 80-word executive summary for Q3 Operations: costs +13.7%, logistics -5.7%, recommend two actions."},
]


def run_one(api_base: str, token: str, node: str, prompt_id: str, prompt: str) -> dict:
    url = f"{api_base.rstrip('/')}/api/chat"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"message": prompt, "node_id": node, "stream": False}
    start = time.perf_counter()
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=180.0)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if r.status_code != 200:
            return {"prompt_id": prompt_id, "node": node, "latency_ms": latency_ms, "output_length": 0, "success": False, "error": r.text[:500]}
        data = r.json()
        reply = data.get("reply", "")
        return {
            "prompt_id": prompt_id,
            "node": node,
            "model": data.get("actual_model", ""),
            "actual_node": data.get("actual_node", node),
            "latency_ms": latency_ms,
            "reported_latency_ms": data.get("latency_ms"),
            "output_length": len(reply),
            "success": True,
            "reply": reply[:200].replace("\n", " "),
            "human_quality_score": "",
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {"prompt_id": prompt_id, "node": node, "latency_ms": latency_ms, "output_length": 0, "success": False, "error": str(e)[:500], "human_quality_score": ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("API_BASE", "http://localhost:8000"))
    ap.add_argument("--out", default="benchmark_results.csv")
    ap.add_argument("--nodes", nargs="*", default=["node1", "node2"])
    args = ap.parse_args()

    token = os.getenv("NEXUS_TOKEN", "")
    if not token:
        print("ERROR: NEXUS_TOKEN environment variable not set (login via POST /api/auth/login)", file=sys.stderr)
        sys.exit(1)

    rows = []
    for node in args.nodes:
        for p in PROMPTS:
            print(f"Benchmark {p['id']} on {node} ...")
            row = run_one(args.api, token, node, p["id"], p["prompt"])
            print(f"  -> {row.get('success')} {row.get('latency_ms')}ms len={row.get('output_length')}")
            rows.append(row)
            time.sleep(0.5)

    out_path = Path(args.out)
    # Collect all keys
    keys = ["prompt_id", "node", "model", "actual_node", "latency_ms", "reported_latency_ms", "output_length", "success", "human_quality_score", "reply", "error"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to {out_path}")
    print("Next: fill human_quality_score manually per §17 — do not auto-select best model on tokens/sec alone.")


if __name__ == "__main__":
    main()
