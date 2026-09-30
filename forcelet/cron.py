"""Cron expression parsing and scheduling. — Forcelet platform module.

Supports the standard 5-field cron format::

    minute hour day-of-month month day-of-week

Each field accepts ``*``, ``*/step``, ``a,b,c`` lists, ``a-b`` ranges, and
plain values.  Month and weekday names (``jan``..``dec``, ``mon``..``sun``)
are accepted.  Both 0 and 7 mean Sunday.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations

from datetime import datetime, timedelta

_MONTHS = {n: i + 1 for i, n in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
_WEEKDAYS = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4,
             "fri": 5, "sat": 6}

_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_FIELD_NAMES = ["minute", "hour", "day of month", "month", "day of week"]


def _parse_value(token: str, lo: int, hi: int, names: dict | None) -> int:
    token = token.strip().lower()
    if names and token in names:
        return names[token]
    try:
        v = int(token)
    except ValueError:
        raise ValueError(f"bad cron value {token!r}")
    if token.startswith("+") or token.startswith("-"):
        raise ValueError(f"bad cron value {token!r}")
    if not (lo <= v <= hi):
        raise ValueError(f"cron value {v} out of range {lo}-{hi}")
    return v


def _parse_field(field: str, lo: int, hi: int, names: dict | None = None) -> set:
    field = field.strip()
    if field == "*":
        values = set(range(lo, hi + 1))
    else:
        values: set = set()
        for part in field.split(","):
            part = part.strip()
            if "/" in part:
                base, step_s = part.split("/", 1)
                try:
                    step = int(step_s)
                except ValueError:
                    raise ValueError(f"bad cron step {part!r}")
                if step < 1:
                    raise ValueError(f"cron step must be >= 1 in {part!r}")
                span = (lo, hi) if base in ("", "*") else None
                if span is None:
                    if "-" in base:
                        a_s, b_s = base.split("-", 1)
                        span = (_parse_value(a_s, lo, hi, names),
                                _parse_value(b_s, lo, hi, names))
                    else:
                        v = _parse_value(base, lo, hi, names)
                        span = (v, hi)
                a, b = span
                if a > b:
                    raise ValueError(f"bad cron range {part!r}")
                values.update(range(a, b + 1, step))
            elif "-" in part:
                a_s, b_s = part.split("-", 1)
                a = _parse_value(a_s, lo, hi, names)
                b = _parse_value(b_s, lo, hi, names)
                if a > b:
                    raise ValueError(f"bad cron range {part!r}")
                values.update(range(a, b + 1))
            else:
                values.add(_parse_value(part, lo, hi, names))
    # normalize Sunday: 7 -> 0 for the weekday field
    if hi == 7:
        values = {0 if v == 7 else v for v in values}
    return {v for v in values if lo <= v <= (6 if hi == 7 else hi)}


class CronSpec:
    """Parsed 5-field cron schedule."""

    def __init__(self, minutes: set, hours: set, doms: set,
                 months: set, dows: set, source: str):
        self.minutes, self.hours = minutes, hours
        self.doms, self.months, self.dows = doms, months, dows
        self.source = source

    def matches(self, dt: datetime) -> bool:
        # cron OR-semantics: dom/dow — a day matches when either field is
        # unrestricted, or when the day satisfies either restricted field.
        # Internal weekday convention is cron's: Sunday=0 .. Saturday=6.
        dow = (dt.weekday() + 1) % 7
        dom_star = self.doms == set(range(1, 32))
        dow_star = self.dows == set(range(0, 7))
        if dom_star and dow_star:
            day_ok = True
        elif dom_star:
            day_ok = dow in self.dows
        elif dow_star:
            day_ok = dt.day in self.doms
        else:
            day_ok = dt.day in self.doms or dow in self.dows
        return (dt.minute in self.minutes and dt.hour in self.hours
                and dt.month in self.months and day_ok)

    def next_occurrence(self, after: datetime) -> datetime:
        """Next datetime strictly after ``after`` matching the schedule."""
        cand = (after.replace(second=0, microsecond=0) + timedelta(minutes=1))
        limit = after + timedelta(days=366 + 2)
        while cand <= limit:
            if self.matches(cand):
                return cand
            cand += timedelta(minutes=1)
        raise ValueError(f"no occurrence of {self.source!r} within a year")

    def describe(self) -> str:
        return _describe(self)


def parse(expr: str) -> CronSpec:
    """Parse a 5-field cron expression; raises ValueError on bad input."""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        raise ValueError(
            f"cron expression needs 5 fields (minute hour dom month dow), got {len(parts)}")
    mins = _parse_field(parts[0], 0, 59)
    hours = _parse_field(parts[1], 0, 23)
    doms = _parse_field(parts[2], 1, 31)
    months = _parse_field(parts[3], 1, 12, _MONTHS)
    dows = _parse_field(parts[4], 0, 7, _WEEKDAYS)
    return CronSpec(mins, hours, doms, months, dows, " ".join(parts))


def _describe(spec: CronSpec) -> str:
    mins = sorted(spec.minutes)
    hours = sorted(spec.hours)
    all_hr = spec.hours == set(range(0, 24))
    all_dom = spec.doms == set(range(1, 32))
    all_mon = spec.months == set(range(1, 13))
    all_dow = spec.dows == set(range(0, 7))
    if len(mins) == 1 and len(hours) == 1 and all_dom and all_mon:
        when = f"{hours[0]:02d}:{mins[0]:02d}"
        if all_dow:
            return f"Daily at {when}"
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        return f"Weekly on {', '.join(names[d] for d in sorted(spec.dows))} at {when}"
    if all_hr and all_dom and all_mon and all_dow and len(mins) > 1 and mins[0] == 0:
        diffs = {b - a for a, b in zip(mins, mins[1:])}
        if len(diffs) == 1 and 60 % next(iter(diffs)) == 0:
            return f"Every {next(iter(diffs))} minutes"
    return f"Cron '{spec.source}'"


def is_due(cron_expr: str, last_run_iso: str | None, now: datetime) -> bool:
    """True when the cron schedule has an occurrence at or before ``now``
    that hasn't been consumed yet (i.e. after ``last_run_iso``)."""
    spec = parse(cron_expr)
    if not last_run_iso:
        return True  # never ran: run once, then follow the schedule
    try:
        last = datetime.fromisoformat(last_run_iso)
    except Exception:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    try:
        nxt = spec.next_occurrence(last)
    except ValueError:
        return False
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=now.tzinfo)
    return nxt <= now
