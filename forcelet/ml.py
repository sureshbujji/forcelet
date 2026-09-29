"""Small ML helpers for Forcelet.

Lead scoring is a plain logistic regression implemented in pure Python
(no third-party dependencies), trained on historical Lead outcomes:
Converted = positive, Unqualified = negative. Deterministic: weights start
at zero and gradient descent has no randomness.
"""
from __future__ import annotations

import math

# (feature key, human label)
FEATURES = [
    ("has_email", "Has email"),
    ("has_phone", "Has phone"),
    ("rating_hot", "Rating is Hot"),
    ("rating_warm", "Rating is Warm"),
    ("rating_cold", "Rating is Cold"),
    ("src_web", "Source is Web"),
    ("src_referral", "Source is Referral"),
    ("src_partner", "Source is Partner"),
    ("src_tradeshow", "Source is Trade Show"),
    ("src_other", "Source is Other"),
]

LEAD_SOURCES = ["Web", "Referral", "Partner", "Trade Show", "Other"]


def featurize(lead: dict) -> list:
    rating = (lead.get("Rating") or "").strip()
    src = (lead.get("LeadSource") or "").strip()
    return [
        1.0 if lead.get("Email") else 0.0,
        1.0 if lead.get("Phone") else 0.0,
        1.0 if rating == "Hot" else 0.0,
        1.0 if rating == "Warm" else 0.0,
        1.0 if rating == "Cold" else 0.0,
        1.0 if src == "Web" else 0.0,
        1.0 if src == "Referral" else 0.0,
        1.0 if src == "Partner" else 0.0,
        1.0 if src == "Trade Show" else 0.0,
        1.0 if src == "Other" else 0.0,
    ]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def train_logreg(X: list, y: list, iters: int = 2000,
                 lr: float = 0.5, l2: float = 1e-3) -> list:
    """Batch gradient descent for logistic regression. Returns [bias, *weights]."""
    n = len(X)
    dim = len(X[0])
    w = [0.0] * (dim + 1)  # w[0] is the bias
    for _ in range(iters):
        grad = [0.0] * (dim + 1)
        for xi, yi in zip(X, y):
            z = w[0] + sum(wi * xij for wi, xij in zip(w[1:], xi))
            err = _sigmoid(z) - yi
            grad[0] += err
            for j, xij in enumerate(xi):
                grad[j + 1] += err * xij
        for j in range(dim + 1):
            reg = l2 * w[j] if j > 0 else 0.0
            w[j] -= lr * (grad[j] / n + reg)
    return w


def predict_proba(weights: list, x: list) -> float:
    z = weights[0] + sum(wi * xij for wi, xij in zip(weights[1:], x))
    return _sigmoid(z)


def train_lead_scoring(leads: list) -> dict:
    """Train on Converted (1) vs Unqualified (0) leads.

    Returns {"weights", "features", "samples", "positives", "accuracy"}.
    Raises ValueError when there is too little history to learn from.
    """
    rows = []
    for lead in leads:
        status = (lead.get("Status") or "").strip()
        if status == "Converted":
            rows.append((featurize(lead), 1))
        elif status == "Unqualified":
            rows.append((featurize(lead), 0))
    if len(rows) < 10:
        raise ValueError(
            f"Need at least 10 converted/unqualified leads to train "
            f"(found {len(rows)}).")
    positives = sum(y for _, y in rows)
    if positives == 0 or positives == len(rows):
        raise ValueError(
            "Need both converted and unqualified leads to train "
            f"(found {positives} converted of {len(rows)}).")
    X = [x for x, _ in rows]
    y = [label for _, label in rows]
    weights = train_logreg(X, y)
    correct = sum(1 for xi, yi in zip(X, y)
                  if (predict_proba(weights, xi) >= 0.5) == bool(yi))
    return {
        "weights": weights,
        "features": [k for k, _ in FEATURES],
        "samples": len(rows),
        "positives": positives,
        "accuracy": round(correct / len(rows), 3),
    }


def score_lead(model: dict, lead: dict) -> dict:
    """Score a lead 0-100 with human-readable top factors."""
    x = featurize(lead)
    proba = predict_proba(model["weights"], x)
    contribs = []
    labels = [label for _, label in FEATURES]
    for key, label, xi, wi in zip(model["features"], labels, x,
                                  model["weights"][1:]):
        if xi:
            contribs.append({"feature": label, "impact": round(wi, 3)})
    contribs.sort(key=lambda c: abs(c["impact"]), reverse=True)
    score = int(round(proba * 100))
    grade = "Hot" if score >= 70 else ("Warm" if score >= 40 else "Cold")
    return {"score": score, "grade": grade,
            "factors": contribs[:4], "probability": round(proba, 3)}
