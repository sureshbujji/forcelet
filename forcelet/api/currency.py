"""Multi-currency REST API. — Forcelet REST API domain module.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from flask import Flask, jsonify, request

from .. import currency as _cur
from ._shared import _audit, require_admin, require_auth


def register(app: Flask):
    store = app.mf_store
    _cur.ensure_defaults(store)
    _cur.migrate_legacy_rates(store)

    @app.get("/api/currencies")
    @require_auth
    def list_currencies():
        return jsonify(_cur.list_currencies(store))

    @app.post("/api/currencies")
    @require_auth
    @require_admin
    def upsert_currency_ep():
        body = request.json or {}
        try:
            cur = _cur.upsert_currency(store, body)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit("upsert", "Currency", cur["code"])
        return jsonify(cur), 201

    @app.put("/api/currencies/<code>/corporate")
    @require_auth
    @require_admin
    def set_corporate(code):
        try:
            cur = _cur.set_corporate_currency(store, code)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit("update", "CorporateCurrency", code)
        return jsonify(cur)

    @app.get("/api/currencies/rates")
    @require_auth
    def list_rates():
        return jsonify(_cur.list_rates(store, request.args.get("currency")))

    @app.post("/api/currencies/rates")
    @require_auth
    @require_admin
    def set_rate():
        body = request.json or {}
        try:
            row = _cur.set_rate(store, body.get("currency_code"),
                                body.get("start_date"), body.get("rate"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        _audit("upsert", "ExchangeRate", row["id"])
        return jsonify(row), 201

    @app.delete("/api/currencies/rates/<rid>")
    @require_auth
    @require_admin
    def delete_rate(rid):
        if not _cur.delete_rate(store, rid):
            return jsonify({"error": "Not found"}), 404
        _audit("delete", "ExchangeRate", rid)
        return jsonify({"ok": True})

    @app.post("/api/currencies/convert")
    @require_auth
    def convert():
        body = request.json or {}
        try:
            amount = _cur.convert(store, body.get("amount"),
                                  body.get("from"), body.get("to"),
                                  body.get("date"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"amount": amount, "from": (body.get("from") or "").upper(),
                        "to": (body.get("to") or "").upper()})
