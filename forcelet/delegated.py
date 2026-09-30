"""Delegated administration: limited-scope admin groups.

A delegated group grants non-admin users a subset of admin powers:

- ``users`` — full user CRUD (optionally restricted to users in given roles)
- ``passwords`` — password reset + unlock (optionally role-restricted)
- ``profiles`` — profile management
- ``roles`` — role hierarchy management

Groups live in the ``mf_delegated_groups`` config table::

    {"name": ..., "description": ...,
     "members": ["<user_id>", ...],
     "scopes": [{"scope": "users", "roles": ["Sales Rep"]}, ...]}

A scope entry without ``roles`` applies org-wide for that domain; with
``roles``, user-targeting endpoints additionally require the *target*
user's role to be in the list. Endpoints enforce this through
:func:`forcelet.api._shared.require_admin_scope`.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

TABLE = "mf_delegated_groups"

SCOPES = ("users", "passwords", "profiles", "roles")


def validate_group(group: dict, store) -> list[str]:
    errors = []
    name = (group.get("name") or "").strip()
    if not name:
        errors.append("name is required")
    else:
        for g in store.config_all(TABLE):
            if g.get("id") != group.get("id") and \
                    (g.get("name") or "").strip().lower() == name.lower():
                errors.append(f"a delegated group named '{name}' already exists")
                break
    members = group.get("members") or []
    if not isinstance(members, list):
        errors.append("members must be a list of user ids")
    scopes = group.get("scopes") or []
    if not isinstance(scopes, list):
        errors.append("scopes must be a list")
    else:
        for i, entry in enumerate(scopes):
            if not isinstance(entry, dict):
                errors.append(f"scopes[{i}] must be an object")
                continue
            if entry.get("scope") not in SCOPES:
                errors.append(f"scopes[{i}].scope must be one of {SCOPES}")
            roles = entry.get("roles")
            if roles is not None and not isinstance(roles, list):
                errors.append(f"scopes[{i}].roles must be a list of role names")
            elif roles:
                known = {r.get("name") for r in store.meta_all("mf_roles")}
                for rn in roles:
                    if rn not in known:
                        errors.append(f"scopes[{i}]: unknown role '{rn}'")
    return errors


def groups_for_user(store, user_id: str) -> list[dict]:
    return [g for g in store.config_all(TABLE)
            if user_id in (g.get("members") or [])]


def scope_entries(store, user_id: str, scope: str) -> list[dict]:
    """Return the user's scope entries for a domain (empty = no grant)."""
    out = []
    for g in groups_for_user(store, user_id):
        for entry in g.get("scopes") or []:
            if isinstance(entry, dict) and entry.get("scope") == scope:
                out.append(entry)
    return out


def has_scope(store, user: dict, scope: str) -> bool:
    """True when the user holds the scope in any of their groups."""
    return bool(user and scope_entries(store, user.get("id"), scope))


def target_in_scope(store, user: dict, scope: str,
                    target_role: str | None) -> bool:
    """Role-restricted check for user-targeting endpoints.

    A scope entry without ``roles`` grants access to any target; an entry
    with ``roles`` only grants access when the target user's role is listed.
    """
    entries = scope_entries(store, user.get("id"), scope)
    for entry in entries:
        roles = entry.get("roles")
        if not roles:
            return True
        if target_role and target_role in roles:
            return True
    return False
