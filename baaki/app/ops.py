"""Operational endpoints and telemetry: what Kubernetes, Prometheus and an on-call engineer need.

- /healthz  liveness: the process is up and serving. Never touches the database, so a database
            outage does not make Kubernetes restart healthy web pods in a loop.
- /readyz   readiness: the database answers and the schema has been migrated. A failing pod is
            taken out of the Service's endpoints (no traffic) but not restarted.
- /version  the git commit this image was built from, so the running version is traceable.
- /metrics  Prometheus exposition: request rate, errors and latency histogram (RED).

Every request also gets one JSON log line carrying a request id, echoed back as X-Request-ID.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text

from .db import current_revision, engine

GIT_SHA = os.environ.get("GIT_SHA", "dev")

# Labels are bounded on purpose: the route *template* (/app/invoices/{inv_id}), never the raw path,
# so one series exists per endpoint rather than one per invoice id.
REQUESTS = Counter("baaki_http_requests_total", "HTTP requests served", ["method", "route", "status"])
LATENCY = Histogram(
    "baaki_http_request_duration_seconds", "HTTP request latency", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Probes and scrapes would otherwise dominate the request rate and flatter the latency numbers.
_UNMEASURED = {"/healthz", "/readyz", "/metrics"}

log = logging.getLogger("baaki.access")
router = APIRouter()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
                 "logger": record.name, "msg": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def configure_logging() -> None:
    if any(isinstance(h.formatter, JsonFormatter) for h in log.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


@router.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz():
    try:
        with engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        revision = current_revision()
    except Exception as e:  # any DB failure means "don't send me traffic"
        return JSONResponse({"status": "unavailable", "reason": type(e).__name__}, status_code=503)
    if revision is None:
        return JSONResponse({"status": "unavailable", "reason": "schema not migrated"}, status_code=503)
    return {"status": "ready", "schema": revision}


@router.get("/version", include_in_schema=False)
def version():
    return {"version": GIT_SHA}


@router.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install(app: FastAPI) -> None:
    configure_logging()
    app.include_router(router)

    @app.middleware("http")
    async def telemetry(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
        finally:
            elapsed = time.perf_counter() - start
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            if request.url.path not in _UNMEASURED:
                REQUESTS.labels(request.method, template, str(status)).inc()
                LATENCY.labels(request.method, template).observe(elapsed)
                log.info("request", extra={"fields": {
                    "request_id": request_id, "method": request.method, "route": template,
                    "path": request.url.path, "status": status, "duration_ms": round(elapsed * 1000, 1),
                    "version": GIT_SHA}})
        response.headers["X-Request-ID"] = request_id
        return response
