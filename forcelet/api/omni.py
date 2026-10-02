"""Omni-Channel REST API. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .. import omni as _omni
from .. import queues as _queues
from ._shared import _audit, require_admin, require_auth


def register(app: Flask):
    store, security = app.mf_store, app.mf_security
    _omni.ensure_defaults(store)

    # ------------------------------------------------------------ presence
    @app.get("/api/omni/presence")
    @require_auth
    def my_presence():
        return jsonify(_omni.get_presence(store, request.mf_user["id"]))

    @app.put("/api/omni/presence")
    @require_auth
    def set_my_presence():
        body = request.json or {}
        try:
            row = _omni.set_presence(store, request.mf_user["id"],
                                     body.get("status"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(row)

    @app.get("/api/omni/presence/all")
    @require_auth
    @require_admin
    def all_presence():
        return jsonify(_omni.all_presence(store))

    # ------------------------------------------------------------ capacity
    @app.get("/api/omni/capacity")
    @require_auth
    def my_capacity():
        uid = request.args.get("user_id") or request.mf_user["id"]
        if uid != request.mf_user["id"] and not security.is_admin(request.mf_user):
            return jsonify({"error": "Forbidden"}), 403
        cap = _omni.get_capacity(store, uid)
        cap["current_load"] = _omni.agent_load(store, uid)
        return jsonify(cap)

    @app.put("/api/omni/capacity")
    @require_auth
    @require_admin
    def set_capacity_ep():
        body = request.json or {}
        if not body.get("user_id"):
            return jsonify({"error": "user_id is required"}), 400
        try:
            cap = _omni.set_capacity(store, body["user_id"],
                                     body.get("max_capacity"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit("update", "AgentCapacity", body["user_id"])
        return jsonify(cap)

    # ------------------------------------------------------------ channels
    @app.get("/api/omni/channels")
    @require_auth
    def list_channels():
        return jsonify(store.config_all(_omni.CHANNEL_TABLE))

    @app.post("/api/omni/channels")
    @require_auth
    @require_admin
    def create_channel():
        body = request.json or {}
        if not (body.get("name") or "").strip():
            return jsonify({"error": "name is required"}), 400
        body.setdefault("active", True)
        cid = store.config_put(_omni.CHANNEL_TABLE, body)
        _audit("create", "ServiceChannel", cid)
        return jsonify(store.config_get(_omni.CHANNEL_TABLE, cid)), 201

    # ------------------------------------------------------------ routing configs
    @app.get("/api/omni/routing-configs")
    @require_auth
    def list_routing_configs():
        return jsonify(store.config_all(_omni.ROUTING_TABLE))

    @app.post("/api/omni/routing-configs")
    @require_auth
    @require_admin
    def create_routing_config():
        body = request.json or {}
        if not _omni.find_channel(store, body.get("channel") or ""):
            return jsonify({"error": "Unknown channel"}), 400
        q = _queues.get_queue(store, body.get("queue") or "") or \
            _queues.find_queue(store, body.get("queue") or "")
        if not q:
            return jsonify({"error": "Unknown queue"}), 400
        ch = _omni.find_channel(store, body.get("channel"))
        body["channel_id"] = ch["id"]
        body["queue_id"] = q["id"]
        body.setdefault("priority", 0)
        body.setdefault("active", True)
        rid = store.config_put(_omni.ROUTING_TABLE, body)
        _audit("create", "RoutingConfig", rid)
        return jsonify(store.config_get(_omni.ROUTING_TABLE, rid)), 201

    # ------------------------------------------------------------ work items
    @app.post("/api/omni/work")
    @require_auth
    def enqueue_work_ep():
        body = request.json or {}
        try:
            item = _omni.enqueue_work(store, body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        # Best-effort immediate routing.
        try:
            _omni.route_work(store, security, item["id"])
            item = _omni.get_work(store, item["id"])
        except Exception:
            pass
        return jsonify(item), 201

    @app.post("/api/omni/work/<wid>/route")
    @require_auth
    @require_admin
    def route_work_ep(wid):
        result = _omni.route_work(store, security, wid)
        return jsonify({"routed": len(result["routed"]),
                        "unrouted": len(result["unrouted"])})

    @app.post("/api/omni/work/route-all")
    @require_auth
    @require_admin
    def route_all_ep():
        result = _omni.route_work(store, security)
        return jsonify({"routed": len(result["routed"]),
                        "unrouted": len(result["unrouted"])})

    @app.post("/api/omni/work/<wid>/accept")
    @require_auth
    def accept_work_ep(wid):
        try:
            item = _omni.accept_work(store, wid, request.mf_user["id"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(item)

    @app.post("/api/omni/work/<wid>/decline")
    @require_auth
    def decline_work_ep(wid):
        try:
            item = _omni.decline_work(store, wid, request.mf_user["id"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(item)

    @app.post("/api/omni/work/<wid>/complete")
    @require_auth
    def complete_work_ep(wid):
        item = _omni.get_work(store, wid)
        if not item:
            return jsonify({"error": "Not found"}), 404
        user = request.mf_user
        if item.get("assigned_to") != user["id"] and not security.is_admin(user):
            return jsonify({"error": "Forbidden"}), 403
        return jsonify(_omni.complete_work(store, wid))

    @app.get("/api/omni/queues/<qid>/work")
    @require_auth
    def queue_work(qid):
        q = _queues.get_queue(store, qid) or _queues.find_queue(store, qid)
        if not q:
            return jsonify({"error": "Not found"}), 404
        return jsonify(_omni.queue_snapshot(store, q["id"]))

    @app.get("/api/omni/work/mine")
    @require_auth
    def my_work():
        uid = request.mf_user["id"]
        items = [w for w in store.config_all(_omni.WORK_TABLE)
                 if w.get("assigned_to") == uid and w.get("status") == "assigned"]
        return jsonify(items)
