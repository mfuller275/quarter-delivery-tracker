"""Roll scrum-team delivery up to product and portfolio squads, with velocity
and a confidence score derived from throughput vs. remaining effort."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .config import Settings
from .metrics import _parse_date, _points_of, child_records, feature_metrics, parse_release_quarter
from .squads import _norm, build_index, resolve_team, velocity_override

_SEVERITY = {"off-track": 0, "at-risk": 1, "on-track": 2}
_ITER_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _sev(status: str) -> int:
    return _SEVERITY.get(status, 3)


def _team_field_value(fields: dict, settings: Settings) -> str:
    team = fields.get(settings.team_field) or ""
    if isinstance(team, dict):
        team = team.get("displayName") or ""
    return str(team)


def _has_tag(tags: str | None, term: str) -> bool:
    return bool(term) and term.lower() in (tags or "").lower()


# --- quarter helpers -----------------------------------------------------

def current_quarter(today: date) -> tuple[int, int]:
    return (today.month - 1) // 3 + 1, today.year


def quarter_bounds(quarter: int, year: int) -> tuple[date, date]:
    start = date(year, (quarter - 1) * 3 + 1, 1)
    end_month = (quarter - 1) * 3 + 3
    if end_month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def available_quarters(today: date, count: int) -> list[dict]:
    """Current quarter plus `count` future quarters (no past quarters)."""
    q, y = current_quarter(today)
    out: list[dict] = []
    for _ in range(count + 1):
        out.append({"value": f"{y}.{q}", "label": f"Q{q} {y}", "quarter": q, "year": y})
        q += 1
        if q > 4:
            q, y = 1, y + 1
    return out


def resolve_quarter(value: str | None, today: date) -> tuple[int, int]:
    if value:
        parsed = parse_release_quarter(value)
        if parsed and parsed["year"]:
            return parsed["quarter"], parsed["year"]
    return current_quarter(today)


# --- metrics -------------------------------------------------------------

def _confidence(velocity: float, months_remaining: float, remaining_effort: float) -> int:
    if remaining_effort <= 0:
        return 100
    if velocity <= 0:
        return 0
    return round(min(velocity * months_remaining / remaining_effort, 1.0) * 100)


def _status(confidence: int, pct: float) -> str:
    if pct >= 100:
        return "on-track"
    if confidence >= 80:
        return "on-track"
    if confidence >= 50:
        return "at-risk"
    return "off-track"


def _metrics(items: int, done_items: int, points: float, done_points: float,
             velocity: float, months_remaining: float, months_quarter: float) -> dict:
    pct = round(done_items / items * 100, 1) if items else 0.0
    remaining = round(points - done_points, 1)
    has_data = items > 0 or points > 0
    confidence = _confidence(velocity, months_remaining, remaining) if has_data else 0
    status = _status(confidence, pct) if has_data else "no-data"
    return {
        "totalItems": items,
        "completedItems": done_items,
        "pctComplete": pct,
        "totalPoints": round(points, 1),
        "completedPoints": round(done_points, 1),
        "remainingEffort": remaining,
        "velocityPerMonth": round(velocity, 1),
        "predictedQuarterCapacity": round(velocity * months_quarter, 1),
        "predictedRemainingCapacity": round(velocity * months_remaining, 1),
        "confidence": confidence,
        "hasData": has_data,
        "status": status,
    }


def _aggregate(children: list[dict], name: str, lead: str | None, level: str,
               months_remaining: float, months_quarter: float) -> dict:
    items = sum(c["totalItems"] for c in children)
    done = sum(c["completedItems"] for c in children)
    points = sum(c["totalPoints"] for c in children)
    done_points = sum(c["completedPoints"] for c in children)
    velocity = sum(c["velocityPerMonth"] for c in children)
    node = _metrics(items, done, points, done_points, velocity, months_remaining, months_quarter)
    sub_off = sum(
        c.get("subOffTrack", 0) + (1 if c.get("level") == "team" and c.get("status") == "off-track" else 0)
        for c in children
    )
    sub_at = sum(
        c.get("subAtRisk", 0) + (1 if c.get("level") == "team" and c.get("status") == "at-risk" else 0)
        for c in children
    )
    node.update({
        "name": name,
        "lead": lead,
        "level": level,
        "featureCount": sum(c.get("featureCount", 0) for c in children),
        "subOffTrack": sub_off,
        "subAtRisk": sub_at,
    })
    return node


def _sorted(nodes: list[dict]) -> list[dict]:
    return sorted(nodes, key=lambda n: (_sev(n["status"]), -n["confidence"], n["name"].lower()))


def _pbi_stats(pbis: list[dict]) -> tuple[int, int, float, float, float]:
    total = len(pbis)
    done = sum(1 for p in pbis if p["completed"])
    points = sum(p["points"] for p in pbis)
    done_points = sum(p["points"] for p in pbis if p["completed"])
    pct = round(done / total * 100, 1) if total else 0.0
    return total, done, points, done_points, pct


def _feature_entry(meta: dict, pbis: list[dict], assigned_higher: bool) -> dict:
    total, done, points, done_points, pct = _pbi_stats(pbis)
    if total == 0:
        pct = 100.0 if meta.get("completed") else 0.0
    # Show every PBI of the feature, flagging the ones that belong to the squad
    # this drill-down started from (metrics stay based on that squad's PBIs).
    mine_ids = {p["id"] for p in pbis}
    all_pbis = [
        {
            "id": ch["id"],
            "title": ch["title"],
            "type": ch["type"],
            "state": ch["state"],
            "points": ch["points"],
            "completed": ch["completed"],
            "assignedTo": ch.get("assignedTo"),
            "team": ch.get("teamName") or ch.get("team") or None,
            "mapped": ch.get("mapped", False),
            "mine": ch["id"] in mine_ids,
            "highlight": ch.get("highlight", False),
            "iterationName": ch.get("iterationName"),
            "iterationAfterQuarter": ch.get("iterationAfterQuarter", False),
        }
        for ch in meta.get("children", [])
    ]
    all_pbis.sort(key=lambda p: (not p["mine"], p["completed"], (p["title"] or "").lower()))
    return {
        "id": meta["id"],
        "title": meta["title"],
        "state": meta["state"],
        "quarter": meta["quarter"],
        "team": meta["featureTeam"],
        "owner": meta.get("owner"),
        "featureLevel": meta["featureLevel"],
        "assignedHigher": assigned_higher,
        "highlight": meta.get("highlight", False),
        "percentComplete": pct,
        "completedChildren": done,
        "totalChildren": total,
        "totalPbis": len(all_pbis),
        "completedPoints": round(done_points, 1),
        "totalPoints": round(points, 1),
        "epicId": meta.get("epicId"),
        "epicTitle": meta.get("epicTitle"),
        "pbis": all_pbis,
    }


def _iteration_after_quarter(path: str | None, iteration_dates: dict[str, dict],
                             quarter_end: date, quarter_year: int) -> bool:
    """True when the PBI's iteration is scheduled beyond the quarter it is committed
    to (i.e. the item cannot deliver within that quarter). Prefers the iteration's
    real start date; falls back to a year embedded in the iteration path such as
    'PPM\\Nucleons\\2027\\2027_02'."""
    info = iteration_dates.get(path or "")
    if info:
        ref = _parse_date(info.get("start") or info.get("finish"))
        if ref:
            return ref > quarter_end
    if path:
        years = [int(y) for y in _ITER_YEAR_RE.findall(path)]
        if years and max(years) > quarter_year:
            return True
    return False


def compute_rollup(features_raw: list[dict], children_by_id: dict[int, dict],
                   completed_items: list[dict], settings: Settings,
                   quarter: int, year: int, today: date,
                   epic_map: dict[int, dict] | None = None,
                   iteration_dates: dict[str, dict] | None = None) -> dict:
    idx = build_index()
    data = idx["data"]
    cutoff = settings.match_cutoff
    epic_map = epic_map or {}
    iteration_dates = iteration_dates or {}

    # Velocity is based on each team's ACTUAL active span (first completion in the
    # window → today), so brand-new teams are measured by their weekly rate rather
    # than diluted across the full window. Established teams active the whole window
    # are unchanged. We track points completed plus the earliest completion date.
    window_days = settings.velocity_window_days
    min_days = max(settings.velocity_min_days, 1)
    closed_field = "Microsoft.VSTS.Common.ClosedDate"

    def _accumulate(stats: dict, key: str, pts: float, closed) -> None:
        s = stats.setdefault(key, {"points": 0.0, "earliest": None})
        s["points"] += pts
        if closed and (s["earliest"] is None or closed < s["earliest"]):
            s["earliest"] = closed

    def _velocity(entry: dict | None) -> float:
        if not entry or entry["points"] <= 0:
            return 0.0
        earliest = entry["earliest"]
        active_days = ((today - earliest).days + 1) if earliest else window_days
        active_days = min(max(active_days, min_days), window_days)
        return entry["points"] * 30.44 / active_days

    canon_stats: dict[str, dict] = {}
    prod_stats: dict[str, dict] = {}
    for item in completed_items:
        fields = item.get("fields", {})
        _, pl, _ = resolve_team(
            fields.get(settings.iteration_field),
            _team_field_value(fields, settings), idx, cutoff,
        )
        pts = _points_of(fields, settings)
        closed = _parse_date(fields.get(closed_field))
        if pl and pl["level"] == "team":
            _accumulate(canon_stats, pl["scrumTeam"], pts, closed)
        elif pl and pl["level"] == "product":
            _accumulate(prod_stats, pl["product"], pts, closed)

    canon_vel = {k: _velocity(v) for k, v in canon_stats.items()}
    prod_vel = {k: _velocity(v) for k, v in prod_stats.items()}

    qstart, qend = quarter_bounds(quarter, year)
    window_start = max(today, qstart)
    total_days = (qend - qstart).days + 1
    remaining_days = min(max((qend - window_start).days + 1, 0), total_days)
    months_remaining = remaining_days / 30.44
    months_quarter = total_days / 30.44

    # Accumulators: PBIs are attributed to the dev squad that owns them, driven by
    # the feature's quarter, even when the feature sits at a product squad.
    team_acc: dict[str, dict] = {}      # canon dev squad -> {placement, features:{fid:{meta,pbis,assignedHigher}}}
    prod_acc: dict[tuple[str, str], dict] = {}  # (portfolio, product) -> {fid:{meta,pbis}}
    unmapped: dict[str, int] = {}

    def add_team(pl: dict, meta: dict, rec: dict | None, assigned_higher: bool) -> None:
        canon = pl["scrumTeam"]
        bucket = team_acc.setdefault(canon, {"placement": pl, "features": {}})
        fe = bucket["features"].setdefault(
            meta["id"], {"meta": meta, "pbis": [], "assignedHigher": assigned_higher}
        )
        if not assigned_higher:
            fe["assignedHigher"] = False
        if rec is not None:
            fe["pbis"].append(rec)

    def add_residual(pl: dict, meta: dict, rec: dict | None) -> None:
        key = (pl["portfolio"], pl["product"])
        bucket = prod_acc.setdefault(key, {})
        fe = bucket.setdefault(meta["id"], {"meta": meta, "pbis": []})
        if rec is not None:
            fe["pbis"].append(rec)

    for feat in features_raw:
        fm = feature_metrics(feat, children_by_id, settings)
        if fm["state"] in settings.excluded_state_set:
            continue
        if fm["quarterNum"] != quarter:
            continue
        if fm["year"] is not None and fm["year"] != year:
            continue
        feat_name, feat_placement, _ = resolve_team(
            fm.get("iterationPath"), fm["team"], idx, cutoff
        )
        epic = epic_map.get(fm.get("parentId"))
        epic_title = None
        if epic and epic.get("fields", {}).get("System.WorkItemType") == "Epic":
            epic_title = epic["fields"].get("System.Title")
        # Highlight items tagged with the highlight term, or under a parent that is.
        term = settings.highlight_tag
        epic_tags = epic.get("fields", {}).get("System.Tags") if epic else ""
        feature_highlight = _has_tag(fm.get("tags"), term) or _has_tag(epic_tags, term)
        meta = {
            "id": fm["id"], "title": fm["title"], "state": fm["state"],
            "completed": fm["completed"], "quarter": fm["quarter"],
            "featureTeam": feat_name,
            "owner": fm.get("owner"),
            "featureLevel": feat_placement["level"] if feat_placement else "unmapped",
            "epicId": fm.get("parentId") if epic_title else None,
            "epicTitle": epic_title,
            "highlight": feature_highlight,
        }
        recs = child_records(feat, children_by_id, settings)
        meta["children"] = recs
        for rec in recs:
            rec["highlight"] = feature_highlight or _has_tag(rec.get("tags"), term)
            path = rec.get("iterationPath")
            rec["iterationName"] = path.split("\\")[-1] if path else None
            rec["iterationAfterQuarter"] = _iteration_after_quarter(
                path, iteration_dates, qend, year
            )

        # Split children into those owned by a dev squad vs. the rest, resolving
        # each child's squad from its iteration path.
        mapped_children: list[tuple[dict, dict]] = []
        residual_children: list[dict] = []
        for rec in recs:
            cname, cp, _ = resolve_team(rec.get("iterationPath"), rec["team"], idx, cutoff)
            is_team = bool(cp and cp["level"] == "team")
            rec["teamName"] = cname
            rec["mapped"] = is_team
            rec["resolvedTeam"] = cname if is_team else None
            if is_team:
                mapped_children.append((rec, cp))
            else:
                residual_children.append(rec)

        contributed = False
        for rec, cp in mapped_children:
            native = (
                feat_placement is not None
                and feat_placement.get("level") == "team"
                and _norm(feat_placement["scrumTeam"]) == _norm(cp["scrumTeam"])
            )
            add_team(cp, meta, rec, assigned_higher=not native)
            contributed = True

        if not recs:
            # Feature with no children: place by the feature's own assignment.
            if feat_placement is None:
                unmapped[feat_name] = unmapped.get(feat_name, 0) + 1
            elif feat_placement["level"] == "team":
                add_team(feat_placement, meta, None, assigned_higher=False)
            else:
                add_residual(feat_placement, meta, None)
            continue

        # Residual (unteamed / non-dev-squad) children follow the feature.
        if feat_placement is None:
            if not contributed:
                unmapped[feat_name] = unmapped.get(feat_name, 0) + 1
        elif feat_placement["level"] == "team":
            for rec in residual_children:
                add_team(feat_placement, meta, rec, assigned_higher=False)
        else:
            for rec in residual_children:
                add_residual(feat_placement, meta, rec)

    def _team_name_and_lead(value: object) -> tuple[str | None, str | None]:
        if isinstance(value, dict):
            name = value.get("name") or value.get("team")
            lead = value.get("lead") or value.get("teamLead")
            return (str(name) if name else None, str(lead) if lead else None)
        return (str(value) if value is not None else None, None)

    # Build dev-squad (team) nodes from attributed PBIs.
    def build_team_node(canon: str, bucket: dict) -> dict:
        pl = bucket["placement"]
        feats = bucket["features"]
        all_pbis = [p for fe in feats.values() for p in fe["pbis"]]
        total, done, points, done_points, _ = _pbi_stats(all_pbis)
        override = velocity_override(canon)
        if override is not None:
            velocity, source = override, "override"
        else:
            velocity, source = canon_vel.get(canon, 0.0), "measured"
        node = _metrics(total, done, points, done_points, velocity,
                        months_remaining, months_quarter)
        entries = [
            _feature_entry(fe["meta"], fe["pbis"], fe["assignedHigher"])
            for fe in feats.values()
        ]
        entries.sort(key=lambda f: (f["percentComplete"], (f["title"] or "").lower()))
        node.update({
            "name": canon,
            "lead": pl.get("teamLead") or pl.get("productLead"),
            "level": "team",
            "featureCount": len(feats),
            "velocitySource": source,
            "features": entries,
            "empty": False,
            "nextQuarter": None,
            "subOffTrack": 0,
            "subAtRisk": 0,
        })
        return node

    by_product: dict[tuple[str, str], list[dict]] = {}
    for canon, bucket in team_acc.items():
        node = build_team_node(canon, bucket)
        pl = bucket["placement"]
        by_product.setdefault((pl["portfolio"], pl["product"]), []).append(node)

    # Ensure every mapped dev squad shows a tile, even with no commitment this
    # quarter. Empty squads keep their measured velocity and get a nextQuarter
    # (filled in by the API layer) pointing at their next committed quarter.
    existing = set(team_acc.keys())
    for portfolio in data.get("portfolios", []):
        for product in portfolio.get("productSquads", []):
            for st in product.get("scrumTeams", []):
                st_name, st_lead = _team_name_and_lead(st)
                if not st_name or st_name in existing:
                    continue
                override = velocity_override(st_name)
                velocity = override if override is not None else canon_vel.get(st_name, 0.0)
                node = _metrics(0, 0, 0.0, 0.0, velocity, months_remaining, months_quarter)
                node.update({
                    "name": st_name,
                    "lead": st_lead or product.get("lead"),
                    "level": "team",
                    "featureCount": 0,
                    "velocitySource": "override" if override is not None else "measured",
                    "features": [],
                    "empty": True,
                    "nextQuarter": None,
                    "subOffTrack": 0,
                    "subAtRisk": 0,
                })
                by_product.setdefault((portfolio["name"], product["name"]), []).append(node)
                existing.add(st_name)

    def build_product_node(portfolio: dict, product: dict, teams: list[dict]) -> dict:
        residual = prod_acc.get((portfolio["name"], product["name"]), {})
        res_pbis = [p for fe in residual.values() for p in fe["pbis"]]
        r_total, r_done, r_points, r_done_points, _ = _pbi_stats(res_pbis)

        t_items = sum(t["totalItems"] for t in teams)
        t_done = sum(t["completedItems"] for t in teams)
        t_points = sum(t["totalPoints"] for t in teams)
        t_done_points = sum(t["completedPoints"] for t in teams)
        t_velocity = sum(t["velocityPerMonth"] for t in teams)

        direct_velocity = 0.0
        if residual:
            override = velocity_override(product["name"])
            direct_velocity = (
                override if override is not None
                else prod_vel.get(product["name"], 0.0)
            )
        node = _metrics(
            t_items + r_total, t_done + r_done, t_points + r_points,
            t_done_points + r_done_points, t_velocity + direct_velocity,
            months_remaining, months_quarter,
        )
        res_entries = [
            _feature_entry(fe["meta"], fe["pbis"], assigned_higher=False)
            for fe in residual.values()
        ]
        res_entries.sort(key=lambda f: (f["percentComplete"], (f["title"] or "").lower()))
        source = "teams+direct" if (teams and residual) else ("direct" if residual else "team-rollup")
        node.update({
            "name": product["name"],
            "lead": product.get("lead"),
            "level": "product",
            "featureCount": sum(t.get("featureCount", 0) for t in teams) + len(res_entries),
            "teams": _sorted(teams),
            "features": res_entries,
            "velocitySource": source,
            "subOffTrack": sum(1 for t in teams if t["status"] == "off-track"),
            "subAtRisk": sum(1 for t in teams if t["status"] == "at-risk"),
        })
        return node

    portfolios_out: list[dict] = []
    for portfolio in data.get("portfolios", []):
        product_nodes: list[dict] = []
        for product in portfolio.get("productSquads", []):
            teams = by_product.get((portfolio["name"], product["name"]), [])
            product_nodes.append(build_product_node(portfolio, product, teams))
        pf_node = _aggregate(product_nodes, portfolio["name"], portfolio.get("lead"),
                             "portfolio", months_remaining, months_quarter)
        pf_node["products"] = _sorted(product_nodes)
        portfolios_out.append(pf_node)

    totals = _aggregate(portfolios_out, "All portfolios", None, "all",
                        months_remaining, months_quarter)

    return {
        "quarter": {"label": f"Q{quarter} {year}", "value": f"{year}.{quarter}",
                    "quarter": quarter, "year": year},
        "highlightTag": settings.highlight_tag,
        "window": {
            "today": today.isoformat(),
            "quarterStart": qstart.isoformat(),
            "quarterEnd": qend.isoformat(),
            "monthsRemaining": round(months_remaining, 2),
            "velocityWindowDays": settings.velocity_window_days,
            "velocityBasis": data.get("velocityBasis", "storyPoints"),
        },
        "totals": totals,
        "portfolios": _sorted(portfolios_out),
        "unmappedTeams": [
            {"team": t, "features": n}
            for t, n in sorted(unmapped.items(), key=lambda kv: -kv[1])
        ],
    }
