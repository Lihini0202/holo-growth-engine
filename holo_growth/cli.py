"""Command line: `holo-growth --help`."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import typer

from holo_growth import reports
from holo_growth.logging_setup import configure

app = typer.Typer(help="Holo Growth Engine: go-to-market automation for Holo.", no_args_is_help=True)
leads_app = typer.Typer(help="Event lead pipeline.", no_args_is_help=True)
retention_app = typer.Typer(help="Lifecycle stages from Holo usage.", no_args_is_help=True)
content_app = typer.Typer(help="Campaign drafts with pre-send checks.", no_args_is_help=True)
geo_app = typer.Typer(help="Track how Holo shows up in AI answers.", no_args_is_help=True)
review_app = typer.Typer(help="Human review queue.", no_args_is_help=True)
consent_app = typer.Typer(help="Consent ledger.", no_args_is_help=True)
report_app = typer.Typer(help="Reports.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluate enrichment quality.", no_args_is_help=True)
for name, sub in [
    ("leads", leads_app),
    ("retention", retention_app),
    ("content", content_app),
    ("geo", geo_app),
    ("review", review_app),
    ("consent", consent_app),
    ("report", report_app),
    ("eval", eval_app),
]:
    app.add_typer(sub, name=name)


def _services():
    from holo_growth.services import build

    configure(os.environ.get("HOLO_GROWTH_LOG_LEVEL", "WARNING"))
    return build()


def _print_json(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


@app.command()
def status() -> None:
    """Show which integrations are live and which are running offline."""
    _print_json(_services().mode())


# -- leads -------------------------------------------------------------------


@leads_app.command("process")
def leads_process(
    file: Path = typer.Argument(..., exists=True, readable=True, help="CSV or XLSX export"),
    event: str = typer.Option(..., "--event", "-e", help="Event name, stored on every contact"),
    dry_run: bool = typer.Option(False, help="Clean and enrich but write nothing"),
    force: bool = typer.Option(False, help="Re-run a file that was already processed"),
    as_json: bool = typer.Option(False, "--json", help="Print the full report as JSON"),
) -> None:
    """Clean, dedupe, enrich and upload an event signup sheet."""
    from holo_growth.leads.pipeline import make_enricher, run_leads

    s = _services()
    report = run_leads(
        file,
        event,
        s.conn,
        s.crm,
        make_enricher(s.llm),
        s.settings.review_threshold,
        dry_run=dry_run,
        force=force,
    )
    if as_json:
        _print_json(asdict(report))
        return
    typer.echo(report.summary())
    if report.unmapped_columns:
        typer.echo(f"Ignored columns: {', '.join(report.unmapped_columns)}")
    problems = [i for i in report.issues if i["field"] in ("email", "age", "crm")]
    if problems:
        typer.echo(f"\nRows needing attention ({len(problems)}):")
        for i in problems[:15]:
            typer.echo(f"  row {i['row_number']}: {i['field']} {i['problem']} {i['value']}")
    if report.hot_leads:
        typer.echo("\nHot leads:")
        for h in report.hot_leads[:10]:
            typer.echo(f"  {h['score']:>3}  {h['name']}  <{h['email']}>  {h['persona']}")


@leads_app.command("sample")
def leads_sample(
    out: Path = typer.Argument(Path("samples/campus_launch_signups.csv")), rows: int = 120, seed: int = 7
) -> None:
    """Write a deliberately messy synthetic signup sheet to try the pipeline on."""
    from holo_growth.synthetic import make_event_file

    typer.echo(f"Wrote {make_event_file(out, rows, seed)}")


# -- retention ---------------------------------------------------------------


@retention_app.command("sync")
def retention_sync(
    dry_run: bool = False, synthetic: bool = typer.Option(False, help="Use synthetic Holo users")
) -> None:
    """Compute lifecycle stages from Holo usage and sync consented users to the CRM."""
    from holo_growth.retention.sync import run_retention

    s = _services()
    report = run_retention(s.conn, s.usage_source(synthetic), s.crm, dry_run=dry_run)
    typer.echo(report.summary())


# -- content -----------------------------------------------------------------


@content_app.command("draft")
def content_draft(
    segment: str = typer.Argument(..., help="new | at_risk | dormant | validation_pending"),
    language: str = typer.Option("en", help="en | si | ta"),
) -> None:
    """Draft a lifecycle email and run it through the pre-send checks."""
    from holo_growth.content.campaigns import draft_campaign

    s = _services()
    d = draft_campaign(s.conn, segment, language, s.llm, set(s.settings.link_domains))
    typer.echo(f"Draft {d.id} [{d.status}] ({d.source})\nSubject: {d.subject}\n\n{d.body}\n")
    for e in d.errors:
        typer.echo(f"ERROR   {e}")
    for w in d.warnings:
        typer.echo(f"WARNING {w}")


# -- geo ---------------------------------------------------------------------


@geo_app.command("run")
def geo_run(config: Path | None = typer.Option(None, help="Custom prompts/competitors JSON")) -> None:
    """Ask AI assistants Holo-relevant questions and record brand mentions."""
    from holo_growth.geo.tracker import GeoConfig, providers_from_env, run_geo

    s = _services()
    providers = providers_from_env(s.settings, s.llm)
    report = run_geo(s.conn, providers, GeoConfig.load(str(config) if config else None))
    if [p.name for p in providers] == ["demo"]:
        typer.echo("No API keys set: ran against canned DEMO answers, not real assistants.")
    typer.echo(report.summary())


# -- review ------------------------------------------------------------------


@review_app.command("list")
def review_list() -> None:
    """Show open review items."""
    rows = reports.open_reviews(_services().conn)
    if not rows:
        typer.echo("Nothing waiting for review.")
    for r in rows:
        typer.echo(f"#{r['id']:<4} {r['kind']:<10} {r['ref']:<40} {r['reason']}")


@review_app.command("approve")
def review_approve(review_id: int, persona: str | None = None, intent: str | None = None) -> None:
    """Approve an enrichment (optionally correcting it) and push it to the CRM."""
    from holo_growth.models import Intent, Persona
    from holo_growth.review import resolve_enrichment

    s = _services()
    _print_json(
        resolve_enrichment(
            s.conn,
            s.crm,
            review_id,
            Persona(persona) if persona else None,
            Intent(intent) if intent else None,
        )
    )


@review_app.command("reject")
def review_reject(review_id: int) -> None:
    from holo_growth.review import resolve_enrichment

    s = _services()
    _print_json(resolve_enrichment(s.conn, s.crm, review_id, reject=True))


# -- consent -----------------------------------------------------------------


@consent_app.command("withdraw")
def consent_withdraw(email: str, source: str = "manual") -> None:
    """Record an unsubscribe for every purpose and mark the CRM contact opted out."""
    from holo_growth.consent import ConsentLedger

    s = _services()
    ConsentLedger(s.conn).withdraw(email, source)
    s.crm.upsert_contacts(
        [{"email": email.lower(), "holo_marketing_consent": "false", "holo_lifecycle_stage": "opted_out"}]
    )
    typer.echo(f"{email} withdrawn from all purposes.")


@consent_app.command("show")
def consent_show(email: str) -> None:
    from holo_growth.consent import ConsentLedger

    _print_json(ConsentLedger(_services().conn).history(email))


# -- reports -----------------------------------------------------------------


@report_app.command("brief")
def report_brief(out: Path | None = None) -> None:
    """Weekly growth brief in markdown."""
    text = reports.weekly_brief(_services().conn)
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text)


@report_app.command("runs")
def report_runs(limit: int = 10) -> None:
    for r in reports.recent_runs(_services().conn, limit):
        typer.echo(f"#{r['id']:<4} {r['kind']:<10} {r['status']:<8} {r['started_at']}  {r['label'] or ''}")


# -- eval --------------------------------------------------------------------


@eval_app.command("enrichment")
def eval_enrichment(
    file: Path = typer.Argument(Path("evals/enrichment_labelled.csv")),
    out: Path | None = typer.Option(None, help="Write results JSON here"),
) -> None:
    """Score the rules baseline (and the LLM, if a key is set) on the labelled set."""
    from holo_growth.evaluation import evaluate
    from holo_growth.leads.enrich import LLMEnricher, RuleEnricher

    s = _services()
    if not file.exists():
        from holo_growth.synthetic import make_eval_set

        make_eval_set(file)
        typer.echo(f"Created labelled set at {file}")
    results = [evaluate(file, RuleEnricher(), s.settings.review_threshold).as_dict()]
    if s.llm is not None:
        results.append(evaluate(file, LLMEnricher(s.llm), s.settings.review_threshold).as_dict())
    else:
        typer.echo("No ANTHROPIC_API_KEY: only the rules baseline was evaluated.")
    _print_json(results)
    if out:
        out.write_text(json.dumps(results, indent=2), encoding="utf-8")


# -- demo --------------------------------------------------------------------


@app.command()
def demo(db_path: Path = typer.Option(Path("demo.db"), help="Fresh database for the demo")) -> None:
    """Run the whole system end to end on synthetic data. No keys needed."""
    os.environ["HOLO_GROWTH_DB"] = str(db_path)
    if db_path.exists():
        db_path.unlink()
    from holo_growth.content.campaigns import draft_campaign
    from holo_growth.geo.tracker import providers_from_env, run_geo
    from holo_growth.leads.pipeline import make_enricher, run_leads
    from holo_growth.retention.sync import run_retention
    from holo_growth.synthetic import make_event_file

    s = _services()
    typer.echo(f"Mode: {json.dumps(s.mode())}\n")

    events = [("Campus Launch - University of Moratuwa", 7), ("Colombo Tech Meetup", 21)]
    for name, seed in events:
        f = make_event_file(Path("samples") / f"{name.split(' - ')[-1].replace(' ', '_')}.csv", 110, seed)
        r = run_leads(f, name, s.conn, s.crm, make_enricher(s.llm), s.settings.review_threshold)
        typer.echo(f"[leads] {r.summary()}")

    converts = [
        row["email"] for row in s.conn.execute("SELECT email FROM leads ORDER BY score DESC LIMIT 40")
    ]
    rr = run_retention(s.conn, s.usage_source(synthetic=True, convert_emails=converts), s.crm)
    typer.echo(f"[retention] {rr.summary()}")

    for seg in ("new", "at_risk", "dormant", "validation_pending"):
        d = draft_campaign(s.conn, seg, "en", s.llm, set(s.settings.link_domains))
        typer.echo(f"[content] {seg}: {d.status} ({d.source})")
    d = draft_campaign(s.conn, "at_risk", "si", s.llm, set(s.settings.link_domains))
    typer.echo(f"[content] at_risk/si: {d.status} ({'; '.join(d.errors)})")

    g = run_geo(s.conn, providers_from_env(s.settings, s.llm))
    typer.echo(f"[geo] {g.summary()}\n")
    typer.echo(reports.weekly_brief(s.conn))


if __name__ == "__main__":
    app()
