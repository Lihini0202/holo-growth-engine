import httpx
import respx

from holo_growth.retention.usage import SupabaseUsageSource

URL = "https://holo.supabase.test"


@respx.mock
def test_reads_usage_without_risk_fields_and_joins_consent():
    logs = respx.get(f"{URL}/rest/v1/analysis_logs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"auth_user_id": "u1", "created_at": "2026-10-01T10:00:00Z", "actual_outcome": None},
                {"auth_user_id": "u1", "created_at": "2026-10-20T10:00:00+00:00", "actual_outcome": True},
                {"auth_user_id": "ghost-user", "created_at": "2026-10-20T10:00:00Z", "actual_outcome": None},
            ],
        )
    )
    respx.get(f"{URL}/rest/v1/user_profiles").mock(
        return_value=httpx.Response(200, json=[{"id": "u1", "first_name": "Nethmi"}])
    )
    respx.get(f"{URL}/rest/v1/marketing_consents").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user_id": "u1", "granted": False, "recorded_at": "2026-09-01T00:00:00Z"},
                {"user_id": "u1", "granted": True, "recorded_at": "2026-09-05T00:00:00Z"},
            ],
        )
    )
    respx.get(f"{URL}/auth/v1/admin/users").mock(
        return_value=httpx.Response(
            200,
            json={
                "users": [
                    {"id": "u1", "email": "n@x.com", "created_at": "2026-09-01T00:00:00Z"},
                    {"id": "u2", "email": "m@x.com", "created_at": "2026-09-02T00:00:00Z"},
                ]
            },
        )
    )

    users = {u.user_id: u for u in SupabaseUsageSource(URL, "service-key").users()}

    select = logs.calls[0].request.url.params["select"]
    assert "risk" not in select and "partner" not in select
    assert logs.calls[0].request.headers["apikey"] == "service-key"
    assert set(users) == {"u1", "u2"}  # rows for unknown users are ignored
    u1 = users["u1"]
    assert u1.first_name == "Nethmi" and len(u1.analyses) == 2
    assert u1.outcome_recorded == [False, True]
    assert u1.marketing_opt_in is True  # latest choice wins
    assert users["u2"].marketing_opt_in is None


@respx.mock
def test_missing_consent_table_means_nobody_is_marketable():
    respx.get(f"{URL}/rest/v1/analysis_logs").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{URL}/rest/v1/user_profiles").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{URL}/rest/v1/marketing_consents").mock(return_value=httpx.Response(404))
    respx.get(f"{URL}/auth/v1/admin/users").mock(
        return_value=httpx.Response(
            200, json={"users": [{"id": "u1", "email": "n@x.com", "created_at": "2026-09-01T00:00:00Z"}]}
        )
    )
    users = SupabaseUsageSource(URL, "k").users()
    assert users[0].marketing_opt_in is None


@respx.mock
def test_paginates():
    src = SupabaseUsageSource(URL, "k")
    src.PAGE = 2
    route = respx.get(f"{URL}/rest/v1/x").mock(
        side_effect=[
            httpx.Response(200, json=[{"a": 1}, {"a": 2}]),
            httpx.Response(200, json=[{"a": 3}]),
        ]
    )
    assert len(src._get("/rest/v1/x", {})) == 3 and route.call_count == 2
