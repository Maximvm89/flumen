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


# Statuses outside the plan: finished work and tasks cut from the show.
INACTIVE_STATUSES = frozenset({"done", "omitted"})


def health(task: dict, today: datetime.date, cfg: dict) -> str:
    if task.get("status") in INACTIVE_STATUSES:
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
        if t.get("status") in INACTIVE_STATUSES:
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


def load_shot_elements(sftp, remote_root: str,
                       tasks: list[dict]) -> dict[str, list[str]]:
    """{shot_entity: [asset entities in its element list]} for every shot task,
    from the shots' assembly.json files (read concurrently when supported).
    Feeds the rig->layout dependency in propose_schedule."""
    import json as _json
    from . import elements as elements_mod
    ents = sorted({t.get("entity") for t in tasks or []
                   if t.get("type") == "shot" and t.get("entity")})
    rr = remote_root.rstrip("/")
    paths = {e: rr + "/" + elements_mod.assembly_rel(e) for e in ents}
    reader = getattr(sftp, "read_many", None)
    if callable(reader):
        texts = reader(list(paths.values()))
    else:
        texts = {p: sftp.read_text(p) for p in paths.values()}
    out: dict[str, list[str]] = {}
    for ent, p in paths.items():
        try:
            doc = _json.loads(texts.get(p) or "{}")
        except ValueError:
            doc = {}
        out[ent] = [el.get("asset") for el in (doc.get("elements") or [])
                    if el.get("kind") == "asset" and el.get("asset")
                    and el.get("enabled", True)]
    return out


# Pipeline depth for dependency-safe scheduling order: every task's
# prerequisites always have a strictly smaller depth.
STEP_DEPTH = {
    ("asset", "model"): 0,
    ("asset", "surface"): 1, ("asset", "rig"): 1,
    ("asset", "dressing"): 2,
    ("shot", "layout"): 3, ("shot", "animation"): 4, ("shot", "lighting"): 5,
}


def task_dependencies(tasks: list[dict],
                      shot_elements: dict[str, list[str]] | None = None
                      ) -> dict[str, list[str]]:
    """{task_id: [prerequisite task_ids]} across the whole production:
    surface/rig/dressing wait for their asset's model; a shot's layout waits
    for the rigs of every asset in its element list (model when the asset has
    no rig task); animation waits for the shot's layout (+ the rigs);
    lighting waits for animation (the alembic caches)."""
    by_key = {(t.get("type"), t.get("entity"), t.get("step")): t["id"]
              for t in tasks or [] if t.get("id")}
    deps: dict[str, list[str]] = {}
    for t in tasks or []:
        ttype, ent, step = t.get("type"), t.get("entity"), t.get("step")
        found: list[str] = []

        def _dep(tt, e, s):
            tid = by_key.get((tt, e, s))
            if tid and tid != t.get("id"):
                found.append(tid)

        if ttype == "asset" and step in ("surface", "rig", "dressing"):
            _dep("asset", ent, "model")
        elif ttype == "shot":
            elements = (shot_elements or {}).get(ent, [])
            if step == "layout":
                for a in elements:
                    if ("asset", a, "rig") in by_key:
                        _dep("asset", a, "rig")
                    else:
                        _dep("asset", a, "model")
            elif step == "animation":
                _dep("shot", ent, "layout")
                for a in elements:
                    _dep("asset", a, "rig")
            elif step == "lighting":
                _dep("shot", ent, "animation")
        if found:
            deps[t["id"]] = found
    return deps


def propose_schedule(tasks: list[dict], today: datetime.date, cfg: dict,
                     shot_elements: dict[str, list[str]] | None = None
                     ) -> tuple[dict[str, str], list[str]]:
    """Hybrid auto-plan, dependency-aware: tasks are scheduled in pipeline
    depth order; each starts no earlier than BOTH its artist's previous task
    and every prerequisite's finish — so a surface never lands before its
    model, layout waits for the element rigs, animation for layout, lighting
    for animation. Done/omitted prerequisites don't block. Returns
    ({task_id: due date}, warnings): unassigned tasks aren't scheduled (and
    block their dependents' accuracy — warned), overflow past the deadline
    warns too."""
    deadline = parse_date(cfg.get("deadline", ""))
    deps = task_dependencies(tasks, shot_elements)
    by_id = {t["id"]: t for t in tasks or [] if t.get("id")}
    warnings: list[str] = []

    todo = [t for t in tasks or []
            if t.get("status") not in INACTIVE_STATUSES and t.get("id")]
    todo.sort(key=lambda t: (
        STEP_DEPTH.get((t.get("type"), t.get("step")), 9),
        t.get("entity", ""), t.get("step", "")))

    finish: dict[str, float] = {}      # task_id -> workdays from today
    cursor: dict[str, float] = {}      # artist -> end of their queue so far
    proposal: dict[str, str] = {}
    for t in todo:
        label = f"{t.get('entity')} · {t.get('step')}"
        names = t.get("assignees") or []
        if not names:
            warnings.append(f"unassigned (not scheduled): {label}")
            continue
        user = names[0]                # the first assignee owns the slot
        start = cursor.get(user, 0.0)
        for d in deps.get(t["id"], []):
            dt = by_id.get(d)
            if dt is None or dt.get("status") in INACTIVE_STATUSES:
                continue               # finished/cut prerequisites don't block
            if d in finish:
                start = max(start, finish[d])
            else:
                warnings.append(f"{label}: prerequisite "
                                f"{dt.get('entity')} · {dt.get('step')} is "
                                f"unscheduled (unassigned?) — date unreliable")
        pace = availability_of(user, cfg) / 5.0
        end = start + estimate_of(t, cfg) / (pace or 1.0)
        finish[t["id"]] = end
        cursor[user] = end
        due = add_workdays(today, end)
        proposal[t["id"]] = due.isoformat()
        if deadline and due > deadline:
            warnings.append(f"{user}: {label} lands {due.isoformat()} — "
                            f"past the deadline")
    return proposal, warnings
