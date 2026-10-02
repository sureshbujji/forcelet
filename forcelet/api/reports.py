"""Reports and dashboards. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.

Report documents (mf_reports) support:
  name, object, columns[], filters (expression JSON), sort[{field,dir}],
  group_by: str | [str] (up to 3 levels), group_sort,
  aggregate (legacy {func, field}) | aggregates[{func, field, alias}],
  bucket {field, buckets[{name, from, to}]},
  cross_filters[{object, via, mode}], summary_formulas[{name, label, formula}],
  highlight[{field, op, value, color}], chart{type},
  folder_id, run_as_user, active.

Dashboard documents (mf_dashboards) support:
  name, widgets[{report_id, type, w, group_by?, aggregate?/aggregates?}],
  filters[] (up to 3 global), folder_id, run_as ("viewer" | user id).
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from xml.sax.saxutils import escape as _xml_escape

from flask import Flask, jsonify, request, Response

from .. import automation
from .. import crypto as _crypto
from .. import datamodel as _datamodel
from ..expressions import eval_expr, record_context
from ..field_types import mask_secret
from ._shared import (
    _audit, require_admin, require_auth, ctx,
)


_SENTINEL = object()
AGG_FUNCS = ("sum", "avg", "min", "max", "count", "count_distinct")
GROUP_SORTS = ("count_desc", "count_asc", "label_asc")
CHART_TYPES = ("bar", "line", "donut")
WIDGET_TYPES = ("bar", "line", "donut", "stat", "table", "gauge", "funnel", "scatter")
MAX_WIDGETS = 20


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


def _enrich_related(store, obj, raw_records, rows, relpaths):
    """Null-safe parent-lookup resolution (R4). Mutates rows in place."""
    by_lookup: dict = {}
    for rp in relpaths:
        info = _rel_info(obj, rp)
        if info:
            by_lookup.setdefault(info[0], {})[(info[1], info[2])] = rp
    if not by_lookup:
        return
    cache: dict = {}
    for raw, row in zip(raw_records, rows):
        for lkp, subs in by_lookup.items():
            pid = raw.get(lkp)
            for (pobj, pfield), rp in subs.items():
                if pid:
                    if (pobj, pid) not in cache:
                        try:
                            cache[(pobj, pid)] = store.get(pobj, pid)
                        except Exception:
                            cache[(pobj, pid)] = None
                    prow = cache[(pobj, pid)]
                else:
                    prow = None
                row[rp] = (prow or {}).get(pfield)


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
    gb = rep.get("group_by")
    if isinstance(gb, str):
        return [gb] if gb else []
    if isinstance(gb, list):
        return [g for g in gb if g][:3]
    return []


def _bucket_value(bucket, value):
    for b in (bucket or {}).get("buckets") or []:
        lo, hi = b.get("from"), b.get("to")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (lo is None or value >= lo) and (hi is None or value <= hi):
                return b.get("name") or "(blank)"
    return "(other)"


def _group_key(row, level, bucket):
    if level == "_bucket" and bucket:
        return _bucket_value(bucket, row.get(bucket.get("field")))
    v = row.get(level)
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


def _eval_summary_formulas(formulas, aggs_dict):
    out = []
    for sf in formulas or []:
        try:
            val = eval_expr(_rewrite_agg_refs(sf.get("formula") or {}),
                            dict(aggs_dict))
            if isinstance(val, float):
                val = round(val, 4)
        except Exception:
            val = None
        out.append({"name": sf.get("name"), "label": sf.get("label")
                    or sf.get("name"), "value": val})
    return out


def _build_groups(rows, levels, aggs, sort, bucket, formulas):
    if not levels:
        return []
    buckets, order = {}, []
    for r in rows:
        k = _group_key(r, levels[0], bucket)
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
                "aggregates": node_aggs,
                "formulas": _eval_summary_formulas(formulas, node_aggs)}
        children = _build_groups(b["rows"], levels[1:], aggs, sort, bucket,
                                 formulas)
        if children:
            node["children"] = children
        nodes.append(node)
    if sort == "count_asc":
        nodes.sort(key=lambda n: n["count"])
    elif sort == "label_asc":
        nodes.sort(key=lambda n: str(n["key"]))
    else:  # count_desc (default, matches legacy behavior)
        nodes.sort(key=lambda n: n["count"], reverse=True)
    return nodes


def run_report_data(store, registry, security, user, rep, extra_filters=None,
                    page=1, page_size=500, group_by_override=_SENTINEL,
                    aggregates_override=_SENTINEL):
    """Run a report definition. Usable from request handlers and the scheduler.

    extra_filters: [{"field","op","value"}] applied with AND (dashboard filters,
    drill-down). group_by_override/aggregates_override: per-widget config (D4).
    Returns a JSON-serializable dict (or {"error": ...}).
    """
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
    _enrich_related(store, obj, raw_records, rows,
                    _collect_relpaths(rep, extra_filters))
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
    formulas = rep.get("summary_formulas") or []
    tree = _build_groups(rows, levels, aggs, sort_mode, bucket, formulas)
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
    return {
        "report": rep.get("name"), "row_count": total,
        "columns": rep.get("columns") or [],
        "rows": page_rows, "page": page, "page_size": page_size,
        "pages": pages, "groups": flat, "group_tree": tree,
        "grand_total": {"count": total, "aggregates": grand_aggs,
                        "formulas": _eval_summary_formulas(formulas, grand_aggs)},
        "highlight": rep.get("highlight") or [],
        "chart": rep.get("chart") or {},
        "bucket": bucket or {},
    }


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


# ============================================================ validation
def _validate_report(body, registry):
    obj = registry.get_object(body.get("object") or "")
    if not obj:
        return "Unknown object"
    gb = body.get("group_by")
    levels = [gb] if isinstance(gb, str) else (gb or [])
    if len([lv for lv in levels if lv]) > 3:
        return "group_by supports at most 3 levels"
    for a in _norm_aggregates({"aggregate": body.get("aggregate"),
                               "aggregates": body.get("aggregates")}):
        if a["func"] not in AGG_FUNCS:
            return f"Unknown aggregate function '{a['func']}'"
    if body.get("group_sort") and body["group_sort"] not in GROUP_SORTS:
        return f"group_sort must be one of {', '.join(GROUP_SORTS)}"
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
        err = _validate_report(body, registry)
        if err:
            return jsonify({"error": err}), 422
        if body.get("folder_id") and not store.config_get(
                "mf_folders", body["folder_id"]):
            return jsonify({"error": "Unknown folder"}), 422
        rid = store.config_put("mf_reports", {
            "name": body.get("name") or "Report",
            "object": body.get("object"),
            "columns": body.get("columns") or [],
            "filters": body.get("filters") or {},
            "sort": body.get("sort") or [],
            "group_by": body.get("group_by"),
            "group_sort": body.get("group_sort") or "count_desc",
            "aggregate": body.get("aggregate"),
            "aggregates": body.get("aggregates"),
            "bucket": body.get("bucket"),
            "cross_filters": body.get("cross_filters") or [],
            "summary_formulas": body.get("summary_formulas") or [],
            "highlight": body.get("highlight") or [],
            "chart": body.get("chart") or {},
            "folder_id": body.get("folder_id"),
            "run_as_user": body.get("run_as_user"),
            "active": body.get("active", True),
            "created_by": request.mf_user["id"],
        })
        _audit("create", "reports", body.get("name") or rid)
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
        cols = _export_columns(rep, data["rows"])
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

    # ------------------------------------------------------------ dashboards
    def _dash_visible(user, dash, folders):
        fid = dash.get("folder_id")
        if not fid:
            return True
        f = folders.get(fid)
        return f is None or _folder_visible(user, f)

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
        widgets = body.get("widgets") or []
        if len(widgets) > MAX_WIDGETS:
            return jsonify({"error": f"At most {MAX_WIDGETS} components"}), 422
        for w in widgets:
            if w.get("type") and w["type"] not in WIDGET_TYPES:
                return jsonify({"error": f"Unknown widget type '{w['type']}'"}), 422
        if body.get("folder_id") and not store.config_get(
                "mf_folders", body["folder_id"]):
            return jsonify({"error": "Unknown folder"}), 422
        rid = store.config_put("mf_dashboards", {
            "name": body.get("name") or "Dashboard",
            "widgets": widgets,
            "filters": (body.get("filters") or [])[:3],
            "folder_id": body.get("folder_id"),
            "run_as": body.get("run_as") or "viewer",
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
        if len(widgets) > MAX_WIDGETS:
            return jsonify({"error": f"At most {MAX_WIDGETS} components"}), 422
        for w in widgets:
            if w.get("type") and w["type"] not in WIDGET_TYPES:
                return jsonify({"error": f"Unknown widget type '{w['type']}'"}), 422
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
            data = run_report_data(
                store, registry, security, run_user, rep,
                extra_filters=dash_filters,
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
        return jsonify({"dashboard": dash.get("name"), "widgets": out,
                        "run_as": run_user.get("username")})
