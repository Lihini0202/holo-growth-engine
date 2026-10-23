"""Sync lifecycle stages to the CRM, consent first.

* Users whose Holo marketing choice is "yes" get their stage synced.
* Users who withdrew are synced once more as opted_out (so CRM workflows stop),
  then never again.
* Users never asked are not synced at all, and their email is not stored here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from holo_growth import db
from holo_growth.consent import ConsentLedger, Purpose
from holo_growth.crm.base import CRM
from holo_growth.models import LifecycleStage
from holo_growth.retention.lifecycle import classify
from holo_growth.retention.usage import UsageSource

log = logging.getLogger(__name__)


@dataclass
class RetentionReport:
    run_id: int
    status: str
    users_read: int = 0
    synced: int = 0
    not_consented: int = 0
    opted_out_synced: int = 0
    failed: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    pending_validations_total: int = 0

    def summary(self) -> str:
        stages = ", ".join(f"{k} {v}" for k, v in sorted(self.stages.items()))
        return (
            f"{self.users_read} Holo users read. Stages: {stages}. Synced {self.synced} to CRM "
            f"({self.opted_out_synced} as opted out); {self.not_consented} not synced (no "
            f"consent). {self.pending_validations_total} predictions awaiting outcomes."
        )


def run_retention(
    conn: sqlite3.Connection,
    source: UsageSource,
    crm: CRM,
    now: datetime | None = None,
    dry_run: bool = False,
) -> RetentionReport:
    now = now or datetime.now(UTC)
    run_id = db.start_run(conn, "retention", source.name + (" (dry run)" if dry_run else ""))
    report = RetentionReport(run_id, "running")
    ledger = ConsentLedger(conn)
    stages: Counter[str] = Counter()
    contacts: list[dict[str, str]] = []
    try:
        users = source.users()
        report.users_read = len(users)
        for act in users:
            email = (act.email or "").lower() or None
            if email and act.marketing_opt_in is not None and not dry_run:
                ledger.record_if_changed(
                    email,
                    Purpose.MARKETING_EMAIL,
                    act.marketing_opt_in,
                    "holo_app",
                    at=act.marketing_opt_in_at,
                )
            state = ledger.state(email, Purpose.MARKETING_EMAIL) if email else None
            if dry_run and act.marketing_opt_in is not None:
                state = act.marketing_opt_in
            # "Unsubscribed" and "never said yes" are different: only the first is
            # ever told to the CRM, so its workflows stop.
            withdrawn = bool(email) and not dry_run and ledger.withdrew_after_grant(email or "")
            snap = classify(act, now, consent_withdrawn=withdrawn)
            stages[snap.stage.value] += 1
            report.pending_validations_total += snap.pending_validations

            keep_email: str | None = None
            if state is True:
                keep_email = email
            elif withdrawn:
                previously = conn.execute(
                    "SELECT stage FROM lifecycle WHERE holo_user_id = ?", (act.user_id,)
                ).fetchone()
                if previously is None or previously["stage"] != LifecycleStage.OPTED_OUT:
                    keep_email = email  # tell the CRM once, then never again

            if keep_email:
                props = {
                    "email": keep_email,
                    "holo_lifecycle_stage": snap.stage.value,
                    "holo_marketing_consent": "true" if state is True else "false",
                }
                if state is True:
                    props["holo_pending_validations"] = str(snap.pending_validations)
                    if act.first_name:
                        props["firstname"] = act.first_name
                contacts.append(props)
                if withdrawn:
                    report.opted_out_synced += 1
            elif not withdrawn:
                report.not_consented += 1

            if not dry_run:
                conn.execute(
                    """
                    INSERT INTO lifecycle (holo_user_id, email, stage, analyses_14d,
                        analyses_total, pending_validations, last_active, synced_run)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(holo_user_id) DO UPDATE SET
                        email = excluded.email, stage = excluded.stage,
                        analyses_14d = excluded.analyses_14d,
                        analyses_total = excluded.analyses_total,
                        pending_validations = excluded.pending_validations,
                        last_active = excluded.last_active, synced_run = excluded.synced_run,
                        updated_at = datetime('now')
                    """,
                    (
                        act.user_id,
                        email if state is True else None,
                        snap.stage.value,
                        snap.analyses_14d,
                        snap.analyses_total,
                        snap.pending_validations,
                        snap.last_active.isoformat(),
                        run_id if keep_email else None,
                    ),
                )
        conn.commit()

        if contacts and not dry_run:
            crm.ensure_properties()
            result = crm.upsert_contacts(contacts)
            report.synced = result.created + result.updated
            report.failed = len(result.failed)
        elif dry_run:
            report.synced = len(contacts)

        report.stages = dict(stages)
        report.status = "partial" if report.failed else "ok"
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", json.dumps({"error": str(exc)[:500]}))
        log.exception("retention run failed")
        raise
    db.finish_run(conn, run_id, report.status, json.dumps(asdict(report)))
    return report
