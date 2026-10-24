"""CRM interface. Pipelines depend on this protocol, not on a specific CRM."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (email, error)


@dataclass(frozen=True)
class SubscriptionChange:
    """Target marketing-email subscription state for one contact, derived from the consent ledger."""

    email: str
    subscribed: bool
    explanation: str  # Source and time of the consent decision; stored as the legal-basis explanation


@dataclass
class SubscriptionResult:
    updated: list[str] = field(default_factory=list)  # Emails accepted by the CRM
    failed: list[tuple[str, str]] = field(default_factory=list)  # (email, error); retried on the next sync
    unavailable: str | None = None  # Reason the CRM rejected all updates (plan, scope or configuration)


class CRM(Protocol):
    name: str

    def ensure_properties(self) -> None: ...

    def existing_emails(self, emails: list[str]) -> set[str]: ...

    def upsert_contacts(self, contacts: list[dict[str, str]]) -> UpsertResult: ...

    def set_email_subscriptions(self, changes: list[SubscriptionChange]) -> SubscriptionResult: ...


# Custom contact properties owned by this project; created on first run.
CUSTOM_PROPERTIES: list[dict[str, object]] = [
    {"name": "holo_source_event", "label": "Holo source event", "type": "string", "fieldType": "text"},
    {
        "name": "holo_persona",
        "label": "Holo persona",
        "type": "enumeration",
        "fieldType": "select",
        "options": ["young_adult", "young_professional", "other"],
    },
    {
        "name": "holo_intent",
        "label": "Holo intent",
        "type": "enumeration",
        "fieldType": "select",
        "options": ["high", "medium", "low"],
    },
    {
        "name": "holo_language",
        "label": "Holo preferred language",
        "type": "enumeration",
        "fieldType": "select",
        "options": ["en", "si", "ta"],
    },
    {"name": "holo_lead_score", "label": "Holo lead score", "type": "number", "fieldType": "number"},
    {
        "name": "holo_lifecycle_stage",
        "label": "Holo lifecycle stage",
        "type": "enumeration",
        "fieldType": "select",
        "options": ["lead", "new", "activated", "engaged", "at_risk", "dormant", "opted_out"],
    },
    {
        "name": "holo_marketing_consent",
        "label": "Holo marketing consent",
        "type": "enumeration",
        "fieldType": "booleancheckbox",
        "options": ["true", "false"],
    },
    {
        "name": "holo_pending_validations",
        "label": "Holo predictions awaiting outcome",
        "type": "number",
        "fieldType": "number",
    },
]
