"""Synthetic but realistic SME receivables ledger. Deterministic for a given seed.

Each debtor has a hidden behavioural archetype that drives the simulator; the visible history
(prior late count, partial payments...) is *correlated* with it, so risk scoring has signal but
is not a cheat.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict
from pathlib import Path

from .domain import Archetype, Debtor, Invoice

FIRST = ["Sharma", "Kavya", "Mehta", "Iyer", "Reddy", "Patel", "Banerjee", "Das", "Nair", "Gupta", "Joshi", "Khan", "Rao", "Singh", "Verma", "Pillai", "Chawla", "Bose", "Mishra", "Desai"]
KIND = ["Traders", "Textiles", "Enterprises", "Agro", "Electricals", "Packaging", "Foods", "Logistics", "Prints", "Steel Works", "Ceramics", "Autoparts", "Pharma Distributors", "Garments", "Hardware"]
CITIES = ["Surat", "Ludhiana", "Coimbatore", "Indore", "Jaipur", "Pune", "Rajkot", "Kanpur", "Tiruppur", "Nagpur", "Hyderabad", "Vadodara", "Bengaluru", "Kolkata", "Kochi"]
ITEMS = ["cotton yarn lot", "printed labels", "corrugated boxes", "MS pipes", "packaging film", "spare parts", "dyes & chemicals", "office fit-out", "catering services", "CNC job work", "pallets", "fasteners"]

ARCHETYPE_MIX = [
    (Archetype.PROMPT, 0.20),
    (Archetype.FORGETFUL, 0.25),
    (Archetype.CASH_STRAPPED, 0.25),
    (Archetype.DISPUTER, 0.12),
    (Archetype.GHOST, 0.12),
    (Archetype.INSOLVENT, 0.06),
]


def _pick_archetype(rng: random.Random) -> Archetype:
    r = rng.random()
    acc = 0.0
    for a, p in ARCHETYPE_MIX:
        acc += p
        if r <= acc:
            return a
    return Archetype.FORGETFUL


def _history(rng: random.Random, a: Archetype) -> tuple[int, int, float, int]:
    n = rng.randint(2, 14)
    late_rate = {
        Archetype.PROMPT: 0.05,
        Archetype.FORGETFUL: 0.45,
        Archetype.CASH_STRAPPED: 0.7,
        Archetype.DISPUTER: 0.35,
        Archetype.GHOST: 0.8,
        Archetype.INSOLVENT: 0.85,
    }[a]
    late = sum(1 for _ in range(n) if rng.random() < late_rate)
    avg_late = 0.0 if late == 0 else round(rng.uniform(3, 12) * (1 + late_rate * 3), 1)
    partial = rng.randint(0, 3) if a in (Archetype.CASH_STRAPPED, Archetype.INSOLVENT) else (1 if rng.random() < 0.1 else 0)
    return n, late, avg_late, partial


def generate(seed: int = 7, n_debtors: int = 40, n_invoices: int = 120) -> tuple[list[Debtor], list[Invoice]]:
    rng = random.Random(seed)
    debtors: list[Debtor] = []
    used = set()
    for i in range(n_debtors):
        while True:
            name = f"{rng.choice(FIRST)} {rng.choice(KIND)}"
            if name not in used:
                used.add(name)
                break
        a = _pick_archetype(rng)
        n, late, avg_late, partial = _history(rng, a)
        slug = name.lower().replace(" ", "").replace("&", "")
        debtors.append(
            Debtor(
                id=f"cust_{i+1:03d}",
                name=name,
                email=f"accounts@{slug}.in",
                contact=f"+91{rng.randint(70000, 99999)}{rng.randint(10000, 99999)}",
                city=rng.choice(CITIES),
                prior_invoices=n,
                prior_late_count=late,
                avg_days_late=avg_late,
                prior_partial_payments=partial,
                archetype=a,
            )
        )

    invoices: list[Invoice] = []
    for j in range(n_invoices):
        d = rng.choice(debtors)
        amount = rng.choice([8500, 12000, 18500, 24000, 32000, 45000, 60000, 85000, 120000, 175000, 240000]) * 100
        amount += rng.randint(0, 999) * 100
        due_day = -rng.randint(1, 45)  # already overdue at sim start (day 0)
        issue_day = due_day - 30
        invoices.append(
            Invoice(
                id=f"inv_{j+1:04d}",
                debtor_id=d.id,
                amount_paise=amount,
                issue_day=issue_day,
                due_day=due_day,
                description=f"{rng.choice(ITEMS)} — PO {rng.randint(1000, 9999)}",
            )
        )
    return debtors, invoices


def save(path: Path, debtors: list[Debtor], invoices: list[Invoice]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": "archetype is HIDDEN ground truth for the simulator; the agent never receives it.",
        "debtors": [asdict(d) for d in debtors],
        "invoices": [
            {k: v for k, v in asdict(i).items() if k in ("id", "debtor_id", "amount_paise", "issue_day", "due_day", "description")}
            for i in invoices
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))


def load(path: Path) -> tuple[list[Debtor], list[Invoice]]:
    raw = json.loads(path.read_text())
    debtors = [Debtor(**{**d, "archetype": Archetype(d["archetype"])}) for d in raw["debtors"]]
    invoices = [Invoice(**i) for i in raw["invoices"]]
    return debtors, invoices
