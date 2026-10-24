"""MCP server exposing the growth engine as tools over stdio.

Example requests:
    "Process the signup sheet from Saturday's safety workshop"
    "Who are the hottest leads from the tech meetup?"
    "Write the at-risk email and check it"
    "Give me this week's growth brief"

Client configuration:

    {"mcpServers": {"holo-growth": {"command": "holo-growth-mcp"}}}

Tools that write to the CRM default to dry_run=True. Lead emails are masked unless
explicitly requested.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from holo_growth import reports
from holo_growth.logging_setup import configure

mcp = MCPServer(
    "holo-growth",
    instructions=(
        "Go-to-market tools for Holo, a dating-safety app. Lead and user data is personal: "
        "only show emails when the user needs them, and always run process_event_leads with "
        "dry_run=true first, then ask the user before running it for real."
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@lru_cache(maxsize=1)
def _svc():
    from holo_growth.services import build

    configure(os.environ.get("HOLO_GROWTH_LOG_LEVEL", "WARNING"))
    return build()


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"


def _allowed_upload_path(file_path: str) -> Path:
    """Resolve a signup file inside HOLO_GROWTH_INBOX (default ./inbox); reject anything else."""
    inbox = Path(os.environ.get("HOLO_GROWTH_INBOX", "inbox")).resolve()
    path = Path(file_path)
    path = (path if path.is_absolute() else inbox / path).resolve()
    if inbox not in path.parents:
        raise ValueError(f"file must be inside the inbox folder {inbox}")
    if path.suffix.lower() not in (".csv", ".xlsx", ".xlsm"):
        raise ValueError("only .csv and .xlsx files are accepted")
    if not path.exists():
        raise ValueError(f"{path.name} not found in the inbox")
    return path


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=True))
def process_event_leads(file_name: str, event_name: str, dry_run: bool = True) -> dict[str, Any]:
    """Clean, dedupe, enrich and upload an event signup sheet from the inbox folder.

    Run with dry_run=true first to preview counts and problems; nothing is written.
    Re-running the same file is skipped automatically. Returns run statistics, rows
    that need attention, and hot leads (emails masked).
    """
    from holo_growth.leads.pipeline import make_enricher, run_leads

    s = _svc()
    report = run_leads(
        _allowed_upload_path(file_name),
        event_name,
        s.conn,
        s.crm,
        make_enricher(s.llm),
        s.settings.review_threshold,
        dry_run=dry_run,
    )
    data = asdict(report)
    data["hot_leads"] = [{**h, "email": _mask(str(h["email"]))} for h in report.hot_leads[:10]]
    data["issues"] = [i for i in report.issues if i["field"] in ("email", "age", "crm")][:25]
    for issue in data["issues"]:
        issue["value"] = _mask(issue["value"]) if "@" in issue["value"] else issue["value"]
    data["summary"] = report.summary()
    return data


@mcp.tool(annotations=READ_ONLY)
def find_hot_leads(
    event_name: str | None = None, limit: int = 10, include_emails: bool = False
) -> list[dict[str, Any]]:
    """Highest-scoring reviewed leads, optionally for one event. Emails masked by default."""
    rows = reports.hot_leads(_svc().conn, event_name, max(1, min(limit, 50)))
    if not include_emails:
        for r in rows:
            r["email"] = _mask(r["email"])
    return rows


@mcp.tool(annotations=READ_ONLY)
def event_funnel() -> list[dict[str, Any]]:
    """Per event: leads, hot leads, leads in review, how many became Holo users and are active."""
    return reports.event_funnel(_svc().conn)


@mcp.tool(annotations=READ_ONLY)
def lifecycle_summary() -> list[dict[str, Any]]:
    """Holo users per lifecycle stage (new, activated, engaged, at_risk, dormant, opted_out)."""
    return reports.lifecycle_counts(_svc().conn)


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=True))
def sync_lifecycle_to_crm(dry_run: bool = True) -> dict[str, Any]:
    """Recompute lifecycle stages from Holo usage and sync consented users to the CRM."""
    from holo_growth.retention.sync import run_retention

    s = _svc()
    report = run_retention(s.conn, s.usage_source(), s.crm, dry_run=dry_run)
    return {**asdict(report), "summary": report.summary()}


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False))
def draft_campaign_email(segment: str, language: str = "en") -> dict[str, Any]:
    """Draft a lifecycle email for a segment and run the pre-send checks.

    segment: new | at_risk | dormant | validation_pending. language: en | si | ta.
    Drafts are never sent; passing drafts are ready for a person to load into HubSpot.
    """
    from holo_growth.content.campaigns import draft_campaign

    s = _svc()
    return asdict(draft_campaign(s.conn, segment, language, s.llm, set(s.settings.link_domains)))


@mcp.tool(annotations=READ_ONLY)
def check_campaign_copy(subject: str, body: str, language: str = "en") -> dict[str, Any]:
    """Check any email copy against Holo's tone, privacy and link rules before it goes out."""
    from holo_growth.content.checks import check_copy

    s = _svc()
    r = check_copy(subject, body, set(s.settings.link_domains), language, s.llm)
    return {"passed": r.passed, "errors": r.errors, "warnings": r.warnings}


@mcp.tool(annotations=ToolAnnotations(open_world_hint=True))
def run_geo_check() -> dict[str, Any]:
    """Ask AI assistants Holo-related questions and record whether Holo is mentioned."""
    from holo_growth.geo.tracker import providers_from_env, run_geo

    s = _svc()
    providers = providers_from_env(s.settings, s.llm)
    report = run_geo(s.conn, providers)
    return {
        "summary": report.summary(),
        "by_provider": report.by_provider,
        "competitors": report.competitor_counts,
        "demo_data": [p.name for p in providers] == ["demo"],
    }


@mcp.tool(annotations=READ_ONLY)
def weekly_growth_brief() -> str:
    """Markdown brief: events, lifecycle stages, AI search visibility, and what needs a human."""
    return reports.weekly_brief(_svc().conn)


@mcp.tool(annotations=READ_ONLY)
def review_queue() -> list[dict[str, Any]]:
    """Items waiting for a person: low-confidence enrichments and drafts that need review."""
    rows = reports.open_reviews(_svc().conn)
    for r in rows:
        if "@" in r["ref"]:
            r["ref"] = _mask(r["ref"])
    return rows


@mcp.tool(annotations=READ_ONLY)
def recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Latest pipeline runs with status and statistics."""
    return reports.recent_runs(_svc().conn, max(1, min(limit, 50)))


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
