"""Forecasting depth — quota attainment and forecast rollups.

Adds quota tracking (``ForecastQuota`` records) and a rollup over open
opportunities grouped by forecast category.

``Opportunity.ForecastCategory`` is defined in
``metadata/fragments/platform_core_objects.json`` and may not exist on
older databases yet, so every read here treats it as optional: a missing
or blank value falls back to ``"Pipeline"``.
"""
from __future__ import annotations

from datetime import datetime

CLOSED_STAGES = {"Closed Won", "Closed Lost"}
CATEGORIES = ("Commit", "Best Case", "Pipeline", "Omitted", "Closed")


def current_period() -> str:
    """Default forecast period label, e.g. ``2026-Q4``."""
    now = datetime.now()
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def _category(record: dict) -> str:
    cat = (record.get("ForecastCategory") or "").strip()
    return cat if cat in CATEGORIES else "Pipeline"


def forecast_summary(store, owner_id: str, period: str | None = None) -> dict:
    """Aggregate open opportunities for ``owner_id`` in ``period``.

    Returns ``{"owner_id", "period", "quota", "by_category", "commit",
    "best_case", "pipeline", "open_count", "attainment_pct"}``.
    ``attainment_pct`` is commit/quota*100, or None when no quota exists.
    """
    period = period or current_period()
    opps = [r for r in store.query("Opportunity", owner_ids=None, limit=10000)
            if r.get("owner_id") == owner_id
            and (r.get("Stage") or "") not in CLOSED_STAGES]
    by_category: dict[str, float] = {}
    for opp in opps:
        cat = _category(opp)
        by_category[cat] = by_category.get(cat, 0.0) + float(opp.get("Amount") or 0)
    quota = 0.0
    for q in store.query("ForecastQuota", owner_ids=None, limit=10000):
        if q.get("OwnerId") == owner_id and (q.get("Period") or "") == period \
                and (q.get("ObjectName") or "Opportunity") == "Opportunity":
            quota += float(q.get("QuotaAmount") or 0)
    commit = by_category.get("Commit", 0.0)
    attainment = round(commit / quota * 100, 2) if quota else None
    return {
        "owner_id": owner_id,
        "period": period,
        "quota": quota,
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "commit": round(commit, 2),
        "best_case": round(by_category.get("Best Case", 0.0), 2),
        "pipeline": round(by_category.get("Pipeline", 0.0), 2),
        "open_count": len(opps),
        "attainment_pct": attainment,
    }
