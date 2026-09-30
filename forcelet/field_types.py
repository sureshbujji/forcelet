"""Field types and value validation.

Every field on every object (standard or custom) has a type from FIELD_TYPES.
validate_value() coerces and checks a raw input against a field definition
and returns (ok, normalized_value, error_message).
"""
from __future__ import annotations

import json
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
    "MasterDetail":  {"sql": "TEXT",    "desc": "Master-detail: required parent, cascade delete, inherits sharing"},
    "Geolocation":   {"sql": "TEXT",    "desc": "Latitude/longitude pair (stored 'lat;lng')"},
    "Address":       {"sql": "TEXT",    "desc": "Compound street/city/state/postal/country (stored as JSON)"},
    "Time":          {"sql": "TEXT",    "desc": "Time of day (HH:MM:SS)"},
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
        # A dict/list submitted for a text field (e.g. a condition-builder
        # expression for a Criteria textarea) is stored as JSON, not str().
        v = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
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

    if ftype == "MasterDetail":
        v = str(value).strip()
        if not v:
            return False, None, f"{label} is required (master-detail cannot be empty)"
        return True, v, None

    if ftype == "Geolocation":
        lat = lng = None
        if isinstance(value, dict):
            lat = value.get("latitude", value.get("lat"))
            lng = value.get("longitude", value.get("lng"))
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            lat, lng = value
        elif isinstance(value, str):
            parts = re.split(r"[;,]", value.strip())
            if len(parts) == 2:
                lat, lng = parts
        try:
            lat, lng = float(lat), float(lng)
        except (TypeError, ValueError):
            return False, None, f"{label} must be 'latitude;longitude' (e.g. '37.77;-122.41')"
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return False, None, f"{label} is out of range (lat -90..90, lng -180..180)"
        return True, f"{lat};{lng}", None

    if ftype == "Address":
        if isinstance(value, str):
            # idempotent: accept an already-normalized JSON address string
            try:
                parsed = json.loads(value)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                value = parsed
        if not isinstance(value, dict):
            return False, None, f"{label} must be an object with street/city/state/postal_code/country"
        keys = ("street", "city", "state", "postal_code", "country")
        addr = {k: str(value.get(k) or "") for k in keys}
        if not any(addr.values()):
            return False, None, f"{label} needs at least one address component"
        return True, json.dumps(addr), None

    if ftype == "Time":
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", str(value).strip())
        if not m:
            return False, None, f"{label} must be a time like 14:30 or 14:30:00"
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            return False, None, f"{label} is not a valid time"
        return True, f"{hh:02d}:{mm:02d}:{ss:02d}", None

    return False, None, f"Unhandled field type '{ftype}'"
