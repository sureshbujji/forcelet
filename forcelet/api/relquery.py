"""Relationship query helpers for the record API.

``?select=`` — comma-separated field list; dotted segments traverse parent
relationships (e.g. ``?select=LastName,Account.Name``). The output nests each
traversed path under its relationship names::

    {"Id": "...", "LastName": "Smith", "Account": {"Name": "Acme"}}

Multi-level paths are supported (``Account.ParentAccount.Name``, depth cap 5)
and are null-safe: a missing intermediate record or null FK yields ``None``
for that branch rather than an error. ``Id`` is always included.

``?children=`` — comma-separated child relationship names (e.g.
``?children=Contacts,Opportunities``). Each name matches a child object's
**plural label** (case-insensitive), falling back to the child object API
name. A child object is any object holding a Lookup/MasterDetail/
PolymorphicLookup field that points at the parent; a child row is included
when any of its FKs to the parent matches this record. Child rows go through
the standard serializer (field-level security applied) and record sharing is
enforced per row. Results are keyed by the requested relationship name::

    {"Contacts": [{"Id": ..., "LastName": ...}, ...]}

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from .. import datamodel as _datamodel
from ..expressions import _relationship_field_for, DOTTED_PATH_MAX_DEPTH

#: Max child rows returned per requested relationship.
CHILDREN_LIMIT = 200


def _targets(fdef: dict) -> list:
    ref = fdef.get("reference_to")
    return [t for t in (ref if isinstance(ref, list) else [ref]) if t]


def _select_node(user, obj_name: str, record: dict, segment: str,
                 subpaths: list, depth: int):
    """Resolve one relationship hop for ``?select=``. Returns a dict of the
    selected sub-fields, or None when the hop is unresolvable/invisible."""
    from ._shared import ctx, serialize
    store, registry, security = ctx()
    obj_def = registry.get_object(obj_name)
    if not obj_def or depth > DOTTED_PATH_MAX_DEPTH:
        return None
    fdef = _relationship_field_for(obj_def, segment)
    if not fdef:
        return None
    # The lookup field itself must be readable to traverse it.
    if fdef["name"] not in security.readable_fields(user, obj_def):
        return None
    fk = (record or {}).get(fdef["name"])
    if not fk:
        return None
    target_rec, target_obj = None, None
    for t in _targets(fdef):
        if not registry.get_object(t):
            continue
        try:
            cand = store.get(t, fk)
        except Exception:
            cand = None
        if cand:
            target_rec, target_obj = cand, t
            break
    if not target_rec:
        return None
    if not security.can(user, "read", target_obj):
        return None
    if not security.can_see_record(user, target_rec, target_obj):
        return None
    target_def = registry.get_object(target_obj)
    leaf_view = serialize(user, target_def, target_rec)  # FLS applied
    out: dict = {}
    deeper: dict[str, list] = {}
    for sp in subpaths:
        head, _, rest = sp.partition(".")
        if not rest:
            if head in leaf_view:
                out[head] = leaf_view[head]
            # unknown leaf fields are silently dropped — never leak
        else:
            deeper.setdefault(head, []).append(rest)
    for seg2, sub2 in deeper.items():
        node = _select_node(user, target_obj, target_rec, seg2, sub2, depth + 1)
        if node is not None:
            out[seg2] = node
    return out


def apply_select(user, obj_name: str, record: dict, serialized: dict,
                 select_param: str) -> dict:
    """Build the ``?select=`` projection for one record."""
    specs = [s.strip() for s in (select_param or "").split(",") if s.strip()]
    out: dict = {"Id": serialized.get("Id", (record or {}).get("id"))}
    nested: dict[str, list] = {}
    for spec in specs:
        if spec == "Id":
            continue
        if "." not in spec:
            # Plain fields come from the standard serialization, which
            # already enforces field-level security.
            if spec in serialized:
                out[spec] = serialized[spec]
            # unknown field names are silently dropped — never leak
        else:
            head, _, rest = spec.partition(".")
            nested.setdefault(head, []).append(rest)
    for segment, subpaths in nested.items():
        node = _select_node(user, obj_name, record, segment, subpaths, 1)
        # A null/unresolvable hop yields None (null-safe, like SOQL).
        out[segment] = node
    return out


def _child_relationships(parent_obj_name: str) -> list:
    """All (child_object, fk_field, relationship_name) triples pointing at
    the parent. Relationship name is the child object's plural label,
    falling back to its API name."""
    from ._shared import ctx
    _store, registry, _security = ctx()
    out = []
    for codef in registry.list_objects():
        cname = codef.get("name")
        if not cname or cname == parent_obj_name:
            continue
        for f in _datamodel.relationship_fields(codef):
            if parent_obj_name in _targets(f):
                rel_name = codef.get("plural") or cname
                out.append((cname, f["name"], rel_name))
    return out


def apply_children(user, parent_obj_name: str, record: dict,
                   children_param: str) -> dict:
    """Build the ``?children=`` subqueries for one parent record."""
    from ._shared import ctx, serialize
    store, registry, security = ctx()
    requested = [c.strip() for c in (children_param or "").split(",")
                 if c.strip()]
    rels = _child_relationships(parent_obj_name)
    parent_id = str((record or {}).get("id") or "")
    out: dict = {}
    for req in requested:
        matched = [r for r in rels
                   if r[2].lower() == req.lower() or r[0].lower() == req.lower()]
        rows: list = []
        seen: set = set()
        for cname, fk, _rel in matched:
            codef = registry.get_object(cname)
            if not codef or not security.can(user, "read", cname):
                continue
            try:
                candidates = store.query(cname, limit=100000)
            except Exception:
                continue
            for cr in candidates:
                if str(cr.get(fk) or "") != parent_id:
                    continue
                cid = cr.get("id")
                if not cid or cid in seen:
                    continue
                if not security.can_see_record(user, cr, cname):
                    continue
                seen.add(cid)
                rows.append(serialize(user, codef, cr))
                if len(rows) >= CHILDREN_LIMIT:
                    break
            if len(rows) >= CHILDREN_LIMIT:
                break
        out[req] = rows
    return out


def apply_record_view(user, obj_name: str, record: dict, serialized: dict,
                      select_param: str | None, children_param: str | None) -> dict:
    """Apply ``?select=`` and/or ``?children=`` to one serialized record."""
    out = serialized
    if select_param and select_param.strip():
        out = apply_select(user, obj_name, record, serialized, select_param)
    if children_param and children_param.strip():
        out = {**out, **apply_children(user, obj_name, record, children_param)}
    return out
