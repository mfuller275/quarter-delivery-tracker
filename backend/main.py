"""FastAPI application: serves the dashboard and the metrics API."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .ado_client import ADOAuthError, ADOClient
from .config import get_settings
from .device_auth import get_device_auth_manager
from .metrics import collect_child_ids, collect_parent_ids, compute_summary, compute_team_features, parse_release_quarter
from .rollup import available_quarters, compute_rollup, resolve_quarter
from .squads import build_index, resolve_team

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Quarter Delivery Tracker")
app.add_middleware(SessionMiddleware, secret_key=get_settings().session_secret, https_only=False)
app.include_router(auth.router)

_client: ADOClient | None = None


def get_client(request: Request) -> ADOClient:
    """Return an ADO client for the current request.

    - oauth mode: a per-user client built from the signed-in user's token
      (raises 401 via get_ado_token if they aren't authenticated).
    - devicecode mode: a shared client using a token acquired via the device-code
      flow; raises 401 (X-Auth-Redirect: device) so the UI can show the code.
    - interactive mode: a single shared client (original behaviour).
    """
    settings = get_settings()
    if settings.auth_mode == "oauth":
        return ADOClient(settings, access_token=auth.get_ado_token(request))
    if settings.auth_mode == "devicecode":
        token = get_device_auth_manager(settings).get_token_silent()
        if not token:
            raise HTTPException(
                status_code=401,
                detail="Azure DevOps sign-in required",
                headers={"X-Auth-Redirect": "device"},
            )
        return ADOClient(settings, access_token=token)
    global _client
    if _client is None:
        _client = ADOClient(settings)
    return _client


def _handle_ado_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, ADOAuthError):
        return HTTPException(status_code=401, detail=f"Azure DevOps sign-in failed: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(
            status_code=exc.response.status_code,
            detail=f"Azure DevOps API error: {exc.response.text[:400]}",
        )
    return HTTPException(status_code=500, detail=str(exc))


@app.get("/api/config")
def api_config() -> dict:
    settings = get_settings()
    start, end = settings.quarter_range()
    return {
        "org": settings.ado_org,
        "project": settings.ado_project,
        "teamField": settings.team_field,
        "usingSavedQuery": bool(settings.query_id),
        "quarter": {"start": start.isoformat(), "end": end.isoformat()},
    }


@app.post("/api/login")
def api_login(client: ADOClient = Depends(get_client)) -> dict:
    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        raise _handle_ado_errors(exc) from exc
    return {"status": "authenticated"}


@app.post("/api/device-login/start")
def api_device_login_start() -> dict:
    """Start (or reuse) a device-code sign-in and return the code to display."""
    settings = get_settings()
    if settings.auth_mode != "devicecode":
        raise HTTPException(status_code=404, detail="Device-code sign-in is not enabled")
    return get_device_auth_manager(settings).start_device_login()


@app.get("/api/device-login/status")
def api_device_login_status() -> dict:
    settings = get_settings()
    if settings.auth_mode != "devicecode":
        raise HTTPException(status_code=404, detail="Device-code sign-in is not enabled")
    return get_device_auth_manager(settings).status()


@app.get("/api/summary")
def api_summary(client: ADOClient = Depends(get_client)) -> dict:
    try:
        ids = client.get_work_item_ids()
        raw_items = client.get_work_items(ids)
    except Exception as exc:  # noqa: BLE001
        raise _handle_ado_errors(exc) from exc
    return compute_summary(raw_items, get_settings())


@app.get("/api/quarters")
def api_quarters() -> dict:
    settings = get_settings()
    quarters = available_quarters(date.today(), settings.future_quarters)
    return {"quarters": quarters, "current": quarters[0]["value"]}


@app.get("/api/rollup")
def api_rollup(quarter: str | None = None, client: ADOClient = Depends(get_client)) -> dict:
    """Portfolio/product/team rollup with % complete, velocity and confidence."""
    settings = get_settings()
    today = date.today()
    q_num, q_year = resolve_quarter(quarter, today)
    try:
        feature_ids = client.get_feature_ids_by_release(q_num, q_year)
        features = client.get_work_items_with_relations(feature_ids)
        children = client.get_work_items(collect_child_ids(features))
        epics = client.get_work_items(collect_parent_ids(features))
        epic_map = {e.get("id"): e for e in epics}

        # Map every mapped dev squad to its iteration node subtree(s), used for
        # UNDER queries (velocity + next-quarter lookahead).
        idx = build_index()
        children_by_id = {c.get("id"): c for c in children}
        squad_nodes: dict[str, set[str]] = {}
        for path in client.get_iteration_node_paths():
            name, pl, node = resolve_team(path, "", idx, settings.match_cutoff)
            if pl and pl["level"] == "team" and node:
                squad_nodes.setdefault(name, set()).add(node)

        # Iteration -> date range, used to flag PBIs scheduled beyond the quarter.
        iteration_dates = client.get_iteration_dates()

        # Dev squads with work this quarter — resolved from iteration paths of
        # features AND child PBIs — then measure velocity from items completed in
        # their iteration subtrees.
        present: set[str] = set()
        for item in features + children:
            fields = item.get("fields", {})
            team = fields.get(settings.team_field) or ""
            if isinstance(team, dict):
                team = team.get("displayName") or ""
            _, pl, _ = resolve_team(fields.get(settings.iteration_field), str(team),
                                    idx, settings.match_cutoff)
            if pl and pl["level"] == "team":
                present.add(pl["scrumTeam"])

        velocity_roots: set[str] = set()
        for canon in present:
            velocity_roots |= squad_nodes.get(canon, set())

        since = (today - timedelta(days=settings.velocity_window_days)).isoformat()
        completed_ids = client.get_completed_item_ids(sorted(velocity_roots), since)
        completed_items = client.get_work_items(completed_ids)
    except Exception as exc:  # noqa: BLE001
        raise _handle_ado_errors(exc) from exc

    result = compute_rollup(features, children_by_id, completed_items, settings,
                            q_num, q_year, today, epic_map, iteration_dates)
    _fill_next_quarters(result, client, settings, q_num, q_year, today, squad_nodes)
    return result


def _fill_next_quarters(result: dict, client: ADOClient, settings, q_num: int,
                        q_year: int, today: date, squad_nodes: dict[str, set]) -> None:
    """For dev squads with no commitment this quarter, find the next future
    quarter (within the horizon) that has features in their iteration subtree."""
    horizon = available_quarters(today, settings.future_quarters)
    lookahead = [q for q in horizon if (q["year"], q["quarter"]) > (q_year, q_num)]
    if not lookahead:
        return
    empty_nodes = [
        tm
        for pf in result.get("portfolios", [])
        for prod in pf.get("products", [])
        for tm in prod.get("teams", [])
        if tm.get("empty")
    ]
    cache: dict[str, dict | None] = {}
    for tm in empty_nodes:
        name = tm["name"]
        if name in cache:
            tm["nextQuarter"] = cache[name]
            continue
        nodes = sorted(squad_nodes.get(name, set()))
        found = None
        for q in lookahead:
            try:
                if nodes and client.get_feature_ids_under(nodes, q["quarter"], q["year"]):
                    found = {"label": q["label"], "value": q["value"]}
                    break
            except Exception:  # noqa: BLE001 - a lookahead failure shouldn't break the page
                break
        cache[name] = found
        tm["nextQuarter"] = found


@app.get("/api/unmapped-teams")
def api_unmapped_teams(quarter: str | None = None,
                       client: ADOClient = Depends(get_client)) -> dict:
    """ADO scrum teams with features this quarter that don't match the mapping."""
    result = api_rollup(quarter, client)
    return {"quarter": result["quarter"], "unmappedTeams": result["unmappedTeams"]}


@app.get("/api/features")
def api_features(team: str, quarter: str | None = None,
                 client: ADOClient = Depends(get_client)) -> dict:
    """All Features for a scrum team in a release quarter, with % complete."""
    settings = get_settings()
    focus = settings.focus_quarter()
    q_num, q_year, q_label = focus["quarter"], focus["year"], focus["label"]
    if quarter:
        override = parse_release_quarter(quarter)
        if override:
            q_num, q_year, q_label = override["quarter"], override["year"], override["label"]
    try:
        feature_ids = client.get_feature_ids_for_team(team, quarter=q_num, year=q_year)
        features = client.get_work_items_with_relations(feature_ids)
        children = client.get_work_items(collect_child_ids(features))
    except Exception as exc:  # noqa: BLE001
        raise _handle_ado_errors(exc) from exc
    children_by_id = {c.get("id"): c for c in children}
    result = compute_team_features(features, children_by_id, settings, quarter=q_num, year=q_year)
    result["quarter"] = q_label
    return result


@app.get("/api/fields")
def api_fields(q: str | None = None, client: ADOClient = Depends(get_client)) -> list[dict]:
    """List work item fields. Use ?q=team to help find your custom Team field."""
    try:
        fields = client.list_fields()
    except Exception as exc:  # noqa: BLE001
        raise _handle_ado_errors(exc) from exc
    if q:
        needle = q.lower()
        fields = [
            f for f in fields
            if needle in (f.get("name") or "").lower()
            or needle in (f.get("referenceName") or "").lower()
        ]
    return sorted(fields, key=lambda f: f.get("name") or "")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
