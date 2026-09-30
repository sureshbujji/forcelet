"""Duplicate rules engine — Forcelet platform module.

MatchingRule records declare *which fields identify a duplicate* for an
object (exact or fuzzy comparison). DuplicateRule records bind a matching
rule to an enforcement *action* (Block / Warn) for create and/or update
events.

This module is intentionally decoupled from the HTTP layer: the record
create/update handlers call :func:`evaluate_duplicate_rules` (see the
"WIRE-UP" note below). All functions take the ``store`` explicitly, the
same convention as ``forcelet/automation.py``.

WIRE-UP (for the main agent — this file must NOT import or touch
``forcelet/api/records.py``):

    In ``forcelet/api/records.py`` ``create_record()``, after validation
    and before the insert, add::

        from forcelet.duplicate_rules import evaluate_duplicate_rules
        action, message = evaluate_duplicate_rules(
            store, obj_name, "create", request.json or {})
        if action == "block":
            return jsonify({"error": message}), 409
        # if action == "warn": stash the warning to attach to the response

    In ``update_record()``, same shape with event ``"update"`` and
    ``exclude_id=rid``::

        action, message = evaluate_duplicate_rules(
            store, obj_name, "update", request.json or {}, exclude_id=rid)
        if action == "block":
            return jsonify({"error": message}), 409

    For a warning (non-blocking), return 200/201 with
    ``{"warning": message, ...}`` merged into the normal payload.

The strictest matching active rule wins: ``block`` > ``warn`` > no action.
"""
from __future__ import annotations

import json
import re

from .expressions import eval_expr, record_context

BLOCK = "block"
WARN = "warn"

_SEVERITY = {BLOCK: 2, WARN: 1}


def _norm(value) -> str:
    """Normalize for fuzzy comparison: lowercase, alphanumeric only."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _rule_fields(rule: dict) -> list:
    raw = rule.get("Fields") or ""
    return [f.strip() for f in str(raw).split(",") if f.strip()]


def active_matching_rules(store, object_name: str) -> list:
    """All active MatchingRule records for ``object_name``."""
    return [r for r in store.query("MatchingRule", owner_ids=None, limit=10000)
            if r.get("IsActive", True)
            and (r.get("ObjectName") or "") == object_name]


def _fields_match(rule: dict, field_values: dict, record: dict):
    """Return the matched {field: value} map, or None on no match.

    Only fields that carry a non-empty incoming value participate; a rule
    with no usable incoming values never matches (avoids flagging every
    record when the caller submits nothing relevant).
    """
    fields = [f for f in _rule_fields(rule)
              if field_values.get(f) not in (None, "")]
    if not fields:
        return None
    fuzzy = (rule.get("MatchType") or "Exact") == "Fuzzy"
    matched = {}
    for f in fields:
        want = field_values[f]
        got = record.get(f)
        if got in (None, ""):
            return None
        if fuzzy:
            nw, ng = _norm(want), _norm(got)
            if not nw or not ng:
                return None
            if not (nw == ng
                    or (len(nw) >= 4 and nw in ng)
                    or (len(ng) >= 4 and ng in nw)):
                return None
        elif str(got) != str(want):
            return None
        matched[f] = want
    return matched


def find_duplicates(store, object_name: str, field_values: dict,
                    exclude_id: str | None = None, rules: list | None = None) -> list:
    """Return matching record dicts for ``object_name``.

    Each entry: ``{"rule", "rule_id", "record_id", "matched"}``. Exact
    rules compare with ``str()`` equality; fuzzy rules use the normalized
    comparison in :func:`_fields_match`.
    """
    if rules is None:
        rules = active_matching_rules(store, object_name)
    dups = []
    for rule in rules:
        for rec in store.query(object_name, owner_ids=None, limit=10000):
            if exclude_id and rec["id"] == exclude_id:
                continue
            matched = _fields_match(rule, field_values, rec)
            if matched is not None:
                dups.append({"rule": rule.get("Name"),
                             "rule_id": rule["id"],
                             "record_id": rec["id"],
                             "matched": matched})
    return dups


def _criteria_matches(criteria, field_values: dict) -> bool:
    """True when the rule's Criteria expression matches the record values.

    Blank criteria means "always". An invalid expression never matches, so a
    misconfigured rule fails closed instead of blocking saves unexpectedly.
    """
    if not criteria:
        return True
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except (ValueError, TypeError):
            return False
    try:
        return bool(eval_expr(criteria, record_context(field_values or {})))
    except Exception:
        return False


def evaluate_duplicate_rules(store, object_name: str, event: str,
                             field_values: dict,
                             exclude_id: str | None = None):
    """Return ``(action, message)`` for the strictest matching active rule.

    ``event`` is ``"create"`` or ``"update"``. Returns ``(None, None)``
    when no active DuplicateRule matches.
    """
    event = (event or "").lower()
    candidates = []
    for drule in store.query("DuplicateRule", owner_ids=None, limit=10000):
        if not drule.get("IsActive", True):
            continue
        if (drule.get("ObjectName") or "") != object_name:
            continue
        applies = (drule.get("AppliesOn") or "Both").lower()
        if applies not in ("both", event):
            continue
        mrule = store.get("MatchingRule", drule.get("MatchingRuleId") or "")
        if not mrule or not mrule.get("IsActive", True):
            continue
        if not _criteria_matches(drule.get("Criteria"), field_values):
            continue
        if find_duplicates(store, object_name, field_values,
                            exclude_id=exclude_id, rules=[mrule]):
            action = (drule.get("Action") or "Warn").lower()
            message = drule.get("Message") or \
                f"Possible duplicate {object_name} found"
            candidates.append((action, message))
    if not candidates:
        return None, None
    candidates.sort(key=lambda c: _SEVERITY.get(c[0], 0), reverse=True)
    return candidates[0]
