"""Forecasting depth — quota attainment and forecast rollups.

Adds quota tracking (``ForecastQuota`` records) and a rollup over open
opportunities grouped by forecast category, plus manager forecast
adjustments (stored in the ``mf_forecast_adjustments`` config table).

``Opportunity.ForecastCategory`` is defined in
``metadata/fragments/platform_core_objects.json`` and may not exist on
older databases yet, so every read here treats it as optional: a missing
or blank value falls back to ``"Pipeline"``.

Period format on this path is quarterly: ``YYYY-QN`` (e.g. ``2026-Q4``).
``forecast_summary`` only counts open opportunities whose ``CloseDate``
falls inside the requested quarter. Opportunities with no (or an
unparseable) ``CloseDate`` cannot be attributed to a period, so they are
counted in every period — this keeps the pre-period-filter behavior for
dateless records. A monthly ``YYYY-MM`` period is also accepted for
convenience. When ``period`` is absent or unrecognized, no date filter is
applied (legacy behavior).
"""
from __future__ import annotations

import re
from datetime import date, datetime

CLOSED_STAGES = {"Closed Won", "Closed Lost"}
CATEGORIES = ("Commit", "Best Case", "Pipeline", "Omitted", "Closed")

_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def current_period() -> str:
    """Default forecast period label, e.g. ``2026-Q4``."""
    now = datetime.now()
    return f"{now.year}-Q{(now.month - 1) // 3 + 1}"


def _period_bounds(period: str | None) -> tuple[date | None, date | None]:
    """(start, end) dates a CloseDate must fall in, or (None, None).

    ``YYYY-QN`` selects a calendar quarter; ``YYYY-MM`` selects a month.
    Anything else (including None) means "no date filter".
    """
    m = _QUARTER_RE.match(period or "")
    if m:
        year, quarter = int(m.group(1)), int(m.group(2))
        start_month = (quarter - 1) * 3 + 1
        start = date(year, start_month, 1)
        end = date(year + 1, 1, 1) if start_month == 10 \
            else date(year, start_month + 3, 1)
        return start, end
    m = _MONTH_RE.match(period or "")
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 \
                else date(year, month + 1, 1)
            return start, end
    return None, None


def _close_date(record: dict) -> date | None:
    try:
        return date.fromisoformat(str(record.get("CloseDate") or "")[:10])
    except (ValueError, TypeError):
        return None


def _category(record: dict) -> str:
    cat = (record.get("ForecastCategory") or "").strip()
    return cat if cat in CATEGORIES else "Pipeline"


# ------------------------------------------------- manager adjustments
def get_forecast_adjustment(store, owner_id: str, period: str) -> dict | None:
    """The stored manager adjustment for (owner, period), if any."""
    try:
        rows = store.config_all("mf_forecast_adjustments")
    except Exception:
        return None
    for row in rows:
        if row.get("owner_id") == owner_id and row.get("period") == period:
            return row
    return None


def set_forecast_adjustment(store, owner_id: str, period: str,
                            adjusted_amount: float, adjusted_by: str,
                            note: str | None = None) -> dict:
    """Create or replace the manager adjustment for (owner, period)."""
    existing = get_forecast_adjustment(store, owner_id, period)
    row = {
        "id": (existing or {}).get("id"),
        "owner_id": owner_id,
        "period": period,
        "adjusted_amount": round(float(adjusted_amount), 2),
        "adjusted_by": adjusted_by,
        "note": note or "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    rid = store.config_put("mf_forecast_adjustments", row)
    return store.config_get("mf_forecast_adjustments", rid)


def forecast_summary(store, owner_id: str, period: str | None = None) -> dict:
    """Aggregate open opportunities for ``owner_id`` in ``period``.

    ``period`` is a quarterly label ``YYYY-QN`` (``YYYY-MM`` is also
    accepted); only opportunities whose ``CloseDate`` falls inside the
    period are counted, plus dateless opportunities which cannot be
    attributed to a period. When ``period`` is absent or unrecognized, no
    date filter is applied (legacy behavior); the returned ``period`` label
    still defaults to the current quarter.

    Returns ``{"owner_id", "period", "quota", "by_category", "commit",
    "best_case", "pipeline", "open_count", "attainment_pct",
    "adjusted_amount", "adjustment"}``. ``attainment_pct`` is
    commit/quota*100 (a percent, 0-100), or None when no quota exists.
    ``adjusted_amount``/``adjustment`` carry the manager's forecast
    adjustment for this owner+period when one was set, otherwise None —
    the remaining fields are the unadjusted (original) numbers.
    """
    label = period or current_period()
    start, end = _period_bounds(period) if period else (None, None)
    opps = []
    for r in store.query("Opportunity", owner_ids=None, limit=10000):
        if r.get("owner_id") != owner_id:
            continue
        if (r.get("Stage") or "") in CLOSED_STAGES:
            continue
        if start and end:
            cd = _close_date(r)
            if cd is not None and not (start <= cd < end):
                continue
        opps.append(r)
    by_category: dict[str, float] = {}
    for opp in opps:
        cat = _category(opp)
        by_category[cat] = by_category.get(cat, 0.0) + float(opp.get("Amount") or 0)
    quota = 0.0
    for q in store.query("ForecastQuota", owner_ids=None, limit=10000):
        if q.get("OwnerId") == owner_id and (q.get("Period") or "") == label \
                and (q.get("ObjectName") or "Opportunity") == "Opportunity":
            quota += float(q.get("QuotaAmount") or 0)
    commit = by_category.get("Commit", 0.0)
    attainment = round(commit / quota * 100, 2) if quota else None
    adjustment = get_forecast_adjustment(store, owner_id, label)
    return {
        "owner_id": owner_id,
        "period": label,
        "quota": quota,
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items())},
        "commit": round(commit, 2),
        "best_case": round(by_category.get("Best Case", 0.0), 2),
        "pipeline": round(by_category.get("Pipeline", 0.0), 2),
        "open_count": len(opps),
        "attainment_pct": attainment,
        "adjusted_amount": (round(float(adjustment["adjusted_amount"]), 2)
                            if adjustment else None),
        "adjustment": adjustment,
    }
