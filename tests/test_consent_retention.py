from datetime import UTC, datetime, timedelta

from holo_growth.consent import ConsentLedger, Purpose
from holo_growth.models import LifecycleStage
from holo_growth.retention.lifecycle import classify, pending_validations
from holo_growth.retention.sync import run_retention
from holo_growth.retention.usage import UserActivity

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
M = Purpose.MARKETING_EMAIL


def user(days_ago=(), outcomes=None, signed_up_days=60, **kw):
    analyses = [NOW - timedelta(days=d) for d in days_ago]
    return UserActivity(
        user_id=kw.pop("uid", "u1"),
        email=kw.pop("email", "u1@x.com"),
        first_name="U",
        signed_up=NOW - timedelta(days=signed_up_days),
        analyses=analyses,
        outcome_recorded=outcomes or [False] * len(analyses),
        **kw,
    )


# -- consent -------------------------------------------------------------------


def test_no_record_means_no_consent(conn):
    assert ConsentLedger(conn).allowed("a@x.com") is False


def test_purposes_are_separate(conn):
    ledger = ConsentLedger(conn)
    ledger.record("a@x.com", Purpose.PRODUCT_UPDATES, True, "app")
    assert not ledger.allowed("a@x.com", M)


def test_withdrawal_wins_over_reimported_grant(conn):
    ledger = ConsentLedger(conn)
    ledger.record_if_changed("a@x.com", M, True, "event_form")
    ledger.withdraw("a@x.com")
    assert ledger.record_if_changed("a@x.com", M, True, "event_form") is False
    assert not ledger.allowed("a@x.com")


def test_later_explicit_grant_can_reopt_in(conn):
    ledger = ConsentLedger(conn)
    ledger.withdraw("a@x.com")
    assert ledger.record_if_changed(
        "a@x.com", M, True, "holo_app", at=datetime.now(UTC) + timedelta(minutes=5)
    )
    assert ledger.allowed("a@x.com")


def test_withdrew_after_grant_distinguishes_never_said_yes(conn):
    ledger = ConsentLedger(conn)
    ledger.record("never@x.com", M, False, "app")
    ledger.record("left@x.com", M, True, "app")
    ledger.record("left@x.com", M, False, "unsubscribe")
    assert not ledger.withdrew_after_grant("never@x.com")
    assert ledger.withdrew_after_grant("left@x.com")


# -- lifecycle -----------------------------------------------------------------


def test_stages():
    assert classify(user(signed_up_days=3), NOW).stage == LifecycleStage.NEW
    assert classify(user([2]), NOW).stage == LifecycleStage.ACTIVATED
    assert classify(user([1, 3, 5]), NOW).stage == LifecycleStage.ENGAGED
    assert classify(user([20, 25]), NOW).stage == LifecycleStage.AT_RISK
    assert classify(user([45]), NOW).stage == LifecycleStage.DORMANT
    assert classify(user([1, 2, 3]), NOW, consent_withdrawn=True).stage == LifecycleStage.OPTED_OUT


def test_signup_without_use_eventually_counts_as_at_risk():
    assert classify(user(signed_up_days=20), NOW).stage == LifecycleStage.AT_RISK


def test_pending_validations_window_matches_holo():
    act = user([5, 15, 30, 59, 61], outcomes=[False, False, True, False, False])
    # 5 days: too early. 15 and 59: due. 30: answered. 61: expired.
    assert pending_validations(act, NOW) == 2


# -- sync ----------------------------------------------------------------------


class Source:
    name = "test"

    def __init__(self, users):
        self._users = users

    def users(self):
        return self._users


def test_only_consented_users_reach_crm(conn, crm):
    users = [
        user([1, 2, 3], uid="a", email="a@x.com", marketing_opt_in=True),
        user([40], uid="b", email="b@x.com", marketing_opt_in=False),
        user([2], uid="c", email="c@x.com"),  # never asked
    ]
    report = run_retention(conn, Source(users), crm, now=NOW)
    assert report.synced == 1 and report.not_consented == 2
    assert crm.get("a@x.com")["holo_lifecycle_stage"] == "engaged"
    assert crm.get("b@x.com") is None and crm.get("c@x.com") is None
    stored = conn.execute("SELECT email FROM lifecycle WHERE holo_user_id = 'b'").fetchone()
    assert stored["email"] is None  # no email kept for people who didn't consent


def test_unsubscribe_is_synced_once_as_opted_out(conn, crm):
    u = user([1], uid="a", email="a@x.com", marketing_opt_in=True)
    run_retention(conn, Source([u]), crm, now=NOW)
    ConsentLedger(conn).withdraw("a@x.com")
    stale = user([1], uid="a", email="a@x.com", marketing_opt_in=True)  # app value is stale
    r2 = run_retention(conn, Source([stale]), crm, now=NOW)
    assert r2.opted_out_synced == 1
    assert crm.get("a@x.com")["holo_lifecycle_stage"] == "opted_out"
    assert crm.get("a@x.com")["holo_marketing_consent"] == "false"
    r3 = run_retention(conn, Source([stale]), crm, now=NOW)
    assert r3.opted_out_synced == 0 and r3.synced == 0


def test_no_risk_data_is_sent(conn, crm):
    run_retention(conn, Source([user([1], marketing_opt_in=True)]), crm, now=NOW)
    props = crm.get("u1@x.com")
    assert not any("risk" in k for k in props)
