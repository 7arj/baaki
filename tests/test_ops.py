"""Operational endpoints: probes, version, metrics and request ids."""

import pytest
from fastapi.testclient import TestClient

from baaki.app import db as db_mod
from baaki.app.db import init_db, make_engine
from baaki.app.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    eng = make_engine(f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setattr(db_mod, "_engine", eng)
    init_db(eng)
    return TestClient(create_app())


def test_liveness_does_not_need_the_database(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_ready_when_database_is_migrated(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_not_ready_before_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("BAAKI_MIGRATE_ON_START", "false")
    monkeypatch.setattr(db_mod, "_engine", make_engine(f"sqlite:///{tmp_path}/empty.db"))
    r = TestClient(create_app()).get("/readyz")
    assert r.status_code == 503
    assert r.json()["reason"] == "schema not migrated"


def test_version_reports_build_commit(client):
    assert "version" in client.get("/version").json()


def test_metrics_use_route_templates_and_skip_probes(client):
    client.get("/login")
    client.get("/healthz")
    body = client.get("/metrics").text
    assert 'baaki_http_requests_total{method="GET",route="/login",status="200"}' in body
    assert 'route="/healthz"' not in body


def test_request_id_is_echoed(client):
    assert client.get("/login", headers={"X-Request-ID": "abc123"}).headers["X-Request-ID"] == "abc123"
    assert client.get("/login").headers["X-Request-ID"]
