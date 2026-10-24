import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from holo_growth import reports
from holo_growth.web.traffic import (
    GA4Error,
    GA4Source,
    SyntheticTrafficSource,
    TrafficRow,
    run_web_sync,
    service_account_token,
)

RUN_REPORT = "https://analyticsdata.googleapis.com/v1beta/properties/123:runReport"
YESTERDAY = datetime.now(UTC).date() - timedelta(days=1)


def ga4_rows(rows, total=None):
    return {
        "rows": [
            {
                "dimensionValues": [{"value": v} for v in (d, src, med, camp)],
                "metricValues": [{"value": str(n)}],
            }
            for d, src, med, camp, n in rows
        ],
        "rowCount": total if total is not None else len(rows),
    }


def source(**kw):
    return GA4Source("properties/123", lambda: "tok", client=httpx.Client(), sleep=lambda s: None, **kw)


@respx.mock
def test_ga4_merges_sessions_and_signups_per_day_and_channel():
    def reply(request):
        body = json.loads(request.content)
        assert body["dateRanges"] == [{"startDate": "28daysAgo", "endDate": "yesterday"}]
        assert [d["name"] for d in body["dimensions"]] == [
            "date",
            "sessionSource",
            "sessionMedium",
            "sessionCampaignName",
        ]
        assert request.headers["Authorization"] == "Bearer tok"
        if body["metrics"] == [{"name": "sessions"}]:
            assert "dimensionFilter" not in body
            return httpx.Response(
                200,
                json=ga4_rows(
                    [
                        ("20261020", "instagram", "social", "(not set)", 120),
                        ("20261020", "google", "organic", "(organic)", 80),
                    ]
                ),
            )
        assert body["metrics"] == [{"name": "eventCount"}]
        assert body["dimensionFilter"]["filter"] == {
            "fieldName": "eventName",
            "stringFilter": {"matchType": "EXACT", "value": "sign_up"},
        }
        return httpx.Response(200, json=ga4_rows([("20261020", "instagram", "social", "(not set)", 14)]))

    respx.post(RUN_REPORT).mock(side_effect=reply)
    rows = source().rows(28)
    assert TrafficRow(date(2026, 10, 20), "instagram", "social", "(not set)", 120, 14) in rows
    assert TrafficRow(date(2026, 10, 20), "google", "organic", "(organic)", 80, 0) in rows


@respx.mock
def test_ga4_pages_through_large_reports():
    pages = {
        0: [("20261020", f"s{i}", "m", "c", 1) for i in range(3)],
        10_000: [("20261021", "x", "m", "c", 5)],
    }

    def reply(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=ga4_rows(pages.get(body["offset"], []), total=10_001))

    route = respx.post(RUN_REPORT).mock(side_effect=reply)
    rows = source().rows(7)
    assert route.call_count == 4  # two pages for each of the two reports
    assert sum(r.sessions for r in rows) == 8


@respx.mock
def test_ga4_retries_rate_limits_and_explains_refusals():
    respx.post(RUN_REPORT).mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "quota"}}),
            httpx.Response(200, json=ga4_rows([])),
            httpx.Response(403, json={"error": {"message": "User does not have sufficient permissions"}}),
        ]
    )
    with pytest.raises(GA4Error, match="403: User does not have sufficient permissions"):
        source().rows(28)


@respx.mock
def test_service_account_token_signs_and_caches(tmp_path):
    pytest.importorskip("google.auth")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    creds = tmp_path / "sa.json"
    creds.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "growth@proj.iam.gserviceaccount.com",
                "private_key": pem,
                "private_key_id": "k1",
                "token_uri": "https://oauth2.test/token",
            }
        )
    )
    route = respx.post("https://oauth2.test/token").mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
    )
    token = service_account_token(str(creds), httpx.Client())
    assert token() == "ya29.test" and token() == "ya29.test"
    assert route.call_count == 1  # cached until near expiry
    form = dict(httpx.QueryParams(route.calls[0].request.content.decode()))
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(form["assertion"], verify=False)
    assert claims["iss"] == "growth@proj.iam.gserviceaccount.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/analytics.readonly"


class Fixed:
    def __init__(self, name, rows):
        self.name, self._rows = name, rows

    def rows(self, days):
        return self._rows


def test_sync_replaces_the_window_instead_of_duplicating(conn):
    rows = [TrafficRow(YESTERDAY, "instagram", "social", "(not set)", 100, 9)]
    run_web_sync(conn, Fixed("ga4", rows))
    report = run_web_sync(
        conn, Fixed("ga4", [TrafficRow(YESTERDAY, "instagram", "social", "(not set)", 110, 10)])
    )
    assert report.sessions == 110 and report.top_channel == "instagram / social"
    assert reports.traffic_by_channel(conn) == [
        {"source": "instagram", "medium": "social", "sessions": 110, "signups": 10, "signup_rate_pct": 9.1}
    ]


def test_real_data_replaces_demo_data_entirely(conn):
    run_web_sync(conn, SyntheticTrafficSource())
    run_web_sync(conn, Fixed("ga4", [TrafficRow(YESTERDAY, "google", "organic", "(organic)", 5, 1)]))
    assert reports.traffic_provider(conn) == "ga4"
    assert [c["source"] for c in reports.traffic_by_channel(conn)] == ["google"]


def test_synthetic_traffic_is_deterministic_and_in_the_brief(conn):
    a = SyntheticTrafficSource(today=date(2026, 10, 24)).rows(28)
    b = SyntheticTrafficSource(today=date(2026, 10, 24)).rows(28)
    assert (
        a == b and min(r.day for r in a) == date(2026, 9, 26) and max(r.day for r in a) == date(2026, 10, 23)
    )
    run_web_sync(conn, SyntheticTrafficSource())
    brief = reports.weekly_brief(conn)
    assert "## Website sign-ups by source" in brief and "Synthetic demo data." in brief


def test_brief_without_web_data(conn):
    assert "No website data yet" in reports.weekly_brief(conn)
