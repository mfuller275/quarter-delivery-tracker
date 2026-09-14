"""Load the portfolio/product/scrum-team mapping and match ADO teams to it."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from .config import get_settings


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def load_squads() -> dict:
    """Read the mapping fresh each call so edits apply on the next Refresh."""
    path = Path(__file__).resolve().parent / get_settings().squads_file
    return json.loads(path.read_text(encoding="utf-8"))


def _team_entry(raw: object) -> tuple[str | None, str | None]:
    """Normalize a scrum-team entry to (name, lead). Supports legacy strings and
    object form such as {"name": "Nucleons", "lead": "Alyssa"}."""
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("team")
        lead = raw.get("lead") or raw.get("teamLead")
        return str(name) if name else None, str(lead) if lead else None
    name = str(raw) if raw is not None else None
    return name, None


def build_index() -> dict:
    """Build lookup structures for matching ADO team values to the hierarchy.

    Returns a bundle with:
      - teamIndex:    norm(scrumTeam)  -> placement (level="team")
      - productIndex: norm(product)    -> placement (level="product")
      - teamNames:    list of normalized scrum-team names (for fuzzy matching)
      - ignore:       set of normalized ADO team names to never match
    """
    data = load_squads()
    team_index: dict[str, dict] = {}
    product_index: dict[str, dict] = {}
    team_names: list[str] = []
    ignore = {_norm(t) for t in data.get("ignoreTeams", []) if _norm(t)}

    for portfolio in data.get("portfolios", []):
        for product in portfolio.get("productSquads", []):
            base = {
                "portfolio": portfolio["name"],
                "portfolioLead": portfolio.get("lead"),
                "product": product["name"],
                "productLead": product.get("lead"),
            }
            pkey = _norm(product["name"])
            if pkey:
                product_index[pkey] = {**base, "scrumTeam": None, "level": "product"}
            for scrum_team in product.get("scrumTeams", []):
                team_name, team_lead = _team_entry(scrum_team)
                key = _norm(team_name)
                if not key:
                    continue
                team_index[key] = {
                    **base,
                    "scrumTeam": team_name,
                    "teamLead": team_lead,
                    "level": "team",
                }
                team_names.append(key)

    return {
        "data": data,
        "teamIndex": team_index,
        "productIndex": product_index,
        "teamNames": team_names,
        "ignore": ignore,
    }


def match_team(ado_team: str, idx: dict, cutoff: float) -> dict | None:
    """Match an ADO team value to a scrum team or product squad.

    Order: ignore list -> exact scrum team -> exact product squad ->
    fuzzy scrum team. Product names match exactly only (short names are
    prone to false fuzzy matches). Returns None if nothing matches.
    """
    key = _norm(ado_team)
    if not key or key in idx["ignore"]:
        return None
    if key in idx["teamIndex"]:
        return idx["teamIndex"][key]
    if key in idx["productIndex"]:
        return idx["productIndex"][key]
    close = difflib.get_close_matches(key, idx["teamNames"], n=1, cutoff=cutoff)
    return idx["teamIndex"][close[0]] if close else None


def resolve_team(iteration_path: str | None, fallback_team: str, idx: dict,
                 cutoff: float) -> tuple[str, dict | None, str | None]:
    """Determine (team name, placement, iteration node path) for a work item.

    Iteration paths look like ``PPM\\<Team>\\...`` and, for product squads, can be
    deeper (``PPM\\InConcertOUX\\Shire\\...``). We scan every segment and prefer a
    dev-squad (team-level) match over a product-level one, so the most specific
    squad wins. The node path is the iteration path truncated to the matched
    segment (e.g. ``PPM\\InConcertOUX\\Shire``), suitable for a WIQL UNDER query.
    """
    parts: list[str] = str(iteration_path).split("\\") if iteration_path else []
    segments = parts[1:]  # drop the project root

    best: tuple[str, dict, str] | None = None       # team-level match
    best_product: tuple[str, dict, str] | None = None
    for i, seg in enumerate(segments):
        placement = match_team(seg, idx, cutoff)
        if not placement:
            continue
        node_path = "\\".join(parts[: i + 2])
        if placement["level"] == "team" and best is None:
            best = (placement["scrumTeam"], placement, node_path)
        elif placement["level"] == "product" and best_product is None:
            best_product = (placement["product"], placement, node_path)

    if best:
        return best
    if best_product:
        return best_product

    if fallback_team:
        placement = match_team(fallback_team, idx, cutoff)
        if placement:
            name = placement.get("scrumTeam") or placement.get("product")
            return (name, placement, None)

    display = segments[0] if segments else (fallback_team or "Unassigned")
    return (display, None, None)


def velocity_override(scrum_team: str) -> float | None:
    overrides = load_squads().get("overrides", {})
    norm_map = {_norm(k): v for k, v in overrides.items()}
    entry = norm_map.get(_norm(scrum_team))
    if entry and "velocityPerMonth" in entry:
        return float(entry["velocityPerMonth"])
    return None
