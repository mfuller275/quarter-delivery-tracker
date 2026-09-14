"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_quarter(today: date | None = None) -> tuple[date, date]:
    """Return the (start, end) dates of the calendar quarter containing `today`."""
    today = today or date.today()
    quarter_index = (today.month - 1) // 3
    start_month = quarter_index * 3 + 1
    start = date(today.year, start_month, 1)
    if start_month + 3 > 12:
        end = date(today.year, 12, 31)
    else:
        end = date(today.year, start_month + 3, 1) - timedelta(days=1)
    return start, end


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # Azure DevOps
    ado_org: str = "hexagonPPMCOL"
    ado_project: str = "PPM"
    api_version: str = "7.0"
    query_id: str = ""

    # Authentication
    # "interactive" = single shared identity via a browser popup on the server
    #   (original behaviour; good for local/single-user, flaky on some servers).
    # "devicecode"  = single shared identity via the device-code flow (open a URL
    #   and type a code; robust on servers/RDP where the browser redirect fails).
    # "oauth"       = each user signs in with their own Microsoft account/MFA via
    #   the OAuth 2.0 authorization-code flow (see backend/auth.py).
    auth_mode: str = "interactive"
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""
    # Where Entra sends the user back after login. Must EXACTLY match a redirect
    # URI registered on the app registration (path is /auth/callback).
    oauth_redirect_uri: str = "http://localhost:8000/auth/callback"
    # Secret used to sign the session cookie. Set a long random value in prod.
    session_secret: str = "change-me-please-set-a-long-random-session-secret"

    # Field mapping
    team_field: str = "System.AreaPath"
    points_field: str = "Microsoft.VSTS.Scheduling.StoryPoints"
    effort_field: str = "Microsoft.VSTS.Scheduling.Effort"
    target_date_field: str = "Microsoft.VSTS.Scheduling.TargetDate"
    release_field: str = "Custom.Release"
    feature_type: str = "Feature"
    iteration_field: str = "System.IterationPath"

    completed_states: str = "Closed,Done,Completed,Resolved,Ready to Release,Released"
    excluded_states: str = "Removed,Cut"
    blocked_tags: str = "Blocked,At Risk,Impediment"
    highlight_tag: str = "ADNOC"

    # Quarter window overrides
    quarter_start: str = ""
    quarter_end: str = ""

    # Rollup / velocity
    squads_file: str = "squads.json"
    match_cutoff: float = 0.82
    velocity_window_days: int = 90
    velocity_min_days: int = 7
    future_quarters: int = 4

    @property
    def completed_state_set(self) -> set[str]:
        return {s.strip() for s in self.completed_states.split(",") if s.strip()}

    @property
    def excluded_state_set(self) -> set[str]:
        return {s.strip() for s in self.excluded_states.split(",") if s.strip()}

    @property
    def blocked_tag_set(self) -> set[str]:
        return {s.strip().lower() for s in self.blocked_tags.split(",") if s.strip()}

    def quarter_range(self) -> tuple[date, date]:
        if self.quarter_start and self.quarter_end:
            return date.fromisoformat(self.quarter_start), date.fromisoformat(self.quarter_end)
        return default_quarter()

    def focus_quarter(self) -> dict:
        """The quarter the dashboard is focused on, as Q#/year/label."""
        start, _ = self.quarter_range()
        q = (start.month - 1) // 3 + 1
        return {"label": f"Q{q} {start.year}", "quarter": q, "year": start.year}

    def fetch_fields(self) -> list[str]:
        """Fields to request from Azure DevOps for each work item."""
        base = [
            "System.Id",
            "System.Title",
            "System.WorkItemType",
            "System.State",
            "System.AssignedTo",
            "System.AreaPath",
            "System.IterationPath",
            "System.Tags",
            "Microsoft.VSTS.Common.ClosedDate",
            "Microsoft.VSTS.CMMI.Blocked",
            self.points_field,
            self.effort_field,
            self.target_date_field,
            self.team_field,
            self.release_field,
        ]
        # De-duplicate while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for f in base:
            if f not in seen:
                seen.add(f)
                ordered.append(f)
        return ordered


@lru_cache
def get_settings() -> Settings:
    return Settings()
