"""Custom applications (Salesforce-style Apps). — Forcelet platform module.

An *application* is a named bundle of navigation tabs: standard/custom
objects plus utility tabs (Reports, Dashboards, Chatter, ...). Admins
create apps in Setup → App Manager, choose the ordered tabs, optionally
override the page layout and related lists shown for each object tab,
and control which profiles can see the app (with a per-profile default).

Users pick an app from the App Launcher (▦) in the header; the tab bar
then shows only that app's tabs. If no apps are configured the UI keeps
its legacy behaviour (every object as a tab).

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from .field_types import is_valid_api_name
from .store import new_id

APP_TABLE = "mf_apps"

# utility tab key -> label shown in the App Manager
UTILITY_TABS = {
    "reports":    {"label": "Reports"},
    "dashboards": {"label": "Dashboards"},
    "chatter":    {"label": "Chatter"},
    "approvals":  {"label": "Approvals"},
    "forecasts":  {"label": "Forecasts"},
    "calendar":   {"label": "Calendar"},
    "dispatch":   {"label": "Dispatch"},
    "flows":      {"label": "Flows"},
}
# utilities only shown to admins
ADMIN_UTILITIES = {"bulk", "admin"}
ADMIN_UTILITY_LABELS = {"bulk": "Bulk Jobs", "admin": "Admin Setup"}
ALL_UTILITIES = {**{k: v["label"] for k, v in UTILITY_TABS.items()},
                 **ADMIN_UTILITY_LABELS}


def normalize_app(data: dict) -> dict:
    """Validate and fill defaults for an app definition. Raises ValueError."""
    data = dict(data or {})
    name = (data.get("name") or "").strip()
    if not name or not is_valid_api_name(name):
        raise ValueError("app 'name' is required and must be a valid API name")
    label = (data.get("label") or "").strip() or name
    tabs = []
    for i, t in enumerate(data.get("tabs") or []):
        t = dict(t or {})
        kind = t.get("kind")
        ref = (t.get("ref") or "").strip()
        if kind not in ("object", "utility"):
            raise ValueError(f"tab {i}: kind must be 'object' or 'utility'")
        if not ref:
            raise ValueError(f"tab {i}: ref is required")
        if kind == "utility" and ref not in ALL_UTILITIES:
            raise ValueError(f"tab {i}: unknown utility '{ref}'")
        layout = t.get("layout")
        if layout is not None:
            if not isinstance(layout, dict):
                raise ValueError(f"tab {i}: layout must be an object or null")
            layout = {"profile": (layout.get("profile") or "").strip() or "Default",
                      "record_type": (layout.get("record_type") or "").strip() or "Default"}
        rel = t.get("related_lists")
        if rel is not None:
            if not isinstance(rel, list) or not all(isinstance(x, str) for x in rel):
                raise ValueError(f"tab {i}: related_lists must be a list of object names or null")
            rel = [x for x in rel if x]
        tabs.append({"id": t.get("id") or new_id(), "kind": kind, "ref": ref,
                     "label": (t.get("label") or "").strip() or None,
                     "layout": layout, "related_lists": rel or None})
    access = {}
    for prof, cfg in (data.get("profile_access") or {}).items():
        cfg = cfg or {}
        vis, dflt = bool(cfg.get("visible", True)), bool(cfg.get("default", False))
        if not vis and not dflt:
            continue  # meaningless restriction -> drop, keeps "empty = everyone"
        access[prof] = {"visible": vis, "default": dflt}
    return {"id": data.get("id") or new_id(),
            "name": name, "label": label,
            "description": (data.get("description") or "").strip(),
            "icon": (data.get("icon") or "").strip() or "",
            "color": (data.get("color") or "").strip() or "#1f6feb",
            "active": bool(data.get("active", True)),
            "sort_order": int(data.get("sort_order") or 0),
            "tabs": tabs, "profile_access": access}


def list_apps(store) -> list:
    apps = store.config_all(APP_TABLE)
    apps.sort(key=lambda a: (a.get("sort_order", 0), a.get("label", "")))
    return apps


def get_app(store, app_id: str):
    return store.config_get(APP_TABLE, app_id)


def save_app(store, data: dict) -> dict:
    app = normalize_app(data)
    for other in store.config_all(APP_TABLE):
        if other["id"] != app["id"] and other.get("name") == app["name"]:
            raise ValueError(f"an app named '{app['name']}' already exists")
    store.config_put(APP_TABLE, app)
    return app


def delete_app(store, app_id: str) -> bool:
    return store.config_delete(APP_TABLE, app_id)


def app_visible_to(app: dict, profile: str) -> bool:
    """An app with no profile_access rows is visible to every profile."""
    access = app.get("profile_access") or {}
    if not access:
        return True
    return bool((access.get(profile) or {}).get("visible", False))


def visible_apps(store, security, user: dict) -> list:
    """Apps the user may open, with tabs filtered to what they can use."""
    profile = user.get("profile") or ""
    is_admin = profile == "System Administrator"
    out = []
    for app in list_apps(store):
        if not app.get("active", True) or not app_visible_to(app, profile):
            continue
        tabs = []
        for t in app.get("tabs", []):
            if t["kind"] == "object":
                if not security.can(user, "read", t["ref"]):
                    continue
            elif t["ref"] in ADMIN_UTILITIES and not is_admin:
                continue
            tabs.append({k: t.get(k) for k in
                         ("id", "kind", "ref", "label", "layout", "related_lists")})
        access = (app.get("profile_access") or {}).get(profile) or {}
        out.append({"id": app["id"], "name": app["name"], "label": app["label"],
                    "description": app.get("description", ""),
                    "icon": app.get("icon", ""), "color": app.get("color", "#1f6feb"),
                    "default": bool(access.get("default")), "tabs": tabs})
    return out


def default_app_id(apps: list) -> str | None:
    for a in apps:
        if a.get("default"):
            return a["id"]
    return apps[0]["id"] if apps else None


def tab_for_object(app: dict, obj_name: str):
    for t in app.get("tabs", []):
        if t.get("kind") == "object" and t.get("ref") == obj_name:
            return t
    return None


def resolve_layout(store, app_id: str, obj_name: str,
                   profile: str, record_type: str = "Default"):
    """Return the app's layout override for an object tab, or None.

    Only honours the override when the app exists and the profile may see
    it; otherwise the caller falls back to the profile's own layout.
    """
    app = get_app(store, app_id) if app_id else None
    if not app or not app_visible_to(app, profile):
        return None
    tab = tab_for_object(app, obj_name)
    layout_ref = (tab or {}).get("layout")
    if not layout_ref:
        return None
    return store.layout_get(obj_name, layout_ref.get("profile") or "Default",
                            layout_ref.get("record_type") or record_type)


# ------------------------------------------------------------------ seeding
def seed_apps() -> list:
    """Standard Sales and Service apps (tabs reference seeded objects)."""
    sales_tabs = ["Account", "Contact", "Lead", "Opportunity", "Product",
                  "Quote", "Campaign", "Task", "Event"]
    service_tabs = ["Account", "Contact", "Case", "WorkOrder",
                    "ServiceAppointment", "KnowledgeArticle", "Task", "Event"]
    sales_utils = ["reports", "dashboards", "forecasts", "chatter"]
    service_utils = ["reports", "dashboards", "calendar", "chatter"]

    def tabs(objs, utils):
        return ([{"kind": "object", "ref": o} for o in objs]
                + [{"kind": "utility", "ref": u} for u in utils])

    return [
        normalize_app({"name": "Sales", "label": "Sales", "icon": "",
                       "color": "#1f6feb", "sort_order": 1,
                       "description": "Sell faster: leads, opportunities, quotes and forecasts.",
                       "tabs": tabs(sales_tabs, sales_utils)}),
        normalize_app({"name": "Service", "label": "Service", "icon": "",
                       "color": "#0e7c3e", "sort_order": 2,
                       "description": "Support customers: cases, work orders and knowledge.",
                       "tabs": tabs(service_tabs, service_utils)}),
    ]


def seed_apps_if_missing(store) -> list:
    """Create the standard apps unless an app with the same name exists."""
    existing = {a.get("name") for a in store.config_all(APP_TABLE)}
    created = []
    for app in seed_apps():
        if app["name"] not in existing:
            store.config_put(APP_TABLE, app)
            created.append(app)
    return created
