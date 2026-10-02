"""Field types and value validation.

Every field on every object (standard or custom) has a type from FIELD_TYPES.
validate_value() coerces and checks a raw input against a field definition
and returns (ok, normalized_value, error_message).
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from html.parser import HTMLParser

FIELD_TYPES = {
    "Text":          {"sql": "TEXT",    "desc": "Short text (configurable max length)"},
    "TextArea":      {"sql": "TEXT",    "desc": "Long text"},
    "RichTextArea":  {"sql": "TEXT",    "desc": "Rich text (HTML), sanitized on save"},
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
    "AutoNumber":    {"sql": "TEXT",    "desc": "Auto-generated sequence (prefix + zero-padded number); read-only"},
    "EncryptedText": {"sql": "TEXT",    "desc": "Text encrypted at rest; masked display (configurable visible chars)"},
    "Formula":       {"sql": "TEXT",    "desc": "Computed formula field (choose a return type)"},
    "Geolocation":   {"sql": "TEXT",    "desc": "Latitude/longitude pair (stored 'lat;lng')"},
    "Address":       {"sql": "TEXT",    "desc": "Compound street/city/state/postal/country (stored as JSON)"},
    "Time":          {"sql": "TEXT",    "desc": "Time of day (HH:MM:SS)"},
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# ------------------------------------------------------- rich-text sanitizer
# Allowlist-based HTML sanitizer for RichTextArea values. Runs server-side on
# every save, so stored HTML is safe to inject unescaped into the detail view.
RICH_TEXT_TAGS = {
    "p": (), "br": (), "b": (), "i": (), "u": (),
    "strong": (), "em": (), "ul": (), "ol": (), "li": (),
    "a": ("href",), "h1": (), "h2": (), "h3": (), "h4": (),
    "blockquote": (), "code": (), "pre": (), "span": (), "div": (),
}
# Tags whose content is dropped entirely (not just the tag itself).
_RICH_TEXT_DROP = {"script", "style", "iframe", "object", "embed",
                   "applet", "form", "input", "button", "select",
                   "textarea", "link", "meta", "base"}
_VOID_TAGS = {"br"}
_HREF_RE = re.compile(r"^(https?://|mailto:)", re.IGNORECASE)


class _HTMLSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._out = []
        self._drop_depth = 0
        # stack of [tag, out_index, has_content] for open non-void allowed tags,
        # so elements that end up empty can have their start tag removed.
        self._open_tags = []

    def _mark_content(self):
        for entry in self._open_tags:
            entry[2] = True

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _RICH_TEXT_DROP:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        allowed = RICH_TEXT_TAGS.get(tag)
        if allowed is None:
            return  # strip the tag, keep its content
        kept = []
        for name, value in attrs:
            name = name.lower()
            if name.startswith("on") or name in ("style",):
                continue  # no event handlers, no inline styles
            if name not in allowed:
                continue
            value = value or ""
            if tag == "a" and name == "href":
                value = value.strip()
                if not _HREF_RE.match(value):
                    continue  # no javascript:/data: URLs
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        if tag in _VOID_TAGS:
            self._out.append(f"<{tag}>")
            self._mark_content()  # e.g. <img>/<br> count as content
        else:
            self._open_tags.append([tag, len(self._out), False])
            self._out.append(f"<{tag}{''.join(kept)}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _RICH_TEXT_DROP:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag in RICH_TEXT_TAGS and tag not in _VOID_TAGS:
            for i in range(len(self._open_tags) - 1, -1, -1):
                if self._open_tags[i][0] == tag:
                    _, idx, has_content = self._open_tags.pop(i)
                    if has_content:
                        self._out.append(f"</{tag}>")
                    else:
                        del self._out[idx]  # collapse empty element
                    break
            else:
                self._out.append(f"</{tag}>")

    def close(self):
        super().close()
        # drop unclosed tags that never received content (e.g. input "<p>")
        for _tag, idx, has_content in reversed(self._open_tags):
            if not has_content:
                del self._out[idx]
        self._open_tags.clear()

    def handle_data(self, data):
        if self._drop_depth:
            return
        if data:
            self._mark_content()
        self._out.append(html.escape(data))

    def handle_entityref(self, name):
        if not self._drop_depth:
            self._mark_content()
            self._out.append(f"&{name};")

    def handle_charref(self, name):
        if not self._drop_depth:
            self._mark_content()
            self._out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self._out)


def sanitize_html(value) -> str:
    """Strip disallowed tags/attributes from rich-text HTML (allowlist-based)."""
    if not value:
        return ""
    parser = _HTMLSanitizer()
    try:
        parser.feed(str(value))
        parser.close()
    except Exception:
        # On malformed input, fall back to plain-text escaping rather than
        # risking unsanitized output.
        return html.escape(str(value))
    return parser.result()


def mask_secret(value, show_last: int = 4) -> str:
    """Mask a secret for display, e.g. '••••••1234' (SFDC-style masked field)."""
    s = "" if value is None else str(value)
    n = max(int(show_last or 0), 0)
    if not s:
        return ""
    if n == 0:
        return s  # masking disabled: show plaintext
    if len(s) <= n:
        return "•" * len(s)
    return "•" * (len(s) - n) + s[-n:]


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
        if field.get("dynamic_picklist"):
            return True, str(value), None  # validated per-context by app logic
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

    if ftype == "AutoNumber":
        # System-assigned on create; user input is always rejected (the
        # validate_record computed-field check fires first with a clearer
        # message — this is the backstop for direct validate_value callers).
        return False, None, f"{label} is auto-generated and cannot be set"

    if ftype == "Formula":
        return False, None, f"{label} is a computed formula field and cannot be set"

    if ftype == "EncryptedText":
        v = str(value)
        max_len = field.get("length")
        if max_len and len(v) > max_len:
            return False, None, f"{label} exceeds max length of {max_len}"
        # Encryption at rest is applied by the metadata layer (the field is
        # normalized with encrypted=True); this returns the plaintext for it.
        return True, v, None

    if ftype == "RichTextArea":
        v = sanitize_html(value)
        max_len = field.get("length")
        if max_len and len(v) > max_len:
            return False, None, f"{label} exceeds max length of {max_len}"
        return True, v, None

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
