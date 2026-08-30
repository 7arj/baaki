"""Risk model: probability an invoice will NOT be paid within 30 days without intervention.

A tiny logistic regression (pure Python, no numpy) trained on a debtor-level split. Labels come
from the do-nothing baseline simulation, so metrics are reported on held-out debtors only.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from .domain import Debtor, Invoice

FEATURES = ["bias", "days_overdue", "late_ratio", "avg_days_late", "partial_hist", "log_amount"]


def features(inv: Invoice, d: Debtor, day: int) -> list[float]:
    late_ratio = d.prior_late_count / d.prior_invoices if d.prior_invoices else 0.0
    return [
        1.0,
        min(inv.days_overdue(day), 90) / 90.0,
        late_ratio,
        min(d.avg_days_late, 60) / 60.0,
        min(d.prior_partial_payments, 3) / 3.0,
        (math.log10(max(inv.amount_paise, 100)) - 5.0) / 3.0,
    ]


def is_holdout(debtor_id: str, frac: float = 0.4) -> bool:
    h = int(hashlib.sha256(debtor_id.encode()).hexdigest(), 16) % 1000
    return h < frac * 1000


@dataclass
class RiskModel:
    weights: list[float] = field(default_factory=lambda: [0.0] * len(FEATURES))
    threshold: float = 0.5

    def predict(self, inv: Invoice, d: Debtor, day: int) -> float:
        z = sum(w * x for w, x in zip(self.weights, features(inv, d, day)))
        return 1.0 / (1.0 + math.exp(-z))

    def fit(self, rows: list[tuple[list[float], int]], epochs: int = 400, lr: float = 0.5, l2: float = 0.01) -> None:
        w = [0.0] * len(FEATURES)
        n = len(rows)
        for _ in range(epochs):
            grad = [0.0] * len(FEATURES)
            for x, y in rows:
                p = 1.0 / (1.0 + math.exp(-sum(wi * xi for wi, xi in zip(w, x))))
                err = p - y
                for i, xi in enumerate(x):
                    grad[i] += err * xi
            for i in range(len(w)):
                w[i] -= lr * (grad[i] / n + l2 * w[i] * (i > 0))
        self.weights = w

    @staticmethod
    def metrics(preds: list[tuple[float, int]], threshold: float) -> dict:
        tp = fp = fn = tn = 0
        for p, y in preds:
            yhat = 1 if p >= threshold else 0
            if yhat and y:
                tp += 1
            elif yhat and not y:
                fp += 1
            elif not yhat and y:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {
            "n": len(preds),
            "positives": tp + fn,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "threshold": threshold,
        }
