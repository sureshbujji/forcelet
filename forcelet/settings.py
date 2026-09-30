"""Organization, security, portal, and chatter settings.

Small admin-controlled settings blobs stored as JSON in the mf_meta
key/value table (no schema changes needed). Each blob has a fixed schema
with defaults so behavior is unchanged until an admin edits it.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import json

ORG_KEY = "org_settings"
SECURITY_KEY = "security_settings"
PORTAL_KEY = "portal_settings"
CHATTER_KEY = "chatter_settings"
LOGIN_KEY = "login_settings"

ORG_DEFAULTS = {
    "org_name": "Forcelet",
    "default_locale": "en-US",
    "default_timezone": "America/Los_Angeles",
    "default_currency": "USD",
    "fiscal_year_start_month": 1,
}

# Defaults mirror the previously hardcoded behavior: 8-char minimum,
# 5 failed attempts per 15 minutes -> 15-minute lockout, 12h idle /
# 7-day absolute sessions, no password expiry, no mandatory 2FA.
SECURITY_DEFAULTS = {
    "password_min_length": 8,
    "password_require_upper": False,
    "password_require_lower": False,
    "password_require_digit": False,
    "password_require_symbol": False,
    "password_expiry_days": 0,
    "lockout_threshold": 5,
    "lockout_window_minutes": 15,
    "lockout_duration_minutes": 15,
    "session_timeout_minutes": 720,
    "session_max_hours": 168,
    "totp_required": "none",          # "none" | "all" | "profiles"
    "totp_required_profiles": [],     # profile names, used when "profiles"
}

PORTAL_DEFAULTS = {
    "enabled": True,
    "title": "Forcelet Community",
    "welcome_message": "",
}

CHATTER_DEFAULTS = {
    "feed_enabled": True,
    "mentions_enabled": True,
}

# Login branding + post-login flow toggles, honored by the sign-in screens
# (web/index.html and web/portal.html) and the post-login announcement API.
LOGIN_DEFAULTS = {
    "logo_url": "",
    "headline": "Forcelet",
    "tagline": "",  # empty = fall back to the i18n login.tagline label
    "primary_color": "#0176d3",
    "background": "",  # CSS background value for the login screen
    "announcement_enabled": False,
    "announcement_title": "",
    "announcement_body": "",
    "login_flow": [  # ordered post-login steps an admin can toggle
        {"key": "announcement_banner", "enabled": True},
        {"key": "totp_enrollment_nudge", "enabled": True},
    ],
}

_DEFAULTS = {
    ORG_KEY: ORG_DEFAULTS,
    SECURITY_KEY: SECURITY_DEFAULTS,
    PORTAL_KEY: PORTAL_DEFAULTS,
    CHATTER_KEY: CHATTER_DEFAULTS,
    LOGIN_KEY: LOGIN_DEFAULTS,
}


def get_settings(store, key: str) -> dict:
    """Return the settings blob merged over its defaults."""
    raw = store.meta_kv_get(key)
    data = {}
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            data = {}
    merged = dict(_DEFAULTS[key])
    merged.update({k: v for k, v in data.items() if k in merged})
    return merged


def save_settings(store, key: str, patch: dict) -> dict:
    """Validate a partial update, persist it, and return the merged blob."""
    defaults = _DEFAULTS[key]
    clean = {}
    for k, v in (patch or {}).items():
        if k not in defaults:
            raise ValueError(f"Unknown setting '{k}'")
        want = type(defaults[k])
        if want is bool and not isinstance(v, bool):
            raise ValueError(f"Setting '{k}' must be true/false")
        if want is int and isinstance(v, bool):
            raise ValueError(f"Setting '{k}' must be a number")
        if want is int:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"Setting '{k}' must be a number")
        if want is str and not isinstance(v, str):
            raise ValueError(f"Setting '{k}' must be text")
        if want is list and not isinstance(v, list):
            raise ValueError(f"Setting '{k}' must be a list")
        clean[k] = v
    if key == SECURITY_KEY:
        _validate_security(clean, get_settings(store, key))
    if key == LOGIN_KEY:
        _validate_login(clean)
    if key == ORG_KEY and "fiscal_year_start_month" in clean:
        m = clean["fiscal_year_start_month"]
        if not 1 <= m <= 12:
            raise ValueError("fiscal_year_start_month must be 1-12")
    merged = get_settings(store, key)
    merged.update(clean)
    store.meta_kv_set(key, json.dumps(merged))
    return merged


def _validate_security(patch: dict, current: dict):
    merged = {**current, **patch}
    if merged["password_min_length"] < 1:
        raise ValueError("password_min_length must be at least 1")
    if merged["password_expiry_days"] < 0:
        raise ValueError("password_expiry_days cannot be negative")
    if merged["lockout_threshold"] < 1:
        raise ValueError("lockout_threshold must be at least 1")
    for k in ("lockout_window_minutes", "lockout_duration_minutes",
              "session_timeout_minutes", "session_max_hours"):
        if merged[k] < 1:
            raise ValueError(f"{k} must be at least 1")
    if merged["totp_required"] not in ("none", "all", "profiles"):
        raise ValueError("totp_required must be 'none', 'all', or 'profiles'")
    if merged["totp_required"] == "profiles" and not merged["totp_required_profiles"]:
        raise ValueError("totp_required_profiles needs at least one profile "
                         "when totp_required is 'profiles'")


def _validate_login(patch: dict):
    import re as _re
    if "primary_color" in patch:
        if not _re.match(r"^#[0-9a-fA-F]{6}$", patch["primary_color"] or ""):
            raise ValueError("primary_color must be a hex color like '#0176d3'")
    if "logo_url" in patch and patch["logo_url"]:
        u = patch["logo_url"]
        if not (u.startswith("https://") or u.startswith("http://")
                or u.startswith("data:image/") or u.startswith("/")):
            raise ValueError("logo_url must be an http(s) URL, a data:image URI, "
                             "or a site-relative path")
    if "login_flow" in patch:
        flow = patch["login_flow"]
        keys = {s.get("key") for s in
                LOGIN_DEFAULTS["login_flow"]}
        for step in flow:
            if not isinstance(step, dict) or step.get("key") not in keys:
                raise ValueError(
                    f"login_flow steps must use keys {sorted(keys)}")
            if not isinstance(step.get("enabled"), bool):
                raise ValueError("login_flow step 'enabled' must be true/false")


def check_password_policy(password: str, username: str, settings: dict) -> str | None:
    """Return an error message when the password violates policy, else None."""
    pw = password or ""
    if len(pw) < settings.get("password_min_length", 8):
        return (f"Password must be at least "
                f"{settings.get('password_min_length', 8)} characters")
    if pw == (username or ""):
        return "Password must differ from the username"
    missing = []
    if settings.get("password_require_upper") and not any(c.isupper() for c in pw):
        missing.append("an uppercase letter")
    if settings.get("password_require_lower") and not any(c.islower() for c in pw):
        missing.append("a lowercase letter")
    if settings.get("password_require_digit") and not any(c.isdigit() for c in pw):
        missing.append("a digit")
    if settings.get("password_require_symbol") and not any(not c.isalnum() for c in pw):
        missing.append("a symbol")
    if missing:
        return "Password must contain " + ", ".join(missing)
    return None
