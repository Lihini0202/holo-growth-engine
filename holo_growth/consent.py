"""Consent ledger.

Append-only: every grant or withdrawal is an event with its source, and the
current state is the latest event per (email, purpose). That gives an audit
trail of when and how someone opted in, which is what a regulator (or a user)
will ask for.

Rules enforced here:
* No consent record means no marketing. Silence is not consent.
* A withdrawal always wins over an older grant, from any source.
* Purposes are separate: product updates do not imply marketing email.
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
        """Record only if it changes the current state. Returns True if recorded.

        A grant can only override an existing withdrawal when it carries a
        timestamp later than that withdrawal (a user re-enabling the toggle in
        the app). A re-imported signup sheet or a stale app value has no such
        timestamp, so it can never re-grant consent to someone who
        unsubscribed. Withdrawals are always recorded.
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

    def withdrew_after_grant(self, email: str, purpose: Purpose = Purpose.MARKETING_EMAIL) -> bool:
        """True if the current state is withdrawn and consent was granted at some point.

        Distinguishes "unsubscribed" (must be told to the CRM so workflows stop)
        from "never said yes" (must never reach the CRM at all).
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
