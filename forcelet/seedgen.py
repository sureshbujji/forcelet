"""Sandbox seeding: generate realistic test records from templates.

A seed template names an object, a record count, and per-field generation
rules. Supported rule modes:

- ``fixed`` — use ``value`` verbatim
- ``picklist_random`` — random choice from the field's picklist values
- ``pattern`` — expand ``value`` as a mini-template with tokens:
  ``{seq}`` (1-based row number), ``{int:lo-hi}``, ``{first}``,
  ``{last}``, ``{word}``, ``{pick:a|b|c}``, ``{bool}``, ``{email}``
- ``seq`` — ``value`` as a prefix + the 1-based row number
  (e.g. ``ACME-`` -> ``ACME-1``)

Any other field is left blank and goes through normal record validation.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import random
import re

TABLE = "mf_seed_templates"
MAX_COUNT = 1000

_FIRST = ["Ava", "Liam", "Maya", "Noah", "Priya", "Arjun", "Sofia", "Ethan",
          "Zara", "Rohan", "Ivy", "Kiran", "Nora", "Dev", "Lena", "Omar"]
_LAST = ["Sharma", "Patel", "Garcia", "Kim", "Nguyen", "Singh", "Lopez",
         "Chen", "Khan", "Murphy", "Das", "Rossi", "Ali", "Brooks", "Iyer"]
_WORDS = ["apex", "beacon", "cipher", "drift", "ember", "flux", "grove",
          "harbor", "ion", "jolt", "kestrel", "lumen", "meridian", "north",
          "onyx", "prism", "quill", "ridge", "summit", "tide"]

_TOKEN_RE = re.compile(r"\{([a-z_]+)(?::([^}]*))?\}")

MODES = ("fixed", "picklist_random", "pattern", "seq")


def _expand_token(name: str, arg: str | None, seq: int, rng: random.Random) -> str:
    if name == "seq":
        return str(seq)
    if name == "int":
        try:
            lo_s, hi_s = (arg or "1-100").split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        except (ValueError, AttributeError):
            lo, hi = 1, 100
        if hi < lo:
            lo, hi = hi, lo
        return str(rng.randint(lo, hi))
    if name == "first":
        return rng.choice(_FIRST)
    if name == "last":
        return rng.choice(_LAST)
    if name == "word":
        return rng.choice(_WORDS)
    if name == "pick":
        opts = (arg or "").split("|") if arg else []
        opts = [o for o in opts if o]
        return rng.choice(opts) if opts else ""
    if name == "bool":
        return rng.choice(["true", "false"])
    if name == "email":
        return (f"{rng.choice(_FIRST)}.{rng.choice(_LAST)}{seq}"
                f"@example.com").lower()
    return "{" + name + (f":{arg}" if arg else "") + "}"


def expand_pattern(pattern: str, seq: int, rng: random.Random) -> str:
    def _sub(m: re.Match) -> str:
        return _expand_token(m.group(1), m.group(2), seq, rng)
    return _TOKEN_RE.sub(_sub, pattern or "")


def generate_value(rule: dict, field: dict | None, seq: int,
                   rng: random.Random) -> object:
    mode = rule.get("mode") or "fixed"
    value = rule.get("value")
    if mode == "fixed":
        return value
    if mode == "seq":
        return f"{value or ''}{seq}"
    if mode == "picklist_random":
        opts = (field or {}).get("picklist_values") or []
        if value:  # explicit option list overrides the field picklist
            opts = [o.strip() for o in str(value).split(",") if o.strip()]
        return rng.choice(opts) if opts else ""
    if mode == "pattern":
        return expand_pattern(str(rule.get("pattern") or value or ""), seq, rng)
    return value


def generate_row(rules: list[dict], field_map: dict, seq: int,
                 rng: random.Random) -> dict:
    row = {}
    for rule in rules or []:
        fname = rule.get("field")
        if not fname or fname not in field_map:
            continue
        row[fname] = generate_value(rule, field_map.get(fname), seq, rng)
    return row


def validate_template(tpl: dict, registry) -> list[str]:
    """Return a list of error strings; empty means valid."""
    errors = []
    obj = registry.get_object((tpl.get("object") or ""))
    if not obj:
        errors.append(f"unknown object '{tpl.get('object')}'")
        return errors
    try:
        count = int(tpl.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count < 1:
        errors.append("count must be at least 1")
    elif count > MAX_COUNT:
        errors.append(f"count may not exceed {MAX_COUNT}")
    fmap = {f["name"]: f for f in obj.get("fields", [])}
    for i, rule in enumerate(tpl.get("field_rules") or []):
        if not isinstance(rule, dict):
            errors.append(f"field_rules[{i}] must be an object")
            continue
        if rule.get("field") not in fmap:
            errors.append(f"unknown field '{rule.get('field')}'")
        if rule.get("mode") not in MODES:
            errors.append(f"field_rules[{i}].mode must be one of {MODES}")
    return errors
