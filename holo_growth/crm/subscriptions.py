"""Outbox that propagates the consent ledger to CRM email subscription status.

The ledger is the source of truth. For each email, the latest unprocessed ledger
event maps to a subscription change:

* granted                  -> SUBSCRIBED
* withdrawn after a grant  -> UNSUBSCRIBED
* withdrawn, never granted -> not sent

An event is marked processed only after the CRM accepts it, so failed or rejected
updates are retried on the next sync. Repeated syncs are idempotent.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from holo_growth.consent import Purpose
from holo_growth.crm.base import CRM, SubscriptionChange

log = logging.getLogger(__name__)


@dataclass
class SubscriptionSyncReport:
    subscribed: int = 0
    unsubscribed: int = 0
    not_sent: int = 0  # Never opted in; not sent
    failed: list[tuple[str, str]] = field(default_factory=list)
    unavailable: str | None = None
    pending: int = 0  # Changes not yet accepted by the CRM

    def summary(self) -> str:
        text = f"Subscriptions: {self.subscribed} subscribed, {self.unsubscribed} unsubscribed"
        if self.failed:
            text += f", {len(self.failed)} failed (retried next sync)"
        if self.unavailable:
            text += f"; {self.pending} waiting, CRM subscriptions unavailable ({self.unavailable})"
        return text + "."


def pending_changes(
    conn: sqlite3.Connection, purpose: Purpose = Purpose.MARKETING_EMAIL
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.id, e.email, e.granted, e.source, e.recorded_at,
               EXISTS (SELECT 1 FROM consent_events g
                       WHERE g.email = e.email AND g.purpose = e.purpose AND g.granted = 1) AS ever_granted
        FROM consent_events e
        JOIN (SELECT email, MAX(id) AS id FROM consent_events WHERE purpose = ? GROUP BY email) latest
             ON latest.id = e.id
        LEFT JOIN consent_crm_sync s ON s.email = e.email AND s.purpose = e.purpose
        WHERE s.event_id IS NULL OR s.event_id < e.id
        ORDER BY e.id
        """,
        (purpose.value,),
    ).fetchall()


def sync_subscriptions(
    conn: sqlite3.Connection, crm: CRM, purpose: Purpose = Purpose.MARKETING_EMAIL
) -> SubscriptionSyncReport:
    report = SubscriptionSyncReport()
    changes: list[SubscriptionChange] = []
    event_for: dict[str, int] = {}
    for r in pending_changes(conn, purpose):
        if r["granted"]:
            note = f"Opted in via {r['source']} at {r['recorded_at']} UTC (Holo consent ledger)"
        elif r["ever_granted"]:
            note = f"Unsubscribed via {r['source']} at {r['recorded_at']} UTC (Holo consent ledger)"
        else:
            _mark(conn, r["email"], purpose, r["id"], pushed=False)
            report.not_sent += 1
            continue
        changes.append(SubscriptionChange(r["email"], bool(r["granted"]), note))
        event_for[r["email"]] = r["id"]

    if changes:
        result = crm.set_email_subscriptions(changes)
        accepted = set(result.updated)
        for ch in changes:
            if ch.email in accepted:
                _mark(conn, ch.email, purpose, event_for[ch.email], pushed=True)
                if ch.subscribed:
                    report.subscribed += 1
                else:
                    report.unsubscribed += 1
        report.failed = result.failed
        report.unavailable = result.unavailable
        report.pending = len(changes) - len(accepted)
        if result.unavailable:
            log.warning("CRM subscriptions unavailable", extra={"reason": result.unavailable})
    conn.commit()
    return report


def _mark(conn: sqlite3.Connection, email: str, purpose: Purpose, event_id: int, pushed: bool) -> None:
    conn.execute(
        """
        INSERT INTO consent_crm_sync (email, purpose, event_id, pushed) VALUES (?, ?, ?, ?)
        ON CONFLICT(email, purpose) DO UPDATE SET event_id = excluded.event_id,
            pushed = excluded.pushed, synced_at = datetime('now')
        """,
        (email, purpose.value, event_id, int(pushed)),
    )
