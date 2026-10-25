"""SQLite-backed CRM used when no HubSpot credentials are configured.

Implements the same upsert-by-email semantics as the HubSpot client.
"""

from __future__ import annotations

import json
import sqlite3

from holo_growth.crm.base import SubscriptionChange, SubscriptionResult, UpsertResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_crm_contacts (
    email      TEXT PRIMARY KEY,
    properties TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Stored separately from contact properties, mirroring HubSpot.
CREATE TABLE IF NOT EXISTS local_crm_subscriptions (
    email       TEXT PRIMARY KEY,
    status      TEXT NOT NULL,                -- SUBSCRIBED | UNSUBSCRIBED
    explanation TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class LocalCRM:
    name = "local"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.executescript(SCHEMA)

    def ensure_properties(self) -> None:
        return None

    def existing_emails(self, emails: list[str]) -> set[str]:
        if not emails:
            return set()
        found: set[str] = set()
        for i in range(0, len(emails), 500):
            chunk = emails[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT email FROM local_crm_contacts WHERE email IN ({marks})", chunk
            ).fetchall()
            found |= {r["email"] for r in rows}
        return found

    def upsert_contacts(self, contacts: list[dict[str, str]]) -> UpsertResult:
        result = UpsertResult()
        for c in contacts:
            email = c["email"].lower()
            row = self.conn.execute(
                "SELECT properties FROM local_crm_contacts WHERE email = ?", (email,)
            ).fetchone()
            if row:
                merged = {**json.loads(row["properties"]), **c}
                self.conn.execute(
                    "UPDATE local_crm_contacts SET properties = ?, updated_at = datetime('now') "
                    "WHERE email = ?",
                    (json.dumps(merged), email),
                )
                result.updated += 1
            else:
                self.conn.execute(
                    "INSERT INTO local_crm_contacts (email, properties) VALUES (?, ?)",
                    (email, json.dumps(c)),
                )
                result.created += 1
        self.conn.commit()
        return result

    def set_email_subscriptions(self, changes: list[SubscriptionChange]) -> SubscriptionResult:
        result = SubscriptionResult()
        for ch in changes:
            self.conn.execute(
                """
                INSERT INTO local_crm_subscriptions (email, status, explanation) VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET status = excluded.status,
                    explanation = excluded.explanation, updated_at = datetime('now')
                """,
                (ch.email.lower(), "SUBSCRIBED" if ch.subscribed else "UNSUBSCRIBED", ch.explanation),
            )
            result.updated.append(ch.email.lower())
        self.conn.commit()
        return result

    def subscription(self, email: str) -> str | None:
        row = self.conn.execute(
            "SELECT status FROM local_crm_subscriptions WHERE email = ?", (email.lower(),)
        ).fetchone()
        return row["status"] if row else None

    def get(self, email: str) -> dict[str, str] | None:
        row = self.conn.execute(
            "SELECT properties FROM local_crm_contacts WHERE email = ?", (email.lower(),)
        ).fetchone()
        return json.loads(row["properties"]) if row else None


def crm_from_settings(settings, conn: sqlite3.Connection):
    if settings.crm == "salesforce":
        from holo_growth.crm.salesforce import SalesforceCRM

        return SalesforceCRM(
            settings.salesforce_domain,
            settings.salesforce_client_id,
            settings.salesforce_client_secret,
            settings.salesforce_api_version,
        )
    if settings.crm == "hubspot":
        from holo_growth.crm.hubspot import HubSpotCRM

        return HubSpotCRM(
            settings.hubspot_token,
            settings.hubspot_base_url,
            subscription_id=settings.hubspot_subscription_id,
        )
    return LocalCRM(conn)
