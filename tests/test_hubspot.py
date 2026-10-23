import json

import httpx
import pytest
import respx

from holo_growth.crm.base import CUSTOM_PROPERTIES
from holo_growth.crm.hubspot import HubSpotCRM, HubSpotError

BASE = "https://api.hubapi.test"


@pytest.fixture
def hs():
    sleeps: list[float] = []
    client = HubSpotCRM("pat-test-token", BASE, client=httpx.Client(), sleep=sleeps.append)
    client.sleeps = sleeps  # type: ignore[attr-defined]
    return client


def contacts(n):
    return [{"email": f"u{i}@x.com", "firstname": f"U{i}"} for i in range(n)]


@respx.mock
def test_upsert_batches_by_100_and_counts_new_vs_updated(hs):
    def reply(request):
        inputs = json.loads(request.content)["inputs"]
        assert all(i["idProperty"] == "email" for i in inputs)
        return httpx.Response(
            200, json={"results": [{"id": str(k), "new": k % 2 == 0} for k in range(len(inputs))]}
        )

    route = respx.post(f"{BASE}/crm/v3/objects/contacts/batch/upsert").mock(side_effect=reply)
    result = hs.upsert_contacts(contacts(250))
    assert route.call_count == 3  # 100 + 100 + 50
    assert result.created + result.updated == 250
    assert route.calls[0].request.headers["Authorization"] == "Bearer pat-test-token"


@respx.mock
def test_retries_on_429_respecting_retry_after(hs):
    route = respx.post(f"{BASE}/crm/v3/objects/contacts/batch/upsert").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "rate limit"}),
            httpx.Response(502),
            httpx.Response(200, json={"results": [{"id": "1", "new": True}]}),
        ]
    )
    result = hs.upsert_contacts(contacts(1))
    assert route.call_count == 3 and result.created == 1
    assert hs.sleeps[0] == 2.0


@respx.mock
def test_400_is_not_retried_and_batch_is_marked_failed(hs):
    route = respx.post(f"{BASE}/crm/v3/objects/contacts/batch/upsert").mock(
        return_value=httpx.Response(400, json={"message": "Property values were not valid"})
    )
    result = hs.upsert_contacts(contacts(3))
    assert route.call_count == 1
    assert len(result.failed) == 3 and "not valid" in result.failed[0][1]


@respx.mock
def test_partial_multi_status_errors_are_reported(hs):
    respx.post(f"{BASE}/crm/v3/objects/contacts/batch/upsert").mock(
        return_value=httpx.Response(
            207,
            json={
                "results": [{"id": "1", "new": False}],
                "errors": [{"message": "INVALID_EMAIL", "context": {"ids": ["u1@x.com"]}}],
            },
        )
    )
    result = hs.upsert_contacts(contacts(2))
    assert result.updated == 1 and result.failed == [("u1@x.com", "INVALID_EMAIL")]


@respx.mock
def test_gives_up_after_max_attempts(hs):
    respx.get(f"{BASE}/crm/v3/properties/contacts/x").mock(return_value=httpx.Response(503))
    with pytest.raises(HubSpotError):
        hs._request("GET", "/crm/v3/properties/contacts/x")


@respx.mock
def test_ensure_properties_creates_only_missing(hs):
    respx.post(f"{BASE}/crm/v3/properties/contacts/groups").mock(
        return_value=httpx.Response(409, json={"message": "exists"})
    )
    for p in CUSTOM_PROPERTIES:
        status = 200 if p["name"] == "holo_persona" else 404
        respx.get(f"{BASE}/crm/v3/properties/contacts/{p['name']}").mock(
            return_value=httpx.Response(status, json={})
        )
    create = respx.post(f"{BASE}/crm/v3/properties/contacts").mock(return_value=httpx.Response(201, json={}))
    hs.ensure_properties()
    created = [json.loads(c.request.content)["name"] for c in create.calls]
    assert "holo_persona" not in created
    assert len(created) == len(CUSTOM_PROPERTIES) - 1
    stage = next(
        json.loads(c.request.content)
        for c in create.calls
        if json.loads(c.request.content)["name"] == "holo_lifecycle_stage"
    )
    assert {o["value"] for o in stage["options"]} >= {"at_risk", "opted_out"}


@respx.mock
def test_existing_emails(hs):
    respx.post(f"{BASE}/crm/v3/objects/contacts/batch/read").mock(
        return_value=httpx.Response(
            207, json={"results": [{"properties": {"email": "U1@x.com"}}], "errors": []}
        )
    )
    assert hs.existing_emails(["u1@x.com", "u2@x.com"]) == {"u1@x.com"}


def test_token_required():
    with pytest.raises(ValueError):
        HubSpotCRM("")
