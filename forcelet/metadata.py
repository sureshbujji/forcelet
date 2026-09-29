"""Object metadata registry.

Object definitions (standard + custom) live in the mf_objects table, so new
objects and fields can be created at runtime via the API/UI. Standard objects
are seeded from metadata/standard_objects.json on first run.
"""
from __future__ import annotations

import json
import os

from .field_types import is_valid_api_name, FIELD_TYPES

RESERVED_NAMES = {"Object", "Field", "User", "Profile", "Role", "Layout"}


class MetadataRegistry:
    def __init__(self, store):
        self.store = store

    # ---------------------------------------------------------------- seed
    def seed_if_empty(self, metadata_dir: str):
        if self.store.meta_count("mf_objects") > 0:
            return
        path = os.path.join(metadata_dir, "standard_objects.json")
        with open(path) as f:
            for obj in json.load(f):
                self.store.meta_put("mf_objects", obj["name"], obj)
                self.store.ensure_object_table(obj)

    # ------------------------------------------------------------------ read
    def list_objects(self):
        return sorted(self.store.meta_all("mf_objects"), key=lambda o: o["name"])

    def get_object(self, name: str):
        return self.store.meta_get("mf_objects", name)

    def field_map(self, obj_def: dict) -> dict:
        return {f["name"]: f for f in obj_def.get("fields", [])}

    # ----------------------------------------------------------------- write
    def create_object(self, name: str, label: str, plural: str, is_custom: bool = True):
        if not is_valid_api_name(name):
            raise ValueError("Object API name must start with a letter and contain only letters, digits, underscores")
        if self.get_object(name):
            raise ValueError(f"Object '{name}' already exists")
        obj = {"name": name, "label": label or name, "plural": plural or f"{label}s",
               "is_custom": is_custom, "fields": []}
        self.store.meta_put("mf_objects", name, obj)
        self.store.ensure_object_table(obj)
        return obj

    def add_field(self, obj_name: str, field: dict):
        obj = self.get_object(obj_name)
        if not obj:
            raise ValueError(f"Unknown object '{obj_name}'")
        fname = field.get("name", "")
        if not is_valid_api_name(fname):
            raise ValueError("Field API name must start with a letter and contain only letters, digits, underscores")
        if fname.lower() in ("id", "owner_id", "created_by", "created_date", "last_modified_date"):
            raise ValueError(f"'{fname}' is a reserved system field name")
        if fname in self.field_map(obj):
            raise ValueError(f"Field '{fname}' already exists on {obj_name}")
        ftype = field.get("type")
        if ftype not in FIELD_TYPES:
            raise ValueError(f"Unknown field type '{ftype}'. Valid: {sorted(FIELD_TYPES)}")
        if ftype == "Lookup" and not field.get("reference_to"):
            raise ValueError("Lookup fields require 'reference_to' (target object)")
        if ftype in ("Picklist", "MultiPicklist") and not field.get("picklist_values"):
            raise ValueError("Picklist fields require 'picklist_values'")
        full = {
            "name": fname,
            "label": field.get("label") or fname,
            "type": ftype,
            "required": bool(field.get("required", False)),
            "unique": bool(field.get("unique", False)),
            "length": field.get("length"),
            "picklist_values": field.get("picklist_values"),
            "reference_to": field.get("reference_to"),
            "default": field.get("default"),
            "formula": field.get("formula"),  # expression JSON; computed on read, never stored
            "rollup": field.get("rollup"),    # {"object","via","field","func","filter?"}; computed
            "encrypted": bool(field.get("encrypted")),  # encrypted at rest (Text-ish types)
            "external_id": bool(field.get("external_id")),  # unique external identifier for upserts
        }
        if full["external_id"]:
            if ftype not in ("Text", "Email", "Phone", "URL", "Number"):
                raise ValueError("Only Text, Email, Phone, URL, and Number fields can be external IDs")
            if full["encrypted"]:
                raise ValueError("Encrypted fields cannot be external IDs (ciphertext is randomized)")
            if full["formula"] or full["rollup"]:
                raise ValueError("Computed fields cannot be external IDs")
            full["unique"] = True  # external IDs are unique by definition
        if full["encrypted"]:
            if ftype not in ("Text", "TextArea", "Email", "Phone", "URL"):
                raise ValueError("Only text-like fields can be encrypted")
            if full["unique"]:
                raise ValueError("Encrypted fields cannot be unique (ciphertext is randomized)")
            if full["rollup"]:
                raise ValueError("Roll-up fields cannot be encrypted")
        if full["rollup"]:
            spec = full["rollup"]
            for key in ("object", "via", "func"):
                if key not in spec:
                    raise ValueError(f"Roll-up spec needs '{key}'")
            if spec["func"] not in ("sum", "avg", "min", "max", "count"):
                raise ValueError(f"Unknown roll-up func '{spec['func']}'")
            if spec["func"] != "count" and "field" not in spec:
                raise ValueError("Roll-up spec needs 'field' for sum/avg/min/max")
            full["required"] = False
        obj["fields"].append(full)
        self.store.meta_put("mf_objects", obj_name, obj)
        self.store.add_column(obj_name, full)
        if full.get("external_id"):
            self.store.add_unique_index(obj_name, fname)
        return full

    def validate_record(self, obj_def: dict, values: dict, partial: bool = False):
        """Validate a record's field values. Returns (clean: dict, errors: list)."""
        from .field_types import validate_value
        fmap = self.field_map(obj_def)
        clean, errors = {}, []
        for key, value in values.items():
            if key not in fmap:
                errors.append(f"Unknown field '{key}' on {obj_def['name']}")
                continue
            if fmap[key].get("formula") or fmap[key].get("rollup"):
                errors.append(f"{fmap[key]['label']} is a computed field and cannot be set")
                continue
            ok, norm, err = validate_value(fmap[key], value)
            if not ok:
                errors.append(err)
            else:
                clean[key] = norm
        if not partial:
            for fname, fdef in fmap.items():
                if fdef.get("formula") or fdef.get("rollup"):
                    continue
                if fname not in values:
                    if fdef.get("default") is not None:
                        clean[fname] = fdef["default"]
                    elif fdef.get("required"):
                        errors.append(f"{fdef['label']} is required")
        # field-level encryption: applied last, idempotent (never double-encrypts)
        from . import crypto as _crypto
        for fname, fdef in fmap.items():
            if fdef.get("encrypted") and fname in clean:
                clean[fname] = _crypto.encrypt(clean[fname]) \
                    if not _crypto.is_encrypted(clean[fname]) else clean[fname]
        return clean, errors
