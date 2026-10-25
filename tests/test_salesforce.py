import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from holo_growth.config import Settings
from holo_growth.crm.base import SubscriptionChange
from holo_growth.crm.salesforce import FIELDS, SalesforceCRM

DOMAIN = "https://holo.my.salesforce.test"
API = f"{DOMAIN}/services/data/v62.0"
UPSERT = f"{API}/composite/sobjects/Lead/Holo_Email__c"


@pytest.fixture
def sf():
    return SalesforceCRM(DOMAIN, "client-id", "client-secret", client=httpx.Client(), sleep=lambda s: None)


def token_route(token="tok-1"):
    return respx.post(f"{DOMAIN}/services/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": token, "instance_url": DOMAIN})
    )


def query_route(records):
    return respx.get(url__regex=rf"{API}/query\?q=.*").mock(
        return_value=httpx.Response(200, json={"records": records, "done": True})
    )


def soql(request):
    return parse_qs(urlparse(str(request.url)).query)["q"][0]


@respx.mock
def test_upsert_maps_fields_and_counts_created_vs_updated(sf):
    token = token_route()
    query_route([{"Holo_Email__c": "old@x.com"}])

    def reply(request):
        records = json.loads(request.content)["records"]
        return httpx.Response(
            200, json=[{"success": True, "created": r["Holo_Email__c"] != "old@x.com"} for r in records]
        )

    upsert = respx.patch(UPSERT).mock(side_effect=reply)
    result = sf.upsert_contacts(
        [
            {
                "email": "New@X.com",
                "firstname": "Nethmi",
                "holo_lead_score": "85",
                "holo_marketing_consent": "true",
            },
            {"email": "old@x.com", "holo_lifecycle_stage": "engaged"},
        ]
    )
    assert (result.created, result.updated, result.failed) == (1, 1, [])
    assert token.calls[0].request.content.decode().count("grant_type=client_credentials") == 1
    body = json.loads(upsert.calls[0].request.content)
    new, old = body["records"]
    assert body["allOrNone"] is False
    assert new["Holo_Email__c"] == "new@x.com" and new["Email"] == "New@X.com"
    assert new["Holo_Lead_Score__c"] == 85 and new["Holo_Marketing_Consent__c"] is True
    assert (new["LastName"], new["Company"]) == ("Unknown", "Not provided")  # required on create
    assert "LastName" not in old and "Company" not in old  # partial update keeps existing values
    assert upsert.calls[0].request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
def test_upsert_batches_by_200_and_records_failures(sf):
    token_route()
    query_route([])
    responses = iter(
        [
            httpx.Response(
                200,
                json=[{"success": True, "created": True}] * 199
                + [{"success": False, "errors": [{"statusCode": "INVALID_EMAIL_ADDRESS", "message": "bad"}]}],
            ),
            httpx.Response(400, json=[{"message": "batch rejected", "errorCode": "INVALID_FIELD"}]),
        ]
    )
    upsert = respx.patch(UPSERT).mock(side_effect=lambda r: next(responses))
    contacts = [{"email": f"u{i}@x.com"} for i in range(250)]
    result = sf.upsert_contacts(contacts)
    assert upsert.call_count == 2
    assert result.created == 199
    assert result.failed[0] == ("u199@x.com", "INVALID_EMAIL_ADDRESS bad")
    assert len(result.failed) == 1 + 50 and "batch rejected" in result.failed[-1][1]


@respx.mock
def test_existing_emails_escapes_and_lowercases(sf):
    token_route()
    query = query_route([{"Holo_Email__c": "o'neil@x.com"}])
    found = sf.existing_emails(["O'Neil@X.com", "b@x.com"])
    assert found == {"o'neil@x.com"}
    assert "IN ('o\\'neil@x.com', 'b@x.com')" in soql(query.calls[0].request)


@respx.mock
def test_existing_emails_is_empty_before_the_field_exists(sf):
    token_route()
    respx.get(url__regex=rf"{API}/query\?q=.*").mock(
        return_value=httpx.Response(
            400,
            json=[
                {"message": "No such column 'Holo_Email__c' on entity 'Lead'.", "errorCode": "INVALID_FIELD"}
            ],
        )
    )
    assert sf.existing_emails(["a@x.com"]) == set()


@respx.mock
def test_expired_session_reauthenticates_once(sf):
    token = respx.post(f"{DOMAIN}/services/oauth2/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "old", "instance_url": DOMAIN}),
            httpx.Response(200, json={"access_token": "new", "instance_url": DOMAIN}),
        ]
    )
    query = respx.get(url__regex=rf"{API}/query\?q=.*").mock(
        side_effect=[
            httpx.Response(401, json=[{"message": "Session expired", "errorCode": "INVALID_SESSION_ID"}]),
            httpx.Response(200, json={"records": [], "done": True}),
        ]
    )
    assert sf.existing_emails(["a@x.com"]) == set()
    assert token.call_count == 2 and query.calls[1].request.headers["Authorization"] == "Bearer new"


@respx.mock
def test_subscriptions_set_opt_out_without_touching_other_fields(sf):
    token_route()
    upsert = respx.patch(UPSERT).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"success": True},
                {"success": False, "errors": [{"statusCode": "REQUIRED_FIELD_MISSING", "message": "x"}]},
            ],
        )
    )
    result = sf.set_email_subscriptions(
        [SubscriptionChange("A@x.com", True, "note"), SubscriptionChange("gone@x.com", False, "note")]
    )
    assert result.updated == ["A@x.com"] and result.failed[0][0] == "gone@x.com"
    records = json.loads(upsert.calls[0].request.content)["records"]
    assert records[0] == {
        "attributes": {"type": "Lead"},
        "Holo_Email__c": "a@x.com",
        "HasOptedOutOfEmail": False,
    }
    assert records[1]["HasOptedOutOfEmail"] is True


@respx.mock
def test_subscription_refusal_reports_unavailable(sf):
    token_route()
    respx.patch(UPSERT).mock(return_value=httpx.Response(403, json=[{"message": "Insufficient access"}]))
    result = sf.set_email_subscriptions([SubscriptionChange("a@x.com", True, "note")])
    assert result.updated == [] and "403" in (result.unavailable or "")


@respx.mock
def test_ensure_properties_creates_missing_fields_and_grants_access(sf):
    token_route()
    respx.get(f"{API}/sobjects/Lead/describe").mock(
        return_value=httpx.Response(200, json={"fields": [{"name": "Email"}, {"name": "Holo_Email__c"}]})
    )
    create_field = respx.post(f"{API}/tooling/sobjects/CustomField").mock(
        side_effect=lambda r: (
            httpx.Response(400, json=[{"errorCode": "DUPLICATE_DEVELOPER_NAME"}])
            if "Holo_Persona__c" in r.content.decode()
            else httpx.Response(201, json={"id": "fld", "success": True})
        )
    )
    query_route([])  # permission set does not exist yet
    respx.post(f"{API}/sobjects/PermissionSet").mock(return_value=httpx.Response(201, json={"id": "0PS1"}))
    field_perms = respx.post(f"{API}/sobjects/FieldPermissions").mock(
        return_value=httpx.Response(201, json={})
    )
    respx.get(f"{DOMAIN}/services/oauth2/userinfo").mock(
        return_value=httpx.Response(200, json={"user_id": "005U"})
    )
    assign = respx.post(f"{API}/sobjects/PermissionSetAssignment").mock(
        return_value=httpx.Response(400, json=[{"errorCode": "DUPLICATE_VALUE"}])
    )
    sf.ensure_properties()
    created = [json.loads(c.request.content)["FullName"] for c in create_field.calls]
    assert "Lead.Holo_Email__c" not in created and len(created) == len(FIELDS) - 1
    granted = [json.loads(c.request.content)["Field"] for c in field_perms.calls]
    assert len(granted) == len(FIELDS) + 1 and "Lead.HasOptedOutOfEmail" in granted
    assert json.loads(assign.calls[0].request.content) == {"AssigneeId": "005U", "PermissionSetId": "0PS1"}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "local"),
        ({"HUBSPOT_PRIVATE_APP_TOKEN": "pat"}, "hubspot"),
        (
            {"SALESFORCE_DOMAIN": DOMAIN, "SALESFORCE_CLIENT_ID": "i", "SALESFORCE_CLIENT_SECRET": "s"},
            "salesforce",
        ),
        (
            {
                "HUBSPOT_PRIVATE_APP_TOKEN": "pat",
                "CRM_PROVIDER": "salesforce",
                "SALESFORCE_DOMAIN": DOMAIN,
                "SALESFORCE_CLIENT_ID": "i",
                "SALESFORCE_CLIENT_SECRET": "s",
            },
            "salesforce",
        ),
        ({"CRM_PROVIDER": "salesforce"}, "local"),  # chosen but not configured
    ],
)
def test_crm_selection(monkeypatch, env, expected):
    for var in (
        "HUBSPOT_PRIVATE_APP_TOKEN",
        "CRM_PROVIDER",
        "SALESFORCE_DOMAIN",
        "SALESFORCE_CLIENT_ID",
        "SALESFORCE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert Settings().crm == expected
