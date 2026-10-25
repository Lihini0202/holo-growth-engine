"""CLI, MCP server and reporting surfaces."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from holo_growth import reports
from holo_growth.cli import app
from holo_growth.evaluation import evaluate
from holo_growth.leads.enrich import RuleEnricher
from holo_growth.leads.pipeline import run_leads
from holo_growth.synthetic import make_eval_set


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY",
        "HUBSPOT_PRIVATE_APP_TOKEN",
        "HOLO_SUPABASE_URL",
        "OPENROUTER_API_KEY",
        "GA4_PROPERTY_ID",
        "GA4_CREDENTIALS_FILE",
        "CRM_PROVIDER",
        "SALESFORCE_DOMAIN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOLO_GROWTH_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("HOLO_GROWTH_INBOX", str(tmp_path / "inbox"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_demo_runs_offline(env):
    result = CliRunner().invoke(app, ["demo", "--db-path", str(env / "demo.db")])
    assert result.exit_code == 0, result.output
    assert "# Holo growth: weekly brief" in result.output
    assert "rules only" in result.output


def test_cli_leads_process_then_skip(env):
    runner = CliRunner()
    assert runner.invoke(app, ["leads", "sample", "s.csv", "--rows", "20"]).exit_code == 0
    first = runner.invoke(app, ["leads", "process", "s.csv", "--event", "E"])
    assert first.exit_code == 0 and "E: " in first.output
    second = runner.invoke(app, ["leads", "process", "s.csv", "--event", "E"])
    assert "skipped" in second.output


def test_mcp_tools_listed_and_callable(env):
    from holo_growth import mcp_server
    from holo_growth.synthetic import make_event_file

    mcp_server._svc.cache_clear()
    make_event_file(env / "inbox" / "signups.csv", 25)

    async def go():
        names = {t.name for t in await mcp_server.mcp.list_tools()}
        assert {
            "process_event_leads",
            "find_hot_leads",
            "weekly_growth_brief",
            "check_campaign_copy",
            "draft_campaign_email",
        } <= names
        preview = await mcp_server.mcp.call_tool(
            "process_event_leads", {"file_name": "signups.csv", "event_name": "E"}
        )
        data = json.loads(preview.content[0].text)
        assert data["uploaded_created"] == 0  # dry run by default
        real = await mcp_server.mcp.call_tool(
            "process_event_leads", {"file_name": "signups.csv", "event_name": "E", "dry_run": False}
        )
        data = json.loads(real.content[0].text)
        assert data["uploaded_created"] > 0
        assert all("***@" in h["email"] for h in data["hot_leads"])
        with pytest.raises(Exception) as exc_info:  # noqa: B017 - SDK wraps the ValueError
            await mcp_server.mcp.call_tool(
                "process_event_leads", {"file_name": "../../etc/passwd", "event_name": "E"}
            )
        assert "inbox" in str(exc_info.value.__cause__ or exc_info.value)

    asyncio.run(go())
    mcp_server._svc.cache_clear()


def test_lead_rows_agree_with_event_funnel(conn, crm, write_csv):
    p = write_csv("Name,Email,Consent\nA,a@gmail.com,yes\nB,b@gmail.com,yes\nC,c@gmail.com,no\n")
    run_leads(p, "E", conn, crm, RuleEnricher())
    funnel = reports.event_funnel(conn)[0]
    rows = reports.lead_rows(conn)
    assert len(rows) == funnel["leads"]
    assert sum(r["hot"] for r in rows) == funnel["hot"]
    assert sum(r["needs_review"] for r in rows) == funnel["in_review"]
    assert all(r["persona"] == "in review" for r in rows if r["needs_review"])
    assert reports.geo_answers(conn) == [] and reports.recent_drafts(conn) == []


def test_weekly_brief_handles_empty_db(conn):
    text = reports.weekly_brief(conn)
    assert "No event leads processed yet" in text and "No GEO checks yet" in text


def test_eval_harness_reports_accuracy(tmp_path):
    path = make_eval_set(tmp_path / "eval.csv", n=40)
    result = evaluate(path, RuleEnricher()).as_dict()
    assert result["n"] == 40
    for key in ("persona_accuracy", "intent_accuracy", "language_accuracy", "review_rate"):
        assert 0.0 <= result[key] <= 1.0
