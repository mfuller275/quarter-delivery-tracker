# Quarter Delivery Tracker

A local web dashboard that tracks whether teams are on track to deliver everything
assigned to them for the quarter. It reads work items directly from Azure DevOps,
groups them by team, and shows delivery health with color-coded status.

## What it measures

For each team it computes:

- **% of items completed** vs. **% of the quarter elapsed** (are you keeping pace?)
- **Story points / effort delivered** vs. total committed
- **Overdue items** — target date passed but still open
- **Burndown projection** — projected completion date vs. quarter end
- **Blocked / at-risk items** (by tag or the Blocked field)

Each team is rated **on track / at risk / off track**, and the whole portfolio is
summarized at the top.

## Requirements

- Python 3.10+
- Access to the Azure DevOps organization (you'll sign in interactively — MFA supported)

## Quick start (Windows PowerShell)

```powershell
cd "$HOME\Downloads\quarter-delivery-tracker"
.\run.ps1
```

Then open http://127.0.0.1:8000 and click **Sign in & load**. A browser window
opens for your Azure DevOps login (including MFA). Your token is cached, so you
won't need to sign in every time.

### Manual start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn backend.main:app --port 8000
```

## Configuration (`.env`)

The important settings:

| Setting        | Purpose |
|----------------|---------|
| `ADO_ORG`      | Organization (default `hexagonPPMCOL`) |
| `ADO_PROJECT`  | Project (default `PPM`) |
| `QUERY_ID`     | GUID of a **saved** ADO query to use as the data source |
| `TEAM_FIELD`   | Field that identifies a team (default `System.AreaPath`) |
| `POINTS_FIELD` | Field holding story points/effort |
| `TARGET_DATE_FIELD` | Field holding the delivery/target date |
| `COMPLETED_STATES`  | States that count as delivered |
| `BLOCKED_TAGS` | Tags that flag an item as blocked/at-risk |
| `QUARTER_START` / `QUARTER_END` | Override the quarter window (else auto) |

### Using your exact query

The query URL you shared uses a **temporary** id (`tempQueryId`) that won't
persist. To make the dashboard use that exact query:

1. Open the query in Azure DevOps and click **Save** (or Save As).
2. Copy the GUID from the URL: `…/_queries/query/<GUID>`.
3. Put it in `.env` as `QUERY_ID=<GUID>`.

If `QUERY_ID` is empty, the app falls back to a built-in WIQL that pulls active
and recently-changed work items in the project.

### Finding your custom Team field

You mentioned teams are identified by a custom field. To find its exact
reference name, start the app, sign in, then visit:

```
http://127.0.0.1:8000/api/fields?q=team
```

Copy the `referenceName` (e.g. `Custom.Team`) into `TEAM_FIELD` in `.env` and
refresh.

## How "on track" is decided

- **On track** — points delivered is at/above the pace set by time elapsed,
  no overdue items, and the burndown projects completion by quarter end.
- **At risk** — slightly behind pace or a few overdue items, but recoverable.
- **Off track** — materially behind pace, overdue items, or burndown projects
  finishing after the quarter ends.

Thresholds live in [backend/metrics.py](backend/metrics.py) (`_status`) and are
easy to tune.

## Security notes

- Authentication uses Azure AD interactive login against the Azure DevOps
  resource; no passwords or PATs are stored by this app.
- Tokens are cached by the OS-provided secure token cache (`azure-identity`).
- The server binds to `127.0.0.1` (local only).
