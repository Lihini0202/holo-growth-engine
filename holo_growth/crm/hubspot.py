"""HubSpot client (CRM v3 contacts, Communication Preferences v4 subscriptions).

Contacts are upserted in batches keyed on email, with retries on 429 and 5xx
responses. Authentication uses a Service Key or a legacy private app token, sent as
a Bearer token.

Required scopes: crm.objects.contacts.read, crm.objects.contacts.write,
crm.schemas.contacts.write; communication_preferences.read_write when
HUBSPOT_SUBSCRIPTION_ID is set.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

import httpx

from holo_growth.crm.base import CUSTOM_PROPERTIES, SubscriptionChange, SubscriptionResult, UpsertResult

log = logging.getLogger(__name__)

BATCH_SIZE = 100  # HubSpot batch endpoint limit
GROUP_NAME = "holo_growth"


class HubSpotError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HubSpot {status}: {message}")
        self.status = status


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class HubSpotCRM:
    name = "hubspot"

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.hubapi.com",
        client: httpx.Client | None = None,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        subscription_id: str = "",
    ) -> None:
        if not token:
            raise ValueError("HubSpot token is required")
        self._client = client or httpx.Client(timeout=30)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._max_attempts = max_attempts
        self._sleep = sleep
        self.subscription_id = subscription_id

    # -- transport -----------------------------------------------------------

    def _request(
        self, method: str, path: str, json: Any | None = None, ok: tuple[int, ...] = (200, 201, 207)
    ) -> httpx.Response:
        url = f"{self._base}{path}"
        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = self._client.request(method, url, json=json, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt == self._max_attempts:
                    raise HubSpotError(0, f"network error: {type(exc).__name__}") from exc
                self._backoff(attempt, None)
                continue
            if resp.status_code in ok:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_attempts:
                    break
                log.warning(
                    "HubSpot retryable response",
                    extra={"status": resp.status_code, "attempt": attempt, "path": path},
                )
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            # Other 4xx responses indicate invalid input; do not retry.
            raise HubSpotError(resp.status_code, _error_message(resp))
        raise HubSpotError(resp.status_code, f"gave up after {self._max_attempts} attempts")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                self._sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        self._sleep(min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5))

    # -- schema --------------------------------------------------------------

    def ensure_properties(self) -> None:
        self._request(
            "POST",
            "/crm/v3/properties/contacts/groups",
            json={"name": GROUP_NAME, "label": "Holo Growth Engine"},
            ok=(200, 201, 409),
        )
        for prop in CUSTOM_PROPERTIES:
            options: list[str] = list(prop.get("options", []))  # type: ignore[call-overload]
            resp = self._request("GET", f"/crm/v3/properties/contacts/{prop['name']}", ok=(200, 404))
            if resp.status_code == 200:
                if options:
                    self._add_missing_options(str(prop["name"]), options, resp.json().get("options", []))
                continue
            body: dict[str, Any] = {
                "name": prop["name"],
                "label": prop["label"],
                "type": prop["type"],
                "fieldType": prop["fieldType"],
                "groupName": GROUP_NAME,
            }
            if options:
                body["options"] = [_option(o, i) for i, o in enumerate(options)]
            self._request("POST", "/crm/v3/properties/contacts", json=body, ok=(200, 201, 409))
            log.info("created HubSpot property", extra={"property": prop["name"]})

    def _add_missing_options(self, name: str, wanted: list[str], existing: list[dict[str, Any]]) -> None:
        """Append options missing from an existing enumeration. Existing options are kept,
        so values already stored on contacts remain valid."""
        have = {o.get("value") for o in existing}
        missing = [o for o in wanted if o not in have]
        if not missing:
            return
        options = existing + [_option(o, len(existing) + i) for i, o in enumerate(missing)]
        self._request("PATCH", f"/crm/v3/properties/contacts/{name}", json={"options": options})
        log.info("added HubSpot property options", extra={"property": name, "options": missing})

    # -- contacts ------------------------------------------------------------

    def existing_emails(self, emails: list[str]) -> set[str]:
        found: set[str] = set()
        for chunk in _chunks(emails, BATCH_SIZE):
            resp = self._request(
                "POST",
                "/crm/v3/objects/contacts/batch/read",
                json={"idProperty": "email", "properties": ["email"], "inputs": [{"id": e} for e in chunk]},
            )
            for r in resp.json().get("results", []):
                email = (r.get("properties") or {}).get("email")
                if email:
                    found.add(email.lower())
        return found

    def upsert_contacts(self, contacts: list[dict[str, str]]) -> UpsertResult:
        """Create or update contacts by email. Idempotent."""
        result = UpsertResult()
        for chunk in _chunks(contacts, BATCH_SIZE):
            inputs = [{"idProperty": "email", "id": c["email"], "properties": c} for c in chunk]
            try:
                resp = self._request("POST", "/crm/v3/objects/contacts/batch/upsert", json={"inputs": inputs})
            except HubSpotError as exc:
                # Record the batch as failed and continue with the remaining batches.
                result.failed.extend((c["email"], str(exc)) for c in chunk)
                continue
            data = resp.json()
            for r in data.get("results", []):
                if r.get("new"):
                    result.created += 1
                else:
                    result.updated += 1
            for err in data.get("errors", []):
                ids = (err.get("context") or {}).get("ids") or ["unknown"]
                for email in ids:
                    result.failed.append((email, err.get("message", "error")))
        return result

    # -- email subscriptions ---------------------------------------------------

    def subscription_definitions(self) -> list[dict[str, Any]]:
        """Return the account's subscription type definitions."""
        resp = self._request("GET", "/communication-preferences/v4/definitions")
        return list(resp.json().get("results", []))

    def set_email_subscriptions(self, changes: list[SubscriptionChange]) -> SubscriptionResult:
        """Set the configured subscription status for each contact (one request per contact).

        A 401/403 response stops processing and sets `unavailable`, so the caller keeps
        the changes queued. Other per-contact errors are recorded in `failed` and
        processing continues.
        """
        result = SubscriptionResult()
        if not self.subscription_id:
            result.unavailable = "HUBSPOT_SUBSCRIPTION_ID is not set"
            return result
        for ch in changes:
            body = {
                "subscriptionId": int(self.subscription_id),
                "statusState": "SUBSCRIBED" if ch.subscribed else "UNSUBSCRIBED",
                "legalBasis": "CONSENT_WITH_NOTICE",
                "legalBasisExplanation": ch.explanation[:500],
                "channel": "EMAIL",
            }
            try:
                self._request(
                    "POST", f"/communication-preferences/v4/statuses/{quote(ch.email, safe='@')}", json=body
                )
            except HubSpotError as exc:
                if exc.status in (401, 403):
                    result.unavailable = str(exc)
                    return result
                result.failed.append((ch.email, str(exc)))
                continue
            result.updated.append(ch.email)
        return result


def _option(value: str, order: int) -> dict[str, Any]:
    return {"label": value.replace("_", " ").title(), "value": value, "displayOrder": order, "hidden": False}


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", resp.text))[:300]
    except ValueError:
        return resp.text[:300]
