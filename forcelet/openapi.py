"""Generate an OpenAPI 3.0 spec from the Flask app's route table.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import re

import forcelet


def _flask_to_openapi_path(rule: str) -> str:
    # /api/sobjects/<obj_name>/<rid> -> /api/sobjects/{obj_name}/{rid}
    return re.sub(r"<(?:[^:<>]+:)?([^<>]+)>", r"{\1}", rule)


def build_spec(app) -> dict:
    paths: dict = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if not rule.rule.startswith("/api"):
            continue
        if rule.rule == "/api/openapi.json":
            continue
        methods = sorted(m for m in (rule.methods or set()) if m not in ("HEAD", "OPTIONS"))
        if not methods:
            continue
        opath = _flask_to_openapi_path(rule.rule)
        path_item = paths.setdefault(opath, {})
        params = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([^}]+)\}", opath)
        ]
        for method in methods:
            summary = f"{method} {rule.rule}"
            operation = {
                "summary": summary,
                "parameters": params,
                "responses": {
                    "200": {"description": "Success"},
                    "400": {"description": "Bad request"},
                    "401": {"description": "Unauthorized"},
                    "404": {"description": "Not found"},
                },
            }
            if method in ("POST", "PUT", "PATCH"):
                operation["requestBody"] = {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "description": "Record fields or action payload",
                            }
                        }
                    },
                }
            path_item[method.lower()] = operation
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Forcelet API",
            "version": getattr(forcelet, "__version__", "0.1.0"),
            "description": (
                "REST API for Forcelet, a metadata-driven Salesforce-style CRM "
                "platform. Authenticate with a Bearer token from /api/auth/login, "
                "an API key, or an OAuth2 access token."
            ),
        },
        "servers": [{"url": "/"}],
        "security": [{"bearerAuth": []}, {"apiKeyAuth": []}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                },
            },
            "schemas": {
                "Record": {
                    "type": "object",
                    "description": "A CRM record; fields depend on the object definition",
                },
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string"},
                        "details": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "paths": paths,
    }
