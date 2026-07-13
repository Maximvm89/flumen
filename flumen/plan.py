"""Production planning: estimates, due dates and schedule health for tasks.

The plan is not a separate document — it lives on the task records themselves
(`estimate_days`, `due`) next to the assignees and statuses artists already
update by working. The Plan tab confronts the remaining estimated work with
the team's capacity up to the project deadline, and `propose_schedule` spreads
each artist's queue over the remaining workdays in pipeline order (hybrid
planning: the proposal is applied explicitly and manual dates always win).

Pure functions (dates passed in) — unit-testable without a server.
"""

from __future__ import annotations

import datetime
import math

# project_settings "planning" block, merged over these defaults.
DEFAULT_PLANNING = {
    "deadline": "",              # "YYYY-MM-DD"; empty = no deadline math
    "due_soon_days": 3,          # within this many workdays of due -> due_soon
    # Estimate (workdays) used when a task has none, per step. Tune per show.
    "default_estimates": {
        "model": 3.0, "surface": 2.0, "rig": 3.0, "dressing": 2.0,
        "layout": 1.0, "animation": 3.0, "lighting": 2.0,
    },
    "default_estimate": 2.0,     # steps not listed above
    # username -> workdays per week (default 5). Part-timers: {"anna": 2}.
    "availability": {},
}

# Pipeline order inside one entity: earlier ranks must be scheduled earlier.
STEP_RANK = {
    "asset": {"model": 0, "surface": 1, "rig": 1, "dressing": 2},
    "shot": {"layout": 0, "animation": 1, "lighting": 2},
}

HEALTH_DONE = "done"
HEALTH_LATE = "late"
HEALTH_DUE_SOON = "due_soon"
HEALTH_ON_TRACK = "on_track"
HEALTH_UNPLANNED = "unplanned"
HEALTH_LABELS = {
    HEALTH_DONE: "Done", HEALTH_LATE: "LATE", HEALTH_DUE_SOON: "Due soon",
    HEALTH_ON_TRACK: "On track", HEALTH_UNPLANNED: "Unplanned",
}


def planning_config(project_settings: dict | None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v)
           for k, v in DEFAULT_PLANNING.items()}
    for k, v in ((project_settings or {}).get("planning") or {}).items():
        if isinstance(cfg.get(k), dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def parse_date(s: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(s or "").strip())
    except ValueError:
        return None


def workdays_between(start: datetime.date, end: datetime.date) -> int:
    """Mon-Fri days in (start, end] — capacity remaining AFTER `start`."""
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def add_workdays(start: datetime.date, n: float) -> datetime.date:
    """The date `n` workdays after `start` (fractions round up: 0.5d of work
    still needs the day). n <= 0 -> start."""
    left = math.ceil(n)
    d = start
    while left > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def estimate_of(task: dict, cfg: dict) -> float:
    est = task.get("estimate_days")
    try:
        if est is not None and float(est) > 0:
            return float(est)
    except (TypeError, ValueError):
        pass
    per_step = cfg.get("default_estimates") or {}
    return float(per_step.get(task.get("step", ""),
                              cfg.get("default_estimate", 2.0)))


def step_rank(task: dict) -> int:
    return STEP_RANK.get(task.get("type", ""), {}).get(task.get("step", ""), 9)


def health(task: dict, today: datetime.date, cfg: dict) -> str:
    if task.get("status") == "done":
        return HEALTH_DONE
    due = parse_date(task.get("due", ""))
    if due is None:
        return HEALTH_UNPLANNED
    if due < today:
        return HEALTH_LATE
    if workdays_between(today, due) <= int(cfg.get("due_soon_days", 3)):
        return HEALTH_DUE_SOON
    return HEALTH_ON_TRACK


def availability_of(username: str, cfg: dict) -> float:
    try:
        return max(0.0, min(5.0, float(
            (cfg.get("availability") or {}).get(username, 5))))
    except (TypeError, ValueError):
        return 5.0


def plan_summary(tasks: list[dict], roster: list[dict],
                 today: datetime.date, cfg: dict) -> dict:
    """The capacity-vs-remaining header math + per-artist load. Remaining =
    estimates of every not-done task; capacity = workdays to the deadline
    scaled by each active artist's availability."""
    deadline = parse_date(cfg.get("deadline", ""))
    wd_left = workdays_between(today, deadline) if deadline else 0
    active = [u for u in (roster or []) if u.get("active")]
    per_artist: dict[str, dict] = {}
    unassigned_days = 0.0
    remaining_days = 0.0
    for t in tasks or []:
        if t.get("status") == "done":
            continue
        est = estimate_of(t, cfg)
        remaining_days += est
        names = t.get("assignees") or []
        if not names:
            unassigned_days += est
        for u in names:
            a = per_artist.setdefault(u, {"remaining": 0.0, "tasks": 0,
                                          "capacity": 0.0, "late": 0})
            a["remaining"] += est / len(names)
            a["tasks"] += 1
            if health(t, today, cfg) == HEALTH_LATE:
                a["late"] += 1
    capacity_days = 0.0
    for u in active:
        cap = wd_left * availability_of(u["username"], cfg) / 5.0
        capacity_days += cap
        if u["username"] in per_artist:
            per_artist[u["username"]]["capacity"] = cap
    return {
        "deadline": deadline.isoformat() if deadline else "",
        "workdays_left": wd_left,
        "capacity_days": round(capacity_days, 1),
        "remaining_days": round(remaining_days, 1),
        "unassigned_days": round(unassigned_days, 1),
        "fits": (remaining_days <= capacity_days) if deadline else True,
        "per_artist": per_artist,
    }


def propose_schedule(tasks: list[dict], today: datetime.date,
                     cfg: dict) -> tuple[dict[str, str], list[str]]:
    """Hybrid auto-plan: per artist, queue their not-done tasks in pipeline
    order (model before surface/rig before dressing; layout -> animation ->
    lighting), walk the queue accumulating estimates over their available
    workdays, and propose a due date per task. Returns ({task_id: date},
    warnings). Tasks with no assignee are not scheduled (warned instead);
    a queue running past the deadline warns too."""
    deadline = parse_date(cfg.get("deadline", ""))
    queues: dict[str, list[dict]] = {}
    warnings: list[str] = []
    for t in tasks or []:
        if t.get("status") == "done":
            continue
        names = t.get("assignees") or []
        if not names:
            warnings.append(f"unassigned (not scheduled): "
                            f"{t.get('entity')} · {t.get('step')}")
            continue
        # the first assignee owns the schedule slot
        queues.setdefault(names[0], []).append(t)

    proposal: dict[str, str] = {}
    for user, queue in sorted(queues.items()):
        queue.sort(key=lambda t: (step_rank(t), t.get("type", ""),
                                  t.get("entity", ""), t.get("step", "")))
        pace = availability_of(user, cfg) / 5.0
        cursor = 0.0                       # workdays of queued work so far
        for t in queue:
            cursor += estimate_of(t, cfg)
            due = add_workdays(today, cursor / pace if pace else cursor)
            proposal[t["id"]] = due.isoformat()
            if deadline and due > deadline:
                warnings.append(f"{user}: {t.get('entity')} · {t.get('step')} "
                                f"lands {due.isoformat()} — past the deadline")
    return proposal, warnings
