"""Shared fixtures. The engine's contact-hours rule reads a clock; tests pin it to mid-morning
so the suite passes identically at 10:00 and 23:00 IST."""

from datetime import datetime, time as dtime

import pytest

from baaki.app import service
from baaki.domain import IST


@pytest.fixture(autouse=True)
def engine_runs_mid_morning(monkeypatch):
    original = service.RecoveryEngine.__init__

    def pinned(self, db, org, today=None, dry_run=False, at=None):
        if at is None:
            base = today or datetime.now(IST).date()
            at = datetime.combine(base, dtime(10, 0), IST)
        original(self, db, org, today=today, dry_run=dry_run, at=at)

    monkeypatch.setattr(service.RecoveryEngine, "__init__", pinned)
