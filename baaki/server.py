"""Dashboard + Razorpay webhook receiver.

GET  /                       dashboard
GET  /api/summary?brain=     baseline-vs-agent metrics + risk report
GET  /api/run/{key}          full snapshot (invoices, timelines) e.g. agent_rules, naive, none
GET  /api/audit/{key}        audit entries, optional ?invoice=inv_0001
POST /webhooks/razorpay      verifies X-Razorpay-Signature, appends to reports/live_webhooks.jsonl
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .audit import iter_entries
from .razorpay_client import verify_webhook

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Baaki", version="0.1.0")


def _load(path: Path):
    if not path.exists():
        raise HTTPException(404, f"{path.name} not found — run `python -m baaki run` first")
    return json.loads(path.read_text())


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/summary")
def summary(brain: str | None = None):
    if brain is None:
        brain = "claude" if (REPORTS / "summary_claude.json").exists() else "rules"
    data = _load(REPORTS / f"summary_{brain}.json")
    data["brain"] = brain
    data["available_brains"] = [p.stem.split("_", 1)[1] for p in REPORTS.glob("summary_*.json")]
    return data


@app.get("/api/run/{key}")
def run(key: str):
    return _load(REPORTS / f"run_{key}.json")


@app.get("/api/audit/{key}")
def audit(key: str, invoice: str | None = None, limit: int = Query(500, le=5000)):
    path = REPORTS / f"audit_{key}.jsonl"
    if not path.exists():
        raise HTTPException(404, "no audit log")
    rows = [e for e in iter_entries(path) if invoice is None or e.get("invoice") == invoice]
    return rows[-limit:]


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Razorpay-Signature", "")
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "baaki-test-webhook-secret")
    if not verify_webhook(body, sig, secret):
        raise HTTPException(400, "invalid signature")
    event = json.loads(body)
    REPORTS.mkdir(exist_ok=True)
    with (REPORTS / "live_webhooks.jsonl").open("a") as f:
        f.write(json.dumps({"event": event.get("event"), "payload": event.get("payload")}) + "\n")
    return JSONResponse({"status": "ok", "event": event.get("event")})
