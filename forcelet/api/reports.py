"""Reports and dashboards. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.

Report documents (mf_reports) support:
  name, object, columns[], filters (expression JSON), sort[{field,dir}],
  group_by: str | [str | {field, part}] (up to 3 levels),
  column_group_by: str | [str | {field, part}] (up to 2 levels, matrix),
  fiscal_start_month (1-12, default 1),
  aggregate (legacy {func, field}) | aggregates[{func, field, alias}],
  bucket {field, buckets[{name, from, to}]},
  cross_filters[{object, via, mode}], summary_formulas[{name, label, formula}],
  row_formulas[{name, label, formula}] (per-row, may use dotted refs),
  highlight[{field, op, value, color}], chart{type},
  folder_id, run_as_user, active, report_type, related,
  blocks[{name, object, ...}] (2-5, joined report; each block is a full
  report definition run independently).

Date-part grouping: a group_by level may be {"field": "CloseDate",
  "part": "month"} with part in day|week|month|quarter|year|
  fiscal_quarter|fiscal_year. Labels are human-readable ("2026-10",
  "Q4 2026", "FY2027").

Summary formulas support {"agg": alias} refs plus the cross-group
functions {"func": "PARENTGROUPVAL"|"PREVGROUPVAL", "agg": alias}.

Custom report types (mf_report_types) support:
  name, label, primary_object, related[{object, via_field}],
  default_columns[].

Dashboard documents (mf_dashboards) support:
  name, widgets[{report_id, type, w, group_by?, aggregate?/aggregates?,
  filters?}], filters[] (up to 3 global), folder_id, run_as ("viewer" |
  user id), refresh_schedule ("daily"|"weekly"|"monthly"), last_run_at.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape as _xml_escape

from flask import Flask, jsonify, request, Response

from .. import automation
from .. import crypto as _crypto
from .. import datamodel as _datamodel
from ..expressions import (eval_expr, record_context, validate_formula_refs,
                            _relationship_field_for, DOTTED_PATH_MAX_DEPTH)
from ..field_types import mask_secret
from ._shared import (
    _audit, require_admin, require_auth, ctx,
)


_SENTINEL = object()
AGG_FUNCS = ("sum", "avg", "min", "max", "count", "count_distinct")
GROUP_SORTS = ("count_desc", "count_asc", "label_asc")
CHART_TYPES = ("bar", "line", "donut", "pie", "funnel", "scatter",
               "stacked_bar", "area")
#: Max groups rendered per chart type (client SVG renderers cap at these;
#: the API documents them so UI and exports stay consistent).
CHART_CAPS = {"bar": 12, "line": 12, "donut": 8, "pie": 8, "funnel": 10,
              "scatter": 200, "stacked_bar": 12, "area": 12}
WIDGET_TYPES = ("bar", "line", "donut", "stat", "table", "gauge", "funnel", "scatter")
MAX_WIDGETS = 20
#: Date parts accepted in group_by levels: {"field": "CloseDate", "part": "month"}.
DATE_PARTS = ("day", "week", "month", "quarter", "year",
              "fiscal_quarter", "fiscal_year")
#: Dashboard refresh cadence vocabulary (mirrors report subscriptions).
REFRESH_SCHEDULES = ("daily", "weekly", "monthly")


# ============================================================ report run engine
def _serialize_report_row(store, registry, security, user, obj, record):
    """Field-level-security-aware row serialization usable outside a request."""
    readable = set(security.readable_fields(user, obj))
    data = {"Id": record["id"]}
    for f in obj.get("fields", []):
        if f["name"] not in readable:
            continue
        if f.get("formula") or f.get("type") == "Formula":
            try:
                data[f["name"]] = eval_expr(f["formula"], record_context(record))
            except Exception:
                data[f["name"]] = None
        elif f.get("rollup"):
            try:
                data[f["name"]] = automation.compute_rollup(
                    store, security, user, f["rollup"], record["id"])
            except Exception:
                data[f["name"]] = None
        elif f.get("type") == "EncryptedText":
            v = record.get(f["name"])
            v = _crypto.decrypt(v) if _crypto.is_encrypted(v) else v
            data[f["name"]] = mask_secret(v, f.get("mask_chars", 4))
        else:
            v = record.get(f["name"])
            data[f["name"]] = _crypto.decrypt(v) if f.get("encrypted") else v
    if obj["name"] == "Account":
        pn = _datamodel.person_display_name(record)
        if pn:
            data["Name"] = pn
    # System audit fields are not declared object fields; include them so
    # filters (e.g. R5 relative-date filters on CreatedDate) and columns
    # can reference them.
    for _sf, _raw in (("CreatedDate", "created_date"),
                      ("LastModifiedDate", "last_modified_date"),
                      ("OwnerId", "owner_id"),
                      ("CreatedById", "created_by")):
        data.setdefault(_sf, record.get(_raw))
    return data


def _rel_info(obj, relpath):
    """Resolve 'Account.Name' -> (lookup_field, parent_object, parent_field).

    Phase 1 (R4): single-level parent lookup traversal only. Accepts the
    Salesforce-style relationship name ('Account') or the raw lookup field
    name ('AccountId'). Returns None when the path is not a parent lookup.
    """
    if not isinstance(relpath, str) or "." not in relpath:
        return None
    rel, sub = relpath.split(".", 1)
    if "." in sub or not rel or not sub:
        return None
    fmap = {f["name"]: f for f in obj.get("fields", [])}
    lf = fmap.get(rel)
    if not (lf and lf.get("type") in ("Lookup", "MasterDetail")):
        lf = fmap.get(rel + "Id")
    if not (lf and lf.get("type") in ("Lookup", "MasterDetail")
            and lf.get("reference_to")):
        return None
    return lf["name"], lf["reference_to"], sub


def _walk_expr_fields(expr, out):
    if isinstance(expr, dict):
        for k, v in expr.items():
            if k == "field" and isinstance(v, str):
                out.add(v)
            else:
                _walk_expr_fields(v, out)
    elif isinstance(expr, list):
        for v in expr:
            _walk_expr_fields(v, out)


def _collect_relpaths(rep, extra_filters):
    cands = set()
    for c in rep.get("columns") or []:
        if isinstance(c, str) and "." in c:
            cands.add(c)
    _walk_expr_fields(rep.get("filters") or {}, cands)
    for s in rep.get("sort") or []:
        if isinstance(s, dict) and isinstance(s.get("field"), str) \
                and "." in s["field"]:
            cands.add(s["field"])
    for hl in rep.get("highlight") or []:
        if isinstance(hl.get("field"), str) and "." in hl["field"]:
            cands.add(hl["field"])
    for ef in extra_filters or []:
        if isinstance(ef, dict) and isinstance(ef.get("field"), str):
            # {"field","op","value"} shape (drill-down)
            if "." in ef["field"]:
                cands.add(ef["field"])
        else:
            # already an expression (dashboard filters)
            _walk_expr_fields(ef, cands)
    return cands


def _enrich_related(store, registry, security, run_user, obj, raw_records,
                    rows, relpaths):
    """Null-safe parent-lookup resolution (R4). Mutates rows in place.

    Security: a parent cell is populated only when the effective user can
    see the parent record (record sharing) and can read the parent field
    (field-level security); otherwise the cell is None. The child row
    itself is unaffected.
    """
    by_lookup: dict = {}
    for rp in relpaths:
        info = _rel_info(obj, rp)
        if info:
            by_lookup.setdefault(info[0], {})[(info[1], info[2])] = rp
    if not by_lookup:
        return
    # System audit fields are serialized unconditionally by
    # _serialize_report_row; treat them as readable here too.
    _SYS_FIELDS = {"CreatedDate", "LastModifiedDate", "OwnerId", "CreatedById"}
    cache: dict = {}
    readable_cache: dict = {}
    field_cache: dict = {}

    def _parent_cell(pobj, pid, pfield):
        if not pid:
            return None
        if (pobj, pid) not in cache:
            try:
                cache[(pobj, pid)] = store.get(pobj, pid)
            except Exception:
                cache[(pobj, pid)] = None
        prow = cache[(pobj, pid)]
        if prow is None:
            return None
        # Record sharing: the effective user must see the parent record.
        try:
            if not security.can_see_record(run_user, prow, pobj):
                return None
        except Exception:
            return None
        # Field-level security on the requested parent column.
        if pobj not in readable_cache:
            pobj_def = registry.get_object(pobj)
            fields = pobj_def.get("fields", []) if pobj_def else []
            field_cache[pobj] = {f["name"]: f for f in fields}
            try:
                readable_cache[pobj] = set(
                    security.readable_fields(run_user, pobj_def)) | _SYS_FIELDS \
                    if pobj_def else set(_SYS_FIELDS)
            except Exception:
                readable_cache[pobj] = set(_SYS_FIELDS)
        if pfield not in readable_cache[pobj]:
            return None
        v = prow.get(pfield)
        fdef = field_cache[pobj].get(pfield)
        if fdef and fdef.get("type") == "EncryptedText":
            v = _crypto.decrypt(v) if _crypto.is_encrypted(v) else v
            v = mask_secret(v, fdef.get("mask_chars", 4))
        return v

    for raw, row in zip(raw_records, rows):
        for lkp, subs in by_lookup.items():
            pid = raw.get(lkp)
            for (pobj, pfield), rp in subs.items():
                row[rp] = _parent_cell(pobj, pid, pfield)


def _rel_targets_list(fdef):
    ref = fdef.get("reference_to")
    return [t for t in (ref if isinstance(ref, list) else [ref]) if t]


def _secure_dotted_value(store, registry, security, run_user, obj_name,
                         raw_record, path, max_depth=DOTTED_PATH_MAX_DEPTH):
    """Resolve a multi-level dotted path (e.g. ``Account.ParentAccount.Name``)
    with per-hop security, mirroring the single-level `_enrich_related`
    model: each intermediate record must be visible to the effective user
    (record sharing) and the final leaf field must be readable
    (field-level security). Null-safe: any invisible hop or missing
    record yields None instead of raising. Never raises.
    """
    try:
        segs = [s for s in str(path or "").split(".") if s]
        if len(segs) < 2 or len(segs) - 1 > max_depth:
            return None
        _SYS_FIELDS = {"CreatedDate", "LastModifiedDate", "OwnerId",
                       "CreatedById"}
        cache, readable_cache, field_cache = {}, {}, {}
        cur_obj, cur_raw, visited = obj_name, raw_record, set()
        for seg in segs[:-1]:
            odef = registry.get_object(cur_obj)
            if not odef:
                return None
            fdef = _relationship_field_for(odef, seg)
            if not fdef:
                return None
            pid = (cur_raw or {}).get(fdef["name"])
            if not pid:
                return None
            prow, pobj = None, None
            for t in _rel_targets_list(fdef):
                if not registry.get_object(t):
                    continue
                key = (t, pid)
                if key not in cache:
                    try:
                        cache[key] = store.get(t, pid)
                    except Exception:
                        cache[key] = None
                if cache[key]:
                    prow, pobj = cache[key], t
                    break
            if prow is None:
                return None
            try:
                if not security.can_see_record(run_user, prow, pobj):
                    return None
            except Exception:
                return None
            key = (pobj, prow.get("id"))
            if key in visited:
                return None
            visited.add(key)
            cur_raw, cur_obj = prow, pobj
        leaf = segs[-1]
        if cur_obj not in readable_cache:
            pdef = registry.get_object(cur_obj)
            fields = pdef.get("fields", []) if pdef else []
            field_cache[cur_obj] = {f["name"]: f for f in fields}
            try:
                readable_cache[cur_obj] = set(
                    security.readable_fields(run_user, pdef)) | _SYS_FIELDS \
                    if pdef else set(_SYS_FIELDS)
            except Exception:
                readable_cache[cur_obj] = set(_SYS_FIELDS)
        if leaf not in readable_cache[cur_obj]:
            return None
        v = (cur_raw or {}).get(leaf)
        fdef = field_cache[cur_obj].get(leaf)
        if fdef and fdef.get("type") == "EncryptedText":
            v = _crypto.decrypt(v) if _crypto.is_encrypted(v) else v
            v = mask_secret(v, fdef.get("mask_chars", 4))
        return v
    except Exception:
        return None


def _enrich_related_multi(store, registry, security, run_user, obj,
                          raw_records, rows, relpaths):
    """Multi-level parent traversal for related-object columns.

    Extends `_enrich_related` (which handles single-level paths) without
    touching it: any dotted path with 2+ hops resolves via
    `_secure_dotted_value`, so sharing and FLS are enforced per hop.
    """
    multi = [rp for rp in relpaths
             if isinstance(rp, str) and rp.count(".") > 1]
    if not multi:
        return
    for raw, row in zip(raw_records, rows):
        for rp in multi:
            row[rp] = _secure_dotted_value(store, registry, security,
                                           run_user, obj["name"], raw, rp)


def _apply_row_formulas(store, registry, security, run_user, obj_name,
                        raw_records, rows, row_formulas):
    """Evaluate per-row formula columns.

    Formulas use the full expression language; dotted references resolve
    through `_secure_dotted_value` (sharing + FLS enforced, like related
    columns). Plain field refs read the FLS-filtered serialized row, so
    unreadable fields evaluate to None. Returns the computed column names.
    """
    names = []
    for rf in row_formulas or []:
        name = rf.get("name")
        if not name or not isinstance(name, str):
            continue
        names.append(name)
        formula = rf.get("formula") or {}
        for raw, row in zip(raw_records, rows):
            try:
                val = eval_expr(
                    formula, row,
                    rel_resolver=lambda p, _r=raw: _secure_dotted_value(
                        store, registry, security, run_user, obj_name, _r, p))
                if isinstance(val, float):
                    val = round(val, 4)
            except Exception:
                val = None
            row[name] = val
    return names


def _apply_cross_filters(store, rows, rep):
    """WITH / WITHOUT related records (R7), built on lookup traversal."""
    for cf in rep.get("cross_filters") or []:
        child, via = cf.get("object"), cf.get("via")
        want_with = (cf.get("mode") or "with") == "with"
        if not child or not via:
            continue
        with_ids = set()
        try:
            for cr in store.query(child, limit=10000):
                pid = cr.get(via)
                if pid:
                    with_ids.add(pid)
        except Exception:
            continue
        rows = [r for r in rows if (r.get("Id") in with_ids) == want_with]
    return rows


def _safe_eval(expr, row):
    try:
        return bool(eval_expr(expr, record_context(row)))
    except Exception:
        return False


def _sort_rows(rows, sort_spec):
    for s in reversed(sort_spec or []):
        field = s.get("field")
        if not field:
            continue
        desc = (s.get("dir") or "asc").lower() == "desc"

        def _key(r, _f=field):
            v = r.get(_f)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return (0, 0, v, "")
            return (0 if v is None else 1, 1, 0, "" if v is None else str(v))
        try:
            rows.sort(key=_key, reverse=desc)
        except Exception:
            pass
    return rows


def _agg_alias(func, field):
    return f"{func}_{field}" if field else f"{func}_rows"


def _norm_aggregates(rep):
    aggs = rep.get("aggregates")
    if isinstance(aggs, list) and aggs:
        return [{"func": a.get("func"), "field": a.get("field") or "",
                 "alias": a.get("alias") or _agg_alias(a.get("func"),
                                                      a.get("field") or "")}
                for a in aggs if a.get("func") in AGG_FUNCS]
    leg = rep.get("aggregate") or {}
    if leg.get("func") in AGG_FUNCS:
        return [{"func": leg["func"], "field": leg.get("field") or "",
                 "alias": _agg_alias(leg["func"], leg.get("field") or "")}]
    return []


def _norm_group_by(rep):
    # Merges the UI's top-level {"field": "part"} date_parts map into levels:
    # {"field": "CloseDate", "part": "month"}. Explicit dict levels win.
    dp = rep.get("date_parts") or {}
    gb = rep.get("group_by")
    if isinstance(gb, str):
        gb = [gb] if gb else []
    if isinstance(gb, list):
        out = []
        for g in gb:
            if isinstance(g, str) and dp.get(g):
                out.append({"field": g, "part": dp[g]})
            elif g:
                out.append(g)
        return out[:3]
    return []


def _bucket_value(bucket, value):
    for b in (bucket or {}).get("buckets") or []:
        lo, hi = b.get("from"), b.get("to")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (lo is None or value >= lo) and (hi is None or value <= hi):
                return b.get("name") or "(blank)"
    return "(other)"


def _parse_ymd(value):
    s = str(value or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_part_key(value, part, fiscal_start_month=1):
    """Bucket a date/datetime value by part. Labels are human-readable:
    day -> "2026-10-02", week -> "2026-W40", month -> "2026-10",
    quarter -> "Q4 2026", year -> "2026", fiscal_quarter -> "Q2 FY2027",
    fiscal_year -> "FY2027". Unparseable values fall back to the raw value.
    """
    d = _parse_ymd(value)
    if not d:
        return value
    if part == "day":
        return d.isoformat()
    if part == "week":
        iso_y, iso_w, _ = d.isocalendar()
        return f"{iso_y}-W{iso_w:02d}"
    if part == "month":
        return f"{d.year}-{d.month:02d}"
    if part == "quarter":
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    if part == "year":
        return str(d.year)
    fsm = fiscal_start_month or 1
    # Fiscal year is labelled by its ending calendar year (FY2027 = the
    # fiscal year starting Oct 2026 when fsm=10); with fsm=1 it is the
    # calendar year itself.
    fy = d.year if d.month >= fsm else d.year - 1
    label_y = fy + (1 if fsm != 1 else 0)
    if part == "fiscal_quarter":
        fq = (d.month - fsm) % 12 // 3 + 1
        return f"Q{fq} FY{label_y}"
    if part == "fiscal_year":
        return f"FY{label_y}"
    return value


def _group_field(level):
    return level.get("field") if isinstance(level, dict) else level


def _group_key(row, level, bucket, fiscal_start_month=1):
    if level == "_bucket" and bucket:
        return _bucket_value(bucket, row.get(bucket.get("field")))
    field = _group_field(level)
    part = level.get("part") if isinstance(level, dict) else None
    v = row.get(field)
    if part and v not in (None, ""):
        v = _date_part_key(v, part, fiscal_start_month)
    return v if v not in (None, "") else "(blank)"


def _compute_aggs(rows, aggs):
    out = {"count": len(rows)}
    for a in aggs:
        func, field, alias = a["func"], a["field"], a["alias"]
        if func == "count":
            out[alias] = len(rows)
            continue
        if func == "count_distinct":
            out[alias] = len({r.get(field) for r in rows
                              if r.get(field) not in (None, "")})
            continue
        vals = [r.get(field) for r in rows
                if isinstance(r.get(field), (int, float))
                and not isinstance(r.get(field), bool)]
        if func == "sum":
            out[alias] = round(sum(vals), 4) if vals else 0
        elif func == "avg":
            out[alias] = round(sum(vals) / len(vals), 4) if vals else None
        elif func == "min":
            out[alias] = min(vals) if vals else None
        elif func == "max":
            out[alias] = max(vals) if vals else None
    return out


def _rewrite_agg_refs(expr):
    """{"agg": alias} -> {"field": alias} so summary formulas read group aggs."""
    if isinstance(expr, dict):
        if set(expr.keys()) == {"agg"}:
            return {"field": expr["agg"]}
        return {k: _rewrite_agg_refs(v) for k, v in expr.items()}
    if isinstance(expr, list):
        return [_rewrite_agg_refs(v) for v in expr]
    return expr


def _resolve_group_funcs(expr, parent_aggs, prev_aggs):
    """Resolve PARENTGROUPVAL / PREVGROUPVAL refs inside summary formulas.

    {"func": "PARENTGROUPVAL", "agg": alias} -> the parent group's aggregate
    value; {"func": "PREVGROUPVAL", "agg": alias} -> the previous sibling
    group's value (in display/sorted order). May be nested anywhere inside
    a larger expression. Unknown funcs are left for eval_expr to reject.
    """
    if isinstance(expr, dict):
        func = expr.get("func")
        if func == "PARENTGROUPVAL" and set(expr) <= {"func", "agg"}:
            return (parent_aggs or {}).get(expr.get("agg"))
        if func == "PREVGROUPVAL" and set(expr) <= {"func", "agg"}:
            return (prev_aggs or {}).get(expr.get("agg"))
        return {k: _resolve_group_funcs(v, parent_aggs, prev_aggs)
                for k, v in expr.items()}
    if isinstance(expr, list):
        return [_resolve_group_funcs(v, parent_aggs, prev_aggs) for v in expr]
    return expr


def _eval_summary_formulas(formulas, aggs_dict, parent_aggs=None, prev_aggs=None):
    out = []
    for sf in formulas or []:
        try:
            expr = _resolve_group_funcs(sf.get("formula") or {}, parent_aggs,
                                        prev_aggs)
            val = eval_expr(_rewrite_agg_refs(expr), dict(aggs_dict))
            if isinstance(val, float):
                val = round(val, 4)
        except Exception:
            val = None
        out.append({"name": sf.get("name"), "label": sf.get("label")
                    or sf.get("name"), "value": val})
    return out


def _sort_group_nodes(nodes, sort):
    if sort == "count_asc":
        nodes.sort(key=lambda n: n["count"])
    elif sort == "label_asc":
        nodes.sort(key=lambda n: str(n["key"]))
    else:  # count_desc (default, matches legacy behavior)
        nodes.sort(key=lambda n: n["count"], reverse=True)
    return nodes


def _build_groups(rows, levels, aggs, sort, bucket, formulas,
                  parent_aggs=None, fsm=1):
    if not levels:
        return []
    buckets, order = {}, []
    for r in rows:
        k = _group_key(r, levels[0], bucket, fsm)
        ks = json.dumps(k, sort_keys=True, default=str)
        if ks not in buckets:
            buckets[ks] = {"key": k, "rows": []}
            order.append(ks)
        buckets[ks]["rows"].append(r)
    nodes = []
    for ks in order:
        b = buckets[ks]
        node_aggs = _compute_aggs(b["rows"], aggs)
        node = {"key": b["key"], "count": len(b["rows"]),
                "aggregates": node_aggs}
        children = _build_groups(b["rows"], levels[1:], aggs, sort, bucket,
                                 formulas, parent_aggs=node_aggs, fsm=fsm)
        if children:
            node["children"] = children
        nodes.append(node)
    _sort_group_nodes(nodes, sort)
    # Formulas evaluate after sorting so PREVGROUPVAL sees display order.
    for i, node in enumerate(nodes):
        prev = nodes[i - 1]["aggregates"] if i > 0 else None
        node["formulas"] = _eval_summary_formulas(
            formulas, node["aggregates"], parent_aggs=parent_aggs,
            prev_aggs=prev)
    return nodes


def _norm_column_group_by(rep):
    dp = rep.get("date_parts") or {}
    gb = rep.get("column_group_by")
    if isinstance(gb, str):
        gb = [gb] if gb else []
    if isinstance(gb, list):
        out = []
        for g in gb:
            if isinstance(g, str) and dp.get(g):
                out.append({"field": g, "part": dp[g]})
            elif g:
                out.append(g)
        return out[:2]
    return []


def _build_matrix(rows, row_levels, col_levels, aggs, sort, bucket, formulas,
                  fsm=1):
    """Matrix format: row-groups x column-groups with per-cell aggregates.

    Returns {"row_levels", "column_levels", "rows", "column_totals",
    "grand_total"}. Each row node carries "columns" (one cell per column
    leaf: key/count/aggregates/formulas), "row_total", and nested
    "children". Cell formulas see the row node's aggregates as
    PARENTGROUPVAL and the previous cell as PREVGROUPVAL.
    """
    def _path(r, levels, bkt):
        return tuple(_group_key(r, lv, bkt, fsm) for lv in levels)

    def _pks(path):
        return tuple(json.dumps(k, sort_keys=True, default=str) for k in path)

    # Column leaves are partitioned globally so every row shares one
    # column order.
    col_buckets, col_order, col_assign = {}, [], {}
    for r in rows:
        cp = _path(r, col_levels, None)
        cks = _pks(cp)
        col_assign[id(r)] = cks
        if cks not in col_buckets:
            col_buckets[cks] = {"key": list(cp), "rows": []}
            col_order.append(cks)
        col_buckets[cks]["rows"].append(r)
    col_order.sort(key=lambda c: len(col_buckets[c]["rows"]), reverse=True)

    def _nodes(sub_rows, depth, parent_aggs):
        if depth >= len(row_levels):
            return []
        buckets, order = {}, []
        for r in sub_rows:
            k = _group_key(r, row_levels[depth], bucket, fsm)
            ks = json.dumps(k, sort_keys=True, default=str)
            if ks not in buckets:
                buckets[ks] = {"key": k, "rows": []}
                order.append(ks)
            buckets[ks]["rows"].append(r)
        nodes = []
        for ks in order:
            b = buckets[ks]
            node_aggs = _compute_aggs(b["rows"], aggs)
            node = {"key": b["key"], "count": len(b["rows"]),
                    "aggregates": node_aggs}
            children = _nodes(b["rows"], depth + 1, node_aggs)
            if children:
                node["children"] = children
            nodes.append((node, b["rows"]))
        # Sort the (node, rows) tuples with the same semantics as row groups.
        if sort == "count_asc":
            nodes.sort(key=lambda t: t[0]["count"])
        elif sort == "label_asc":
            nodes.sort(key=lambda t: str(t[0]["key"]))
        else:  # count_desc (default)
            nodes.sort(key=lambda t: t[0]["count"], reverse=True)
        out = []
        for i, (node, b_rows) in enumerate(nodes):
            prev_aggs = nodes[i - 1][0]["aggregates"] if i > 0 else None
            by_col = {}
            for r in b_rows:
                by_col.setdefault(col_assign[id(r)], []).append(r)
            cells = []
            for cks in col_order:
                crows = by_col.get(cks, [])
                caggs = _compute_aggs(crows, aggs)
                cells.append({
                    "key": col_buckets[cks]["key"],
                    "count": len(crows), "aggregates": caggs,
                    "formulas": _eval_summary_formulas(
                        formulas, caggs, parent_aggs=node["aggregates"],
                        prev_aggs=cells[-1]["aggregates"] if cells else None),
                })
            node["columns"] = cells
            node["row_total"] = {
                "count": node["count"], "aggregates": node["aggregates"],
                "formulas": _eval_summary_formulas(
                    formulas, node["aggregates"], parent_aggs=parent_aggs,
                    prev_aggs=prev_aggs)}
            out.append(node)
        return out

    row_nodes = _nodes(rows, 0, None)
    column_totals = []
    for cks in col_order:
        crows = col_buckets[cks]["rows"]
        caggs = _compute_aggs(crows, aggs)
        column_totals.append({
            "key": col_buckets[cks]["key"], "count": len(crows),
            "aggregates": caggs,
            "formulas": _eval_summary_formulas(
                formulas, caggs,
                prev_aggs=column_totals[-1]["aggregates"] if column_totals else None),
        })
    grand_aggs = _compute_aggs(rows, aggs)
    return {
        "row_levels": [_group_field(lv) for lv in row_levels],
        "column_levels": [_group_field(lv) for lv in col_levels],
        "rows": row_nodes,
        "column_totals": column_totals,
        "grand_total": {"count": len(rows), "aggregates": grand_aggs,
                        "formulas": _eval_summary_formulas(formulas, grand_aggs)},
    }


def run_report_data(store, registry, security, user, rep, extra_filters=None,
                    page=1, page_size=500, group_by_override=_SENTINEL,
                    aggregates_override=_SENTINEL):
    """Run a report definition. Usable from request handlers and the scheduler.

    extra_filters: [{"field","op","value"}] applied with AND (dashboard filters,
    drill-down). group_by_override/aggregates_override: per-widget config (D4).
    When the definition has `blocks` (joined report), each block runs
    independently (sharing/FLS per block) and the result carries "blocks".
    Returns a JSON-serializable dict (or {"error": ...}).
    """
    # Joined reports: each block is a full report definition on possibly a
    # different object. Run them independently and combine. Blocks cannot
    # nest; the run-as user propagates to every block.
    blocks = rep.get("blocks")
    if isinstance(blocks, list) and blocks:
        out_blocks = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bdef = {k: v for k, v in b.items() if k != "blocks"}
            if rep.get("run_as_user") and not bdef.get("run_as_user"):
                bdef["run_as_user"] = rep["run_as_user"]
            res = run_report_data(store, registry, security, user, bdef,
                                  page=1, page_size=500)
            bname = b.get("name") or bdef.get("object") or "block"
            if res.get("error"):
                out_blocks.append({"name": bname, "error": res["error"]})
            else:
                out_blocks.append({"name": bname, **res})
        return {"report": rep.get("name"), "block_count": len(out_blocks),
                "blocks": out_blocks}
    obj_name = rep.get("object")
    obj = registry.get_object(obj_name) if obj_name else None
    if not obj:
        return {"error": "Unknown object"}
    # R14: run-as — viewer by default; an admin-set user when configured.
    run_user = user
    rau = rep.get("run_as_user")
    if rau and (user.get("profile") == "System Administrator"
                or user.get("is_admin")):
        try:
            u = security.get_user(rau)
            if u:
                run_user = u
        except Exception:
            pass
    if not security.can(run_user, "read", obj_name):
        return {"error": "No access"}
    raw_records = [r for r in store.query(obj_name, owner_ids=None, limit=10000)
                   if security.can_see_record(run_user, r, obj_name)]
    rows = [_serialize_report_row(store, registry, security, run_user, obj, r)
            for r in raw_records]
    # R4: related-object (parent lookup) traversal, null-safe.
    # Single-level paths go through the secured enricher; multi-level
    # paths (Account.ParentAccount.Name) resolve per-hop with the same
    # sharing/FLS model.
    relpaths = _collect_relpaths(rep, extra_filters)
    _enrich_related(store, registry, security, run_user, obj, raw_records,
                    rows, relpaths)
    _enrich_related_multi(store, registry, security, run_user, obj,
                          raw_records, rows, relpaths)
    # Row-level formula columns (may reference dotted parent fields).
    rf_names = _apply_row_formulas(store, registry, security, run_user,
                                   obj_name, raw_records, rows,
                                   rep.get("row_formulas"))
    # R7: cross filters (WITH/WITHOUT related records).
    rows = _apply_cross_filters(store, rows, rep)
    # Filters: report's own expression AND any extra filters.
    exprs = []
    if rep.get("filters"):
        exprs.append(rep["filters"])
    for ef in extra_filters or []:
        if isinstance(ef, dict) and "field" in ef and not any(
                k in ef for k in ("and", "or", "not")):
            # {"field","op","value"} shape (drill-down)
            op = ef.get("op") or "=="
            exprs.append({op: [{"field": ef.get("field")}, ef.get("value")]})
        else:
            # already a filter expression (dashboard filters)
            exprs.append(ef)
    if exprs:
        combined = {"and": exprs} if len(exprs) > 1 else exprs[0]
        rows = [r for r in rows if _safe_eval(combined, r)]
    # R10: sorting.
    rows = _sort_rows(rows, rep.get("sort") or [])
    # R3: multi-level grouping + richer aggregates.
    levels = (rep.get("group_by") if group_by_override is _SENTINEL
              else group_by_override)
    levels = ([levels] if isinstance(levels, str) else (levels or []))[:3]
    levels = [lv for lv in levels if lv]
    aggs = (_norm_aggregates(rep) if aggregates_override is _SENTINEL
            else aggregates_override)
    if not isinstance(aggs, list):
        aggs = []
    bucket = rep.get("bucket")
    sort_mode = rep.get("group_sort") if rep.get("group_sort") in GROUP_SORTS \
        else "count_desc"
    fsm = rep.get("fiscal_start_month") or 1
    formulas = rep.get("summary_formulas") or []
    tree = _build_groups(rows, levels, aggs, sort_mode, bucket, formulas,
                         fsm=fsm)
    col_levels = _norm_column_group_by(rep)
    matrix = _build_matrix(rows, levels, col_levels, aggs, sort_mode, bucket,
                           formulas, fsm=fsm) if col_levels else None
    grand_aggs = _compute_aggs(rows, aggs)
    # Backward-compatible flat groups for single-level reports.
    flat = None
    if levels:
        flat = []
        for n in tree:
            g = {"key": n["key"], "count": n["count"],
                 "aggregates": n["aggregates"], "formulas": n["formulas"]}
            if aggs:
                g["aggregate"] = n["aggregates"].get(aggs[0]["alias"])
            flat.append(g)
    # R2: server-side pagination.
    total = len(rows)
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(2000, max(1, int(page_size or 500)))
    except (TypeError, ValueError):
        page_size = 500
    pages = max(1, -(-total // page_size))
    page = min(page, pages)
    page_rows = rows[(page - 1) * page_size:page * page_size]
    out_columns = list(rep.get("columns") or []) + rf_names
    result = {
        "report": rep.get("name"), "row_count": total,
        "columns": out_columns,
        "rows": page_rows, "page": page, "page_size": page_size,
        "pages": pages, "groups": flat, "group_tree": tree,
        "grand_total": {"count": total, "aggregates": grand_aggs,
                        "formulas": _eval_summary_formulas(formulas, grand_aggs)},
        "highlight": rep.get("highlight") or [],
        "chart": rep.get("chart") or {},
        "bucket": bucket or {},
        "row_formulas": [{"name": n} for n in rf_names],
    }
    if matrix is not None:
        result["matrix"] = matrix
    return result


# ============================================================ export helpers
def _export_columns(rep, rows):
    cols = rep.get("columns") or []
    if not cols and rows:
        cols = [k for k in rows[0].keys()]
    return cols


def _csv_bytes(columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell_str(r.get(c)) for c in columns])
    return buf.getvalue().encode("utf-8")


def _cell_str(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return str(v)


def _xlsx_bytes(columns, rows):
    """Minimal dependency-free .xlsx writer (inline strings + numbers)."""
    def cell(v):
        if v is None or v == "":
            return "<c/>"
        if isinstance(v, bool):
            return f"<c t=\"b\"><v>{int(v)}</v></c>"
        if isinstance(v, (int, float)):
            return f"<c><v>{v}</v></c>"
        return ("<c t=\"inlineStr\"><is><t>"
                + _xml_escape(str(v)) + "</t></is></c>")
    sheet_rows = ["<row>" + "".join(
        "<c t=\"inlineStr\"><is><t>" + _xml_escape(str(c))
        + "</t></is></c>" for c in columns) + "</row>"]
    for r in rows:
        sheet_rows.append("<row>" + "".join(cell(r.get(c))
                                            for c in columns) + "</row>")
    sheet = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
             "<worksheet xmlns=\"http://schemas.openxmlformats.org/"
             "spreadsheetml/2006/main\"><sheetData>"
             + "".join(sheet_rows) + "</sheetData></worksheet>")
    content_types = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                     "<Types xmlns=\"http://schemas.openxmlformats.org/"
                     "package/2006/content-types\">"
                     "<Default Extension=\"rels\" ContentType=\"application/"
                     "vnd.openxmlformats-package.relationships+xml\"/>"
                     "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
                     "<Override PartName=\"/xl/workbook.xml\" ContentType=\""
                     "application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet.main+xml\"/>"
                     "<Override PartName=\"/xl/worksheets/sheet1.xml\" "
                     "ContentType=\"application/vnd.openxmlformats-"
                     "officedocument.spreadsheetml.worksheet+xml\"/>"
                     "</Types>")
    rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<Relationships xmlns=\"http://schemas.openxmlformats.org/"
            "package/2006/relationships\">"
            "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/"
            "officeDocument/2006/relationships/officeDocument\" "
            "Target=\"xl/workbook.xml\"/></Relationships>")
    wb_rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
               "<Relationships xmlns=\"http://schemas.openxmlformats.org/"
               "package/2006/relationships\">"
               "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/"
               "officeDocument/2006/relationships/worksheet\" "
               "Target=\"worksheets/sheet1.xml\"/></Relationships>")
    workbook = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                "<workbook xmlns=\"http://schemas.openxmlformats.org/"
                "spreadsheetml/2006/main\" xmlns:r=\"http://schemas."
                "openxmlformats.org/officeDocument/2006/relationships\">"
                "<sheets><sheet name=\"Report\" sheetId=\"1\" r:id=\"rId1\"/>"
                "</sheets></workbook>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


# ============================================================ printable view
_PRINT_CSS = """
body{font-family:Arial,Helvetica,sans-serif;color:#111;margin:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:18px 0 6px}
.meta{color:#555;font-size:12px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}
th,td{border:1px solid #bbb;padding:4px 8px;text-align:left}
th{background:#eee}td.num,th.num{text-align:right}
.grp{margin:6px 0 6px 18px;border-left:3px solid #888;padding-left:8px}
.tot{font-weight:bold;background:#f4f4f4}
@media print{.noprint{display:none}}
"""


def _print_chart_svg(chart, groups):
    """Simple embedded bar SVG for the printable view.

    The interactive client renderers draw the full chart types; the print
    page embeds this lightweight summary chart (top groups by count or
    first numeric aggregate, capped per CHART_CAPS).
    """
    ctype = (chart or {}).get("type") or "bar"
    cap = CHART_CAPS.get(ctype, 12)
    items = []
    for g in (groups or [])[:cap]:
        aggs = g.get("aggregates") or {}
        val = next((v for v in aggs.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)),
                   g.get("count", 0))
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            val = 0
        items.append((str(g.get("key")), val))
    if not items:
        return ""
    mx = max(v for _, v in items) or 1
    bh, gap, lab_w = 20, 6, 130
    W = lab_w + 400
    H = len(items) * (bh + gap) + 30
    parts = [f'<text x="0" y="14" font-size="13" font-weight="bold">'
             f'{_xml_escape(ctype)} chart</text>']
    for i, (label, val) in enumerate(items):
        w = max(2, int(val / mx * 380))
        y = 20 + i * (bh + gap)
        parts.append(
            f'<rect x="{lab_w}" y="{y}" width="{w}" height="{bh}" '
            f'fill="#2563eb"/>'
            f'<text x="{lab_w - 8}" y="{y + bh - 6}" text-anchor="end" '
            f'font-size="11">{_xml_escape(label[:22])}</text>'
            f'<text x="{lab_w + w + 6}" y="{y + bh - 6}" '
            f'font-size="11">{val}</text>')
    return (f'<svg width="{W}" height="{H}" '
            f'xmlns="http://www.w3.org/2000/svg" role="img">'
            + "".join(parts) + "</svg>")


def _print_groups_html(nodes, depth=0):
    parts = []
    for n in nodes or []:
        aggs = n.get("aggregates") or {}
        agg_txt = ", ".join(f"{k}: {v}" for k, v in aggs.items()
                            if k != "count")
        parts.append(
            f'<div class="grp"><strong>{_xml_escape(str(n.get("key")))}</strong>'
            f' <span class="meta">({n.get("count", 0)} records'
            + (f"; { _xml_escape(agg_txt)}" if agg_txt else "") + ")</span>"
            + _print_groups_html(n.get("children"), depth + 1) + "</div>")
    return "".join(parts)


def _print_block_parts(data):
    """HTML body parts for one (non-joined) report result."""
    e = _xml_escape
    parts = []
    gt = data.get("grand_total") or {}
    gagg = gt.get("aggregates") or {}
    parts.append(
        f"<p class='meta'>Rows: <strong>{data.get('row_count', 0)}</strong>"
        + "".join(f" &middot; {e(k)}: <strong>{e(str(v))}</strong>"
                  for k, v in gagg.items()) + "</p>")
    if data.get("chart", {}).get("type"):
        parts.append(_print_chart_svg(data["chart"], data.get("groups")))
    matrix = data.get("matrix")
    if matrix:
        cols = matrix.get("column_totals") or []
        parts.append("<h2>Matrix</h2><table><tr><th></th>" + "".join(
            f"<th class='num'>{e(' / '.join(str(k) for k in c.get('key') or []))}"
            f"<br><span class='meta'>{c.get('count', 0)}</span></th>"
            for c in cols) + "<th class='num'>Row total</th></tr>")

        def _mrows(nodes):
            out = []
            for n in nodes or []:
                cells = "".join(
                    f"<td class='num'>{c.get('count', 0)}</td>"
                    for c in n.get("columns") or [])
                rt = n.get("row_total") or {}
                out.append(
                    f"<tr><td><strong>{e(str(n.get('key')))}</strong> "
                    f"<span class='meta'>({n.get('count', 0)})</span></td>"
                    + cells + f"<td class='num tot'>{rt.get('count', 0)}</td></tr>")
                out.extend(_mrows(n.get("children")))
            return out

        parts.extend(_mrows(matrix.get("rows")))
        parts.append("</table>")
    if data.get("groups"):
        parts.append("<h2>Groups</h2>" + _print_groups_html(data["groups"]))
    columns = data.get("columns") or []
    if columns:
        parts.append("<h2>Records</h2><table><tr>" + "".join(
            f"<th>{e(str(c))}</th>" for c in columns) + "</tr>")
        for r in data.get("rows") or []:
            parts.append("<tr>" + "".join(
                f"<td>{e('' if r.get(c) is None else str(r.get(c)))}</td>"
                for c in columns) + "</tr>")
        parts.append("</table>")
    return parts


def _print_html(rep, data, user):
    e = _xml_escape
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{e(rep.get('name') or 'Report')}</title>"
        f"<style>{_PRINT_CSS}</style></head><body>",
        f"<h1>{e(rep.get('name') or 'Report')}</h1>",
        f"<p class='meta'>Run by {e(user.get('username') or '')} at "
        f"{e(datetime.now(timezone.utc).isoformat(timespec='seconds'))} UTC"
        f" &middot; Object: {e(str(rep.get('object') or ''))}</p>",
    ]
    if data.get("blocks"):
        for b in data["blocks"]:
            parts.append(f"<h2>{e(b.get('name') or '')}</h2>")
            if b.get("error"):
                parts.append(f"<p class='meta'>Error: {e(b['error'])}</p>")
            else:
                parts.extend(_print_block_parts(b))
    else:
        parts.extend(_print_block_parts(data))
    parts.append("</body></html>")
    return "".join(parts)


# ============================================================ validation
def _validate_group_levels(levels, spec_name, max_levels):
    levels = [levels] if isinstance(levels, str) else (levels or [])
    levels = [lv for lv in levels if lv]
    if len(levels) > max_levels:
        return f"{spec_name} supports at most {max_levels} levels"
    for lv in levels:
        if isinstance(lv, dict):
            if not lv.get("field"):
                return f"{spec_name} entries need a field"
            if lv.get("part") not in DATE_PARTS:
                return (f"Unknown date part '{lv.get('part')}' in "
                        f"{spec_name}; must be one of {', '.join(DATE_PARTS)}")
    return None


def _validate_report_def(body, registry):
    """Validate one report definition (also used per joined-report block)."""
    obj = registry.get_object(body.get("object") or "")
    if not obj:
        return "Unknown object"
    err = _validate_group_levels(body.get("group_by"), "group_by", 3)
    if err:
        return err
    err = _validate_group_levels(body.get("column_group_by"),
                                 "column_group_by", 2)
    if err:
        return err
    dp = body.get("date_parts") or {}
    if not isinstance(dp, dict):
        return "date_parts must be an object mapping field names to date parts"
    for f, p in dp.items():
        if p not in DATE_PARTS:
            return (f"Unknown date part '{p}' in date_parts; must be one of "
                    f"{', '.join(DATE_PARTS)}")
    fsm = body.get("fiscal_start_month")
    if fsm is not None and (not isinstance(fsm, int) or isinstance(fsm, bool)
                            or not 1 <= fsm <= 12):
        return "fiscal_start_month must be an integer 1-12"
    for a in _norm_aggregates({"aggregate": body.get("aggregate"),
                               "aggregates": body.get("aggregates")}):
        if a["func"] not in AGG_FUNCS:
            return f"Unknown aggregate function '{a['func']}'"
    if body.get("group_sort") and body["group_sort"] not in GROUP_SORTS:
        return f"group_sort must be one of {', '.join(GROUP_SORTS)}"
    if body.get("chart") and body["chart"].get("type") \
            and body["chart"]["type"] not in CHART_TYPES:
        return f"chart type must be one of {', '.join(CHART_TYPES)}"
    seen = set()
    for rf in body.get("row_formulas") or []:
        name = rf.get("name")
        if not name or not isinstance(name, str):
            return "row_formulas entries need a name"
        if name in seen:
            return f"Duplicate row_formula name '{name}'"
        seen.add(name)
        try:
            validate_formula_refs(registry, body.get("object"),
                                  rf.get("formula") or {})
        except ValueError as e:
            return f"row_formula '{name}': {e}"
    return None


def _validate_report(body, registry):
    blocks = body.get("blocks")
    if blocks is None:
        return _validate_report_def(body, registry)
    # Joined report: the top-level object is optional (each block has one).
    if not isinstance(blocks, list) or not 2 <= len(blocks) <= 5:
        return "blocks must be a list of 2-5 report definitions"
    for b in blocks:
        if not isinstance(b, dict):
            return "each block must be an object"
        if b.get("blocks"):
            return "blocks cannot be nested"
        err = _validate_report_def(b, registry)
        if err:
            return f"block '{b.get('name') or b.get('object')}': {err}"
    # Top-level extras (chart/folder) still validated when present.
    if body.get("chart") and body["chart"].get("type") \
            and body["chart"]["type"] not in CHART_TYPES:
        return f"chart type must be one of {', '.join(CHART_TYPES)}"
    return None


def _folder_visible(user, folder):
    return (folder.get("visibility") == "shared"
            or folder.get("owner_id") == user.get("id")
            or user.get("profile") == "System Administrator")


def _report_visible(user, rep, folders):
    fid = rep.get("folder_id")
    if not fid:
        return True
    f = folders.get(fid)
    return f is None or _folder_visible(user, f)


# ============================================================ HTTP
def register(app: Flask):
    store, registry, security = app.mf_store, app.mf_registry, app.mf_security

    def _folders():
        return {f["id"]: f for f in store.config_all("mf_folders")}

    # ------------------------------------------------------------ folders (R6/D6)
    @app.get("/api/folders")
    @require_auth
    def list_folders():
        user = request.mf_user
        kind = request.args.get("kind")
        out = [f for f in store.config_all("mf_folders")
               if (not kind or f.get("kind") == kind)
               and _folder_visible(user, f)]
        return jsonify(sorted(out, key=lambda f: f.get("name") or ""))

    @app.post("/api/folders")
    @require_auth
    def create_folder():
        user = request.mf_user
        body = request.json or {}
        name = (body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name is required"}), 422
        kind = body.get("kind") or "report"
        if kind not in ("report", "dashboard"):
            return jsonify({"error": "kind must be report or dashboard"}), 422
        visibility = body.get("visibility") or "private"
        if visibility not in ("private", "shared"):
            return jsonify({"error": "visibility must be private or shared"}), 422
        if visibility == "shared" and user.get("profile") != "System Administrator":
            return jsonify({"error": "Only admins can create shared folders"}), 403
        fid = store.config_put("mf_folders", {
            "name": name, "kind": kind, "visibility": visibility,
            "owner_id": user["id"]})
        _audit("create", "folder", name)
        return jsonify(store.config_get("mf_folders", fid)), 201

    @app.put("/api/folders/<fid>")
    @require_auth
    def update_folder(fid):
        user = request.mf_user
        f = store.config_get("mf_folders", fid)
        if not f:
            return jsonify({"error": "Unknown folder"}), 404
        if f.get("owner_id") != user["id"] \
                and user.get("profile") != "System Administrator":
            return jsonify({"error": "Forbidden"}), 403
        body = request.json or {}
        if body.get("name"):
            f["name"] = body["name"].strip()
        if body.get("visibility") in ("private", "shared"):
            if body["visibility"] == "shared" and user.get("profile") != \
                    "System Administrator" and f.get("visibility") != "shared":
                return jsonify({"error": "Only admins can share folders"}), 403
            f["visibility"] = body["visibility"]
        store.config_put("mf_folders", f)
        return jsonify(f)

    @app.delete("/api/folders/<fid>")
    @require_auth
    def delete_folder(fid):
        user = request.mf_user
        f = store.config_get("mf_folders", fid)
        if not f:
            return jsonify({"error": "Unknown folder"}), 404
        if f.get("owner_id") != user["id"] \
                and user.get("profile") != "System Administrator":
            return jsonify({"error": "Forbidden"}), 403
        kind_key = "mf_reports" if f.get("kind") == "report" else "mf_dashboards"
        used = [d for d in store.config_all(kind_key)
                if d.get("folder_id") == fid]
        if used:
            return jsonify({"error": f"Folder is not empty ({len(used)} items). "
                                     "Move them out first."}), 422
        store.config_delete("mf_folders", fid)
        _audit("delete", "folder", f["name"])
        return jsonify({"ok": True})

    # ------------------------------------------------------------ report types
    def _find_report_type(name_or_id):
        if not name_or_id:
            return None
        return next((t for t in store.config_all("mf_report_types")
                     if t.get("name") == name_or_id
                     or t.get("id") == name_or_id), None)

    def _validate_join_field(pdef, rdef, via):
        """via_field must be a relationship field joining the two objects."""
        for owner, target in ((pdef, rdef.get("name")),
                              (rdef, pdef.get("name"))):
            for f in owner.get("fields", []) or []:
                if f.get("name") == via and f.get("type") in (
                        "Lookup", "MasterDetail", "PolymorphicLookup"):
                    ref = f.get("reference_to")
                    refs = ref if isinstance(ref, list) else [ref]
                    if target in refs:
                        return True
        return False

    def _validate_report_type(body, registry):
        name = (body.get("name") or "").strip()
        if not name:
            return "name is required"
        pdef = registry.get_object(body.get("primary_object") or "")
        if not pdef:
            return "Unknown primary_object"
        for rel in body.get("related") or []:
            rdef = registry.get_object(rel.get("object") or "")
            if not rdef:
                return f"Unknown related object '{rel.get('object')}'"
            if not _validate_join_field(pdef, rdef, rel.get("via_field")):
                return (f"via_field '{rel.get('via_field')}' does not join "
                        f"{pdef.get('name')} and {rdef.get('name')}")
        return None

    @app.get("/api/report-types")
    @require_auth
    def list_report_types():
        return jsonify(sorted(store.config_all("mf_report_types"),
                              key=lambda t: t.get("name") or ""))

    @app.post("/api/admin/report-types")
    @require_auth
    @require_admin
    def create_report_type():
        body = request.json or {}
        err = _validate_report_type(body, registry)
        if err:
            return jsonify({"error": err}), 422
        tid = store.config_put("mf_report_types", {
            "name": (body.get("name") or "").strip(),
            "label": body.get("label") or body.get("name"),
            "primary_object": body.get("primary_object"),
            "related": body.get("related") or [],
            "default_columns": body.get("default_columns") or [],
            "description": body.get("description") or "",
            "created_by": request.mf_user["id"],
        })
        _audit("create", "report_types", body.get("name") or tid)
        return jsonify(store.config_get("mf_report_types", tid)), 201

    @app.put("/api/admin/report-types/<tid>")
    @require_auth
    @require_admin
    def update_report_type(tid):
        cur = store.config_get("mf_report_types", tid)
        if not cur:
            return jsonify({"error": "Unknown report type"}), 404
        body = request.json or {}
        merged = {**cur, **{k: v for k, v in body.items() if k != "id"}}
        err = _validate_report_type(merged, registry)
        if err:
            return jsonify({"error": err}), 422
        store.config_put("mf_report_types", merged)
        _audit("update", "report_types", merged.get("name") or tid)
        return jsonify(store.config_get("mf_report_types", tid))

    @app.delete("/api/admin/report-types/<tid>")
    @require_auth
    @require_admin
    def delete_report_type(tid):
        if not store.config_delete("mf_report_types", tid):
            return jsonify({"error": "Unknown report type"}), 404
        _audit("delete", "report_types", tid)
        return jsonify({"ok": True})

    # ------------------------------------------------------------ reports
    @app.get("/api/reports")
    @require_auth
    def list_reports():
        user = request.mf_user
        folders = _folders()
        return jsonify([r for r in store.config_all("mf_reports")
                        if _report_visible(user, r, folders)])

    @app.post("/api/admin/reports")
    @require_auth
    @require_admin
    def create_report():
        body = request.json or {}
        rtype = _find_report_type(body.get("report_type")
                                or body.get("report_type_id"))
        if (body.get("report_type") or body.get("report_type_id")) \
                and not rtype:
            return jsonify({"error": "Unknown report_type"}), 422
        doc = {
            "name": body.get("name") or "Report",
            "object": body.get("object")
            or (rtype or {}).get("primary_object"),
            "columns": body.get("columns")
            or (rtype or {}).get("default_columns") or [],
            "filters": body.get("filters") or {},
            "sort": body.get("sort") or [],
            "group_by": body.get("group_by"),
            "column_group_by": body.get("column_group_by"),
            "fiscal_start_month": body.get("fiscal_start_month"),
            "group_sort": body.get("group_sort") or "count_desc",
            "aggregate": body.get("aggregate"),
            "aggregates": body.get("aggregates"),
            "bucket": body.get("bucket"),
            "cross_filters": body.get("cross_filters") or [],
            "summary_formulas": body.get("summary_formulas") or [],
            "row_formulas": body.get("row_formulas") or [],
            "highlight": body.get("highlight") or [],
            "chart": body.get("chart") or {},
            "folder_id": body.get("folder_id"),
            "run_as_user": body.get("run_as_user"),
            "active": body.get("active", True),
            "blocks": body.get("blocks"),
            "report_type": (rtype or {}).get("name"),
            "related": (rtype or {}).get("related") or [],
            "created_by": request.mf_user["id"],
        }
        err = _validate_report(doc, registry)
        if err:
            return jsonify({"error": err}), 422
        if doc.get("folder_id") and not store.config_get(
                "mf_folders", doc["folder_id"]):
            return jsonify({"error": "Unknown folder"}), 422
        rid = store.config_put("mf_reports", doc)
        _audit("create", "reports", doc.get("name") or rid)
        return jsonify(store.config_get("mf_reports", rid)), 201

    @app.put("/api/admin/reports/<rep_id>")
    @require_auth
    @require_admin
    def update_report(rep_id):
        rep = store.config_get("mf_reports", rep_id)
        if not rep:
            return jsonify({"error": "Unknown report"}), 404
        body = request.json or {}
        merged = {**rep, **{k: v for k, v in body.items() if k != "id"}}
        err = _validate_report(merged, registry)
        if err:
            return jsonify({"error": err}), 422
        if merged.get("folder_id") and not store.config_get(
                "mf_folders", merged["folder_id"]):
            return jsonify({"error": "Unknown folder"}), 422
        store.config_put("mf_reports", merged)
        _audit("update", "reports", merged.get("name") or rep_id)
        return jsonify(store.config_get("mf_reports", rep_id))

    @app.delete("/api/admin/reports/<rep_id>")
    @require_auth
    @require_admin
    def delete_report(rep_id):
        if not store.config_delete("mf_reports", rep_id):
            return jsonify({"error": "Unknown report"}), 404
        _audit("delete", "reports", rep_id)
        return jsonify({"ok": True})

    @app.post("/api/admin/reports/<rep_id>/clone")
    @require_auth
    @require_admin
    def clone_report(rep_id):
        rep = store.config_get("mf_reports", rep_id)
        if not rep:
            return jsonify({"error": "Unknown report"}), 404
        body = request.json or {}
        clone = {k: v for k, v in rep.items() if k != "id"}
        clone["name"] = body.get("name") or f"{rep.get('name')} (copy)"
        clone["created_by"] = request.mf_user["id"]
        nid = store.config_put("mf_reports", clone)
        _audit("clone", "reports", clone["name"])
        return jsonify(store.config_get("mf_reports", nid)), 201

    @app.get("/api/reports/<rep_id>/run")
    @require_auth
    def run_report(rep_id):
        user = request.mf_user
        rep = store.config_get("mf_reports", rep_id)
        if not rep or not _report_visible(user, rep, _folders()):
            return jsonify({"error": "Unknown report"}), 404
        drill_field = request.args.get("drill_field")
        drill_value = request.args.get("drill_value")
        extra = None
        if drill_field:
            extra = [{"field": drill_field, "op": "==", "value": drill_value}]
        data = run_report_data(store, registry, security, user, rep,
                               extra_filters=extra,
                               page=request.args.get("page", 1),
                               page_size=request.args.get("page_size", 500))
        if data.get("error"):
            return jsonify(data), 404
        return jsonify(data)

    @app.get("/api/reports/<rep_id>/export")
    @require_auth
    def export_report(rep_id):
        user = request.mf_user
        rep = store.config_get("mf_reports", rep_id)
        if not rep or not _report_visible(user, rep, _folders()):
            return jsonify({"error": "Unknown report"}), 404
        fmt = (request.args.get("format") or "csv").lower()
        if fmt not in ("csv", "xlsx"):
            return jsonify({"error": "format must be csv or xlsx"}), 422
        data = run_report_data(store, registry, security, user, rep,
                               page=1, page_size=10000)
        if data.get("error"):
            return jsonify(data), 404
        if data.get("blocks"):
            return jsonify({"error": "Export is not supported for joined "
                                     "reports; export each block's report "
                                     "instead."}), 422
        cols = data.get("columns") or _export_columns(rep, data["rows"])
        fname = "".join(c if c.isalnum() or c in ("-", "_") else "_"
                        for c in (rep.get("name") or "report"))[:60] or "report"
        if fmt == "xlsx":
            payload = _xlsx_bytes(cols, data["rows"])
            mime = ("application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet")
            fname += ".xlsx"
        else:
            payload = _csv_bytes(cols, data["rows"])
            mime = "text/csv"
            fname += ".csv"
        return Response(payload, mimetype=mime,
                        headers={"Content-Disposition":
                                 f"attachment; filename={fname}"})

    @app.get("/api/reports/<rep_id>/print")
    @require_auth
    def print_report(rep_id):
        """Print-friendly HTML rendering of a report run.

        Self-contained (inline CSS), driven by the normal run path so
        sharing and field-level security are enforced for the requesting
        user. Includes the grand total, group tree, matrix (when present),
        the data table, and an embedded chart SVG.
        """
        user = request.mf_user
        rep = store.config_get("mf_reports", rep_id)
        if not rep or not _report_visible(user, rep, _folders()):
            return jsonify({"error": "Unknown report"}), 404
        data = run_report_data(store, registry, security, user, rep,
                               page=1, page_size=2000)
        if data.get("error"):
            return jsonify(data), 404
        return Response(_print_html(rep, data, user), mimetype="text/html")

    # ------------------------------------------------------------ dashboards
    def _dash_visible(user, dash, folders):
        fid = dash.get("folder_id")
        if not fid:
            return True
        f = folders.get(fid)
        return f is None or _folder_visible(user, f)

    def _norm_widget_filters(wf):
        """Normalize per-widget filters to a list of expressions.

        The dashboard builder UI sends either a single expression dict
        ({and:[...]} or one condition) or a list; both are accepted.
        """
        if isinstance(wf, dict):
            return [wf]
        return wf or []

    def _dash_filter_applies(ef, obj_def):
        """A global dashboard filter applies to a widget only when every
        field it references exists on the widget's object (relationship
        heads like "Account" in "Account.Name" count as existing).

        A filter on a foreign field must SKIP the widget instead of
        silently zeroing it (an unevaluated None comparison matches
        nothing).
        """
        if not obj_def:
            return False
        fields = set()
        if isinstance(ef, dict) and "field" in ef and not any(
                k in ef for k in ("and", "or", "not")):
            if isinstance(ef.get("field"), str):
                fields.add(ef["field"])
        else:
            _walk_expr_fields(ef, fields)
        known = {"Id", "CreatedDate", "LastModifiedDate", "OwnerId",
                 "CreatedById"}
        for f in obj_def.get("fields", []) or []:
            known.add(f.get("name"))
            if f.get("type") in ("Lookup", "MasterDetail",
                                 "PolymorphicLookup") and f.get("name"):
                known.add(f["name"])
                if f["name"].endswith("Id"):
                    known.add(f["name"][:-2])
        for fld in fields:
            if not isinstance(fld, str):
                continue
            if fld.split(".")[0] not in known:
                return False
        return True

    def _validate_dashboard_body(body, widgets_key="widgets"):
        widgets = body.get(widgets_key) or []
        if len(widgets) > MAX_WIDGETS:
            return f"At most {MAX_WIDGETS} components"
        for w in widgets:
            if w.get("type") and w["type"] not in WIDGET_TYPES:
                return f"Unknown widget type '{w['type']}'"
            wf = w.get("filters")
            # Accept a single expression dict too (the UI sends {and:[...]}
            # or one condition); normalize to a list.
            if isinstance(wf, dict):
                w["filters"] = wf = [wf]
            if wf is not None and not isinstance(wf, list):
                return "widget filters must be a list of filter expressions"
        sched = body.get("refresh_schedule")
        if sched and sched not in REFRESH_SCHEDULES:
            return (f"refresh_schedule must be one of "
                    f"{', '.join(REFRESH_SCHEDULES)}")
        return None

    @app.get("/api/dashboards")
    @require_auth
    def list_dashboards():
        user = request.mf_user
        folders = _folders()
        return jsonify([d for d in store.config_all("mf_dashboards")
                        if _dash_visible(user, d, folders)])

    @app.post("/api/dashboards")
    @require_auth
    @require_admin
    def create_dashboard():
        body = request.json or {}
        err = _validate_dashboard_body(body)
        if err:
            return jsonify({"error": err}), 422
        widgets = body.get("widgets") or []
        if body.get("folder_id") and not store.config_get(
                "mf_folders", body["folder_id"]):
            return jsonify({"error": "Unknown folder"}), 422
        rid = store.config_put("mf_dashboards", {
            "name": body.get("name") or "Dashboard",
            "widgets": widgets,
            "filters": (body.get("filters") or [])[:3],
            "folder_id": body.get("folder_id"),
            "run_as": body.get("run_as") or "viewer",
            "refresh_schedule": body.get("refresh_schedule"),
            "last_run_at": None,
        })
        _audit("create", "dashboards", body.get("name") or rid)
        return jsonify(store.config_get("mf_dashboards", rid)), 201

    @app.put("/api/dashboards/<did>")
    @require_auth
    @require_admin
    def update_dashboard(did):
        dash = store.config_get("mf_dashboards", did)
        if not dash:
            return jsonify({"error": "Unknown dashboard"}), 404
        body = request.json or {}
        widgets = body.get("widgets", dash.get("widgets") or [])
        err = _validate_dashboard_body({**body, "widgets": widgets})
        if err:
            return jsonify({"error": err}), 422
        dash["name"] = body.get("name", dash.get("name"))
        dash["widgets"] = widgets
        dash["filters"] = (body.get("filters", dash.get("filters") or []))[:3]
        if "folder_id" in body:
            if body["folder_id"] and not store.config_get(
                    "mf_folders", body["folder_id"]):
                return jsonify({"error": "Unknown folder"}), 422
            dash["folder_id"] = body["folder_id"]
        if "run_as" in body:
            dash["run_as"] = body["run_as"] or "viewer"
        if "refresh_schedule" in body:
            dash["refresh_schedule"] = body["refresh_schedule"] or None
        store.config_put("mf_dashboards", dash)
        _audit("update", "dashboards", dash["name"])
        return jsonify(dash)

    @app.delete("/api/dashboards/<did>")
    @require_auth
    @require_admin
    def delete_dashboard(did):
        if not store.config_delete("mf_dashboards", did):
            return jsonify({"error": "Unknown dashboard"}), 404
        _audit("delete", "dashboards", did)
        return jsonify({"ok": True})

    def _resolve_dash_user(dash, user):
        """D5: dashboards run as the viewer by default, or as a set user."""
        run_as = dash.get("run_as") or "viewer"
        if run_as != "viewer" and user.get("profile") == "System Administrator":
            try:
                u = security.get_user(run_as)
                if u:
                    return u
            except Exception:
                pass
        return user

    @app.post("/api/dashboards/<did>/run")
    @require_auth
    def run_dashboard(did):
        """D8: run every widget's report in one request (no N+1 fetches)."""
        user = request.mf_user
        dash = store.config_get("mf_dashboards", did)
        if not dash or not _dash_visible(user, dash, _folders()):
            return jsonify({"error": "Unknown dashboard"}), 404
        body = request.json or {}
        # Preview mode (customize tab): draft widgets/filters may come in the body.
        widgets = body["widgets"] if isinstance(body.get("widgets"), list) \
            else dash.get("widgets") or []
        if len(widgets) > MAX_WIDGETS:
            return jsonify({"error": f"At most {MAX_WIDGETS} components"}), 422
        run_user = _resolve_dash_user(dash, user)
        dash_filters = body["filters"] if isinstance(body.get("filters"), list) \
            else dash.get("filters") or []
        out = []
        for w in widgets:
            rep = store.config_get("mf_reports", w.get("report_id") or "")
            if not rep:
                out.append({"widget": w, "report": None,
                            "error": "Unknown report"})
                continue
            # Security: a widget must not expose a report the effective
            # user cannot see (e.g. a report in a private folder).
            if not _report_visible(run_user, rep, _folders()):
                out.append({"widget": w, "report": None,
                            "error": "No access to report"})
                continue
            # Global filters apply per widget only when their fields exist
            # on the widget's object; otherwise the widget is left
            # unfiltered (never silently zeroed). Per-widget filters AND
            # with the report's own filters for that widget only.
            obj_def = registry.get_object(rep.get("object"))
            scoped = [ef for ef in dash_filters
                      if _dash_filter_applies(ef, obj_def)]
            widget_extra = scoped + _norm_widget_filters(w.get("filters"))
            data = run_report_data(
                store, registry, security, run_user, rep,
                extra_filters=widget_extra,
                page=1, page_size=500,
                group_by_override=w.get("group_by", _SENTINEL),
                aggregates_override=(_norm_aggregates(
                    {"aggregate": w.get("aggregate"),
                     "aggregates": w.get("aggregates")})
                    if ("aggregate" in w or "aggregates" in w) else _SENTINEL))
            gb = rep.get("group_by")
            gb = gb if isinstance(gb, list) else ([gb] if gb else [])
            out.append({"widget": w,
                        "report": {"id": rep.get("id"), "name": rep.get("name"),
                                   "object": rep.get("object"),
                                   "group_field": (gb or [None])[0]},
                        "data": data})
        dash["last_run_at"] = datetime.now(timezone.utc).isoformat()
        store.config_put("mf_dashboards", dash)
        return jsonify({"dashboard": dash.get("name"), "widgets": out,
                        "run_as": run_user.get("username"),
                        "last_run_at": dash["last_run_at"]})
