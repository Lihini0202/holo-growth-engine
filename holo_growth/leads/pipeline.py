"""Event lead pipeline: file in, clean consented contacts in the CRM, run report out.

    ingest -> clean -> dedupe -> consent -> enrich -> score -> CRM upsert -> log

Privacy choices:
* Only people who ticked the marketing box are uploaded or stored. Everyone
  else is counted in the run stats and then dropped (data minimisation).
* A later unsubscribe always wins: re-importing an old sheet cannot re-grant
  consent (see ConsentLedger.record_if_changed).
* Under-18 rows are discarded without storing their details.

Idempotency: the input file's hash is recorded. Re-running the same file is a
no-op unless forced, and the CRM upsert is keyed on email anyway, so a forced
re-run updates contacts instead of duplicating them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from holo_growth import db
from holo_growth.consent import ConsentLedger, Purpose
from holo_growth.crm.base import CRM
from holo_growth.leads.clean import clean_row
from holo_growth.leads.dedupe import dedupe
from holo_growth.leads.enrich import Enricher, LLMEnricher
from holo_growth.leads.ingest import ingest
from holo_growth.leads.scoring import HOT_THRESHOLD, score
from holo_growth.models import EnrichedLead, RowIssue

log = logging.getLogger(__name__)


@dataclass
class LeadRunReport:
    run_id: int
    event: str
    status: str
    rows_read: int = 0
    rejected: int = 0
    duplicates_merged: int = 0
    not_consented: int = 0
    already_in_crm: int = 0
    uploaded_created: int = 0
    uploaded_updated: int = 0
    upload_failed: int = 0
    enriched_llm: int = 0
    enriched_rules: int = 0
    needs_review: int = 0
    hot_leads: list[dict[str, object]] = field(default_factory=list)
    issues: list[dict[str, object]] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    def summary(self) -> str:
        if self.skipped_reason:
            return f"Run {self.run_id} skipped: {self.skipped_reason}"
        return (
            f"{self.event}: {self.rows_read} rows read, {self.rejected} rejected, "
            f"{self.duplicates_merged} duplicates merged, {self.not_consented} without consent "
            f"(not uploaded). CRM: {self.uploaded_created} created, {self.uploaded_updated} "
            f"updated, {self.upload_failed} failed. {self.needs_review} sent to review, "
            f"{len(self.hot_leads)} hot leads."
        )


def _crm_properties(item: EnrichedLead, consent: bool) -> dict[str, str]:
    lead = item.lead
    props: dict[str, str] = {
        "email": lead.email,
        "firstname": lead.first_name,
        "lastname": lead.last_name,
        "holo_source_event": lead.source_event,
        "holo_lifecycle_stage": "lead",
        "holo_marketing_consent": "true" if consent else "false",
    }
    if lead.phone:
        props["phone"] = lead.phone
    if lead.city:
        props["city"] = lead.city
    if lead.organisation:
        props["company"] = lead.organisation
    e = item.enrichment
    if e and not item.needs_review:
        props.update(
            {
                "holo_persona": e.persona.value,
                "holo_intent": e.intent.value,
                "holo_language": e.language.value,
                "holo_lead_score": str(item.score),
            }
        )
    return props


def run_leads(
    path: Path | str,
    event: str,
    conn: sqlite3.Connection,
    crm: CRM,
    enricher: Enricher,
    review_threshold: float = 0.7,
    dry_run: bool = False,
    force: bool = False,
) -> LeadRunReport:
    result = ingest(path)
    label = event + (" (dry run)" if dry_run else "")

    if not force and not dry_run:
        prior = db.previous_successful_run(conn, "leads", result.input_hash)
        if prior:
            run_id = db.start_run(conn, "leads", label, result.input_hash)
            report = LeadRunReport(
                run_id,
                event,
                "skipped",
                skipped_reason=f"same file already processed in run {prior}; use --force to re-run",
            )
            db.finish_run(conn, run_id, "skipped", json.dumps(asdict(report)))
            return report

    run_id = db.start_run(conn, "leads", label, None if dry_run else result.input_hash)
    report = LeadRunReport(
        run_id, event, "running", rows_read=len(result.leads), unmapped_columns=result.unmapped_columns
    )
    issues: list[RowIssue] = []
    try:
        cleaned = []
        for raw in result.leads:
            lead, row_issues = clean_row(raw, event)
            issues.extend(row_issues)
            if lead is None:
                report.rejected += 1
            else:
                cleaned.append(lead)

        unique, dup_issues, merged = dedupe(cleaned)
        issues.extend(dup_issues)
        report.duplicates_merged = merged

        ledger = ConsentLedger(conn)
        consented = []
        for lead in unique:
            if not dry_run and lead.marketing_opt_in:
                ledger.record_if_changed(lead.email, Purpose.MARKETING_EMAIL, True, f"event_form:{event}")
            allowed = ledger.allowed(lead.email) if not dry_run else lead.marketing_opt_in
            if allowed:
                consented.append(lead)
            else:
                report.not_consented += 1

        existing = crm.existing_emails([lead.email for lead in consented]) if consented else set()
        report.already_in_crm = len(existing)

        items: list[EnrichedLead] = []
        for lead in consented:
            enrichment = enricher.enrich(lead)
            source = getattr(enricher, "last_source", enricher.source)
            if source == "llm":
                report.enriched_llm += 1
            else:
                report.enriched_rules += 1
            points, _ = score(lead, enrichment)
            needs_review = enrichment.confidence < review_threshold
            items.append(
                EnrichedLead(
                    lead=lead,
                    enrichment=enrichment,
                    enrichment_source=source,
                    score=points,
                    needs_review=needs_review,
                )
            )

        report.needs_review = sum(1 for i in items if i.needs_review)
        report.hot_leads = sorted(
            (
                {
                    "email": i.lead.email,
                    "name": f"{i.lead.first_name} {i.lead.last_name}".strip(),
                    "score": i.score,
                    "persona": i.enrichment.persona.value if i.enrichment else None,
                }
                for i in items
                if not i.needs_review and i.score >= HOT_THRESHOLD
            ),
            key=lambda d: -int(str(d["score"])),
        )

        if dry_run:
            report.status = "ok"
        else:
            if items:
                crm.ensure_properties()
                upsert = crm.upsert_contacts([_crm_properties(i, True) for i in items])
                report.uploaded_created = upsert.created
                report.uploaded_updated = upsert.updated
                report.upload_failed = len(upsert.failed)
                for email, err in upsert.failed:
                    issues.append(RowIssue(row_number=0, field="crm", problem=err[:200], value=email))
            failed_emails = {i.value for i in issues if i.field == "crm"}
            _persist(conn, run_id, items, failed_emails)
            report.status = "partial" if report.upload_failed else "ok"

        for issue in issues:
            conn.execute(
                "INSERT INTO row_issues (run_id, row_number, field, problem, value) VALUES (?, ?, ?, ?, ?)",
                (run_id, issue.row_number, issue.field, issue.problem, issue.value),
            )
        conn.commit()
        report.issues = [i.model_dump() for i in issues]
    except Exception as exc:
        report.status = "failed"
        log.exception("lead run failed", extra={"run_id": run_id})
        db.finish_run(conn, run_id, "failed", json.dumps({"error": str(exc)[:500]}))
        raise

    stats = asdict(report)
    stats.pop("issues")  # stored row by row in row_issues
    db.finish_run(conn, run_id, report.status, json.dumps(stats))
    log.info("lead run finished", extra={"run_id": run_id, "status": report.status})
    return report


def _persist(conn: sqlite3.Connection, run_id: int, items: list[EnrichedLead], failed: set[str]) -> None:
    for item in items:
        lead, e = item.lead, item.enrichment
        conn.execute(
            """
            INSERT INTO leads (email, first_name, last_name, phone, organisation, city, age,
                               source_event, persona, intent, language, enrichment_source,
                               confidence, score, needs_review, first_seen_run, last_seen_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                first_name = excluded.first_name, last_name = excluded.last_name,
                phone = COALESCE(excluded.phone, leads.phone),
                organisation = COALESCE(excluded.organisation, leads.organisation),
                city = COALESCE(excluded.city, leads.city),
                age = COALESCE(excluded.age, leads.age),
                persona = excluded.persona, intent = excluded.intent,
                language = excluded.language, enrichment_source = excluded.enrichment_source,
                confidence = excluded.confidence, score = excluded.score,
                needs_review = excluded.needs_review,
                last_seen_run = excluded.last_seen_run, updated_at = datetime('now')
            """,
            (
                lead.email,
                lead.first_name,
                lead.last_name,
                lead.phone,
                lead.organisation,
                lead.city,
                lead.age,
                lead.source_event,
                e.persona.value if e else None,
                e.intent.value if e else None,
                e.language.value if e else None,
                item.enrichment_source,
                e.confidence if e else None,
                item.score,
                int(item.needs_review),
                run_id,
                run_id,
            ),
        )
        if item.needs_review and lead.email not in failed:
            conn.execute(
                "INSERT INTO review_queue (kind, ref, reason, payload) "
                "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
                "  SELECT 1 FROM review_queue WHERE kind = 'enrichment' AND ref = ? "
                "  AND status = 'open')",
                (
                    "enrichment",
                    lead.email,
                    f"confidence {e.confidence:.2f} below threshold" if e else "no enrichment",
                    e.model_dump_json() if e else None,
                    lead.email,
                ),
            )
    conn.commit()


def make_enricher(llm) -> Enricher:
    from holo_growth.leads.enrich import RuleEnricher

    return LLMEnricher(llm) if llm is not None else RuleEnricher()
