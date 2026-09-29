"""Forcelet Assistant — a rule-based conversational helper over CRM data.

Answers questions about records, summarizes pipeline/service activity, and can
perform simple actions (e.g. create a task). Deterministic, no external LLM.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import re


def _visible(security, user, obj_name, records):
    return [r for r in records if security.can_see_record(user, r, obj_name)]


def _name_of(rec: dict) -> str:
    for k in ("Name", "Title", "Subject", "OrderNumber", "DeliveryNumber",
              "ContractNumber", "WorkOrderNumber", "ArticleNumber"):
        if rec.get(k):
            return str(rec[k])
    return rec.get("id", "?")[:8]


def _all(store, obj_name, limit=1000):
    try:
        return store.query(obj_name, owner_ids=None, limit=limit)
    except Exception:
        return []


def _fmt_money(v):
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def answer(store, registry, security, user, message: str) -> dict:
    """Return {"reply": str, "data": {...}} for a user message."""
    msg = (message or "").strip()
    low = msg.lower()
    uid = user["id"]

    def q(obj):
        return _visible(security, user, obj, _all(store, obj))

    # ---------------------------------------------------------- greeting/help
    if re.fullmatch(r"(hi|hello|hey|yo|good (morning|afternoon|evening))\b.*", low):
        return {"reply": (
            f"Hi {user.get('username', 'there')}! I'm the Forcelet Assistant. "
            "Ask me things like:\n"
            "- \"show my open cases\"\n"
            "- \"what's the sales pipeline?\"\n"
            "- \"how many vehicles are in stock?\"\n"
            "- \"summarize order ORD-000001\"\n"
            "- \"create task: call Acme about renewal\""),
            "data": {}}

    if low in ("help", "what can you do", "commands"):
        return {"reply": (
            "I can look up records and take simple actions:\n"
            "- **Pipeline**: \"sales pipeline\", \"open opportunities\"\n"
            "- **Service**: \"my open cases\", \"urgent cases\", \"work orders\"\n"
            "- **Automotive**: \"vehicles in stock\", \"recent orders\", \"deliveries\"\n"
            "- **Counts**: \"how many accounts / contacts / leads\"\n"
            "- **Summarize**: \"summarize <object> <name or id>\"\n"
            "- **Act**: \"create task: <subject>\""),
            "data": {}}

    # ---------------------------------------------------------- service
    if re.search(r"\bmy open cases\b|\bopen cases\b|\bcases\b", low) and "summar" not in low:
        cases = [c for c in q("Case") if str(c.get("Status", "")).lower() not in
                 ("closed", "resolved", "completed")]
        mine = [c for c in cases if c.get("owner_id") == uid]
        shown = mine or cases
        lines = [f"- {c.get('CaseNumber', _name_of(c))}: {c.get('Subject', '')} "
                 f"[{c.get('Status')}, {c.get('Priority')}]" for c in shown[:10]]
        who = "your" if mine else "all visible"
        return {"reply": f"Found {len(shown)} open cases ({who}):\n" + "\n".join(lines),
                "data": {"object": "Case", "ids": [c["id"] for c in shown[:10]]}}

    if re.search(r"\burgent|critical|breach|sla", low):
        cases = [c for c in q("Case")
                 if str(c.get("Priority", "")).lower() in ("high", "critical", "urgent")]
        lines = [f"- {c.get('CaseNumber', _name_of(c))}: {c.get('Subject', '')} "
                 f"[{c.get('Status')}]" for c in cases[:10]]
        return {"reply": f"{len(cases)} urgent/high-priority cases:\n" + "\n".join(lines) or
                          "No urgent cases right now. Nice.",
                "data": {"object": "Case", "ids": [c["id"] for c in cases[:10]]}}

    if re.search(r"\bwork orders?\b", low):
        wos = [w for w in q("WorkOrder") if str(w.get("Status", "")).lower()
               not in ("completed", "cancelled")]
        lines = [f"- {w.get('WorkOrderNumber', _name_of(w))}: {w.get('Subject', '')} "
                 f"[{w.get('Status')}, {w.get('Priority')}]" for w in wos[:10]]
        return {"reply": f"{len(wos)} open work orders:\n" + "\n".join(lines),
                "data": {"object": "WorkOrder", "ids": [w["id"] for w in wos[:10]]}}

    # ---------------------------------------------------------- pipeline
    if re.search(r"\bpipeline\b|\bopen opportunities\b|\bforecast", low):
        opps = [o for o in q("Opportunity") if str(o.get("Stage", "")).lower()
                not in ("closed won", "closed lost")]
        by_stage: dict = {}
        for o in opps:
            st = o.get("Stage") or "Unknown"
            by_stage.setdefault(st, {"count": 0, "amount": 0.0})
            by_stage[st]["count"] += 1
            try:
                by_stage[st]["amount"] += float(o.get("Amount") or 0)
            except (TypeError, ValueError):
                pass
        lines = [f"- {st}: {v['count']} opps, {_fmt_money(v['amount'])}"
                 for st, v in by_stage.items()]
        total = sum(v["amount"] for v in by_stage.values())
        return {"reply": f"Open pipeline: {len(opps)} opportunities, "
                         f"{_fmt_money(total)} total.\n" + "\n".join(lines),
                "data": {"object": "Opportunity",
                         "ids": [o["id"] for o in opps[:10]]}}

    # ---------------------------------------------------------- automotive
    if re.search(r"\bvehicles? in stock\b|\bin stock\b", low):
        vehs = [v for v in q("Vehicle") if str(v.get("Status", "")).lower() == "in stock"]
        lines = [f"- {v.get('VIN', _name_of(v))} ({v.get('Color', '')})"
                 for v in vehs[:10]]
        return {"reply": f"{len(vehs)} vehicles in stock:\n" + "\n".join(lines),
                "data": {"object": "Vehicle", "ids": [v["id"] for v in vehs[:10]]}}

    if re.search(r"\border|deliver", low):
        orders = sorted(q("Order"), key=lambda r: r.get("OrderNumber", ""),
                        reverse=True)[:5]
        lines = [f"- {o.get('OrderNumber')}: {o.get('Status')} "
                 f"{_fmt_money(o.get('TotalAmount'))}" for o in orders]
        return {"reply": "Recent orders:\n" + "\n".join(lines),
                "data": {"object": "Order", "ids": [o["id"] for o in orders]}}

    # ---------------------------------------------------------- counts
    m = re.search(r"how many (\w+)", low)
    if m:
        word = m.group(1).rstrip("s")
        mapping = {"account": "Account", "contact": "Contact", "lead": "Lead",
                   "opportunit": "Opportunity", "opportunities": "Opportunity",
                   "case": "Case", "task": "Task", "vehicle": "Vehicle",
                   "order": "Order", "campaign": "Campaign", "contract": "Contract",
                   "article": "KnowledgeArticle", "event": "Event"}
        obj_name = mapping.get(word)
        if obj_name and registry.get_object(obj_name):
            n = len(q(obj_name))
            return {"reply": f"There are {n} {obj_name} records visible to you.",
                    "data": {"object": obj_name, "count": n}}

    # ---------------------------------------------------------- summarize
    m = re.search(r"summar\w+\s+(\w+)\s+(.+)", low)
    if m:
        obj_word, needle = m.group(1), m.group(2).strip().strip("'\"")
        candidates = [o["name"] for o in registry.list_objects()
                      if o["name"].lower().startswith(obj_word.rstrip("s"))]
        if candidates:
            obj_name = candidates[0]
            recs = q(obj_name)
            needle_l = needle.lower()
            hit = next((r for r in recs
                        if needle_l in str(r.get("id", "")).lower()
                        or any(needle_l in str(r.get(k, "")).lower()
                               for k in ("Name", "Title", "Subject", "OrderNumber",
                                         "DeliveryNumber", "ContractNumber",
                                         "WorkOrderNumber", "ArticleNumber", "VIN")
                               if r.get(k))), None)
            if hit:
                obj = registry.get_object(obj_name)
                fields = [(f["label"], hit.get(f["name"]))
                          for f in obj.get("fields", [])
                          if hit.get(f["name"]) not in (None, "", False)]
                lines = [f"- **{label}**: {val}" for label, val in fields[:12]]
                return {"reply": f"**{obj_name}** {_name_of(hit)}:\n" + "\n".join(lines),
                        "data": {"object": obj_name, "ids": [hit["id"]]}}
            return {"reply": f"I couldn't find a {obj_name} matching '{needle}'.",
                    "data": {}}

    # ---------------------------------------------------------- create task
    m = re.search(r"create task[:\s]+(.+)", low)
    if m:
        subject = m.group(1).strip().strip("'\"")[:120]
        if not registry.get_object("Task") or not security.can(user, "create", "Task"):
            return {"reply": "I don't have permission to create tasks.", "data": {}}
        tid = store.insert("Task", {"Subject": subject, "Status": "Not Started",
                                    "owner_id": uid, "created_by": uid})
        return {"reply": f"Done — created task \"{subject}\".", "data": {"ids": [tid]}}

    # ---------------------------------------------------------- top accounts
    if re.search(r"\btop accounts?\b|\baccounts?\b", low):
        accts = q("Account")[:10]
        lines = [f"- {_name_of(a)}" for a in accts]
        return {"reply": f"Accounts ({len(accts)} shown):\n" + "\n".join(lines),
                "data": {"object": "Account", "ids": [a["id"] for a in accts]}}

    return {"reply": (
        "I'm not sure what you mean. Try \"help\" to see what I can do — "
        "for example \"sales pipeline\", \"my open cases\", or "
        "\"summarize account Acme\"."),
        "data": {}}
