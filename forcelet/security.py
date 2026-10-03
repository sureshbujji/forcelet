"""Users, profiles, roles, permissions, and record sharing.

- Profile: what a user may do — CRUD per object + read/edit per field.
- Role: where a user sits in the hierarchy. A user sees records owned by
  themselves and by users in roles below them (Salesforce-style role
  hierarchy sharing). System Administrators bypass sharing.
- Users, profiles, and roles are all stored as metadata, so they are
  configurable at runtime through the API/UI.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time

from .expressions import eval_expr, record_context

ACTIONS = ("create", "read", "edit", "delete")

# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------
SESSION_PREFIX = "mf_sess_"
SESSION_TTL_SECONDS = 12 * 3600        # sliding idle window
SESSION_MAX_SECONDS = 7 * 24 * 3600   # absolute cap from creation


def new_session_token() -> str:
    """Return an unguessable session token. Only its SHA-256 hash is stored."""
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Login brute-force protection (in-process; use a shared store for multi-worker)
# ---------------------------------------------------------------------------
_LOGIN_ATTEMPTS: dict[tuple[str, str], list[float]] = {}
_LOGIN_LOCK = threading.Lock()
_BRUTE_FORCE_MAX = 5          # failed attempts …
_BRUTE_FORCE_WINDOW = 900     # … per 15 minutes …
_BRUTE_FORCE_LOCKOUT = 900    # … then locked out for 15 minutes


def _lockout_params(settings: dict | None) -> tuple[int, int, int]:
    """(max_attempts, window_seconds, lockout_seconds) from admin settings."""
    if not settings:
        return _BRUTE_FORCE_MAX, _BRUTE_FORCE_WINDOW, _BRUTE_FORCE_LOCKOUT
    return (int(settings.get("lockout_threshold", _BRUTE_FORCE_MAX)),
            int(settings.get("lockout_window_minutes", 15)) * 60,
            int(settings.get("lockout_duration_minutes", 15)) * 60)


def _prune_attempts(now: float, window: int) -> None:
    cutoff = now - window
    for key in list(_LOGIN_ATTEMPTS):
        tries = [t for t in _LOGIN_ATTEMPTS[key] if t > cutoff]
        if tries:
            _LOGIN_ATTEMPTS[key] = tries
        else:
            del _LOGIN_ATTEMPTS[key]


def login_locked_out(ip: str, username: str, settings: dict | None = None) -> bool:
    """True if this (ip, username) or this ip alone is currently locked out."""
    max_tries, window, lockout = _lockout_params(settings)
    now = time.time()
    with _LOGIN_LOCK:
        _prune_attempts(now, window)
        pair = _LOGIN_ATTEMPTS.get((ip, username), [])
        if len(pair) >= max_tries and now - pair[-1] < lockout:
            return True
        ip_tries = [t for (i, _u), tries in _LOGIN_ATTEMPTS.items()
                    if i == ip for t in tries]
        if len(ip_tries) >= max_tries * 4 and \
                now - max(ip_tries) < lockout:
            return True
        return False


def record_failed_login(ip: str, username: str, settings: dict | None = None) -> None:
    _, window, _ = _lockout_params(settings)
    with _LOGIN_LOCK:
        _prune_attempts(time.time(), window)
        _LOGIN_ATTEMPTS.setdefault((ip, username), []).append(time.time())


def reset_login_attempts(ip: str, username: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_ATTEMPTS.pop((ip, username), None)


def reset_login_attempts_for_user(username: str) -> None:
    """Admin unlock: clear failed-attempt counters for every IP of this user."""
    with _LOGIN_LOCK:
        for key in [k for k in _LOGIN_ATTEMPTS
                    if k[1].lower() == (username or "").lower()]:
            _LOGIN_ATTEMPTS.pop(key, None)


def session_timeouts(store) -> tuple[int, int]:
    """(idle_ttl_seconds, absolute_max_seconds) from admin security settings."""
    from .settings import SECURITY_KEY, get_settings
    s = get_settings(store, SECURITY_KEY)
    return (int(s["session_timeout_minutes"]) * 60,
            int(s["session_max_hours"]) * 3600)


def totp_required_for_user(store, user: dict) -> bool:
    """True when the org's 2FA policy requires TOTP for this user."""
    from .settings import SECURITY_KEY, get_settings
    s = get_settings(store, SECURITY_KEY)
    mode = s.get("totp_required", "none")
    if mode == "all":
        return True
    if mode == "profiles":
        return user.get("profile") in (s.get("totp_required_profiles") or [])
    return False


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return f"pbkdf2$120000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, hexdk = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return dk.hex() == hexdk
    except Exception:
        return False


class Security:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ seed
    def seed_if_empty(self, roles: list, profiles: list, users: list):
        if self.store.meta_count("mf_roles") == 0:
            for r in roles:
                self.store.meta_put("mf_roles", r["name"], r)
        if self.store.meta_count("mf_profiles") == 0:
            for p in profiles:
                self.store.meta_put("mf_profiles", p["name"], p)
        if self.store.meta_count("mf_users") == 0:
            for u in users:
                self.store.meta_put("mf_users", u["id"], u)

    # ------------------------------------------------------------------ read
    def get_user(self, user_id: str):
        return self.store.meta_get("mf_users", user_id)

    def get_user_by_username(self, username: str):
        for u in self.store.meta_all("mf_users"):
            if u["username"].lower() == username.lower():
                return u
        return None

    def list_users(self):
        return self.store.meta_all("mf_users")

    def get_profile(self, name: str):
        return self.store.meta_get("mf_profiles", name)

    def list_profiles(self):
        return self.store.meta_all("mf_profiles")

    def get_role(self, name: str):
        return self.store.meta_get("mf_roles", name)

    def list_roles(self):
        return self.store.meta_all("mf_roles")

    def is_admin(self, user: dict) -> bool:
        return user.get("profile") == "System Administrator"

    # ------------------------------------------------------------ permissions
    @staticmethod
    def _grants(grants: dict, action: str, obj_name: str, field: str | None) -> bool:
        """Check one grants document (profile or permission set)."""
        obj_perms = (grants.get("object_permissions") or {}).get(obj_name, {})
        wild = (grants.get("object_permissions") or {}).get("*", {})

        def obj_allows(act: str) -> bool:
            return bool(obj_perms.get(act, wild.get(act, False)))

        if field is None:
            return obj_allows(action)
        fp = (grants.get("field_permissions") or {}).get(obj_name, {}).get(field)
        if action == "read":
            return obj_allows("read") if fp is None else bool(fp.get("read", True))
        if action in ("create", "edit"):
            base = obj_allows("create" if action == "create" else "edit")
            return base if fp is None else (base and bool(fp.get("edit", True)))
        return False

    def can(self, user: dict, action: str, obj_name: str, field: str | None = None) -> bool:
        """Object- or field-level permission check.

        Field permissions default to open: a field is readable/editable when
        the profile has the matching object permission, unless the field is
        explicitly restricted in the profile's field_permissions.
        Permission sets grant *additional* access on top of the profile.
        """
        if self.is_admin(user):
            return True
        profile = self.get_profile(user.get("profile"))
        docs = [profile] if profile else []
        for ps_name in user.get("permission_sets") or []:
            ps = self.store.config_get("mf_permission_sets", ps_name)
            if ps:
                docs.append(ps)
            else:  # also allow lookup by name
                for cand in self.store.config_all("mf_permission_sets"):
                    if cand.get("name") == ps_name:
                        docs.append(cand)
                        break
        return any(self._grants(d, action, obj_name, field) for d in docs)

    def readable_fields(self, user: dict, obj_def: dict) -> list:
        if self.is_admin(user):
            return [f["name"] for f in obj_def.get("fields", [])]
        return [f["name"] for f in obj_def.get("fields", []) if self.can(user, "read", obj_def["name"], f["name"])]

    def editable_fields(self, user: dict, obj_def: dict) -> list:
        if self.is_admin(user):
            return [f["name"] for f in obj_def.get("fields", [])
                    if f.get("active") is not False]
        return [f["name"] for f in obj_def.get("fields", [])
                if f.get("active") is not False
                and self.can(user, "edit", obj_def["name"], f["name"])]

    # ---------------------------------------------------------------- sharing
    def _role_subtree(self, role_name: str | None) -> set:
        """Role + all roles below it in the hierarchy."""
        if not role_name:
            return set()
        roles = {r["name"]: r for r in self.list_roles()}
        out, stack = set(), [role_name]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            stack.extend(n for n, r in roles.items() if r.get("parent") == cur)
        return out

    def visible_owner_ids(self, user: dict):
        """User ids whose records this user may see. None = everyone (admin)."""
        if self.is_admin(user):
            return None
        if not user.get("role"):
            # No role: no subordinates; the user sees only their own records.
            return [user["id"]]
        subtree = self._role_subtree(user.get("role"))
        return [u["id"] for u in self.list_users() if u.get("role") in subtree]

    def can_see_record(self, user: dict, record: dict, obj_name: str | None = None,
                       _seen: set | None = None) -> bool:
        if self.is_admin(user):
            return True
        # Divisions partition visibility on top of every other sharing rule:
        # a user sees records in their own division plus global records.
        if obj_name and record and record.get("id"):
            from . import divisions as _divisions
            if not _divisions.record_visible_to_user(
                    self.store, self, user, obj_name, record):
                return False
        if obj_name:
            obj_def = self.store.meta_get("mf_objects", obj_name)
            if obj_def and obj_def.get("org_wide_visible"):
                return True
        owner_ids = self.visible_owner_ids(user)
        if record.get("owner_id") in (owner_ids or []):
            return True
        # master-detail sharing inheritance: access to the master grants
        # access to the detail
        if obj_name:
            from .datamodel import md_parents, territory_grants_access
            _seen = _seen or set()
            obj_def = self.store.meta_get("mf_objects", obj_name)
            if obj_def:
                for parent_obj, parent_id in md_parents(obj_def, record):
                    key = (parent_obj, parent_id)
                    if key in _seen:
                        continue
                    _seen.add(key)
                    parent = self.store.get(parent_obj, parent_id)
                    if parent and self.can_see_record(user, parent, parent_obj, _seen):
                        return True
            # territory-based sharing for accounts (and their opportunities)
            try:
                if territory_grants_access(self.store, user, obj_name, record):
                    return True
            except Exception:
                pass
        # criteria-based sharing rules
        if obj_name:
            for rule in self.store.config_all("mf_sharing_rules"):
                if not rule.get("active", True) or rule.get("object") != obj_name:
                    continue
                try:
                    if not eval_expr(rule.get("criteria") or {}, record_context(record)):
                        continue
                except Exception:
                    continue
                if self._sharing_target_includes(rule.get("share_with") or {}, user):
                    return True
        # case-team sharing: membership in the case's assigned team grants
        # read access, even without ownership/role/sharing-rule coverage.
        # Only evaluated when the grants above failed, and only when the
        # case actually has a team assigned.
        if obj_name == "Case" and record.get("id"):
            try:
                asg = self.store.config_get("mf_case_team_assignments",
                                            record["id"])
                if asg and asg.get("team_def_id"):
                    team_id = asg["team_def_id"]
                    for m in self.store.query("CaseTeamMemberDef",
                                              owner_ids=None, limit=10000):
                        if m.get("CaseTeamDefId") == team_id \
                                and m.get("UserId") == user["id"]:
                            return True
            except Exception:
                pass
        # Org-wide default record access (OWD): the baseline when no grant
        # above applies. "private" preserves the historical behavior
        # (nothing visible unless granted); the public levels extend baseline
        # visibility to every user. Object-level profile permissions still
        # gate create/edit/delete actions.
        from .settings import ORG_KEY, get_settings
        owd = get_settings(self.store, ORG_KEY).get("default_record_access",
                                                   "private")
        if owd in ("public_read_only", "public_read_write"):
            return True
        return False

    def _sharing_target_includes(self, target: dict, user: dict) -> bool:
        ttype = target.get("type")
        if ttype == "user":
            return target.get("id") == user["id"]
        if ttype == "role":
            return user.get("role") in self._role_subtree(target.get("role"))
        if ttype == "profile":
            return user.get("profile") == target.get("name")
        return False

    # ----------------------------------------------------------------- write
    def set_password(self, user_id: str, password: str):
        from datetime import datetime, timezone
        user = self.get_user(user_id)
        if not user:
            raise ValueError("Unknown user")
        user["password_hash"] = hash_password(password)
        user["password_set_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.meta_put("mf_users", user_id, user)

    def check_password(self, user: dict, password: str) -> bool:
        return bool(user.get("password_hash")) and verify_password(password, user["password_hash"])

    def create_user(self, username: str, name: str, profile: str, role: str | None,
                    password: str = "forcelet", email: str = ""):
        from .store import new_id
        from datetime import datetime, timezone
        if self.get_user_by_username(username):
            raise ValueError(f"Username '{username}' already exists")
        if not self.get_profile(profile):
            raise ValueError(f"Unknown profile '{profile}'")
        if role and not self.get_role(role):
            raise ValueError(f"Unknown role '{role}'")
        user = {"id": "u_" + new_id()[:8], "username": username, "name": name,
                "email": email or "", "profile": profile, "role": role,
                "permission_sets": [], "is_active": True,
                "password_hash": hash_password(password),
                "password_set_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                # Admin-set initial passwords must be changed at first login.
                "must_change_password": True}
        self.store.meta_put("mf_users", user["id"], user)
        return user

    def update_user(self, user_id: str, patch: dict):
        """Update name/email/role/profile/is_active. Returns the user."""
        user = self.get_user(user_id)
        if not user:
            raise ValueError("Unknown user")
        if "name" in patch:
            user["name"] = patch["name"] or ""
        if "email" in patch:
            user["email"] = patch["email"] or ""
        if "profile" in patch:
            if not self.get_profile(patch["profile"]):
                raise ValueError(f"Unknown profile '{patch['profile']}'")
            user["profile"] = patch["profile"]
        if "role" in patch:
            if patch["role"] and not self.get_role(patch["role"]):
                raise ValueError(f"Unknown role '{patch['role']}'")
            user["role"] = patch["role"]
        if "is_active" in patch:
            user["is_active"] = bool(patch["is_active"])
        self.store.meta_put("mf_users", user_id, user)
        return user

    def delete_user(self, user_id: str):
        """Hard-delete a user record. Callers must guard (e.g. refuse when
        the user has login history); deactivation is the safer default."""
        user = self.get_user(user_id)
        if not user:
            raise ValueError("Unknown user")
        self.store._execute("DELETE FROM mf_users WHERE id=?", (user_id,))
        self.store._commit()
        return user

    def assign_permission_set(self, user_id: str, ps_id_or_name: str):
        user = self.get_user(user_id)
        if not user:
            raise ValueError("Unknown user")
        psets = user.setdefault("permission_sets", [])
        if ps_id_or_name not in psets:
            psets.append(ps_id_or_name)
        self.store.meta_put("mf_users", user_id, user)
        return user

    def unassign_permission_set(self, user_id: str, ps_id_or_name: str):
        user = self.get_user(user_id)
        if not user:
            raise ValueError("Unknown user")
        psets = user.get("permission_sets", [])
        if ps_id_or_name not in psets:
            raise ValueError("Permission set is not assigned to this user")
        user["permission_sets"] = [p for p in psets if p != ps_id_or_name]
        self.store.meta_put("mf_users", user_id, user)
        return user

    def create_role(self, name: str, parent: str | None):
        if self.get_role(name):
            raise ValueError(f"Role '{name}' already exists")
        if parent and not self.get_role(parent):
            raise ValueError(f"Unknown parent role '{parent}'")
        role = {"name": name, "parent": parent}
        self.store.meta_put("mf_roles", name, role)
        return role

    _UNSET = object()

    def update_role(self, name: str, new_name: str | None = None, parent=_UNSET):
        """Rename and/or reparent a role. Rename cascades to child roles'
        parent refs and users' role refs. Reparenting is cycle-checked."""
        role = self.get_role(name)
        if not role:
            raise KeyError(f"Unknown role '{name}'")
        target_name = new_name.strip() if new_name else name
        if not target_name:
            raise ValueError("Role name cannot be blank")
        if target_name != name and self.get_role(target_name):
            raise ValueError(f"Role '{target_name}' already exists")
        if parent is self._UNSET:
            target_parent = role.get("parent")
        else:
            target_parent = parent or None
            if target_parent and not self.get_role(target_parent):
                raise ValueError(f"Unknown parent role '{target_parent}'")
            if target_parent:
                # cycle check: parent may not be the role itself or one of its descendants
                if target_parent == target_name or target_parent in self._role_subtree(name):
                    raise ValueError(
                        f"Cannot set parent to '{target_parent}': would create a cycle")
        renamed = target_name != name
        if renamed:
            self.store.meta_delete("mf_roles", name)
            for child in self.list_roles():
                if child.get("parent") == name:
                    child["parent"] = target_name
                    self.store.meta_put("mf_roles", child["name"], child)
            for user in self.list_users():
                if user.get("role") == name:
                    user["role"] = target_name
                    self.store.meta_put("mf_users", user["id"], user)
        role = {"name": target_name, "parent": target_parent}
        self.store.meta_put("mf_roles", target_name, role)
        return role

    def delete_role(self, name: str):
        """Delete a role. Refuses when users are assigned to it or child roles exist."""
        if not self.get_role(name):
            raise KeyError(f"Unknown role '{name}'")
        users = [u for u in self.list_users() if u.get("role") == name]
        if users:
            raise ValueError(
                f"Cannot delete role '{name}': {len(users)} user(s) are assigned to it")
        children = [r for r in self.list_roles() if r.get("parent") == name]
        if children:
            raise ValueError(
                f"Cannot delete role '{name}': {len(children)} child role(s) exist")
        self.store.meta_delete("mf_roles", name)

    def create_profile(self, name: str, object_permissions: dict, field_permissions: dict):
        if self.get_profile(name):
            raise ValueError(f"Profile '{name}' already exists")
        profile = {"name": name, "object_permissions": object_permissions,
                   "field_permissions": field_permissions}
        self.store.meta_put("mf_profiles", name, profile)
        return profile
