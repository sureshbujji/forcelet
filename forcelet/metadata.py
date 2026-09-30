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
    def create_object(self, name: str, label: str, plural: str, is_custom: bool = True,
                      big_object: bool = False):
        if not is_valid_api_name(name):
            raise ValueError("Object API name must start with a letter and contain only letters, digits, underscores")
        if self.get_object(name):
            raise ValueError(f"Object '{name}' already exists")
        if big_object and not name.endswith("__b"):
            raise ValueError("Big Object API names must end with __b")
        obj = {"name": name, "label": label or name, "plural": plural or f"{label}s",
               "is_custom": is_custom, "fields": [],
               "is_big_object": bool(big_object)}
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
        if ftype == "MasterDetail":
            from .datamodel import validate_master_detail
            validate_master_detail(self, obj_name, field)
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
            "reparentable": field.get("reparentable", True) if ftype == "MasterDetail" else None,
        }
        if ftype == "MasterDetail":
            full["required"] = True  # a detail record must always have its master
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

    # ------------------------------------------------------ field lifecycle
    #: Patch keys an admin may change on an existing field. ``name`` and
    #: ``type`` are intentionally absent — changing those would silently
    #: invalidate stored data, so they are refused instead.
    FIELD_EDITABLE = {"label", "help_text", "required", "default",
                      "picklist_values", "length", "active", "description"}

    def _managed_object(self, obj_name: str) -> dict:
        """Return the object def, or raise for unknown/standard objects.

        Field and object lifecycle management is restricted to custom
        objects: editing a standard object's schema could break platform
        features that depend on it.
        """
        obj = self.get_object(obj_name)
        if not obj:
            raise KeyError(f"Unknown object '{obj_name}'")
        if not obj.get("is_custom"):
            raise PermissionError(
                f"Object '{obj_name}' is a standard object and cannot be managed here")
        return obj

    def update_field(self, obj_name: str, fname: str, patch: dict) -> dict:
        """Edit a custom field's metadata. Returns the updated field def."""
        obj = self._managed_object(obj_name)
        fmap = self.field_map(obj)
        if fname not in fmap:
            raise KeyError(f"Unknown field '{fname}' on {obj_name}")
        for key in patch:
            if key in ("name", "type"):
                raise ValueError("Field API name and type cannot be changed")
            if key not in self.FIELD_EDITABLE:
                raise ValueError(f"Field attribute '{key}' cannot be edited")
        fdef = fmap[fname]
        if "label" in patch:
            if not str(patch["label"]).strip():
                raise ValueError("Label cannot be blank")
        if "required" in patch and (fdef.get("type") == "MasterDetail" or fdef.get("rollup")):
            raise ValueError("The required flag is fixed for master-detail and roll-up fields")
        if "picklist_values" in patch:
            if fdef.get("type") not in ("Picklist", "MultiPicklist"):
                raise ValueError("picklist_values only applies to picklist fields")
            vals = patch["picklist_values"]
            if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
                raise ValueError("picklist_values must be a list of strings")
        if "length" in patch:
            if fdef.get("type") not in ("Text", "TextArea"):
                raise ValueError("length only applies to Text/TextArea fields")
            try:
                ln = int(patch["length"])
            except (TypeError, ValueError):
                raise ValueError("length must be a positive integer")
            if ln <= 0:
                raise ValueError("length must be a positive integer")
            patch["length"] = ln
        if "active" in patch:
            patch["active"] = bool(patch["active"])
        if "required" in patch:
            patch["required"] = bool(patch["required"])
        fdef.update({k: v for k, v in patch.items()})
        self.store.meta_put("mf_objects", obj_name, obj)
        return fdef

    def set_field_active(self, obj_name: str, fname: str, active: bool) -> dict:
        return self.update_field(obj_name, fname, {"active": bool(active)})

    def field_value_count(self, obj_name: str, fname: str) -> int:
        """How many records hold a non-null value for this field."""
        return self.store._fetchone(
            f'SELECT COUNT(*) AS n FROM {self.store._table(obj_name)} WHERE "{fname}" IS NOT NULL'
        )["n"]

    def delete_field(self, obj_name: str, fname: str) -> bool:
        """Delete a custom field. Refuses when records still hold values."""
        obj = self._managed_object(obj_name)
        fmap = self.field_map(obj)
        if fname not in fmap:
            raise KeyError(f"Unknown field '{fname}' on {obj_name}")
        fdef = fmap[fname]
        if not (fdef.get("formula") or fdef.get("rollup")):
            n = self.field_value_count(obj_name, fname)
            if n:
                raise ValueError(
                    f"Cannot delete field '{fname}': {n} record(s) still hold a value. "
                    "Clear the values or deactivate the field instead.")
        obj["fields"] = [f for f in obj["fields"] if f["name"] != fname]
        self.store.meta_put("mf_objects", obj_name, obj)
        # Best-effort physical cleanup; orphan columns are harmless because
        # every read/write path is driven by the field definitions above.
        try:
            self.store._execute(
                f'ALTER TABLE {self.store._table(obj_name)} DROP COLUMN "{fname}"')
            self.store._commit()
        except Exception:
            pass
        try:
            idx = f"ux_{self.store._table(obj_name)}_{fname}".replace('"', "")
            self.store._execute(f'DROP INDEX IF EXISTS "{idx}"')
            self.store._commit()
        except Exception:
            pass
        return True

    def delete_object(self, obj_name: str) -> bool:
        """Delete a custom object. Refuses when records exist."""
        obj = self._managed_object(obj_name)
        n = self.store.count(obj_name)
        if n:
            raise ValueError(
                f"Cannot delete object '{obj_name}': {n} record(s) exist. "
                "Delete the records first.")
        self.store.meta_delete("mf_objects", obj_name)
        try:
            self.store._execute(f"DROP TABLE {self.store._table(obj_name)}")
            self.store._commit()
        except Exception:
            pass
        return True

    def validate_record(self, obj_def: dict, values: dict, partial: bool = False,
                        skip_required: set | None = None):
        """Validate a record's field values. Returns (clean: dict, errors: list).

        skip_required: field names exempt from the required check (used by
        Dynamic Forms — a required field hidden by a visibility rule must not
        block the save).
        """
        from .field_types import validate_value
        fmap = self.field_map(obj_def)
        clean, errors = {}, []
        for key, value in values.items():
            if key not in fmap:
                errors.append(f"Unknown field '{key}' on {obj_def['name']}")
                continue
            if fmap[key].get("active") is False:
                errors.append(f"{fmap[key]['label']} is deactivated and cannot be set")
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
                if fdef.get("active") is False:
                    continue  # deactivated fields are invisible to validation
                if fname not in values:
                    if fdef.get("default") is not None:
                        clean[fname] = fdef["default"]
                    elif fdef.get("required") and fname not in (skip_required or ()):
                        errors.append(f"{fdef['label']} is required")
        # field-level encryption: applied last, idempotent (never double-encrypts)
        from . import crypto as _crypto
        for fname, fdef in fmap.items():
            if fdef.get("encrypted") and fname in clean:
                clean[fname] = _crypto.encrypt(clean[fname]) \
                    if not _crypto.is_encrypted(clean[fname]) else clean[fname]
        return clean, errors
