"""SQLite persistence layer.

- Metadata tables (mf_objects, mf_profiles, mf_roles, mf_users, mf_layouts)
  hold the configurable platform definition.
- One data table per object (sobj_<ObjectName>) holds records. Tables are
  created/altered automatically when objects and fields are defined.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .field_types import FIELD_TYPES, is_valid_api_name

SYSTEM_COLUMNS = ["id", "owner_id", "created_by", "created_date", "last_modified_date"]


def new_id() -> str:
    return uuid.uuid4().hex[:15]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # check_same_thread=False + a lock: the Flask dev server handles
        # requests on multiple threads sharing this connection.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_meta_tables()

    def _execute(self, sql, params=()):
        with self._lock:
            return self.conn.execute(sql, params)

    def _commit(self):
        with self._lock:
            self.conn.commit()

    # ------------------------------------------------------------------ meta
    def _init_meta_tables(self):
        c = self.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS mf_objects
                     (name TEXT PRIMARY KEY, definition TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_profiles
                     (name TEXT PRIMARY KEY, definition TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_roles
                     (name TEXT PRIMARY KEY, definition TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_users
                     (id TEXT PRIMARY KEY, definition TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_layouts
                     (object_name TEXT, profile_name TEXT, record_type TEXT DEFAULT 'Default',
                      definition TEXT NOT NULL,
                      PRIMARY KEY (object_name, profile_name, record_type))""")
        for table in ("mf_validation_rules", "mf_flows", "mf_approval_processes",
                      "mf_approval_requests", "mf_record_types", "mf_reports",
                      "mf_sharing_rules", "mf_matching_rules", "mf_permission_sets",
                      "mf_webhooks", "mf_triggers", "mf_list_views",
                      "mf_scheduled_jobs", "mf_assignment_rules", "mf_email_templates",
                      "mf_auto_responses", "mf_paths", "mf_ml_models",
                      "mf_sla_policies", "mf_case_milestones", "mf_escalation_rules",
                      "mf_named_credentials", "mf_forecast_quotas", "mf_flow_runs",
                      "mf_dashboards", "mf_case_queues", "mf_macros",
                      "mf_bulk_jobs", "mf_report_subs", "mf_flow_versions"):
            c.execute(f"""CREATE TABLE IF NOT EXISTS {table}
                          (id TEXT PRIMARY KEY, definition TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_history
                     (id TEXT PRIMARY KEY, object_name TEXT, record_id TEXT,
                      field_name TEXT, old_value TEXT, new_value TEXT,
                      changed_by TEXT, changed_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_webhook_deliveries
                     (id TEXT PRIMARY KEY, webhook_id TEXT, event TEXT,
                      payload TEXT, status TEXT, detail TEXT, attempted_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_scheduled_runs
                     (id TEXT PRIMARY KEY, job_id TEXT, ran_at TEXT,
                      status TEXT, detail TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_activities
                     (id TEXT PRIMARY KEY, object_name TEXT, record_id TEXT,
                      activity_type TEXT, subject TEXT, body TEXT,
                      created_by TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_email_log
                     (id TEXT PRIMARY KEY, object_name TEXT, record_id TEXT,
                      recipient TEXT, subject TEXT, body TEXT, template TEXT,
                      sent_by TEXT, sent_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_audit_trail
                     (id TEXT PRIMARY KEY, at TEXT, user_id TEXT, username TEXT,
                      action TEXT, entity_type TEXT, entity_name TEXT, details TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_change_events
                     (seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT,
                      object_name TEXT, record_id TEXT, event TEXT,
                      user_id TEXT, changed_fields TEXT, snapshot TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_refresh_tokens
                     (token_hash TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_api_keys
                     (key_hash TEXT PRIMARY KEY, id TEXT, name TEXT, user_id TEXT,
                      created_at TEXT, last_used_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_feed_posts
                     (id TEXT PRIMARY KEY, object_name TEXT, record_id TEXT,
                      user_id TEXT, body TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_feed_comments
                     (id TEXT PRIMARY KEY, post_id TEXT, user_id TEXT,
                      body TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_feed_likes
                     (post_id TEXT, user_id TEXT, created_at TEXT,
                      PRIMARY KEY (post_id, user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_feed_follows
                     (user_id TEXT, object_name TEXT, record_id TEXT,
                      created_at TEXT,
                      PRIMARY KEY (user_id, object_name, record_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_feed_mentions
                     (id TEXT PRIMARY KEY, post_id TEXT, mentioned_user_id TEXT,
                      created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_lead_conversions
                     (id TEXT PRIMARY KEY, lead_id TEXT, account_id TEXT,
                      contact_id TEXT, opportunity_id TEXT,
                      converted_by TEXT, converted_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_files
                     (id TEXT PRIMARY KEY, object_name TEXT, record_id TEXT,
                      filename TEXT, mime_type TEXT, size INTEGER,
                      uploaded_by TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_notifications
                     (id TEXT PRIMARY KEY, user_id TEXT, ntype TEXT,
                      title TEXT, body TEXT, object_name TEXT, record_id TEXT,
                      is_read INTEGER DEFAULT 0, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS mf_recycle_bin
                     (id TEXT PRIMARY KEY, object_name TEXT, record_id TEXT,
                      data TEXT, deleted_by TEXT, deleted_at TEXT)""")
        self._commit()

    def meta_put(self, table: str, key: str, definition: dict):
        assert table in ("mf_objects", "mf_profiles", "mf_roles", "mf_users")
        pk = "name" if table != "mf_users" else "id"
        self._execute(
            f"INSERT OR REPLACE INTO {table} ({pk}, definition) VALUES (?, ?)",
            (key, json.dumps(definition)),
        )
        self._commit()

    def meta_get(self, table: str, key: str):
        pk = "name" if table != "mf_users" else "id"
        row = self._execute(f"SELECT definition FROM {table} WHERE {pk}=?", (key,)).fetchone()
        # A concurrent writer can leave a momentarily-NULL definition visible;
        # treat it as "not found" instead of raising.
        return json.loads(row["definition"]) if row and row["definition"] else None

    def meta_all(self, table: str):
        rows = self._execute(f"SELECT definition FROM {table}").fetchall()
        return [json.loads(r["definition"]) for r in rows]

    def meta_count(self, table: str) -> int:
        return self._execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    # ------------------------------------------------------------- data DDL
    @staticmethod
    def _table(obj_name: str) -> str:
        if not is_valid_api_name(obj_name):
            raise ValueError(f"Invalid object name '{obj_name}'")
        return f"sobj_{obj_name}"

    def ensure_object_table(self, obj_def: dict):
        """Create the data table for an object (idempotent)."""
        cols = [
            "id TEXT PRIMARY KEY",
            "owner_id TEXT",
            "record_type TEXT DEFAULT 'Default'",
            "created_by TEXT",
            "created_date TEXT",
            "last_modified_date TEXT",
        ]
        for f in obj_def.get("fields", []):
            if f.get("formula") or f.get("rollup"):
                continue  # computed fields are never stored
            cols.append(f'"{f["name"]}" {FIELD_TYPES[f["type"]]["sql"]}')
        self._execute(f"CREATE TABLE IF NOT EXISTS {self._table(obj_def['name'])} ({', '.join(cols)})")
        if "record_type" not in self.existing_columns(obj_def["name"]):
            self._execute(f"ALTER TABLE {self._table(obj_def['name'])} "
                          f"ADD COLUMN record_type TEXT DEFAULT 'Default'")
        self._commit()

    def add_column(self, obj_name: str, field: dict):
        sql_type = FIELD_TYPES[field["type"]]["sql"]
        try:
            self._execute(
                f'ALTER TABLE {self._table(obj_name)} ADD COLUMN "{field["name"]}" {sql_type}'
            )
            self._commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    def add_unique_index(self, obj_name: str, field_name: str):
        idx = f"ux_{self._table(obj_name)}_{field_name}".replace('"', "")
        self._execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx}"'
            f' ON {self._table(obj_name)} ("{field_name}")'
        )
        self._commit()

    def existing_columns(self, obj_name: str) -> set:
        rows = self._execute(f"PRAGMA table_info({self._table(obj_name)})").fetchall()
        return {r["name"] for r in rows}

    # ------------------------------------------------------------------ CRUD
    def insert(self, obj_name: str, record: dict) -> str:
        record = dict(record)
        record.setdefault("id", new_id())
        now = utcnow()
        record.setdefault("created_date", now)
        record["last_modified_date"] = now
        cols = ", ".join(f'"{k}"' for k in record.keys())
        placeholders = ", ".join("?" for _ in record)
        self._execute(
            f"INSERT INTO {self._table(obj_name)} ({cols}) VALUES ({placeholders})",
            tuple(record.values()),
        )
        self._commit()
        return record["id"]

    def get(self, obj_name: str, record_id: str):
        row = self._execute(
            f"SELECT * FROM {self._table(obj_name)} WHERE id=?", (record_id,)
        ).fetchone()
        return dict(row) if row else None

    def query(self, obj_name: str, owner_ids: list | None = None, limit: int = 200):
        if owner_ids is None:
            rows = self._execute(
                f"SELECT * FROM {self._table(obj_name)} ORDER BY created_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if not owner_ids:
                return []
            ph = ", ".join("?" for _ in owner_ids)
            rows = self._execute(
                f"SELECT * FROM {self._table(obj_name)} WHERE owner_id IN ({ph}) "
                f"ORDER BY created_date DESC LIMIT ?",
                (*owner_ids, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update(self, obj_name: str, record_id: str, fields: dict):
        fields = dict(fields)
        fields["last_modified_date"] = utcnow()
        sets = ", ".join(f'"{k}"=?' for k in fields.keys())
        cur = self._execute(
            f"UPDATE {self._table(obj_name)} SET {sets} WHERE id=?",
            (*fields.values(), record_id),
        )
        self._commit()
        return cur.rowcount > 0

    def delete(self, obj_name: str, record_id: str) -> bool:
        cur = self._execute(f"DELETE FROM {self._table(obj_name)} WHERE id=?", (record_id,))
        self._commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------ recycle bin
    def recycle_put(self, obj_name: str, record: dict, deleted_by: str) -> str:
        bid = new_id()
        self._execute(
            "INSERT INTO mf_recycle_bin (id, object_name, record_id, data, deleted_by, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (bid, obj_name, record.get("id"), json.dumps(record), deleted_by, utcnow()),
        )
        self._commit()
        return bid

    def recycle_list(self, deleted_by: str | None = None):
        if deleted_by:
            rows = self._execute(
                "SELECT * FROM mf_recycle_bin WHERE deleted_by=? ORDER BY deleted_at DESC",
                (deleted_by,)).fetchall()
        else:
            rows = self._execute(
                "SELECT * FROM mf_recycle_bin ORDER BY deleted_at DESC").fetchall()
        return [dict(r) for r in rows]

    def recycle_get(self, bid: str):
        row = self._execute("SELECT * FROM mf_recycle_bin WHERE id=?", (bid,)).fetchone()
        return dict(row) if row else None

    def recycle_delete(self, bid: str) -> bool:
        cur = self._execute("DELETE FROM mf_recycle_bin WHERE id=?", (bid,))
        self._commit()
        return cur.rowcount > 0

    def recycle_clear(self, deleted_by: str | None = None) -> int:
        if deleted_by:
            cur = self._execute("DELETE FROM mf_recycle_bin WHERE deleted_by=?", (deleted_by,))
        else:
            cur = self._execute("DELETE FROM mf_recycle_bin")
        self._commit()
        return cur.rowcount

    def count(self, obj_name: str) -> int:
        return self._execute(f"SELECT COUNT(*) AS n FROM {self._table(obj_name)}").fetchone()["n"]

    # --------------------------------------------------------------- layouts
    def layout_put(self, object_name: str, profile_name: str, definition: dict,
                   record_type: str = "Default"):
        self._execute(
            "INSERT OR REPLACE INTO mf_layouts (object_name, profile_name, record_type, definition)"
            " VALUES (?, ?, ?, ?)",
            (object_name, profile_name, record_type, json.dumps(definition)),
        )
        self._commit()

    def layout_get(self, object_name: str, profile_name: str, record_type: str = "Default"):
        for prof, rt in ((profile_name, record_type), (profile_name, "Default"),
                         ("Default", record_type), ("Default", "Default")):
            row = self._execute(
                "SELECT definition FROM mf_layouts WHERE object_name=? AND profile_name=? AND record_type=?",
                (object_name, prof, rt),
            ).fetchone()
            if row:
                return json.loads(row["definition"])
        return None

    def layouts_all(self):
        return [{"object": r["object_name"], "profile": r["profile_name"],
                 "record_type": r["record_type"],
                 **json.loads(r["definition"])}
                for r in self._execute("SELECT * FROM mf_layouts").fetchall()]

    # ------------------------------------------------- generic config tables
    def config_put(self, table: str, definition: dict) -> str:
        rid = definition.get("id") or new_id()
        definition = {**definition, "id": rid}
        self._execute(f"INSERT OR REPLACE INTO {table} (id, definition) VALUES (?, ?)",
                      (rid, json.dumps(definition)))
        self._commit()
        return rid

    def config_get(self, table: str, rid: str):
        row = self._execute(f"SELECT definition FROM {table} WHERE id=?", (rid,)).fetchone()
        return json.loads(row["definition"]) if row else None

    def config_all(self, table: str):
        rows = self._execute(f"SELECT definition FROM {table}").fetchall()
        return [json.loads(r["definition"]) for r in rows]

    def config_delete(self, table: str, rid: str) -> bool:
        cur = self._execute(f"DELETE FROM {table} WHERE id=?", (rid,))
        self._commit()
        return cur.rowcount > 0

    # ------------------------------------------------- dedicated event tables
    def _row_put(self, table: str, row: dict) -> str:
        row = {"id": new_id(), **row}
        cols = ", ".join(f'"{k}"' for k in row)
        self._execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})",
                      tuple(row.values()))
        self._commit()
        return row["id"]

    def _rows(self, table: str, where: str = "", params=(), order="rowid DESC", limit=200):
        q = f"SELECT * FROM {table}"
        if where:
            q += f" WHERE {where}"
        q += f" ORDER BY {order} LIMIT {int(limit)}"
        return [dict(r) for r in self._execute(q, params).fetchall()]

    # scheduled runs
    def log_scheduled_run(self, job_id, status, detail=""):
        return self._row_put("mf_scheduled_runs", {
            "job_id": job_id, "ran_at": utcnow(), "status": status, "detail": detail})

    def scheduled_runs(self, job_id=None, limit=50):
        if job_id:
            return self._rows("mf_scheduled_runs", "job_id=?", (job_id,), limit=limit)
        return self._rows("mf_scheduled_runs", limit=limit)

    # activities
    def add_activity(self, object_name, record_id, activity_type, subject, body, user):
        return self._row_put("mf_activities", {
            "object_name": object_name, "record_id": record_id,
            "activity_type": activity_type, "subject": subject or "",
            "body": body or "", "created_by": user["id"], "created_at": utcnow()})

    def get_activities(self, object_name, record_id, limit=50):
        rows = self._rows("mf_activities", "object_name=? AND record_id=?",
                          (object_name, record_id), order="created_at DESC", limit=limit)
        users = {u["id"]: u.get("name", u["username"]) for u in self.meta_all("mf_users")}
        for r in rows:
            r["created_by_name"] = users.get(r["created_by"], r["created_by"])
        return rows

    # email log
    def log_email(self, object_name, record_id, recipient, subject, body, template, user):
        return self._row_put("mf_email_log", {
            "object_name": object_name, "record_id": record_id,
            "recipient": recipient or "", "subject": subject or "",
            "body": body or "", "template": template or "",
            "sent_by": user["id"], "sent_at": utcnow()})

    def email_log(self, limit=100):
        return self._rows("mf_email_log", limit=limit)

    # setup audit trail
    def audit(self, user, action, entity_type, entity_name, details=""):
        return self._row_put("mf_audit_trail", {
            "at": utcnow(), "user_id": user["id"], "username": user.get("username", ""),
            "action": action, "entity_type": entity_type,
            "entity_name": entity_name or "", "details": details or ""})

    def audit_trail(self, limit=200):
        return self._rows("mf_audit_trail", order="at DESC", limit=limit)

    # change data capture
    def emit_change(self, object_name, record_id, event, user, changed_fields=None,
                    snapshot=None):
        self._execute(
            """INSERT INTO mf_change_events
               (at, object_name, record_id, event, user_id, changed_fields, snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (utcnow(), object_name, record_id, event, user["id"],
             json.dumps(changed_fields or []), json.dumps(snapshot or {})),
        )
        self._commit()

    def change_events(self, since=0, object_name=None, record_id=None, limit=200):
        where, params = ["seq > ?"], [int(since)]
        if object_name:
            where.append("object_name=?")
            params.append(object_name)
        if record_id:
            where.append("record_id=?")
            params.append(record_id)
        rows = self._rows("mf_change_events", " AND ".join(where), tuple(params),
                          order="seq ASC", limit=limit)
        for r in rows:
            r["changed_fields"] = json.loads(r["changed_fields"] or "[]")
            r["snapshot"] = json.loads(r["snapshot"] or "{}")
        return rows

    # refresh tokens + api keys (hashes only)
    def put_refresh_token(self, token_hash, user_id, expires_at):
        self._execute("INSERT OR REPLACE INTO mf_refresh_tokens (token_hash, user_id, expires_at)"
                      " VALUES (?, ?, ?)", (token_hash, user_id, expires_at))
        self._commit()

    def get_refresh_token(self, token_hash):
        row = self._execute("SELECT * FROM mf_refresh_tokens WHERE token_hash=?",
                            (token_hash,)).fetchone()
        return dict(row) if row else None

    def delete_refresh_token(self, token_hash):
        self._execute("DELETE FROM mf_refresh_tokens WHERE token_hash=?", (token_hash,))
        self._commit()

    def put_api_key(self, key_hash, name, user_id):
        kid = new_id()
        self._execute("INSERT INTO mf_api_keys (key_hash, id, name, user_id, created_at)"
                      " VALUES (?, ?, ?, ?, ?)", (key_hash, kid, name, user_id, utcnow()))
        self._commit()
        return kid

    def get_api_key(self, key_hash):
        row = self._execute("SELECT * FROM mf_api_keys WHERE key_hash=?", (key_hash,)).fetchone()
        return dict(row) if row else None

    def touch_api_key(self, key_hash):
        self._execute("UPDATE mf_api_keys SET last_used_at=? WHERE key_hash=?",
                      (utcnow(), key_hash))
        self._commit()

    def api_keys_for(self, user_id):
        return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                 "last_used_at": r["last_used_at"]}
                for r in self._execute("SELECT * FROM mf_api_keys WHERE user_id=?",
                                       (user_id,)).fetchall()]

    def delete_api_key(self, kid, user_id):
        cur = self._execute("DELETE FROM mf_api_keys WHERE id=? AND user_id=?", (kid, user_id))
        self._commit()
        return cur.rowcount > 0

    # ------------------------------------------------- chatter feed
    def feed_post(self, user_id, body, object_name=None, record_id=None):
        return self._row_put("mf_feed_posts", {
            "object_name": object_name, "record_id": record_id,
            "user_id": user_id, "body": body or "", "created_at": utcnow()})

    def feed_add_comment(self, post_id, user_id, body):
        return self._row_put("mf_feed_comments", {
            "post_id": post_id, "user_id": user_id,
            "body": body or "", "created_at": utcnow()})

    def feed_like(self, post_id, user_id):
        self._execute("INSERT OR IGNORE INTO mf_feed_likes (post_id, user_id, created_at)"
                      " VALUES (?, ?, ?)", (post_id, user_id, utcnow()))
        self._commit()

    def feed_unlike(self, post_id, user_id):
        cur = self._execute("DELETE FROM mf_feed_likes WHERE post_id=? AND user_id=?",
                            (post_id, user_id))
        self._commit()
        return cur.rowcount > 0

    def feed_follow(self, user_id, object_name, record_id):
        self._execute("INSERT OR IGNORE INTO mf_feed_follows"
                      " (user_id, object_name, record_id, created_at) VALUES (?, ?, ?, ?)",
                      (user_id, object_name, record_id, utcnow()))
        self._commit()

    def feed_unfollow(self, user_id, object_name, record_id):
        cur = self._execute("DELETE FROM mf_feed_follows"
                            " WHERE user_id=? AND object_name=? AND record_id=?",
                            (user_id, object_name, record_id))
        self._commit()
        return cur.rowcount > 0

    def feed_is_following(self, user_id, object_name, record_id):
        row = self._execute("SELECT 1 FROM mf_feed_follows"
                            " WHERE user_id=? AND object_name=? AND record_id=?",
                            (user_id, object_name, record_id)).fetchone()
        return bool(row)

    def feed_follows_for(self, user_id):
        return [dict(r) for r in self._execute(
            "SELECT * FROM mf_feed_follows WHERE user_id=? ORDER BY created_at DESC",
            (user_id,)).fetchall()]

    def feed_mention(self, post_id, mentioned_user_id):
        return self._row_put("mf_feed_mentions", {
            "post_id": post_id, "mentioned_user_id": mentioned_user_id,
            "created_at": utcnow()})

    def feed_get_post(self, post_id):
        row = self._execute("SELECT * FROM mf_feed_posts WHERE id=?", (post_id,)).fetchone()
        return dict(row) if row else None

    def _feed_enrich(self, posts):
        users = {u["id"]: u.get("name", u["username"]) for u in self.meta_all("mf_users")}
        post_ids = [p["id"] for p in posts]
        likes, comments = {}, {}
        if post_ids:
            ph = ", ".join("?" for _ in post_ids)
            for r in self._execute(
                    f"SELECT post_id, COUNT(*) AS n FROM mf_feed_likes"
                    f" WHERE post_id IN ({ph}) GROUP BY post_id", post_ids).fetchall():
                likes[r["post_id"]] = r["n"]
            for r in self._execute(
                    f"SELECT post_id, COUNT(*) AS n FROM mf_feed_comments"
                    f" WHERE post_id IN ({ph}) GROUP BY post_id", post_ids).fetchall():
                comments[r["post_id"]] = r["n"]
        for p in posts:
            p["user_name"] = users.get(p["user_id"], p["user_id"])
            p["like_count"] = likes.get(p["id"], 0)
            p["comment_count"] = comments.get(p["id"], 0)
        return posts

    def feed_for_record(self, object_name, record_id, limit=50):
        rows = self._rows("mf_feed_posts", "object_name=? AND record_id=?",
                          (object_name, record_id), order="created_at DESC", limit=limit)
        return self._feed_enrich(rows)

    def feed_home(self, user_id, limit=50):
        """Posts on followed records + posts mentioning the user + own posts."""
        follows = self._execute(
            "SELECT object_name, record_id FROM mf_feed_follows WHERE user_id=?",
            (user_id,)).fetchall()
        clauses, params = ["p.user_id=?"], [user_id]
        for f in follows:
            clauses.append("(p.object_name=? AND p.record_id=?)")
            params += [f["object_name"], f["record_id"]]
        mentioned = [r["post_id"] for r in self._execute(
            "SELECT post_id FROM mf_feed_mentions WHERE mentioned_user_id=?", (user_id,))]
        if mentioned:
            ph = ", ".join("?" for _ in mentioned)
            clauses.append(f"p.id IN ({ph})")
            params += mentioned
        rows = self._execute(
            f"SELECT p.* FROM mf_feed_posts p WHERE {' OR '.join(clauses)}"
            f" ORDER BY p.created_at DESC LIMIT {int(limit)}", params).fetchall()
        return self._feed_enrich([dict(r) for r in rows])

    def feed_comments(self, post_id, limit=100):
        rows = self._rows("mf_feed_comments", "post_id=?", (post_id,),
                          order="created_at ASC", limit=limit)
        users = {u["id"]: u.get("name", u["username"]) for u in self.meta_all("mf_users")}
        for r in rows:
            r["user_name"] = users.get(r["user_id"], r["user_id"])
        return rows

    def feed_liked_by(self, post_id, user_id):
        row = self._execute("SELECT 1 FROM mf_feed_likes WHERE post_id=? AND user_id=?",
                            (post_id, user_id)).fetchone()
        return bool(row)

    def feed_mentions_of(self, post_id):
        return [r["mentioned_user_id"] for r in self._execute(
            "SELECT mentioned_user_id FROM mf_feed_mentions WHERE post_id=?",
            (post_id,)).fetchall()]

    # ------------------------------------------------------------ files
    def file_put(self, object_name, record_id, filename, mime_type, size, user):
        return self._row_put("mf_files", {
            "object_name": object_name, "record_id": record_id,
            "filename": filename, "mime_type": mime_type or
            "application/octet-stream", "size": size,
            "uploaded_by": user["id"], "created_at": utcnow()})

    def file_get(self, fid):
        rows = self._execute("SELECT * FROM mf_files WHERE id=?",
                             (fid,)).fetchall()
        return dict(rows[0]) if rows else None

    def files_for_record(self, object_name, record_id):
        return [dict(r) for r in self._execute(
            "SELECT * FROM mf_files WHERE object_name=? AND record_id=? "
            "ORDER BY created_at DESC", (object_name, record_id)).fetchall()]

    def file_delete(self, fid):
        self._execute("DELETE FROM mf_files WHERE id=?", (fid,))
        self._commit()

    # ----------------------------------------------------- notifications
    def notify(self, user_id, ntype, title, body="", object_name=None,
               record_id=None):
        if not user_id:
            return None
        return self._row_put("mf_notifications", {
            "user_id": user_id, "ntype": ntype, "title": title,
            "body": body or "", "object_name": object_name,
            "record_id": record_id, "is_read": 0, "created_at": utcnow()})

    def notifications_for(self, user_id, unread_only=False, limit=50):
        sql = "SELECT * FROM mf_notifications WHERE user_id=? "
        if unread_only:
            sql += "AND is_read=0 "
        sql += "ORDER BY created_at DESC LIMIT ?"
        return [dict(r) for r in self._execute(
            sql, (user_id, limit)).fetchall()]

    def notification_unread_count(self, user_id):
        row = self._execute(
            "SELECT COUNT(*) AS n FROM mf_notifications "
            "WHERE user_id=? AND is_read=0", (user_id,)).fetchone()
        return row["n"]

    def notifications_mark_read(self, user_id, ids=None):
        if ids:
            marks = ",".join("?" for _ in ids)
            self._execute(
                f"UPDATE mf_notifications SET is_read=1 WHERE user_id=? "
                f"AND id IN ({marks})", (user_id, *ids))
        else:
            self._execute("UPDATE mf_notifications SET is_read=1 WHERE user_id=?",
                          (user_id,))
        self._commit()

    # ------------------------------------------------- lead conversions
    def log_lead_conversion(self, lead_id, account_id, contact_id, opportunity_id, user):
        return self._row_put("mf_lead_conversions", {
            "lead_id": lead_id, "account_id": account_id, "contact_id": contact_id,
            "opportunity_id": opportunity_id, "converted_by": user["id"],
            "converted_at": utcnow()})

    def lead_conversion(self, lead_id):
        row = self._execute("SELECT * FROM mf_lead_conversions WHERE lead_id=?"
                            " ORDER BY converted_at DESC LIMIT 1", (lead_id,)).fetchone()
        return dict(row) if row else None
