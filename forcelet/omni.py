"""Omni-Channel: presence, queue routing and agent capacity.

Work items (cases, chats, leads) are enqueued per service channel and routed
to agents who are Available, belong to the routing queue, and still have
capacity. Least-loaded eligible agent wins; declined work returns to the
queue for re-routing.

Tables (all generic config tables):
  mf_presence         {id: user_id, status, updated_at}
  mf_service_channels {name, object_name, active}
  mf_routing_configs  {name, channel_id, queue_id, priority, active}
  mf_work_items       {object_name, record_id, channel_id, queue_id,
                       status, assigned_to, priority, created_at, assigned_at}
  mf_agent_capacity   {id: user_id, max_capacity}

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from . import queues as _queues
from .store import new_id, utcnow

PRESENCE_TABLE = "mf_presence"
CHANNEL_TABLE = "mf_service_channels"
ROUTING_TABLE = "mf_routing_configs"
WORK_TABLE = "mf_work_items"
CAPACITY_TABLE = "mf_agent_capacity"

PRESENCE_STATUSES = ("Available", "Busy", "Offline")
WORK_STATUSES = ("queued", "assigned", "completed", "cancelled")
DEFAULT_CAPACITY = 5


# ------------------------------------------------------------ presence
def set_presence(store, user_id: str, status: str) -> dict:
    if status not in PRESENCE_STATUSES:
        raise ValueError(f"status must be one of {PRESENCE_STATUSES}")
    store.config_put(PRESENCE_TABLE, {"id": user_id, "user_id": user_id,
                                      "status": status, "updated_at": utcnow()})
    return get_presence(store, user_id)


def get_presence(store, user_id: str) -> dict:
    row = store.config_get(PRESENCE_TABLE, user_id)
    if row:
        return row
    return {"id": user_id, "user_id": user_id, "status": "Offline",
            "updated_at": None}


def all_presence(store) -> list:
    return store.config_all(PRESENCE_TABLE)


# ------------------------------------------------------------ capacity
def get_capacity(store, user_id: str) -> dict:
    row = store.config_get(CAPACITY_TABLE, user_id)
    if row:
        return row
    return {"id": user_id, "user_id": user_id, "max_capacity": DEFAULT_CAPACITY}


def set_capacity(store, user_id: str, max_capacity: int) -> dict:
    try:
        max_capacity = int(max_capacity)
    except (TypeError, ValueError):
        raise ValueError("max_capacity must be an integer")
    if max_capacity < 0:
        raise ValueError("max_capacity cannot be negative")
    store.config_put(CAPACITY_TABLE, {"id": user_id, "user_id": user_id,
                                      "max_capacity": max_capacity})
    return get_capacity(store, user_id)


def agent_load(store, user_id: str) -> int:
    return sum(1 for w in store.config_all(WORK_TABLE)
               if w.get("assigned_to") == user_id
               and w.get("status") == "assigned")


def has_capacity(store, user_id: str) -> bool:
    return agent_load(store, user_id) < get_capacity(store, user_id)["max_capacity"]


# ------------------------------------------------------------ channels & routing
def ensure_defaults(store) -> None:
    if not store.config_all(CHANNEL_TABLE):
        store.config_put(CHANNEL_TABLE, {"name": "Cases", "object_name": "Case",
                                          "active": True})
        store.config_put(CHANNEL_TABLE, {"name": "Leads", "object_name": "Lead",
                                          "active": True})


def find_channel(store, ref: str):
    if not ref:
        return None
    ch = store.config_get(CHANNEL_TABLE, ref)
    if ch:
        return ch
    for cand in store.config_all(CHANNEL_TABLE):
        if cand.get("name") == ref:
            return cand
    return None


def routing_configs_for(store, channel_id: str, queue_id: str | None = None) -> list:
    cfgs = [c for c in store.config_all(ROUTING_TABLE)
            if c.get("active", True) and c.get("channel_id") == channel_id]
    if queue_id:
        cfgs = [c for c in cfgs if c.get("queue_id") == queue_id]
    return sorted(cfgs, key=lambda c: c.get("priority", 0))


# ------------------------------------------------------------ work items
def enqueue_work(store, spec: dict) -> dict:
    """Create a queued work item. spec: {object_name, record_id,
    channel (id|name), queue (id|name, optional), priority}."""
    channel = find_channel(store, spec.get("channel") or "")
    if not channel:
        raise ValueError("Unknown service channel")
    if not channel.get("active", True):
        raise ValueError("Channel is not active")
    queue_id = None
    if spec.get("queue"):
        q = _queues.get_queue(store, spec["queue"]) or _queues.find_queue(store, spec["queue"])
        if not q:
            raise ValueError("Unknown queue")
        queue_id = q["id"]
    else:
        cfgs = routing_configs_for(store, channel["id"])
        if cfgs:
            queue_id = cfgs[0].get("queue_id")
    try:
        priority = int(spec.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0
    item = {"object_name": spec.get("object_name"), "record_id": spec.get("record_id"),
            "channel_id": channel["id"], "channel_name": channel.get("name"),
            "queue_id": queue_id, "status": "queued", "assigned_to": None,
            "priority": priority, "created_at": utcnow(), "assigned_at": None}
    wid = store.config_put(WORK_TABLE, item)
    return store.config_get(WORK_TABLE, wid)


def get_work(store, work_id: str):
    return store.config_get(WORK_TABLE, work_id)


def _eligible_agents(store, security, item: dict) -> list:
    """Available queue members with spare capacity, least-loaded first."""
    queue_id = item.get("queue_id")
    if not queue_id:
        return []
    members = _queues.queue_member_ids(store, security, queue_id)
    eligible = []
    for uid in members:
        if get_presence(store, uid)["status"] != "Available":
            continue
        if not has_capacity(store, uid):
            continue
        eligible.append(uid)
    eligible.sort(key=lambda uid: (agent_load(store, uid), uid))
    return eligible


def route_work(store, security, work_id: str | None = None) -> dict:
    """Route one queued item (or all queued) to the least-loaded eligible agent.

    Returns {"routed": [...], "unrouted": [...]} with work-item dicts.
    """
    items = [store.config_get(WORK_TABLE, work_id)] if work_id else \
        [w for w in store.config_all(WORK_TABLE) if w.get("status") == "queued"]
    routed, unrouted = [], []
    for item in items:
        if not item or item.get("status") != "queued":
            continue
        agents = _eligible_agents(store, security, item)
        if not agents:
            unrouted.append(item)
            continue
        item["assigned_to"] = agents[0]
        item["status"] = "assigned"
        item["assigned_at"] = utcnow()
        store.config_put(WORK_TABLE, item)
        routed.append(item)
    return {"routed": routed, "unrouted": unrouted}


def accept_work(store, work_id: str, user_id: str) -> dict:
    item = get_work(store, work_id)
    if not item:
        raise ValueError("Work item not found")
    if item.get("status") != "assigned" or item.get("assigned_to") != user_id:
        raise ValueError("Work item is not assigned to you")
    return item  # assignment already effective; accept is an acknowledgement


def decline_work(store, work_id: str, user_id: str) -> dict:
    item = get_work(store, work_id)
    if not item:
        raise ValueError("Work item not found")
    if item.get("status") != "assigned" or item.get("assigned_to") != user_id:
        raise ValueError("Work item is not assigned to you")
    item["status"] = "queued"
    item["assigned_to"] = None
    item["assigned_at"] = None
    store.config_put(WORK_TABLE, item)
    return item


def complete_work(store, work_id: str) -> dict:
    item = get_work(store, work_id)
    if not item:
        raise ValueError("Work item not found")
    item["status"] = "completed"
    store.config_put(WORK_TABLE, item)
    return item


def queue_snapshot(store, queue_id: str) -> dict:
    items = [w for w in store.config_all(WORK_TABLE) if w.get("queue_id") == queue_id]
    return {
        "queued": [w for w in items if w.get("status") == "queued"],
        "assigned": [w for w in items if w.get("status") == "assigned"],
    }
