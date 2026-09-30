"""Translation workbench: custom labels and per-language translations.

Custom labels are developer/admin-defined string keys (e.g.
``login.tagline``) with a default (English) text. Translations override the
default per language code (``es``, ``fr``, ``de``, ...).

Labels live in the ``mf_custom_labels`` config table; translations in
``mf_translations``. :func:`get_pack` merges them into the language pack
served to the UI — missing translations fall back to the default text.

Deliberate scope: the workbench manages labels and serves packs; wiring
every UI string through it is out of scope. The login screen
(web/index.html) consumes the pack to prove the plumbing.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import re

LABELS_TABLE = "mf_custom_labels"
TRANSLATIONS_TABLE = "mf_translations"

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
LANG_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def validate_label(label: dict, existing_keys: set[str]) -> list[str]:
    errors = []
    key = (label.get("key") or "").strip()
    if not KEY_RE.match(key):
        errors.append("key must look like 'section.name' "
                      "(lowercase letters, digits, underscores, dots)")
    elif key in existing_keys:
        errors.append(f"label key '{key}' already exists")
    if not (label.get("default_text") or "").strip():
        errors.append("default_text is required")
    return errors


def validate_translation(tr: dict, label_keys: set[str]) -> list[str]:
    errors = []
    if tr.get("label_key") not in label_keys:
        errors.append(f"unknown label_key '{tr.get('label_key')}'")
    if not LANG_RE.match((tr.get("language") or "").strip()):
        errors.append("language must be a code like 'es' or 'fr-CA'")
    if not (tr.get("text") or "").strip():
        errors.append("text is required")
    return errors


def get_pack(store, language: str) -> dict:
    """Return {key: text} for a language, falling back to default text."""
    lang = (language or "").strip()
    labels = {l["key"]: l for l in store.config_all(LABELS_TABLE)
              if l.get("key")}
    pack = {k: l.get("default_text", "") for k, l in labels.items()}
    if lang:
        for tr in store.config_all(TRANSLATIONS_TABLE):
            if tr.get("language") == lang and tr.get("label_key") in pack:
                pack[tr["label_key"]] = tr.get("text", "")
    return pack


def seed_defaults(store) -> int:
    """Seed login-screen labels (and a few translations) if none exist."""
    if store.config_all(LABELS_TABLE):
        return 0
    labels = [
        ("login.tagline",
         "Metadata-driven mini-CRM. Built by Suresh Itha. "
         "Sign in with your Forcelet account.",
         "Tagline under the headline on the sign-in screen"),
        ("login.signin", "Sign in", "Sign-in button label"),
        ("login.username", "Username", "Username field label"),
        ("login.password", "Password", "Password field label"),
        ("login.totp_title", "Two-factor authentication",
         "2FA challenge card title"),
        ("login.totp_help",
         "Enter the 6-digit code from your authenticator app.",
         "2FA challenge help text"),
    ]
    for key, default_text, desc in labels:
        store.config_put(LABELS_TABLE,
                         {"key": key, "default_text": default_text,
                          "description": desc, "category": "login"})
    translations = [
        ("login.tagline", "es",
         "Mini-CRM basado en metadatos. Creado por Suresh Itha. "
         "Inicia sesión con tu cuenta de Forcelet."),
        ("login.signin", "es", "Iniciar sesión"),
        ("login.username", "es", "Usuario"),
        ("login.password", "es", "Contraseña"),
        ("login.tagline", "fr",
         "Mini-CRM piloté par les métadonnées. Créé par Suresh Itha. "
         "Connectez-vous avec votre compte Forcelet."),
        ("login.signin", "fr", "Se connecter"),
        ("login.username", "fr", "Nom d'utilisateur"),
        ("login.password", "fr", "Mot de passe"),
        ("login.tagline", "de",
         "Metadatengesteuertes Mini-CRM. Erstellt von Suresh Itha. "
         "Melden Sie sich mit Ihrem Forcelet-Konto an."),
        ("login.signin", "de", "Anmelden"),
        ("login.username", "de", "Benutzername"),
        ("login.password", "de", "Passwort"),
    ]
    for key, lang, text in translations:
        store.config_put(TRANSLATIONS_TABLE,
                         {"label_key": key, "language": lang, "text": text})
    return len(labels)
