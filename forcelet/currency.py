"""Multi-currency with dated exchange rates.

Salesforce convention: every org has one corporate currency. Other active
currencies convert through dated exchange rates — each rate says how many
units of the currency equal one unit of corporate currency, effective from
``start_date``. The rate in force for a date is the latest rate whose
``start_date`` is on or before that date (static fallback: the latest rate
on record).

Currency amount fields store values in the record's own currency; use
:func:`convert` / :func:`convert_to_corporate` (and the
``CurrencyIsoCode`` convention below) to normalize for rollups, forecasts
and reporting.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

CURRENCY_TABLE = "mf_currencies"
RATE_TABLE = "mf_exchange_rates"
RATE_MIGRATION_ID = "exchange_rates_canonical_v1"

DEFAULT_CURRENCIES = [
    {"code": "USD", "name": "US Dollar", "is_corporate": True, "is_active": True, "decimal_places": 2},
    {"code": "EUR", "name": "Euro", "is_corporate": False, "is_active": True, "decimal_places": 2},
    {"code": "GBP", "name": "British Pound", "is_corporate": False, "is_active": True, "decimal_places": 2},
    {"code": "INR", "name": "Indian Rupee", "is_corporate": False, "is_active": True, "decimal_places": 2},
    {"code": "JPY", "name": "Japanese Yen", "is_corporate": False, "is_active": True, "decimal_places": 0},
]


def _norm_code(code: str) -> str:
    return (code or "").strip().upper()


def ensure_defaults(store) -> None:
    """Seed the default currency list once (idempotent)."""
    if store.config_all(CURRENCY_TABLE):
        return
    for cur in DEFAULT_CURRENCIES:
        store.config_put(CURRENCY_TABLE, {**cur, "id": cur["code"]})


def list_currencies(store) -> list:
    ensure_defaults(store)
    return store.config_all(CURRENCY_TABLE)


def get_currency(store, code: str):
    code = _norm_code(code)
    for cur in store.config_all(CURRENCY_TABLE):
        if _norm_code(cur.get("code")) == code:
            return cur
    return None


def corporate_currency(store) -> dict:
    for cur in list_currencies(store):
        if cur.get("is_corporate"):
            return cur
    # Should not happen (defaults seed USD), but degrade gracefully.
    curs = list_currencies(store)
    return curs[0] if curs else {"code": "USD", "name": "US Dollar"}


def validate_currency(defn: dict) -> str | None:
    code = _norm_code(defn.get("code"))
    if len(code) != 3 or not code.isalpha():
        return "code must be a 3-letter ISO currency code"
    if not (defn.get("name") or "").strip():
        return "name is required"
    return None


def upsert_currency(store, defn: dict) -> dict:
    err = validate_currency(defn)
    if err:
        raise ValueError(err)
    code = _norm_code(defn["code"])
    existing = get_currency(store, code) or {}
    merged = {**existing, **defn, "code": code, "id": code,
              "is_corporate": bool(existing.get("is_corporate", False))}
    store.config_put(CURRENCY_TABLE, merged)
    return merged


def set_corporate_currency(store, code: str) -> dict:
    code = _norm_code(code)
    target = get_currency(store, code)
    if not target:
        raise ValueError(f"Unknown currency {code}")
    for cur in store.config_all(CURRENCY_TABLE):
        cur["is_corporate"] = _norm_code(cur.get("code")) == code
        store.config_put(CURRENCY_TABLE, cur)
    return get_currency(store, code)


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD")


def set_rate(store, currency_code: str, start_date, rate: float) -> dict:
    """Set the dated rate: units of currency per 1 corporate unit."""
    code = _norm_code(currency_code)
    cur = get_currency(store, code)
    if not cur:
        raise ValueError(f"Unknown currency {code}")
    if cur.get("is_corporate"):
        raise ValueError("The corporate currency rate is always 1.0")
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        raise ValueError("rate must be a number")
    if rate <= 0:
        raise ValueError("rate must be positive")
    day = _parse_date(start_date).isoformat()
    # One rate per currency per start date: replace any existing entry.
    for row in store.config_all(RATE_TABLE):
        if _norm_code(row.get("currency_code")) == code and row.get("start_date") == day:
            store.config_delete(RATE_TABLE, row["id"])
    rid = store.config_put(RATE_TABLE, {
        "currency_code": code, "start_date": day, "rate": rate})
    return store.config_get(RATE_TABLE, rid)


def delete_rate(store, rid: str) -> bool:
    return store.config_delete(RATE_TABLE, rid)


def migrate_legacy_rates(store) -> int:
    """One-time migration: convert old-shape rate rows to the canonical shape.

    Old shape (previously written by the generic admin API / Setup UI):
        {from_currency, to_currency, effective_date, rate}
    Canonical shape (written by set_rate, read by get_rate/list_rates):
        {currency_code, start_date, rate}

    Rows whose to_currency is a non-corporate currency cannot be represented
    canonically and are left untouched. Duplicate (currency, start_date)
    pairs are collapsed, keeping the first row. Idempotent via a migration
    flag in mf_schema_migrations.
    """
    if store.config_get("mf_schema_migrations", RATE_MIGRATION_ID):
        return 0
    corp = _norm_code(corporate_currency(store).get("code"))
    migrated = 0
    seen = set()
    for row in store.config_all(RATE_TABLE):
        if row.get("currency_code"):
            if row.get("start_date"):
                seen.add((_norm_code(row.get("currency_code")),
                          row.get("start_date")))
            continue
        if not row.get("from_currency"):
            continue
        from_c = _norm_code(row.get("from_currency"))
        to_c = _norm_code(row.get("to_currency"))
        if to_c and to_c != corp:
            continue  # cross rate between non-corporate currencies: leave as-is
        day = str(row.get("effective_date") or "")[:10]
        key = (from_c, day)
        if key in seen:
            store.config_delete(RATE_TABLE, row["id"])  # duplicate: keep first
            continue
        seen.add(key)
        new_row = {k: v for k, v in row.items()
                   if k not in ("from_currency", "to_currency", "effective_date")}
        new_row.update({"currency_code": from_c, "start_date": day})
        store.config_put(RATE_TABLE, new_row)
        migrated += 1
    store.config_put("mf_schema_migrations",
                     {"id": RATE_MIGRATION_ID, "migrated": migrated,
                      "at": datetime.now(timezone.utc).isoformat()})
    return migrated


def list_rates(store, currency_code: str | None = None) -> list:
    migrate_legacy_rates(store)
    rows = store.config_all(RATE_TABLE)
    if currency_code:
        code = _norm_code(currency_code)
        rows = [r for r in rows if _norm_code(r.get("currency_code")) == code]
    return sorted(rows, key=lambda r: (r.get("currency_code"), r.get("start_date")))


def get_rate(store, currency_code: str, on_date=None) -> float:
    """Rate in force for a currency on a date (corporate => 1.0)."""
    migrate_legacy_rates(store)
    code = _norm_code(currency_code)
    corp = corporate_currency(store)
    if code == _norm_code(corp.get("code")):
        return 1.0
    day = _parse_date(on_date).isoformat() if on_date else date.today().isoformat()
    best = None
    for row in store.config_all(RATE_TABLE):
        if _norm_code(row.get("currency_code")) != code:
            continue
        if row.get("start_date") <= day and (best is None or row["start_date"] > best["start_date"]):
            best = row
    if best is None:
        raise ValueError(f"No exchange rate for {code} on or before {day}")
    return float(best["rate"])


def convert(store, amount: float, from_code: str, to_code: str, on_date=None) -> float:
    """Convert an amount between currencies using dated rates."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        raise ValueError("amount must be a number")
    from_code, to_code = _norm_code(from_code), _norm_code(to_code)
    if from_code == to_code:
        return amount
    in_corp = amount / get_rate(store, from_code, on_date)
    return in_corp * get_rate(store, to_code, on_date)


def convert_to_corporate(store, amount: float, from_code: str, on_date=None) -> float:
    corp = corporate_currency(store)
    return convert(store, amount, from_code, corp.get("code"), on_date)


def record_currency_code(store, record: dict | None) -> str:
    """Currency a record's amounts are stored in (CurrencyIsoCode convention)."""
    if record and record.get("CurrencyIsoCode"):
        return _norm_code(record["CurrencyIsoCode"])
    return _norm_code(corporate_currency(store).get("code"))
