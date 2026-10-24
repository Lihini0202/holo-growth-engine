"""Append-only consent ledger.

Each grant or withdrawal is stored as an event with its source and timestamp. The
current state is the latest event per (email, purpose); the event history is the
audit trail.

Invariants:
* No record means no consent.
* A withdrawal overrides any earlier grant, regardless of source.
* Purposes are independent: product updates do not imply marketing email.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from enum import StrEnum


class Purpose(StrEnum):
    MARKETING_EMAIL = "marketing_email"
    PRODUCT_UPDATES = "product_updates"


class ConsentLedger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, email: str, purpose: Purpose, granted: bool, source: str) -> None:
        self.conn.execute(
            "INSERT INTO consent_events (email, purpose, granted, source) VALUES (?, ?, ?, ?)",
            (email.lower(), purpose.value, int(granted), source),
        )
        self.conn.commit()

    def record_if_changed(
        self, email: str, purpose: Purpose, granted: bool, source: str, at: datetime | None = None
    ) -> bool:
        """Record an event only if it changes the current state. Returns True if recorded.

        A grant overrides an existing withdrawal only when it carries a timestamp later
        than the withdrawal (an explicit in-app opt-in). Undated grants, such as a
        re-imported signup sheet, never override a withdrawal. Withdrawals are always
        recorded.
        """
        current = self.state(email, purpose)
        if current is granted:
            return False
        if granted and current is False:
            withdrawn_at = self._latest_at(email, purpose)
            if at is None or withdrawn_at is None or at <= withdrawn_at:
                return False
        self.record(email, purpose, granted, source)
        return True

    def _latest_at(self, email: str, purpose: Purpose) -> datetime | None:
        row = self.conn.execute(
            "SELECT recorded_at FROM consent_events WHERE email = ? AND purpose = ? ORDER BY id DESC LIMIT 1",
            (email.lower(), purpose.value),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["recorded_at"]).replace(tzinfo=UTC)

    def state(self, email: str, purpose: Purpose) -> bool | None:
        row = self.conn.execute(
            "SELECT granted FROM consent_events WHERE email = ? AND purpose = ? ORDER BY id DESC LIMIT 1",
            (email.lower(), purpose.value),
        ).fetchone()
        return None if row is None else bool(row["granted"])

    def allowed(self, email: str, purpose: Purpose = Purpose.MARKETING_EMAIL) -> bool:
        return self.state(email, purpose) is True

    def allowed_after_signup(
        self, email: str, opted_in: bool, purpose: Purpose = Purpose.MARKETING_EMAIL
    ) -> bool:
        """Return what `allowed` would return after an undated sheet opt-in, without writing.

        Used by dry runs so that previews match the real run.
        """
        current = self.state(email, purpose)
        return current is True or (current is None and opted_in)

    def withdrew_after_grant(self, email: str, purpose: Purpose = Purpose.MARKETING_EMAIL) -> bool:
        """Return True if consent is currently withdrawn and was granted at some point.

        Distinguishes an unsubscribe, which must be propagated to the CRM, from a contact
        who never opted in and must never be sent to the CRM.
        """
        if self.state(email, purpose) is not False:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM consent_events WHERE email = ? AND purpose = ? AND granted = 1 LIMIT 1",
            (email.lower(), purpose.value),
        ).fetchone()
        return row is not None

    def withdraw(self, email: str, source: str = "unsubscribe") -> None:
        for purpose in Purpose:
            self.record(email, purpose, False, source)

    def history(self, email: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            "SELECT purpose, granted, source, recorded_at FROM consent_events WHERE email = ? ORDER BY id",
            (email.lower(),),
        ).fetchall()
        return [dict(r) for r in rows]
