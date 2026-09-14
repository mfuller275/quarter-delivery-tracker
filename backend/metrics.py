"""Compute per-team delivery / on-track metrics from Azure DevOps work items."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .config import Settings


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return None


def normalize_item(work_item: dict, settings: Settings, today: date) -> dict:
    fields = work_item.get("fields", {})

    team = fields.get(settings.team_field) or "Unassigned"
    if isinstance(team, dict):
        team = team.get("displayName") or "Unassigned"

    state = fields.get("System.State", "")
    completed = state in settings.completed_state_set

    points = fields.get(settings.points_field)
    if points in (None, ""):
        points = fields.get(settings.effort_field)
    try:
        points = float(points) if points not in (None, "") else 0.0
    except (TypeError, ValueError):
        points = 0.0

    target_date = _parse_date(fields.get(settings.target_date_field))

    tags_raw = fields.get("System.Tags") or ""
    tag_set = {t.strip().lower() for t in tags_raw.split(";") if t.strip()}
    blocked_field = str(fields.get("Microsoft.VSTS.CMMI.Blocked") or "").lower() == "yes"
    blocked = (bool(tag_set & settings.blocked_tag_set) or blocked_field) and not completed

    overdue = bool(target_date and target_date < today and not completed)

    assigned = fields.get("System.AssignedTo")
    assigned_name = (
        assigned.get("displayName") if isinstance(assigned, dict) else (assigned or "Unassigned")
    )

    return {
        "id": work_item.get("id"),
        "title": fields.get("System.Title", ""),
        "type": fields.get("System.WorkItemType", ""),
        "state": state,
        "team": str(team),
        "assignedTo": assigned_name,
        "points": points,
        "targetDate": target_date.isoformat() if target_date else None,
        "completed": completed,
        "blocked": blocked,
        "overdue": overdue,
    }


def _status(basis_pct: float, expected_pct: float, overdue: int, remaining_points: float,
            projected_date: date | None, quarter_end: date, total_days: int) -> str:
    if remaining_points <= 0:
        return "on-track"
    delta = basis_pct - expected_pct
    burndown_ok = projected_date is not None and projected_date <= quarter_end
    grace = timedelta(days=max(total_days * 0.1, 1))
    if delta >= -5 and overdue == 0 and burndown_ok:
        return "on-track"
    if delta >= -20 and overdue <= 2 and (projected_date is not None and projected_date <= quarter_end + grace):
        return "at-risk"
    return "off-track"


def compute_summary(raw_items: list[dict], settings: Settings) -> dict:
    start, end = settings.quarter_range()
    today = date.today()
    total_days = max((end - start).days, 1)
    elapsed_days = min(max((today - start).days, 0), total_days)
    time_elapsed_pct = round(elapsed_days / total_days * 100, 1)

    items = [normalize_item(wi, settings, today) for wi in raw_items]
    excluded = settings.excluded_state_set
    excluded_types = settings.excluded_type_set
    items = [
        i for i in items
        if i["state"] not in excluded and i["type"] not in excluded_types
    ]

    teams: dict[str, list[dict]] = {}
    for item in items:
        teams.setdefault(item["team"], []).append(item)

    team_results: list[dict] = []
    for name, team_items in teams.items():
        total = len(team_items)
        completed = sum(1 for i in team_items if i["completed"])
        total_points = sum(i["points"] for i in team_items)
        completed_points = sum(i["points"] for i in team_items if i["completed"])
        overdue_items = [i for i in team_items if i["overdue"]]
        blocked_items = [i for i in team_items if i["blocked"]]

        item_pct = round(completed / total * 100, 1) if total else 0.0
        points_pct = round(completed_points / total_points * 100, 1) if total_points else 0.0
        remaining_points = round(total_points - completed_points, 2)

        velocity = (completed_points / elapsed_days) if elapsed_days > 0 else 0.0
        if velocity > 0 and remaining_points > 0:
            projected_date = today + timedelta(days=remaining_points / velocity)
        elif remaining_points <= 0:
            projected_date = today
        else:
            projected_date = None

        basis_pct = points_pct if total_points else item_pct
        status = _status(
            basis_pct, time_elapsed_pct, len(overdue_items),
            remaining_points, projected_date, end, total_days,
        )

        at_risk = sorted(
            [i for i in team_items if (i["overdue"] or i["blocked"]) and not i["completed"]],
            key=lambda i: (not i["overdue"], not i["blocked"], i["targetDate"] or "9999"),
        )

        team_results.append({
            "team": name,
            "status": status,
            "totalItems": total,
            "completedItems": completed,
            "itemCompletionPct": item_pct,
            "totalPoints": round(total_points, 1),
            "completedPoints": round(completed_points, 1),
            "remainingPoints": remaining_points,
            "pointsCompletionPct": points_pct,
            "expectedPct": time_elapsed_pct,
            "overdueCount": len(overdue_items),
            "blockedCount": len(blocked_items),
            "projectedCompletion": projected_date.isoformat() if projected_date else None,
            "burndownOnTrack": projected_date is not None and projected_date <= end,
            "atRiskItems": [
                {
                    "id": i["id"], "title": i["title"], "type": i["type"], "state": i["state"],
                    "assignedTo": i["assignedTo"], "targetDate": i["targetDate"],
                    "overdue": i["overdue"], "blocked": i["blocked"], "points": i["points"],
                }
                for i in at_risk
            ],
        })

    severity = {"off-track": 0, "at-risk": 1, "on-track": 2}
    team_results.sort(key=lambda t: (severity[t["status"]], -t["overdueCount"], t["team"]))

    totals = {
        "teams": len(team_results),
        "onTrack": sum(1 for t in team_results if t["status"] == "on-track"),
        "atRisk": sum(1 for t in team_results if t["status"] == "at-risk"),
        "offTrack": sum(1 for t in team_results if t["status"] == "off-track"),
        "totalItems": sum(t["totalItems"] for t in team_results),
        "completedItems": sum(t["completedItems"] for t in team_results),
        "totalPoints": round(sum(t["totalPoints"] for t in team_results), 1),
        "completedPoints": round(sum(t["completedPoints"] for t in team_results), 1),
        "overdueItems": sum(t["overdueCount"] for t in team_results),
        "blockedItems": sum(t["blockedCount"] for t in team_results),
    }

    return {
        "quarter": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "today": today.isoformat(),
            "timeElapsedPct": time_elapsed_pct,
            "daysRemaining": max((end - today).days, 0),
        },
        "config": {
            "org": settings.ado_org,
            "project": settings.ado_project,
            "teamField": settings.team_field,
            "usingSavedQuery": bool(settings.query_id),
        },
        "totals": totals,
        "teams": team_results,
    }


# --- Feature drill-down --------------------------------------------------

_QUARTER_RE = re.compile(r"Q\s*([1-4])", re.IGNORECASE)
_YEAR_RE = re.compile(r"(20\d{2})")
# Release encoded as YYYY.Q (e.g. "2026.3" == 2026 Q3).
_YEAR_DOT_Q_RE = re.compile(r"\b(20\d{2})\.([1-4])\b")

_HIERARCHY_CHILD = "System.LinkTypes.Hierarchy-Forward"
_HIERARCHY_PARENT = "System.LinkTypes.Hierarchy-Reverse"


def parse_release_quarter(value) -> dict | None:
    """Extract a quarter/year from a Release value.

    Handles the "YYYY.Q" form (e.g. "2026.3") and the "Q3 2026" / "2026 Q3" form.
    """
    if not value:
        return None
    text = str(value)
    dot_match = _YEAR_DOT_Q_RE.search(text)
    if dot_match:
        year = int(dot_match.group(1))
        quarter = int(dot_match.group(2))
        return {"label": f"Q{quarter} {year}", "quarter": quarter, "year": year}
    q_match = _QUARTER_RE.search(text)
    if not q_match:
        return None
    year_match = _YEAR_RE.search(text)
    quarter = int(q_match.group(1))
    year = int(year_match.group(1)) if year_match else None
    label = f"Q{quarter} {year}" if year else f"Q{quarter}"
    return {"label": label, "quarter": quarter, "year": year}


def _points_of(fields: dict, settings: Settings) -> float:
    val = fields.get(settings.points_field)
    if val in (None, ""):
        val = fields.get(settings.effort_field)
    try:
        return float(val) if val not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _child_ids(feature: dict) -> list[int]:
    ids: list[int] = []
    for rel in feature.get("relations", []) or []:
        if rel.get("rel") == _HIERARCHY_CHILD:
            tail = (rel.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                ids.append(int(tail))
    return ids


def collect_child_ids(features: list[dict]) -> list[int]:
    seen: set[int] = set()
    for feat in features:
        seen.update(_child_ids(feat))
    return sorted(seen)


def _parent_id(feature: dict) -> int | None:
    for rel in feature.get("relations", []) or []:
        if rel.get("rel") == _HIERARCHY_PARENT:
            tail = (rel.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                return int(tail)
    return None


def collect_parent_ids(features: list[dict]) -> list[int]:
    seen: set[int] = set()
    for feat in features:
        pid = _parent_id(feat)
        if pid is not None:
            seen.add(pid)
    return sorted(seen)


def child_records(feat: dict, children_by_id: dict[int, dict], settings: Settings) -> list[dict]:
    """Return the non-excluded child work items (PBIs) of a feature, each with its
    own scrum-team assignment, so PBIs can be attributed to dev squads."""
    excluded = settings.excluded_state_set
    completed = settings.completed_state_set
    recs: list[dict] = []
    for cid in _child_ids(feat):
        ch = children_by_id.get(cid)
        if not ch:
            continue
        f = ch.get("fields", {})
        item_type = f.get("System.WorkItemType", "")
        state = f.get("System.State", "")
        if state in excluded or item_type in settings.excluded_type_set:
            continue
        team = f.get(settings.team_field) or ""
        if isinstance(team, dict):
            team = team.get("displayName") or ""
        assigned = f.get("System.AssignedTo")
        assigned_name = (
            assigned.get("displayName") if isinstance(assigned, dict) else (assigned or "Unassigned")
        )
        recs.append({
            "id": ch.get("id"),
            "title": f.get("System.Title", ""),
            "type": f.get("System.WorkItemType", ""),
            "state": state,
            "team": str(team),
            "iterationPath": f.get(settings.iteration_field) or None,
            "tags": f.get("System.Tags") or "",
            "assignedTo": assigned_name,
            "points": _points_of(f, settings),
            "completed": state in completed,
        })
    return recs


def feature_metrics(feat: dict, children_by_id: dict[int, dict], settings: Settings) -> dict:
    """Per-feature completion (count-based) with quarter and scrum team."""
    completed_states = settings.completed_state_set
    excluded_states = settings.excluded_state_set
    fields = feat.get("fields", {})
    state = fields.get("System.State", "")
    feature_completed = state in completed_states
    parsed = parse_release_quarter(fields.get(settings.release_field))

    team = fields.get(settings.team_field) or "Unassigned"
    if isinstance(team, dict):
        team = team.get("displayName") or "Unassigned"

    assigned = fields.get("System.AssignedTo")
    owner = assigned.get("displayName") if isinstance(assigned, dict) else (assigned or None)

    children = [
        children_by_id[c]
        for c in _child_ids(feat)
        if c in children_by_id
        and children_by_id[c].get("fields", {}).get("System.State") not in excluded_states
        and children_by_id[c].get("fields", {}).get("System.WorkItemType") not in settings.excluded_type_set
    ]
    total = len(children)
    done = sum(1 for c in children if c.get("fields", {}).get("System.State") in completed_states)
    total_points = sum(_points_of(c.get("fields", {}), settings) for c in children)
    done_points = sum(
        _points_of(c.get("fields", {}), settings)
        for c in children
        if c.get("fields", {}).get("System.State") in completed_states
    )

    if total == 0:
        pct = 100.0 if feature_completed else 0.0
        basis = "feature-state"
    else:
        pct = round(done / total * 100, 1)
        basis = "count"

    target = _parse_date(fields.get(settings.target_date_field))

    return {
        "id": feat.get("id"),
        "title": fields.get("System.Title", ""),
        "state": state,
        "completed": feature_completed,
        "team": str(team),
        "owner": owner,
        "quarter": parsed["label"] if parsed else None,
        "quarterNum": parsed["quarter"] if parsed else None,
        "year": parsed["year"] if parsed else None,
        "release": fields.get(settings.release_field) or None,
        "iterationPath": fields.get(settings.iteration_field) or None,
        "parentId": _parent_id(feat),
        "tags": fields.get("System.Tags") or "",
        "percentComplete": pct,
        "completedChildren": done,
        "totalChildren": total,
        "completedPoints": round(done_points, 1),
        "totalPoints": round(total_points, 1),
        "basis": basis,
        "targetDate": target.isoformat() if target else None,
    }


def compute_team_features(features_raw: list[dict], children_by_id: dict[int, dict],
                          settings: Settings, quarter: int | None = None,
                          year: int | None = None) -> dict:
    results: list[dict] = []
    for feat in features_raw:
        fm = feature_metrics(feat, children_by_id, settings)
        if quarter is not None:
            if fm["quarterNum"] != quarter:
                continue
            if year is not None and fm["year"] is not None and fm["year"] != year:
                continue
        results.append(fm)

    results.sort(key=lambda f: (f["percentComplete"], (f["title"] or "").lower()))

    summary = {
        "totalFeatures": len(results),
        "completedFeatures": sum(1 for f in results if f["percentComplete"] >= 100),
        "avgPercentComplete": (
            round(sum(f["percentComplete"] for f in results) / len(results), 1)
            if results else 0.0
        ),
    }
    return {"summary": summary, "features": results}
