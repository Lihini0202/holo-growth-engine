"""HubSpot CRM v3 client: batch upsert by email, with retries and rate-limit handling.

Uses a private app token (Settings > Integrations > Private apps) with the
scopes crm.objects.contacts.read, crm.objects.contacts.write and
crm.schemas.contacts.write. A free developer test account is enough.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from holo_growth.crm.base import CUSTOM_PROPERTIES, UpsertResult

log = logging.getLogger(__name__)

BATCH_SIZE = 100  # HubSpot's limit for batch endpoints
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
    ) -> None:
        if not token:
            raise ValueError("HubSpot token is required")
        self._client = client or httpx.Client(timeout=30)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._max_attempts = max_attempts
        self._sleep = sleep

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
            # 4xx other than 429: a bug or bad data, retrying will not help.
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
            resp = self._request("GET", f"/crm/v3/properties/contacts/{prop['name']}", ok=(200, 404))
            if resp.status_code == 200:
                continue
            body: dict[str, Any] = {
                "name": prop["name"],
                "label": prop["label"],
                "type": prop["type"],
                "fieldType": prop["fieldType"],
                "groupName": GROUP_NAME,
            }
            options: list[str] = list(prop.get("options", []))  # type: ignore[call-overload]
            if options:
                body["options"] = [
                    {"label": o.replace("_", " ").title(), "value": o, "displayOrder": i, "hidden": False}
                    for i, o in enumerate(options)
                ]
            self._request("POST", "/crm/v3/properties/contacts", json=body, ok=(200, 201, 409))
            log.info("created HubSpot property", extra={"property": prop["name"]})

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
        """Create or update by email. Safe to call repeatedly with the same data."""
        result = UpsertResult()
        for chunk in _chunks(contacts, BATCH_SIZE):
            inputs = [{"idProperty": "email", "id": c["email"], "properties": c} for c in chunk]
            try:
                resp = self._request("POST", "/crm/v3/objects/contacts/batch/upsert", json={"inputs": inputs})
            except HubSpotError as exc:
                # One bad batch should not lose the rest of the run.
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


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", resp.text))[:300]
    except ValueError:
        return resp.text[:300]
