"""Azure DevOps REST client with interactive (MFA-capable) authentication."""
from __future__ import annotations

import time

import httpx
from azure.identity import (
    DeviceCodeCredential,
    InteractiveBrowserCredential,
    TokenCachePersistenceOptions,
)

from .config import Settings

# Resource/application ID for Azure DevOps. Requesting this scope lets us call
# the ADO REST API with an Azure AD token, which supports interactive MFA login.
ADO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"

# ADO work item batch endpoint accepts at most 200 ids per request.
_BATCH_SIZE = 200


class ADOAuthError(RuntimeError):
    pass


class ADOClient:
    def __init__(self, settings: Settings, access_token: str | None = None):
        """Create a client.

        - Pass ``access_token`` to act as a specific signed-in user (per-user
          OAuth mode). The caller owns the token's lifetime/refresh.
        - Omit it to use a shared credential (single shared identity). The auth
          method depends on ``settings.auth_mode``:
            * "devicecode" -> ``DeviceCodeCredential`` (prints a URL + code to
              enter at microsoft.com/devicelogin; robust on servers/RDP).
            * anything else -> ``InteractiveBrowserCredential`` (browser popup).
          Tokens are cached persistently (shared across processes for the same
          OS user), so a one-time sign-in (see signin.py) lets the background
          server refresh silently.
        """
        self.settings = settings
        self._access_token = access_token
        self._credential = None
        if access_token is None:
            cache_opts = TokenCachePersistenceOptions(name="quarter_delivery_tracker")
            if settings.auth_mode == "devicecode":
                self._credential = DeviceCodeCredential(
                    cache_persistence_options=cache_opts,
                )
            else:
                self._credential = InteractiveBrowserCredential(
                    cache_persistence_options=cache_opts,
                )
        self._token: str | None = None
        self._expires_on: float = 0.0
        self._org_url = f"https://dev.azure.com/{settings.ado_org}"
        self._project_url = f"{self._org_url}/{settings.ado_project}"

    # -- auth ------------------------------------------------------------
    def _get_token(self) -> str:
        # Per-user OAuth: the token was supplied by the auth layer.
        if self._access_token is not None:
            return self._access_token
        if self._token and (self._expires_on - 120) > time.time():
            return self._token
        try:
            token = self._credential.get_token(ADO_SCOPE)
        except Exception as exc:  # noqa: BLE001 - surface a clean message to the API layer
            raise ADOAuthError(str(exc)) from exc
        self._token = token.token
        self._expires_on = token.expires_on
        return self._token

    def login(self) -> None:
        """Force an interactive sign-in (opens the browser for MFA).

        Only meaningful in single-user interactive mode; a no-op when a per-user
        access token was supplied.
        """
        if self._access_token is None:
            self._get_token()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # -- queries ---------------------------------------------------------
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=60.0)

    def get_work_item_ids(self) -> list[int]:
        """Return work item ids from the configured saved query or default WIQL."""
        av = self.settings.api_version
        with self._client() as client:
            if self.settings.query_id:
                url = f"{self._project_url}/_apis/wit/wiql/{self.settings.query_id}?api-version={av}"
                resp = client.get(url, headers=self._headers())
            else:
                url = f"{self._project_url}/_apis/wit/wiql?api-version={av}"
                resp = client.post(url, headers=self._headers(), json={"query": self._default_wiql()})
            resp.raise_for_status()
            data = resp.json()
        return [wi["id"] for wi in data.get("workItems", [])]

    def get_work_items(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        av = self.settings.api_version
        fields = self.settings.fetch_fields()
        results: list[dict] = []
        with self._client() as client:
            for start in range(0, len(ids), _BATCH_SIZE):
                chunk = ids[start : start + _BATCH_SIZE]
                url = f"{self._org_url}/_apis/wit/workitemsbatch?api-version={av}"
                body = {"ids": chunk, "fields": fields}
                resp = client.post(url, headers=self._headers(), json=body)
                resp.raise_for_status()
                results.extend(resp.json().get("value", []))
        return results

    def list_fields(self) -> list[dict]:
        av = self.settings.api_version
        url = f"{self._org_url}/_apis/wit/fields?api-version={av}"
        with self._client() as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        return [
            {"name": f.get("name"), "referenceName": f.get("referenceName")}
            for f in data.get("value", [])
        ]

    # -- feature drill-down ---------------------------------------------
    def get_feature_ids_for_team(
        self, team: str, quarter: int | None = None, year: int | None = None
    ) -> list[int]:
        """Return ids of Features for `team`, optionally scoped to a release quarter."""
        av = self.settings.api_version
        team_field = self.settings.team_field
        release_field = self.settings.release_field
        # Escape single quotes for WIQL string literals.
        safe_team = team.replace("'", "''")
        if team.lower() == "unassigned":
            team_clause = f"[{team_field}] = ''"
        else:
            team_clause = f"[{team_field}] = '{safe_team}'"
        release_clause = ""
        if quarter and year:
            # Release is stored as "YYYY.Q" (e.g. 2026.3); also match a "Q3" form.
            release_clause = (
                f" AND ([{release_field}] CONTAINS '{year}.{quarter}'"
                f" OR ([{release_field}] CONTAINS 'Q{quarter}'"
                f" AND [{release_field}] CONTAINS '{year}'))"
            )
        elif quarter:
            release_clause = f" AND [{release_field}] CONTAINS 'Q{quarter}'"
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.settings.ado_project}' "
            f"AND [System.WorkItemType] = '{self.settings.feature_type}' "
            f"AND {team_clause}"
            f"{release_clause} "
            "ORDER BY [System.Id]"
        )
        url = f"{self._project_url}/_apis/wit/wiql?api-version={av}"
        with self._client() as client:
            resp = client.post(url, headers=self._headers(), json={"query": wiql})
            resp.raise_for_status()
            data = resp.json()
        return [wi["id"] for wi in data.get("workItems", [])]

    def _release_contains(self, quarter: int, year: int) -> str:
        release_field = self.settings.release_field
        return (
            f"([{release_field}] CONTAINS '{year}.{quarter}'"
            f" OR ([{release_field}] CONTAINS 'Q{quarter}'"
            f" AND [{release_field}] CONTAINS '{year}'))"
        )

    def get_feature_ids_by_release(self, quarter: int, year: int) -> list[int]:
        """Project-wide Feature ids for a release quarter."""
        av = self.settings.api_version
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.settings.ado_project}' "
            f"AND [System.WorkItemType] = '{self.settings.feature_type}' "
            f"AND {self._release_contains(quarter, year)} "
            "ORDER BY [System.Id]"
        )
        url = f"{self._project_url}/_apis/wit/wiql?api-version={av}"
        with self._client() as client:
            resp = client.post(url, headers=self._headers(), json={"query": wiql})
            resp.raise_for_status()
            return [wi["id"] for wi in resp.json().get("workItems", [])]

    def get_completed_item_ids(self, iteration_nodes: list[str], since: str) -> list[int]:
        """Ids of items completed since `since` (YYYY-MM-DD) within the given
        iteration node subtrees (matched with UNDER, valid for tree fields)."""
        if not iteration_nodes:
            return []
        av = self.settings.api_version
        iteration_field = self.settings.iteration_field
        completed = "','".join(sorted(self.settings.completed_state_set))
        under = " OR ".join(
            f"[{iteration_field}] UNDER '{p.replace(chr(39), chr(39) * 2)}'"
            for p in iteration_nodes
        )
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.settings.ado_project}' "
            "AND [System.WorkItemType] IN "
            "('Product Backlog Item','User Story','Bug','Requirement') "
            f"AND [System.State] IN ('{completed}') "
            f"AND [Microsoft.VSTS.Common.ClosedDate] >= '{since}' "
            f"AND ({under}) "
            "ORDER BY [System.Id]"
        )
        url = f"{self._project_url}/_apis/wit/wiql?api-version={av}"
        with self._client() as client:
            resp = client.post(url, headers=self._headers(), json={"query": wiql})
            resp.raise_for_status()
            return [wi["id"] for wi in resp.json().get("workItems", [])]

    def get_feature_ids_under(self, iteration_nodes: list[str], quarter: int,
                             year: int) -> list[int]:
        """Feature ids in a release quarter whose iteration is under any of the
        given iteration node subtrees."""
        if not iteration_nodes:
            return []
        av = self.settings.api_version
        iteration_field = self.settings.iteration_field
        under = " OR ".join(
            f"[{iteration_field}] UNDER '{p.replace(chr(39), chr(39) * 2)}'"
            for p in iteration_nodes
        )
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.settings.ado_project}' "
            f"AND [System.WorkItemType] = '{self.settings.feature_type}' "
            f"AND ({under}) "
            f"AND {self._release_contains(quarter, year)} "
            "ORDER BY [System.Id]"
        )
        url = f"{self._project_url}/_apis/wit/wiql?api-version={av}"
        with self._client() as client:
            resp = client.post(url, headers=self._headers(), json={"query": wiql})
            resp.raise_for_status()
            return [wi["id"] for wi in resp.json().get("workItems", [])]

    def get_iteration_node_paths(self) -> list[str]:
        """Return every iteration node as a work-item-style path (e.g.
        'PPM\\InConcertOUX\\Shire'), by walking the classification node tree."""
        av = self.settings.api_version
        url = (
            f"{self._project_url}/_apis/wit/classificationnodes/iterations"
            f"?$depth=14&api-version={av}"
        )
        with self._client() as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            root = resp.json()

        paths: list[str] = []

        def walk(node: dict, prefix: str) -> None:
            name = node.get("name", "")
            current = name if not prefix else f"{prefix}\\{name}"
            paths.append(current)
            for child in node.get("children", []) or []:
                walk(child, current)

        walk(root, "")
        return paths

    def get_iteration_dates(self) -> dict[str, dict]:
        """Map each iteration node path (e.g. 'PPM\\InConcertOUX\\Shire\\Sprint 5')
        to its {'start': 'YYYY-MM-DD', 'finish': 'YYYY-MM-DD'} where set."""
        av = self.settings.api_version
        url = (
            f"{self._project_url}/_apis/wit/classificationnodes/iterations"
            f"?$depth=14&api-version={av}"
        )
        with self._client() as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            root = resp.json()

        dates: dict[str, dict] = {}

        def walk(node: dict, prefix: str) -> None:
            name = node.get("name", "")
            current = name if not prefix else f"{prefix}\\{name}"
            attrs = node.get("attributes") or {}
            start, finish = attrs.get("startDate"), attrs.get("finishDate")
            if start or finish:
                dates[current] = {"start": start, "finish": finish}
            for child in node.get("children", []) or []:
                walk(child, current)

        walk(root, "")
        return dates

    def get_work_items_with_relations(self, ids: list[int]) -> list[dict]:
        """Fetch work items including their relation links (parent/child)."""
        if not ids:
            return []
        av = self.settings.api_version
        results: list[dict] = []
        with self._client() as client:
            for start in range(0, len(ids), _BATCH_SIZE):
                chunk = ids[start : start + _BATCH_SIZE]
                id_list = ",".join(str(i) for i in chunk)
                # NOTE: $expand cannot be combined with a fields filter, so this
                # returns all fields plus relations.
                url = (
                    f"{self._org_url}/_apis/wit/workitems"
                    f"?ids={id_list}&$expand=relations&api-version={av}"
                )
                resp = client.get(url, headers=self._headers())
                resp.raise_for_status()
                results.extend(resp.json().get("value", []))
        return results

    def _default_wiql(self) -> str:
        # Scope to items *targeted for delivery this quarter* so we stay under the
        # Azure DevOps 20,000-item query cap and match the "on track for the
        # quarter" question. Provide a saved QUERY_ID in .env to use your own query.
        start, end = self.settings.quarter_range()
        tdf = self.settings.target_date_field
        return (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{self.settings.ado_project}' "
            "AND [System.WorkItemType] IN "
            "('User Story','Bug','Feature','Product Backlog Item','Requirement') "
            f"AND [{tdf}] >= '{start.isoformat()}' "
            f"AND [{tdf}] <= '{end.isoformat()}' "
            "ORDER BY [System.Id]"
        )
