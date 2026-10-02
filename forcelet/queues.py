"""Shared queues: named groups of users used by approvals, notifications,
and Omni-Channel routing.

A queue is setup data (admin-only to manage). Membership is an explicit list
of user ids. ``queue_member_ids`` resolves members, ignoring unknown ids.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations


QUEUE_TABLE = "mf_queues"


def get_queue(store, queue_id: str):
    return store.config_get(QUEUE_TABLE, queue_id)


def find_queue(store, name: str):
    for q in store.config_all(QUEUE_TABLE):
        if q.get("name") == name:
            return q
    return None


def queue_member_ids(store, security, queue) -> set:
    """User ids that belong to a queue (dict or id)."""
    if isinstance(queue, str):
        queue = get_queue(store, queue) or find_queue(store, queue)
    if not queue:
        return set()
    ids = set()
    for uid in queue.get("members") or []:
        if security.get_user(uid):
            ids.add(uid)
    return ids


def validate_queue(defn: dict) -> str | None:
    if not (defn.get("name") or "").strip():
        return "Queue name is required"
    members = defn.get("members") or []
    if not isinstance(members, list):
        return "members must be a list of user ids"
    return None
