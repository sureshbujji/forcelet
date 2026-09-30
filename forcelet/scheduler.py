"""Background scheduler: run due scheduled jobs on a loop. — Forcelet platform module.

Only one process may run jobs at a time. Coordination uses a heartbeat lock
row in mf_meta, so it is safe to start the scheduler in every web worker —
only the lock holder executes jobs. Missed jobs are caught up naturally:
``run_due_scheduled_jobs`` fires anything whose interval has elapsed.

Disable the in-process scheduler with FORCELET_SCHEDULER=0 and run it as a
separate process instead (see docs/DEPLOYMENT.md).

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger("forcelet.scheduler")

LOCK_KEY = "scheduler_lock"
HEARTBEAT_TTL_SECONDS = 180
HOLDER_ID = uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def acquire_lock(store) -> bool:
    """Take the scheduler lock if it is free or its heartbeat expired."""
    raw = store.meta_kv_get(LOCK_KEY)
    now = datetime.now(timezone.utc)
    if raw:
        try:
            holder, beat = raw.split("|", 1)
            last = datetime.fromisoformat(beat)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if holder != HOLDER_ID and now - last < timedelta(seconds=HEARTBEAT_TTL_SECONDS):
                return False
        except ValueError:
            pass
    store.meta_kv_set(LOCK_KEY, f"{HOLDER_ID}|{_now_iso()}")
    # Confirm we won any race: re-read and check the holder is us.
    return store.meta_kv_get(LOCK_KEY, "").startswith(HOLDER_ID + "|")


def release_lock(store) -> None:
    if store.meta_kv_get(LOCK_KEY, "").startswith(HOLDER_ID + "|"):
        store.meta_kv_set(LOCK_KEY, "")


def run_once(store, registry, security) -> list:
    """Run due jobs once if this process holds the scheduler lock."""
    from . import automation
    if not acquire_lock(store):
        return []
    try:
        store.prune_sessions()
        store.prune_portal_sessions()
        results = automation.run_due_scheduled_jobs(store, registry, security)
        results = results + automation.run_due_scheduled_flows(store, registry, security)
        return results
    finally:
        # Refresh the heartbeat so a long job run does not look dead.
        store.meta_kv_set(LOCK_KEY, f"{HOLDER_ID}|{_now_iso()}")


def run_forever(store, registry, security, interval_seconds: int = 60):
    """Loop forever, running due jobs. Intended for a background thread."""
    log.info("scheduler started (holder %s)", HOLDER_ID)
    while True:
        time.sleep(interval_seconds)
        try:
            results = run_once(store, registry, security)
            for r in results:
                log.info("scheduled job: %s", r)
        except Exception:
            traceback.print_exc()


def start_background_thread(store, registry, security) -> threading.Thread | None:
    """Start the scheduler thread unless disabled via FORCELET_SCHEDULER=0."""
    if os.environ.get("FORCELET_SCHEDULER", "1") == "0":
        log.info("in-process scheduler disabled (FORCELET_SCHEDULER=0)")
        return None
    t = threading.Thread(target=run_forever, args=(store, registry, security),
                         daemon=True, name="forcelet-scheduler")
    t.start()
    return t
