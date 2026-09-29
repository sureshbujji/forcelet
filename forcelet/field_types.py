"""Field types and value validation.

Every field on every object (standard or custom) has a type from FIELD_TYPES.
validate_value() coerces and checks a raw input against a field definition
and returns (ok, normalized_value, error_message).
"""
from __future__ import annotations

import re
from datetime import datetime

FIELD_TYPES = {
    "Text":          {"sql": "TEXT",    "desc": "Short text (configurable max length)"},
    "TextArea":      {"sql": "TEXT",    "desc": "Long text"},
    "Number":        {"sql": "REAL",    "desc": "Numeric value"},
    "Currency":      {"sql": "REAL",    "desc": "Currency amount"},
    "Percent":       {"sql": "REAL",    "desc": "Percentage"},
    "Date":          {"sql": "TEXT",    "desc": "Calendar date (YYYY-MM-DD)"},
    "DateTime":      {"sql": "TEXT",    "desc": "Timestamp (ISO-8601)"},
    "Checkbox":      {"sql": "INTEGER", "desc": "Boolean flag"},
    "Picklist":      {"sql": "TEXT",    "desc": "Single-select from a value set"},
    "MultiPicklist": {"sql": "TEXT",    "desc": "Multi-select, stored ;-separated"},
    "Email":         {"sql": "TEXT",    "desc": "Email address (format-checked)"},
    "Phone":         {"sql": "TEXT",    "desc": "Phone number"},
    "URL":           {"sql": "TEXT",    "desc": "Web link"},
    "Lookup":        {"sql": "TEXT",    "desc": "Relationship to another object's record"},
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def is_valid_api_name(name: str) -> bool:
    """API names for objects/fields: letters, digits, underscores; start with a letter."""
    return bool(name) and bool(NAME_RE.match(name))


def validate_value(field: dict, value):
    """Validate + normalize a value for a field definition.

    Returns (ok: bool, normalized, error: str | None).
    """
    ftype = field.get("type")
    label = field.get("label", field.get("name", "Field"))

    if ftype not in FIELD_TYPES:
        return False, None, f"Unknown field type '{ftype}'"

    if value is None or (isinstance(value, str) and value.strip() == ""):
        if field.get("required"):
            return False, None, f"{label} is required"
        return True, None, None

    if ftype in ("Text", "TextArea", "Phone", "URL"):
        v = str(value)
        max_len = field.get("length")
        if max_len and len(v) > max_len:
            return False, None, f"{label} exceeds max length of {max_len}"
        return True, v, None

    if ftype in ("Number", "Currency", "Percent"):
        try:
            return True, float(value), None
        except (TypeError, ValueError):
            return False, None, f"{label} must be a number"

    if ftype == "Date":
        try:
            datetime.strptime(str(value)[:10], "%Y-%m-%d")
            return True, str(value)[:10], None
        except ValueError:
            return False, None, f"{label} must be a date like 2026-09-29"

    if ftype == "DateTime":
        return True, str(value), None

    if ftype == "Checkbox":
        truthy = value is True or value in (1, "1", "true", "True", "TRUE", "yes")
        return True, 1 if truthy else 0, None

    if ftype == "Picklist":
        allowed = field.get("picklist_values") or []
        if allowed and str(value) not in allowed:
            return False, None, f"{label} must be one of {allowed}"
        return True, str(value), None

    if ftype == "MultiPicklist":
        vals = list(value) if isinstance(value, list) else [v.strip() for v in str(value).split(";")]
        vals = [v for v in vals if v]
        allowed = field.get("picklist_values") or []
        bad = [v for v in vals if v not in allowed]
        if bad:
            return False, None, f"{label} has invalid values {bad}; allowed: {allowed}"
        return True, ";".join(vals), None

    if ftype == "Email":
        if not EMAIL_RE.match(str(value)):
            return False, None, f"{label} must be a valid email address"
        return True, str(value), None

    if ftype == "Lookup":
        return True, str(value), None

    return False, None, f"Unhandled field type '{ftype}'"
