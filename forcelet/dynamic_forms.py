"""Dynamic Forms: conditional field visibility rules.

Admins define per-object rules of the form::

    when <field> <operator> <value>  ->  then show|hide [fields...]

The same evaluator runs server-side (record-save validation) and is mirrored
in web/index.html for live form behavior. Rules are stored in the
``mf_dynamic_forms`` config table (seeded from metadata/seed_dynamic_forms.json).
"""

TABLE = "mf_dynamic_forms"

OPERATORS = ("equals", "not_equals", "contains", "greater_than", "less_than",
             "is_blank", "is_not_blank")

ACTIONS = ("show", "hide")


def validate_rule(rule, registry):
    """Return a list of error strings; empty means the rule is valid."""
    errors = []
    if not isinstance(rule, dict):
        return ["rule must be an object"]
    name = (rule.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    obj_name = rule.get("object")
    obj = registry.get_object(obj_name) if obj_name else None
    if not obj:
        errors.append(f"unknown object '{obj_name}'")
        return errors
    fnames = {f["name"] for f in obj.get("fields", [])}
    when = rule.get("when")
    if not isinstance(when, dict):
        errors.append("when must be an object")
    else:
        if when.get("field") not in fnames:
            errors.append(f"unknown when.field '{when.get('field')}'")
        if when.get("operator") not in OPERATORS:
            errors.append(f"unknown operator '{when.get('operator')}'")
        elif when["operator"] not in ("is_blank", "is_not_blank"):
            v = when.get("value")
            if v is None or (isinstance(v, str) and not v.strip()):
                errors.append("when.value is required for this operator")
    then = rule.get("then")
    if not isinstance(then, dict):
        errors.append("then must be an object")
    else:
        if then.get("action") not in ACTIONS:
            errors.append(f"then.action must be one of {ACTIONS}")
        fields = then.get("fields")
        if not isinstance(fields, list) or not fields:
            errors.append("then.fields must be a non-empty list")
        else:
            for fn in fields:
                if fn not in fnames:
                    errors.append(f"unknown then.fields entry '{fn}'")
    if "active" in rule and not isinstance(rule["active"], bool):
        errors.append("active must be a boolean")
    return errors


# --------------------------------------------------------------------------
# condition evaluation


def _is_blank(v):
    return v is None or (isinstance(v, str) and not v.strip()) \
        or (isinstance(v, (list, tuple)) and not v)


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")


def _to_num(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _values_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return _to_bool(a) == _to_bool(b)
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a) == str(b)


def evaluate_condition(when, values):
    """Evaluate a single {field, operator, value} condition against values."""
    if not isinstance(when, dict):
        return False
    actual = (values or {}).get(when.get("field"))
    op = when.get("operator")
    if op == "is_blank":
        return _is_blank(actual)
    if op == "is_not_blank":
        return not _is_blank(actual)
    if op == "equals":
        return _values_equal(actual, when.get("value"))
    if op == "not_equals":
        return not _values_equal(actual, when.get("value"))
    if op == "contains":
        if actual is None:
            return False
        return str(when.get("value") or "").lower() in str(actual).lower()
    if op in ("greater_than", "less_than"):
        na, nb = _to_num(actual), _to_num(when.get("value"))
        if na is None or nb is None:
            return False
        return na > nb if op == "greater_than" else na < nb
    return False


# --------------------------------------------------------------------------
# rule application


def rules_for(store, obj_name, active_only=True):
    rules = [r for r in store.config_all(TABLE)
             if r.get("object") == obj_name]
    if active_only:
        rules = [r for r in rules if r.get("active", True)]
    return sorted(rules, key=lambda r: r.get("name") or "")


def hidden_fields(store, obj_name, values):
    """Return the set of field names hidden by the object's active rules.

    Semantics: a field is hidden when any active "hide" rule whose condition
    is true lists it, or when it is listed by at least one active "show"
    rule but no active "show" rule for it evaluates to true ("show" rules
    gate visibility; "hide" rules take precedence).
    """
    hidden, shown, gated = set(), set(), set()
    for r in rules_for(store, obj_name):
        cond = evaluate_condition(r.get("when"), values)
        then = r.get("then") or {}
        fields = then.get("fields") or []
        if then.get("action") == "hide":
            if cond:
                hidden.update(fields)
        else:  # show
            shown.update(fields)
            if cond:
                gated.update(fields)
    for f in shown:
        if f not in gated:
            hidden.add(f)
    return hidden
