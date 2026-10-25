"""Salesforce client (REST API, Lead object).

Event contacts are upserted as Leads keyed on the external ID field `Holo_Email__c`, in
batches of 200 through the sObject Collections API. Email consent is written to the
standard `HasOptedOutOfEmail` field.

Authentication uses the OAuth 2.0 client credentials flow of an External Client App; the
app's run-as user needs API access and permission to customise the application, because
`ensure_properties` creates the custom fields and a permission set granting access to them.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote

import httpx

from holo_growth.crm.base import SubscriptionChange, SubscriptionResult, UpsertResult

log = logging.getLogger(__name__)

API_VERSION = "v62.0"
BATCH_SIZE = 200  # sObject Collections limit
QUERY_CHUNK = 100  # keeps SOQL IN clauses well under the URL length limit
EXTERNAL_ID = "Holo_Email__c"
PERMISSION_SET = "Holo_Growth_Engine"

# Custom Lead fields owned by this project: API name -> Tooling API metadata.
FIELDS: dict[str, dict[str, Any]] = {
    EXTERNAL_ID: {"label": "Holo email", "type": "Text", "length": 255, "externalId": True, "unique": True},
    "Holo_Source_Event__c": {"label": "Holo source event", "type": "Text", "length": 255},
    "Holo_Persona__c": {"label": "Holo persona", "type": "Text", "length": 40},
    "Holo_Intent__c": {"label": "Holo intent", "type": "Text", "length": 20},
    "Holo_Language__c": {"label": "Holo preferred language", "type": "Text", "length": 10},
    "Holo_Lead_Score__c": {"label": "Holo lead score", "type": "Number", "precision": 5, "scale": 0},
    "Holo_Lifecycle_Stage__c": {"label": "Holo lifecycle stage", "type": "Text", "length": 40},
    "Holo_Marketing_Consent__c": {
        "label": "Holo marketing consent",
        "type": "Checkbox",
        "defaultValue": "false",
    },
    "Holo_Pending_Validations__c": {
        "label": "Holo predictions awaiting outcome",
        "type": "Number",
        "precision": 5,
        "scale": 0,
    },
}

# Standard Lead fields the engine writes. Field level security can hide them from the run as
# user, so the permission set grants access to them alongside the custom fields.
STANDARD_FIELDS = ("HasOptedOutOfEmail",)

# Engine contact keys -> Lead fields. Unlisted keys are not sent.
FIELD_MAP = {
    "email": "Email",
    "firstname": "FirstName",
    "lastname": "LastName",
    "phone": "Phone",
    "city": "City",
    "company": "Company",
    "holo_source_event": "Holo_Source_Event__c",
    "holo_persona": "Holo_Persona__c",
    "holo_intent": "Holo_Intent__c",
    "holo_language": "Holo_Language__c",
    "holo_lead_score": "Holo_Lead_Score__c",
    "holo_lifecycle_stage": "Holo_Lifecycle_Stage__c",
    "holo_marketing_consent": "Holo_Marketing_Consent__c",
    "holo_pending_validations": "Holo_Pending_Validations__c",
}
NUMBER_FIELDS = {"Holo_Lead_Score__c", "Holo_Pending_Validations__c"}
BOOLEAN_FIELDS = {"Holo_Marketing_Consent__c"}
# Lead requires LastName and Company; event sheets do not always provide them.
REQUIRED_DEFAULTS = {"LastName": "Unknown", "Company": "Not provided"}


class SalesforceError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Salesforce {status}: {message}")
        self.status = status


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _soql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class SalesforceCRM:
    name = "salesforce"

    def __init__(
        self,
        domain: str,
        client_id: str,
        client_secret: str,
        api_version: str = API_VERSION,
        client: httpx.Client | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (domain and client_id and client_secret):
            raise ValueError("Salesforce domain, client ID and client secret are required")
        self._domain = domain.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._version = api_version
        self._client = client or httpx.Client(timeout=30)
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._token = ""
        self._instance = ""

    # -- transport -----------------------------------------------------------

    def _authenticate(self) -> None:
        resp = self._client.post(
            f"{self._domain}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if resp.status_code != 200:
            raise SalesforceError(resp.status_code, _error_message(resp))
        body = resp.json()
        self._token = body["access_token"]
        self._instance = str(body.get("instance_url") or self._domain).rstrip("/")

    def _request(
        self, method: str, path: str, json: Any | None = None, ok: tuple[int, ...] = (200, 201, 204)
    ) -> httpx.Response:
        if not self._token:
            self._authenticate()
        reauthenticated = False
        for attempt in range(1, self._max_attempts + 1):
            resp = self._client.request(
                method,
                f"{self._instance}{path}",
                json=json,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code in ok:
                return resp
            if resp.status_code == 401 and not reauthenticated:  # expired session
                self._authenticate()
                reauthenticated = True
                continue
            if resp.status_code >= 500 and attempt < self._max_attempts:
                self._sleep(min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5))
                continue
            raise SalesforceError(resp.status_code, _error_message(resp))
        raise SalesforceError(resp.status_code, f"gave up after {self._max_attempts} attempts")

    def _api(self, path: str) -> str:
        return f"/services/data/{self._version}{path}"

    # -- schema --------------------------------------------------------------

    def ensure_properties(self) -> None:
        """Create missing custom Lead fields and grant the run-as user access to them."""
        describe = self._request("GET", self._api("/sobjects/Lead/describe")).json()
        existing = {f["name"] for f in describe.get("fields", [])}
        for api_name, metadata in FIELDS.items():
            if api_name in existing:
                continue
            resp = self._request(
                "POST",
                self._api("/tooling/sobjects/CustomField"),
                json={"FullName": f"Lead.{api_name}", "Metadata": metadata},
                ok=(200, 201, 400),
            )
            # A field that exists but is not yet visible to this user reports a duplicate name.
            if resp.status_code == 400 and "DUPLICATE" not in resp.text:
                raise SalesforceError(400, _error_message(resp))
            log.info("created Salesforce field", extra={"field": api_name})
        self._grant_field_access()

    def _grant_field_access(self) -> None:
        found = self._query(f"SELECT Id FROM PermissionSet WHERE Name = {_soql_string(PERMISSION_SET)}")
        if found:
            permission_set = str(found[0]["Id"])
        else:
            created = self._request(
                "POST",
                self._api("/sobjects/PermissionSet"),
                json={"Name": PERMISSION_SET, "Label": "Holo Growth Engine"},
            )
            permission_set = str(created.json()["id"])
        for api_name in (*FIELDS, *STANDARD_FIELDS):
            self._create_ignoring_duplicates(
                "FieldPermissions",
                {
                    "ParentId": permission_set,
                    "SobjectType": "Lead",
                    "Field": f"Lead.{api_name}",
                    "PermissionsRead": True,
                    "PermissionsEdit": True,
                },
            )
        user = self._request("GET", "/services/oauth2/userinfo").json()
        self._create_ignoring_duplicates(
            "PermissionSetAssignment", {"AssigneeId": user["user_id"], "PermissionSetId": permission_set}
        )

    def _create_ignoring_duplicates(self, sobject: str, record: dict[str, Any]) -> None:
        resp = self._request("POST", self._api(f"/sobjects/{sobject}"), json=record, ok=(200, 201, 400))
        if resp.status_code == 400 and "DUPLICATE" not in resp.text:
            raise SalesforceError(400, _error_message(resp))

    def _query(self, soql: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        path = self._api(f"/query?q={quote(soql)}")
        while path:
            body = self._request("GET", path).json()
            records.extend(body.get("records", []))
            path = body.get("nextRecordsUrl") or ""
        return records

    # -- contacts ------------------------------------------------------------

    def existing_emails(self, emails: list[str]) -> set[str]:
        """Return the emails that already have a Lead. Before `ensure_properties` has created
        the external ID field, no Lead can carry one, so the result is empty."""
        found: set[str] = set()
        for chunk in _chunks([e.lower() for e in emails], QUERY_CHUNK):
            values = ", ".join(_soql_string(e) for e in chunk)
            try:
                rows = self._query(f"SELECT {EXTERNAL_ID} FROM Lead WHERE {EXTERNAL_ID} IN ({values})")
            except SalesforceError as exc:
                if exc.status == 400 and EXTERNAL_ID in str(exc):
                    return set()
                raise
            for r in rows:
                if r.get(EXTERNAL_ID):
                    found.add(str(r[EXTERNAL_ID]).lower())
        return found

    def upsert_contacts(self, contacts: list[dict[str, str]]) -> UpsertResult:
        """Create or update Leads by email. Idempotent."""
        result = UpsertResult()
        existing = self.existing_emails([c["email"] for c in contacts])
        records = [_lead_record(c, is_new=c["email"].lower() not in existing) for c in contacts]
        for chunk in _chunks(records, BATCH_SIZE):
            outcomes = self._upsert(chunk, result)
            for record, outcome in zip(chunk, outcomes, strict=False):
                if outcome.get("success"):
                    if outcome.get("created"):
                        result.created += 1
                    else:
                        result.updated += 1
                else:
                    result.failed.append((record[EXTERNAL_ID], _record_errors(outcome)))
        return result

    def _upsert(self, records: list[dict[str, Any]], result: UpsertResult) -> list[dict[str, Any]]:
        try:
            resp = self._request(
                "PATCH",
                self._api(f"/composite/sobjects/Lead/{EXTERNAL_ID}"),
                json={"allOrNone": False, "records": records},
            )
        except SalesforceError as exc:
            # Record the batch as failed and continue with the remaining batches.
            result.failed.extend((r[EXTERNAL_ID], str(exc)) for r in records)
            return []
        return list(resp.json())

    # -- email subscriptions ---------------------------------------------------

    def set_email_subscriptions(self, changes: list[SubscriptionChange]) -> SubscriptionResult:
        """Set `HasOptedOutOfEmail` on each Lead from the consent ledger.

        Only existing Leads are updated; the consent outbox runs after the upload that
        creates them. A 401/403 response sets `unavailable`, so the caller keeps the changes
        queued. Per record errors are recorded in `failed` and retried on the next sync.
        """
        result = SubscriptionResult()
        records = [
            {
                "attributes": {"type": "Lead"},
                EXTERNAL_ID: ch.email.lower(),
                "HasOptedOutOfEmail": not ch.subscribed,
            }
            for ch in changes
        ]
        by_email = {ch.email.lower(): ch.email for ch in changes}
        for chunk in _chunks(records, BATCH_SIZE):
            try:
                resp = self._request(
                    "PATCH",
                    self._api(f"/composite/sobjects/Lead/{EXTERNAL_ID}"),
                    json={"allOrNone": False, "records": chunk},
                )
            except SalesforceError as exc:
                if exc.status in (401, 403):
                    result.unavailable = str(exc)
                    return result
                result.failed.extend((by_email[r[EXTERNAL_ID]], str(exc)) for r in chunk)
                continue
            for record, outcome in zip(chunk, resp.json(), strict=False):
                email = by_email[record[EXTERNAL_ID]]
                if outcome.get("success"):
                    result.updated.append(email)
                else:
                    result.failed.append((email, _record_errors(outcome)))
        return result


def _lead_record(contact: dict[str, str], is_new: bool) -> dict[str, Any]:
    """Map an engine contact to a Lead. Required field defaults apply to new Leads only,
    so partial updates never overwrite existing values."""
    record: dict[str, Any] = {"attributes": {"type": "Lead"}, EXTERNAL_ID: contact["email"].lower()}
    for key, value in contact.items():
        field = FIELD_MAP.get(key)
        if field is None or value in (None, ""):
            continue
        if field in NUMBER_FIELDS:
            record[field] = int(float(value))
        elif field in BOOLEAN_FIELDS:
            record[field] = str(value).lower() == "true"
        else:
            record[field] = value
    if is_new:
        for field, default in REQUIRED_DEFAULTS.items():
            record.setdefault(field, default)
    return record


def _record_errors(outcome: dict[str, Any]) -> str:
    errors = outcome.get("errors") or [{"message": "unknown error"}]
    return "; ".join(f"{e.get('statusCode', '')} {e.get('message', '')}".strip() for e in errors)[:300]


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, list) and body:
        return str(body[0].get("message", body))[:300]
    if isinstance(body, dict):
        return str(body.get("error_description") or body.get("message") or body)[:300]
    return resp.text[:300]
