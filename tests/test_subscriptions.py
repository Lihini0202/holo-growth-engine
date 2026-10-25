import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from typer.testing import CliRunner

from holo_growth import db
from holo_growth.cli import app
from holo_growth.consent import ConsentLedger, Purpose
from holo_growth.crm.base import SubscriptionChange, SubscriptionResult
from holo_growth.crm.hubspot import HubSpotCRM
from holo_growth.crm.local import LocalCRM
from holo_growth.crm.subscriptions import sync_subscriptions
from holo_growth.leads.enrich import RuleEnricher
from holo_growth.leads.pipeline import run_leads

M = Purpose.MARKETING_EMAIL
BASE = "https://api.hubapi.test"


class Recorder(LocalCRM):
    """Local CRM that records each batch it receives and can simulate rejections."""

    def __init__(self, conn, unavailable=None, fail=()):
        super().__init__(conn)
        self.batches: list[list[SubscriptionChange]] = []
        self.unavailable = unavailable
        self.fail = set(fail)

    def set_email_subscriptions(self, changes):
        self.batches.append(list(changes))
        if self.unavailable:
            return SubscriptionResult(unavailable=self.unavailable)
        ok = [c for c in changes if c.email not in self.fail]
        result = super().set_email_subscriptions(ok)
        result.failed = [(c.email, "rejected") for c in changes if c.email in self.fail]
        return result


def test_grant_is_pushed_once(conn):
    crm = Recorder(conn)
    ConsentLedger(conn).record("a@x.com", M, True, "event_form:Launch")
    first = sync_subscriptions(conn, crm)
    again = sync_subscriptions(conn, crm)
    assert (first.subscribed, again.subscribed) == (1, 0)
    assert crm.subscription("a@x.com") == "SUBSCRIBED"
    assert len(crm.batches) == 1
    assert "event_form:Launch" in crm.batches[0][0].explanation


def test_never_said_yes_never_reaches_the_crm(conn):
    crm = Recorder(conn)
    ConsentLedger(conn).withdraw("no@x.com")
    report = sync_subscriptions(conn, crm)
    assert report.not_sent == 1 and crm.batches == []
    assert crm.subscription("no@x.com") is None
    assert sync_subscriptions(conn, crm).not_sent == 0  # Processed once; not re-examined


def test_unsubscribe_after_grant_is_pushed(conn):
    crm = Recorder(conn)
    ledger = ConsentLedger(conn)
    ledger.record("a@x.com", M, True, "event_form:Launch")
    sync_subscriptions(conn, crm)
    ledger.withdraw("a@x.com", "unsubscribe_link")
    report = sync_subscriptions(conn, crm)
    assert report.unsubscribed == 1
    assert crm.subscription("a@x.com") == "UNSUBSCRIBED"


def test_newer_explicit_opt_in_resubscribes(conn):
    crm = Recorder(conn)
    ledger = ConsentLedger(conn)
    ledger.record("a@x.com", M, True, "event_form:Launch")
    ledger.withdraw("a@x.com")
    sync_subscriptions(conn, crm)
    later = datetime.now(UTC) + timedelta(days=1)
    assert ledger.record_if_changed("a@x.com", M, True, "holo_app", at=later)
    assert sync_subscriptions(conn, crm).subscribed == 1
    assert crm.subscription("a@x.com") == "SUBSCRIBED"


def test_unavailable_crm_keeps_changes_queued(conn):
    crm = Recorder(conn, unavailable="HubSpot 403: missing scope")
    ConsentLedger(conn).record("a@x.com", M, True, "event_form:Launch")
    report = sync_subscriptions(conn, crm)
    assert report.unavailable and report.pending == 1 and report.subscribed == 0
    assert "1 waiting" in report.summary()
    crm.unavailable = None
    assert sync_subscriptions(conn, crm).subscribed == 1


def test_failed_contact_is_retried_next_sync(conn):
    crm = Recorder(conn, fail={"bad@x.com"})
    ledger = ConsentLedger(conn)
    for email in ("ok@x.com", "bad@x.com"):
        ledger.record(email, M, True, "event_form:Launch")
    report = sync_subscriptions(conn, crm)
    assert report.subscribed == 1 and report.failed == [("bad@x.com", "rejected")]
    crm.fail.clear()
    retry = sync_subscriptions(conn, crm)
    assert retry.subscribed == 1 and [c.email for c in crm.batches[-1]] == ["bad@x.com"]


def test_lead_run_subscribes_consented_leads_only(conn, write_csv):
    crm = LocalCRM(conn)
    sheet = "Name,Email,Consent\nA,a@gmail.com,yes\nB,b@gmail.com,no\n"
    report = run_leads(write_csv(sheet), "Launch", conn, crm, RuleEnricher())
    assert crm.subscription("a@gmail.com") == "SUBSCRIBED"
    assert crm.subscription("b@gmail.com") is None
    assert "Subscriptions: 1 subscribed" in report.summary()


def test_dry_run_sends_no_subscriptions(conn, write_csv):
    crm = Recorder(conn)
    run_leads(
        write_csv("Name,Email,Consent\nA,a@gmail.com,yes\n"), "L", conn, crm, RuleEnricher(), dry_run=True
    )
    assert crm.batches == []


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    for var in (
        "HUBSPOT_PRIVATE_APP_TOKEN",
        "GA4_PROPERTY_ID",
        "GA4_CREDENTIALS_FILE",
        "CRM_PROVIDER",
        "SALESFORCE_DOMAIN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOLO_GROWTH_DB", str(tmp_path / "t.db"))
    monkeypatch.chdir(tmp_path)
    return tmp_path / "t.db"


def test_cli_withdraw_of_someone_who_never_opted_in_sends_nothing(cli_env):
    result = CliRunner().invoke(app, ["consent", "withdraw", "stranger@x.com"])
    assert result.exit_code == 0, result.output
    assert "never opted in" in result.output
    crm = LocalCRM(db.connect(cli_env))
    assert crm.get("stranger@x.com") is None and crm.subscription("stranger@x.com") is None


def test_cli_withdraw_after_opt_in_unsubscribes(cli_env):
    conn = db.connect(cli_env)
    ConsentLedger(conn).record("a@x.com", M, True, "event_form:Launch")
    sync_subscriptions(conn, LocalCRM(conn))
    result = CliRunner().invoke(app, ["consent", "withdraw", "a@x.com"])
    assert result.exit_code == 0, result.output
    assert "1 unsubscribed" in result.output
    crm = LocalCRM(db.connect(cli_env))
    assert crm.subscription("a@x.com") == "UNSUBSCRIBED"
    assert crm.get("a@x.com")["holo_lifecycle_stage"] == "opted_out"


# -- HubSpot v4 communication preferences ---------------------------------------


def hubspot(subscription_id="39644612"):
    return HubSpotCRM(
        "pat-test", BASE, client=httpx.Client(), sleep=lambda s: None, subscription_id=subscription_id
    )


@respx.mock
def test_hubspot_sends_v4_status_per_contact():
    route = respx.post(url__regex=rf"{BASE}/communication-preferences/v4/statuses/.*").mock(
        return_value=httpx.Response(200, json={"status": "SUCCESS"})
    )
    result = hubspot().set_email_subscriptions(
        [
            SubscriptionChange("a+tag@x.com", True, "Opted in via event_form"),
            SubscriptionChange("b@x.com", False, "x"),
        ]
    )
    assert result.updated == ["a+tag@x.com", "b@x.com"] and result.unavailable is None
    first = route.calls[0].request
    assert first.url.raw_path == b"/communication-preferences/v4/statuses/a%2Btag@x.com"
    body = json.loads(first.content)
    assert body == {
        "subscriptionId": 39644612,
        "statusState": "SUBSCRIBED",
        "legalBasis": "CONSENT_WITH_NOTICE",
        "legalBasisExplanation": "Opted in via event_form",
        "channel": "EMAIL",
    }
    assert json.loads(route.calls[1].request.content)["statusState"] == "UNSUBSCRIBED"


@respx.mock
def test_hubspot_plan_or_scope_refusal_stops_and_reports_unavailable():
    route = respx.post(url__regex=rf"{BASE}/communication-preferences/v4/statuses/.*").mock(
        return_value=httpx.Response(403, json={"message": "This app hasn't been granted all required scopes"})
    )
    result = hubspot().set_email_subscriptions(
        [SubscriptionChange(f"u{i}@x.com", True, "x") for i in range(3)]
    )
    assert route.call_count == 1
    assert result.updated == [] and "403" in (result.unavailable or "")


@respx.mock
def test_hubspot_bad_contact_fails_alone():
    respx.post(f"{BASE}/communication-preferences/v4/statuses/bad@x.com").mock(
        return_value=httpx.Response(400, json={"message": "Cannot resubscribe"})
    )
    respx.post(f"{BASE}/communication-preferences/v4/statuses/ok@x.com").mock(
        return_value=httpx.Response(200, json={})
    )
    result = hubspot().set_email_subscriptions(
        [SubscriptionChange("bad@x.com", True, "x"), SubscriptionChange("ok@x.com", True, "x")]
    )
    assert result.updated == ["ok@x.com"]
    assert result.failed[0][0] == "bad@x.com" and "Cannot resubscribe" in result.failed[0][1]


@respx.mock
def test_hubspot_without_subscription_id_sends_nothing():
    route = respx.post(url__regex=rf"{BASE}/.*").mock(return_value=httpx.Response(200, json={}))
    result = hubspot(subscription_id="").set_email_subscriptions([SubscriptionChange("a@x.com", True, "x")])
    assert route.call_count == 0 and "HUBSPOT_SUBSCRIPTION_ID" in (result.unavailable or "")


@respx.mock
def test_hubspot_lists_subscription_types():
    respx.get(f"{BASE}/communication-preferences/v4/definitions").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "COMPLETE",
                "results": [{"id": "39644612", "name": "Holo updates", "isActive": True}],
            },
        )
    )
    assert hubspot().subscription_definitions()[0]["name"] == "Holo updates"
