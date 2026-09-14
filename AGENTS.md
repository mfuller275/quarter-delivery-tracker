# AGENTS.md — Quarter Delivery Tracker

Context for AI agents / developers picking this project up in a new session.

## What it is
A local web dashboard that tracks whether teams are on track to deliver their
committed work for a quarter, sourced live from Azure DevOps (org `hexagonPPMCOL`,
project `PPM`). Front page is a tiered health dashboard:
**Portfolio squad → Product squad → Dev squad (scrum team) → Features → PBIs**.

## Location & run
- Project root: `C:\Users\mfuller\OneDrive - Octave\Code\quarter-delivery-tracker`
- Python: `.\.venv\Scripts\python.exe` (Python 3.12). System `python` is a Windows Store stub — always use the venv exe.
- Start (no reload — important, see gotchas):
  ```powershell
  .\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
  ```
  or `.\run.ps1`. To share on LAN use `--host 0.0.0.0` (needs an admin firewall rule for TCP 8000).
- URL: http://127.0.0.1:8000 (machine name: `HEX-W5F8DQF4.ingrnet.com`).

## Architecture
- **Backend**: FastAPI. Files in `backend/`:
  - `main.py` — endpoints + orchestration; also builds `squad_nodes` (iteration-node map) and runs the next-quarter lookahead.
  - `ado_client.py` — Azure DevOps REST client. Auth = `InteractiveBrowserCredential` (MFA via browser popup on the server machine; token cached in OS store). WIQL + work-item batch fetches.
  - `config.py` — pydantic-settings; all tunables (env-overridable). `fetch_fields()` lists work-item fields pulled.
  - `metrics.py` — per-feature metrics, `child_records` (PBIs), quarter parsing, tags, parent/epic ids.
  - `squads.py` — loads `squads.json`, builds match index, `match_team` (fuzzy for dev squads, exact for products), `resolve_team` (iteration-path → team).
  - `rollup.py` — the core: builds the portfolio/product/team tree, PBI attribution, velocity, confidence, capacity, sub-risk rollup, epic grouping, highlight flags.
- **Frontend**: vanilla JS SPA in `frontend/` (`index.html`, `app.js`, `styles.css`). No build step; served as static files. Reads `/api/rollup`.

## Key API endpoints
- `GET /api/config` — org/project/team field.
- `GET /api/quarters` — current + future quarters (no past).
- `GET /api/rollup?quarter=YYYY.Q` — the whole tree (portfolios→products→teams→features→pbis) with metrics.
- `GET /api/unmapped-teams` — dev squads with work not matched to the mapping.
- `POST /api/login` — force interactive sign-in.

## Azure DevOps data model (verified facts)
- **Team identity comes from the ITERATION PATH**, not the Scrum Team field. Path looks like `PPM\<Team>\...`; for product squads it's deeper e.g. `PPM\InConcertOUX\Shire\...` (Shire is the dev squad). `resolve_team` scans segments, prefers the deepest dev-squad match over a product match, and returns the iteration node path for `UNDER` queries. Falls back to `Custom.ScrumTeam` then first segment.
- **Release/quarter** is on the feature in field `Custom.Release`, formatted `YYYY.Q` (e.g. `2026.3` = Q3 2026). Parser also accepts `Q3 2026`.
- **Completed states** (org workflow): `Ready to Release` is the main done state. Default COMPLETED_STATES = `Closed,Done,Completed,Resolved,Ready to Release,Released`.
- **Excluded states**: `Removed,Cut` — removed from all counts (features AND PBIs).
- **Scrum team field**: `Custom.ScrumTeam` (used only as fallback now).
- **Story points**: `Microsoft.VSTS.Scheduling.StoryPoints` (velocity basis). Points are inconsistently filled, so feature % complete is COUNT-based (PBIs done / total), not points.
- **WIQL gotcha**: iteration path is a tree field — you CANNOT use `CONTAINS` on it; use `UNDER '<node path>'`. That's why `main.py` fetches the iteration classification-node tree (`get_iteration_node_paths`) and maps squads → node paths for velocity and lookahead queries.

## Rollup semantics (rollup.py)
- **Features** are fetched project-wide by release quarter; **children (PBIs)** fetched via relations; **epics** (feature parents) fetched for grouping.
- **PBI attribution**: each non-excluded PBI is attributed to the dev squad resolved from ITS OWN iteration path — so a feature owned at a product/other squad still contributes its PBIs to the right dev squads. Team metrics are based on the squad's own PBIs. Features assigned directly to a product squad name roll up at product level (residual).
- Feature entry carries: `owner` (person = System.AssignedTo), `team` (owning squad from iteration), `assignedHigher` (owned outside current squad), `pbis` (ALL children with `mine`/`mapped`/`team`/`highlight` flags), `epicId`/`epicTitle`, `highlight`.
- **Velocity** = span-based: points completed in the last `VELOCITY_WINDOW_DAYS` (90) divided by the team's ACTUAL active span (first completion → today, floored at `VELOCITY_MIN_DAYS`=7, capped at window), scaled to per-month. New teams thus reflect a weekly rate extrapolated up; established teams (active whole window) are unchanged. Overrides via `squads.json.overrides` (e.g. Nucleons = 15/mo).
- **Confidence** = `min(velocity × months_remaining / remaining_effort, 100%)`. Status: ≥80 on-track, 50–79 at-risk, <50 off-track (or pct≥100 → on-track). Nodes with no data → `no-data`.
- **Capacity**: `predictedQuarterCapacity` = velocity × whole-quarter months; `predictedRemainingCapacity` = velocity × months left (clamped ≤ quarter; equal for a not-yet-started quarter — day counting is inclusive on both). `remainingEffort` = open (incomplete) points.
- **Sub-risk rollup**: each node has `subOffTrack`/`subAtRisk` = counts of descendant dev squads off/at-risk, summed up product → portfolio → all. Shown as a ⚠ indicator on parent tiles.
- **Highlight**: items whose tag contains `HIGHLIGHT_TAG` (default `ADNOC`), OR whose parent (epic/feature) does — propagates down. Amber styling + chip.
- **Empty squads**: every mapped dev squad always gets a tile; empty ones show the next future quarter that has committed work (iteration-subtree lookahead).

## Config (.env keys → config.py defaults)
`ADO_ORG`, `ADO_PROJECT`, `QUERY_ID`, `TEAM_FIELD` (=Custom.ScrumTeam),
`ITERATION_FIELD` (=System.IterationPath), `RELEASE_FIELD` (=Custom.Release),
`FEATURE_TYPE` (=Feature), `POINTS_FIELD`, `TARGET_DATE_FIELD`,
`COMPLETED_STATES`, `EXCLUDED_STATES`, `BLOCKED_TAGS`, `HIGHLIGHT_TAG` (=ADNOC),
`SQUADS_FILE` (=squads.json), `MATCH_CUTOFF` (=0.82), `VELOCITY_WINDOW_DAYS` (=90),
`VELOCITY_MIN_DAYS` (=7), `FUTURE_QUARTERS` (=4), `QUARTER_START`/`QUARTER_END` (blank = auto).

## squads.json (the org mapping — NOT in ADO)
Structure: `portfolios[] → productSquads[] → scrumTeams[]`. Also top-level
`ignoreTeams` (e.g. `Viz_Moonshot`), `overrides` (per-team velocityPerMonth), `velocityBasis`.
- Dev-squad names must match the ADO iteration segment (fuzzy, cutoff 0.82). Product names match exactly and can receive features directly (e.g. `InConcertOUX`).
- Visualization portfolio intentionally excluded. `The Shire` was renamed to `Shire` to match the iteration segment.
- It is re-read on every request (no restart needed for mapping edits — just Refresh).

## Gotchas / operating notes
- **LAN/hostname access needs `--host 0.0.0.0`.** `--host 127.0.0.1` binds loopback only → `localhost` works but `http://hex-w5f8dqf4.ingrnet.com:8000/` fails. Check binding with `Get-NetTCPConnection -LocalPort 8000 -State Listen | Select LocalAddress`. Restart with `0.0.0.0` to share on LAN (firewall rule for TCP 8000 already exists).
- **Backend code changes require a server restart** (running WITHOUT `--reload`). Frontend changes only need a browser **hard refresh** (Ctrl+Shift+R) — the browser aggressively caches `app.js`/`index.html`/`styles.css`.
- **Do NOT run with `--reload` from the OneDrive folder** — OneDrive syncing `.venv` triggers an endless reload storm. `.venv` in OneDrive also syncs tens of thousands of files (recommend excluding it from sync).
- Server is typically launched hidden via `Start-Process ... -WindowStyle Hidden`; find/stop it via `Get-NetTCPConnection -LocalPort 8000`.
- Auth is single-user (the running user's cached ADO token). Sharing the URL means viewers see data under that identity with no sign-in. A true multi-user deploy needs per-user Azure AD OAuth (auth-code flow) — discussed but not built.
- `grep_search`/`file_search` tools return empty in this environment (no workspace folder open); read files directly by path.

## Common tasks
- Discover ADO field reference names: `GET /api/fields?q=<term>`.
- Change highlight tag: set `HIGHLIGHT_TAG` in `.env`, restart.
- Tune new-team velocity aggressiveness: `VELOCITY_MIN_DAYS`.
- Add/rename a dev squad: edit `backend/squads.json`, click Refresh.

## Change log / key decisions (most recent first)
- **Device-code sign-in surfaced in the web UI (`AUTH_MODE=devicecode`)**: first sign-in (and re-auth after token expiry) can be done from ANY browser, not just the host console. `backend/device_auth.py` `DeviceAuthManager` runs the blocking `DeviceCodeCredential` flow in a background thread; its `prompt_callback` records the URL+code (and raises `_SilentAuthRequired` for silent-only attempts so normal API requests never block). `main.py` `get_client` in devicecode mode uses `get_token_silent()` and returns 401 `X-Auth-Redirect: device` when there's no token; new endpoints `POST /api/device-login/start` + `GET /api/device-login/status`. Frontend shows the code in `#status` (`startDeviceLogin`/`pollDeviceLogin`, styled `.device-code`) and auto-continues once authenticated. Still a single shared identity. `signin.py` remains as a console fallback. Token cache lives at `%LOCALAPPDATA%\.IdentityService\quarter_delivery_tracker*`; delete to clear a stale token (fixes AADSTS70008). Also added `DeviceCodeCredential` option in `ado_client.py` and the moved deployment to `C:\Apps\quarter-delivery-tracker` on server `in-pxl-gdstest` via `deploy-setup.ps1` (robust Python detection, venv, scheduled task, firewall).
- **Per-user OAuth auth added (opt-in via `AUTH_MODE=oauth`)**: each browser user signs in with their own Microsoft account/MFA (Entra ID authorization-code flow, MSAL confidential client). Default stays `AUTH_MODE=interactive` (single shared identity, original behaviour — nothing changes unless you flip it). New `backend/auth.py` = MSAL app + routes `/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/me` + `get_ado_token(request)` (silent refresh) + in-memory server-side session store (`_SESSIONS`, keyed by a signed session-id cookie via Starlette `SessionMiddleware`). `ADOClient` now takes an optional `access_token` (per-user) and only builds `InteractiveBrowserCredential` when none is given. `main.py` `get_client` became a request-aware `Depends` dependency (oauth → per-user client from session token, else shared singleton); every `/api/*` endpoint updated to `Depends(get_client)`. Frontend: `#auth-box` shows signed-in user + sign in/out, and `/api/rollup` 401 with `X-Auth-Redirect` header sends the user to `/auth/login`. New deps: `msal`, `itsdangerous`. New `.env` keys: `AUTH_MODE`, `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `OAUTH_REDIRECT_URI`, `SESSION_SECRET`. TODO before oauth goes live: create the Entra app registration (Web redirect = `OAUTH_REDIRECT_URI`, delegated ADO `user_impersonation` granted, client secret), serve over HTTPS, and note the in-memory session store assumes a single uvicorn process (no `--reload`; swap for Redis/DB if replicated).
- Summary strip now shows **remaining open pts** (`remainingEffort`) and **remaining capacity pts** (`predictedRemainingCapacity`) in place of the whole-quarter `predictedQuarterCapacity` tile — makes the confidence % self-explanatory (remaining capacity ÷ remaining open).
- **"Iteration after quarter" PBI indicator**: a PBI committed to a quarter but scheduled on an iteration beyond it gets a red ⚠ chip. Backend `_iteration_after_quarter` (rollup.py) uses the iteration classification-node `startDate` (from new `ado_client.get_iteration_dates()`), falling back to a 4-digit year parsed from the iteration path (e.g. `PPM\Nucleons\2027\2027_02` → flagged when delivery year is earlier). Year-based fallback only (avoids false positives from ambiguous period numbers); same-year-later relies on real dates. PBI carries `iterationName` + `iterationAfterQuarter`.
- **"Hide Ready to Release" → "Hide Ready to Release / Removed"**: toggle now also hides individual RTR PBI *rows* (not just whole features), and hides an `assignedHigher` feature when all its in-scope (`mine`) PBIs are RTR. Frontend `HIDDEN_STATES = {ready to release, removed, cut}` in app.js; filter runs after `mergeFeatures`. Removed/Cut stay backend-excluded (decision: not surfaced for display); frontend filter is symmetric/defensive.
- Velocity is **squad-wide throughput**, NOT scoped to this quarter's feature children: `get_completed_item_ids` queries all items UNDER the team iteration node with `ClosedDate` in the last 90d. Points-based. A team reads 0 velocity if it has no closures in the window OR its items carry no Story Points. In this org `ClosedDate` is already stamped when an item reaches `Ready to Release`, so "use the RTR date" would be a no-op.
  - Navigators = 0 velocity: 514 done items but ALL have empty Story Points, and latest closure was 2026-03-25 (nothing in the 90d window). Mapping is fine (`PPM\Navigators`).
  - Nucleons override `velocityPerMonth: 15` in `squads.json` is INTENTIONAL (team downsized recently) — do NOT remove it even though measured velocity (~87/mo) is much higher.
- Sub-squad risk roll-up (`subOffTrack`/`subAtRisk`) added; ⚠ indicator on parent tiles. Guarded frontend against stale-HTML null (`.sub-alert` may be missing if page cached).
- Velocity switched to span-based (weekly rate extrapolated) so brand-new OUX squads aren't diluted; `VELOCITY_MIN_DAYS` floor added.
- Capacity shows both remaining and full-quarter; fixed inclusive day-count so a not-yet-started quarter has remaining == quarter.
- Feature "owner" = person (System.AssignedTo), not team. Squad origin shown as `↑ <squad>` separately.
- PBI drill shows every PBI's team with colour coding: solid blue = current squad, light blue = mapped squad in structure, grey = outside structure. Duplicate feature rows merged into one across squads (`mergeFeatures` in app.js); PBIs expandable at all levels.
- Features grouped by parent Epic where the parent is an Epic.
- "Hide Ready to Release" toggle (frontend-only filter).
- ADNOC tag highlight with parent propagation.
- Team identity moved from Custom.ScrumTeam field to iteration path (handles InConcertOUX sub-squads like Shire/Acadia/Arches/Everglades/Glacier/Yosemite). Required switching velocity/lookahead WIQL to `UNDER` on iteration node paths.
- Project moved from `~/Downloads` to the OneDrive `Code` folder; run without `--reload` to avoid OneDrive sync reload storm.
- Publishing for multiple users was scoped out (would need per-user Azure AD OAuth).

## Open ideas / not done
- Per-user Azure AD auth for a shared deployment — **scaffolded** (see change log; `AUTH_MODE=oauth`). Remaining: Entra app registration, HTTPS, and a shared session store if running >1 process.
- Optional safeguard: dampen velocity for teams with very few recent items (min item count or blend to full-window rate).
- Option to highlight only items tagged directly (no parent inheritance).

