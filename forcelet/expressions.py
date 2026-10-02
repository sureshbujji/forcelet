"""Safe expression evaluator shared by validation rules, flows, sharing rules,
record-type logic, and report filters.

Expression JSON supports:
  literals: 42, "text", true, null
  {"field": "Stage"}            value of a field on the record
  {"field_old": "Stage"}        value before the change (flows / rules on update)
  {"today": true} / {"now": true}
  comparisons: ==, !=, <, <=, >, >=   (date strings compare lexicographically, ISO)
  {"in": [a, [v1, v2]]}
  {"and": [...]}, {"or": [...]}, {"not": x}
  {"contains": [a, b]}, {"isblank": x}, {"len": x}
  arithmetic: {"+": [a, b]}, {"-": ...}, {"*": ...}, {"/": ...}
  {"days_between": [dateA, dateB]}
  relative-date predicates (report filters, also usable in rules/flows):
  {"is_today": [{"field": "CloseDate"}]},
  {"is_this_week"|"is_this_month"|"is_this_quarter"|"is_this_year": [{"field": "CloseDate"}]},
  {"is_last_n_days": [{"field": "CloseDate"}, 30]}

render_template() expands {{Trigger.Field}}, {{Trigger.Id}}, {{User.Id}} etc.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from datetime import datetime, timezone


def _to_date(v):
    if v is None:
        return None
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _today_date() -> date:
    return datetime.now(timezone.utc).date()


def relative_date_range(kind: str) -> tuple[date, date]:
    """(start, end) inclusive date range for a relative-date literal kind.

    Kinds: today, this_week (Mon-Sun), this_month, this_quarter, this_year.
    """
    today = _today_date()
    if kind == "today":
        return today, today
    if kind == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if kind == "this_month":
        start = today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt - timedelta(days=1)
    if kind == "this_quarter":
        qm = (today.month - 1) // 3 * 3 + 1
        start = today.replace(month=qm, day=1)
        nm, yr = qm + 3, today.year
        if nm > 12:
            nm, yr = nm - 12, yr + 1
        return start, date(yr, nm, 1) - timedelta(days=1)
    if kind == "this_year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    raise ValueError(f"Unknown relative date kind '{kind}'")


def _in_relative_range(value, kind: str, n: int | None = None) -> bool:
    d = _to_date(value)
    if d is None:
        return False
    if kind == "last_n_days":
        today = _today_date()
        return today - timedelta(days=int(n or 0)) <= d <= today
    start, end = relative_date_range(kind)
    return start <= d <= end


#: {"is_today": [...]} style predicate ops handled directly in eval_expr.
REL_DATE_PREDICATES = ("is_today", "is_this_week", "is_this_month",
                       "is_this_quarter", "is_this_year", "is_last_n_days")


# Resolvers for $CustomMetadata / $CustomSetting in formulas and flows.
# Registered by forcelet.devops.register_expression_resolvers(store); kept here
# (rather than threading a store through eval_expr) so every evaluation path
# — validation rules, flows, formula fields, report filters — picks them up.
_custom_metadata_resolver = None
_custom_setting_resolver = None


def set_custom_metadata_resolver(fn):
    global _custom_metadata_resolver
    _custom_metadata_resolver = fn


def set_custom_setting_resolver(fn):
    global _custom_setting_resolver
    _custom_setting_resolver = fn

def eval_expr(expr, record: dict, old_record: dict | None = None, user: dict | None = None,
            rel_resolver=None):
    """Evaluate a formula/validation expression against a record.

    ``rel_resolver`` is an optional callable taking a dotted path like
    ``"Account.Name"`` and returning the traversed value (or None). When
    provided, ``{"field": "A.B"}`` references resolve through relationship
    fields; without it they behave as before (plain ``record.get``).
    """
    if isinstance(expr, dict):
        if "field" in expr:
            fname = expr["field"]
            if isinstance(fname, str) and "." in fname and rel_resolver is not None:
                try:
                    return rel_resolver(fname)
                except Exception:
                    return None
            return record.get(fname)
        if "field_old" in expr:
            return (old_record or {}).get(expr["field_old"])
        if expr.get("today"):
            return _today()
        if expr.get("now"):
            return datetime.now(timezone.utc).isoformat(timespec="seconds")
        for _pred in REL_DATE_PREDICATES:
            if _pred in expr:
                _args = expr[_pred]
                _fld = eval_expr(_args[0], record, old_record, user, rel_resolver) \
                    if isinstance(_args, list) else eval_expr(_args, record, old_record, user, rel_resolver)
                if _pred == "is_last_n_days":
                    _n = eval_expr(_args[1], record, old_record, user, rel_resolver) \
                        if isinstance(_args, list) and len(_args) > 1 else 0
                    return _in_relative_range(_fld, "last_n_days", _n)
                return _in_relative_range(_fld, _pred[3:])
        if expr.get("user_id"):
            return (user or {}).get("id")
        if "custom_metadata" in expr:
            spec = expr["custom_metadata"] or {}
            if _custom_metadata_resolver is None:
                raise ValueError("Custom metadata is not available in this context")
            return _custom_metadata_resolver(spec.get("type"), spec.get("record"),
                                             spec.get("field"))
        if "custom_setting" in expr:
            spec = expr["custom_setting"] or {}
            if _custom_setting_resolver is None:
                raise ValueError("Custom settings are not available in this context")
            return _custom_setting_resolver(spec.get("name"), spec.get("field"))
        if len(expr) == 1:
            op, args = next(iter(expr.items()))
            return _apply_op(op, args, record, old_record, user, rel_resolver)
        raise ValueError(f"Invalid expression: {expr}")
    if isinstance(expr, list):
        return [eval_expr(e, record, old_record, user, rel_resolver) for e in expr]
    return expr


def _val(x, record, old_record, user, rel_resolver=None):
    return eval_expr(x, record, old_record, user, rel_resolver)


def _apply_op(op, args, record, old_record, user, rel_resolver=None):
    v = lambda x: _val(x, record, old_record, user, rel_resolver)
    if op == "==":
        return v(args[0]) == v(args[1])
    if op == "!=":
        return v(args[0]) != v(args[1])
    if op == "<":
        a, b = v(args[0]), v(args[1])
        return a is not None and b is not None and a < b
    if op == "<=":
        a, b = v(args[0]), v(args[1])
        return a is not None and b is not None and a <= b
    if op == ">":
        a, b = v(args[0]), v(args[1])
        return a is not None and b is not None and a > b
    if op == ">=":
        a, b = v(args[0]), v(args[1])
        return a is not None and b is not None and a >= b
    if op == "in":
        return v(args[0]) in (v(args[1]) or [])
    if op == "and":
        return all(v(a) for a in args)
    if op == "or":
        return any(v(a) for a in args)
    if op == "not":
        return not v(args)
    if op == "contains":
        a, b = v(args[0]), v(args[1])
        return a is not None and b is not None and str(b) in str(a)
    if op == "starts_with":
        a, b = v(args[0]), v(args[1])
        return a is not None and b is not None and str(a).startswith(str(b))
    if op == "isblank":
        a = v(args)
        return a is None or (isinstance(a, str) and a.strip() == "")
    if op == "len":
        a = v(args)
        return len(a) if a is not None else 0
    if op in ("+", "-", "*", "/"):
        a, b = v(args[0]), v(args[1])
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return None
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        return a / b if b else None
    if op == "days_between":
        a, b = _to_date(v(args[0])), _to_date(v(args[1]))
        return abs((b - a).days) if a and b else None
    if op in ("upper", "lower"):
        a = v(args[0]) if isinstance(args, list) else v(args)
        if a is None:
            return None
        return str(a).upper() if op == "upper" else str(a).lower()
    raise ValueError(f"Unknown operator '{op}'")


TEMPLATE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def render_template(template: str, record: dict, user: dict):
    """Expand {{Trigger.FieldName}}, {{Trigger.Id}}, {{User.Id}} etc."""
    def repl(m):
        path = m.group(1).split(".")
        if path[0] == "Trigger":
            val = record.get("Id") if path[1] == "Id" else record.get(path[1])
        elif path[0] == "User":
            val = user.get("id") if path[1] == "Id" else user.get(path[1].lower())
        else:
            return m.group(0)
        return "" if val is None else str(val)
    return TEMPLATE_RE.sub(repl, template)


def render_value(value, record: dict, user: dict):
    if isinstance(value, str):
        return render_template(value, record, user)
    if isinstance(value, list):
        return [render_value(x, record, user) for x in value]
    if isinstance(value, dict):
        return {k: render_value(x, record, user) for k, x in value.items()}
    return value


def record_context(rec: dict | None) -> dict:
    """Raw store rows use snake_case system columns; expressions and templates
    use Salesforce casing (Id, CreatedDate, ...). This merges both."""
    if not rec:
        return {}
    from . import crypto as _crypto
    ctx = {k: (_crypto.decrypt(v) if _crypto.is_encrypted(v) else v)
           for k, v in rec.items()}
    ctx.setdefault("Id", ctx.get("id"))
    ctx.setdefault("OwnerId", ctx.get("owner_id"))
    ctx.setdefault("CreatedDate", ctx.get("created_date"))
    ctx.setdefault("LastModifiedDate", ctx.get("last_modified_date"))
    ctx.setdefault("RecordType", ctx.get("record_type"))
    return ctx


# ------------------------------------------------- relationship traversal
#: Max relationship hops when resolving a dotted path like
#: ``Account.ParentAccount.Name``.
DOTTED_PATH_MAX_DEPTH = 5

_RELATIONSHIP_TYPES = ("Lookup", "MasterDetail", "PolymorphicLookup")


def _relationship_field_for(obj_def: dict, segment: str):
    """Find the relationship field whose Salesforce-style relationship name
    matches ``segment``: the exact API name, or the API name minus a trailing
    ``Id`` (``AccountId`` -> ``Account``). Returns None when no match."""
    for f in obj_def.get("fields", []) or []:
        if f.get("type") not in _RELATIONSHIP_TYPES:
            continue
        name = f.get("name") or ""
        if segment == name:
            return f
        if name.endswith("Id") and segment == name[:-2]:
            return f
    return None


def _rel_targets(fdef: dict) -> list:
    ref = fdef.get("reference_to")
    return [t for t in (ref if isinstance(ref, list) else [ref]) if t]


def resolve_dotted_field(store, registry, obj_name: str, record: dict,
                         path: str, max_depth: int = DOTTED_PATH_MAX_DEPTH):
    """Resolve a dotted relationship path (``Account.Name``,
    ``Account.ParentAccount.Name``) starting from ``record`` of ``obj_name``.

    Null-safe: a missing intermediate record, a null FK, or an unresolvable
    segment yields None instead of raising. Traversal is cycle-guarded with
    a visited set and capped at ``max_depth`` hops. Never raises.
    """
    try:
        segments = [s for s in str(path or "").split(".") if s]
        if len(segments) < 2 or len(segments) - 1 > max_depth:
            return None
        cur_obj, cur_rec = obj_name, record
        visited = set()
        for seg in segments[:-1]:
            obj_def = registry.get_object(cur_obj)
            if not obj_def:
                return None
            fdef = _relationship_field_for(obj_def, seg)
            if not fdef:
                return None
            fk = (cur_rec or {}).get(fdef["name"])
            if not fk:
                return None
            nxt, nxt_obj = None, None
            for target in _rel_targets(fdef):
                if not registry.get_object(target):
                    continue
                try:
                    cand = store.get(target, fk)
                except Exception:
                    cand = None
                if cand:
                    nxt, nxt_obj = cand, target
                    break
            if not nxt:
                return None
            key = (nxt_obj, nxt.get("id"))
            if key in visited:
                return None
            visited.add(key)
            cur_rec, cur_obj = nxt, nxt_obj
        return (cur_rec or {}).get(segments[-1])
    except Exception:
        return None


#: System keys available in formula contexts (see record_context).
FORMULA_SYSTEM_KEYS = frozenset({
    "Id", "OwnerId", "CreatedDate", "LastModifiedDate", "RecordType",
    "id", "owner_id", "created_by", "created_date", "last_modified_date",
    "record_type",
})


def validate_formula_refs(registry, obj_name: str, formula,
                          max_depth: int = DOTTED_PATH_MAX_DEPTH):
    """Validate every ``{"field": ...}`` reference in a formula expression.

    Plain names must be fields (or system keys) on ``obj_name``; dotted paths
    must traverse real relationship fields (depth-capped) and end on a real
    field of the final object (for polymorphic targets, resolution on any one
    target suffices). Raises ValueError on the first bad reference.
    """
    obj_def = registry.get_object(obj_name)
    if not obj_def:
        raise ValueError(f"Unknown object '{obj_name}'")

    def _resolvable(cur_def: dict, segs: list) -> bool:
        if len(segs) == 1:
            leaf = segs[0]
            names = {f.get("name") for f in cur_def.get("fields", []) or []}
            return leaf in names or leaf in FORMULA_SYSTEM_KEYS
        fdef = _relationship_field_for(cur_def, segs[0])
        if not fdef:
            return False
        for target in _rel_targets(fdef):
            tdef = registry.get_object(target)
            if tdef and _resolvable(tdef, segs[1:]):
                return True
        return False

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "field" and isinstance(v, str):
                    segs = [s for s in v.split(".") if s]
                    if not segs:
                        raise ValueError("Formula contains an empty field reference")
                    if len(segs) - 1 > max_depth:
                        raise ValueError(
                            f"Formula field reference '{v}' exceeds the max "
                            f"traversal depth of {max_depth}")
                    if not _resolvable(obj_def, segs):
                        raise ValueError(
                            f"Formula field reference '{v}' does not resolve "
                            f"on {obj_name}")
                else:
                    _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(formula)
